"""Central logging: rotating application log + per-task LLM request/response traces.

Two kinds of output, both under ``<config dir>/logs`` (override with the
``WINAGENT_LOGS_DIR`` environment variable):

* ``winagent.log`` – human-readable, rotating (5 MB x 3).  Written by both the
  CLI and the GUI (the GUI previously logged to the console only).
* ``sessions/<timestamp>-<task>.jsonl`` – one file per agent task with the FULL
  model conversation: every request (sanitised messages, tool schema,
  temperature, retry level), every response (finish reason, latency, token
  usage, raw provider envelope), tool results, and a session summary.  These
  files are exactly what is needed to replay and debug a run.

Privacy: the API key is scrubbed from every record, dict entries whose key
looks like a credential are masked, and base64 screenshots are replaced by
short placeholders.  Logging is best-effort: a failure to write a log never
interrupts a run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import default_config_dir

LOG_BASENAME = "winagent.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
MAX_TRACE_FILE_BYTES = 10 * 1024 * 1024   # a single session file is capped at 10 MB
MAX_TEXT_CHARS = 20_000                   # long text fields are clipped in traces

# Strict on purpose: bare "token"/"key" substrings would redact legitimate fields such as
# usage.prompt_tokens / completion_tokens.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"secret|password|passwd|credential)", re.I)
_SETUP_MARKER = "_winagent_logging_configured"
_log = logging.getLogger(__name__)


# --------------------------------------------------------------------- setup
def logs_dir() -> Path:
    """Where all logs live (created on demand).  ``WINAGENT_LOGS_DIR`` overrides."""
    env = os.environ.get("WINAGENT_LOGS_DIR")
    base = Path(env).expanduser() if env else default_config_dir() / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def setup_logging(level: str = "INFO", *, console: bool = True) -> Path:
    """Configure root logging once: optional console + rotating file.  Idempotent.

    Returns the logs directory.  Safe to call from both entry points; the GUI
    passes ``console=False`` (it has its own status UI).
    """
    root = logging.getLogger()
    if getattr(root, _SETUP_MARKER, False):
        return logs_dir()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)
    file_handler = RotatingFileHandler(logs_dir() / LOG_BASENAME, maxBytes=MAX_BYTES,
                                       backupCount=BACKUP_COUNT, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    for noisy in ("urllib3", "PIL", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    setattr(root, _SETUP_MARKER, True)
    return logs_dir()


# ----------------------------------------------------------------- redaction
def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Return a copy of ``value`` with credentials removed.

    * dict entries whose key looks like a secret are masked (unless empty)
    * every occurrence of a literal secret string is replaced
    """
    secret_list = [s for s in secrets if s]
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k) and v:
                out[k] = "<redacted>"
            else:
                out[k] = redact(v, secret_list)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, secret_list) for v in value]
    if isinstance(value, str):
        for s in secret_list:
            value = value.replace(s, "<redacted>")
        return value
    return value


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f" … [truncated {len(text) - limit} chars]"


def sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of a request's messages with screenshots replaced by placeholders.

    Message lists carry base64 data-URL images (tens of KB each); they are
    useless for debugging model behaviour and would bloat the trace file.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        msg = dict(msg)
        content = msg.get("content")
        if isinstance(content, list):
            parts: list[Any] = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    url = ((p.get("image_url") or {}).get("url")) or ""
                    head = url.split(",", 1)[0] if "," in url else url[:64]
                    parts.append({"type": "image_url",
                                  "image_url": {"url": f"<image omitted: {head or 'data'}, {len(url)} chars>"}})
                else:
                    parts.append(p)
            msg["content"] = parts
        elif isinstance(content, str):
            msg["content"] = _clip(content)
        out.append(msg)
    return out


# ------------------------------------------------------------------ tracing
class LLMTraceWriter:
    """Append JSON lines for one agent task.  All failures are swallowed –
    logging must never break a run."""

    def __init__(self, path: Path, *, secrets: Iterable[str] = (),
                 max_file_bytes: int = MAX_TRACE_FILE_BYTES):
        self.path = Path(path)
        self.secrets = list(secrets)
        self.max_file_bytes = max_file_bytes
        self.exceeded = False
        self._lock = threading.Lock()
        self._fh: Optional[object] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        except Exception:
            _log.exception("could not open trace file %s", path)

    def record(self, **fields: Any) -> None:
        if self._fh is None or self.exceeded:
            return
        with self._lock:
            if self._fh is None:
                return
            try:
                if self.path.exists() and self.path.stat().st_size > self.max_file_bytes:
                    self.exceeded = True
                    _log.warning("trace file %s exceeded %d bytes; further records skipped",
                                 self.path, self.max_file_bytes)
                    return
                fields = redact(fields, self.secrets)
                line = json.dumps({"ts": time.time(), **fields}, ensure_ascii=False, default=str)
                self._fh.write(line + "\n")
                self._fh.flush()
            except Exception:
                _log.exception("trace write failed")

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
