"""Bundle all logs into a single zip the user can send to the developer.

The bundle contains ``meta.json`` (version, platform, redacted configuration),
the rotating application log, and the most recent per-task session traces –
everything needed to analyse request/response behaviour offline.
"""

from __future__ import annotations

import json
import platform
import sys
import time
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from . import __app_name__, __version__
from .config import Config
from .logging_setup import BACKUP_COUNT, LOG_BASENAME, logs_dir, redact

MAX_SESSIONS = 20
MAX_SESSION_BYTES = 5 * 1024 * 1024   # session traces contribute at most 5 MB to the bundle


def _safe_config(cfg: Optional[Config]) -> dict:
    if cfg is None:
        return {}
    data = redact(asdict(cfg), [cfg.api_key])
    data["api_key"] = "<set>" if cfg.api_key else "<unset>"   # presence is useful, the value never
    return data


def make_log_bundle(logs: Optional[Path] = None, *, config: Optional[Config] = None,
                    max_sessions: int = MAX_SESSIONS) -> Path:
    """Create ``<logs>/WinAgent-log-bundle-<stamp>.zip`` and return its path."""
    logs = logs or logs_dir()
    sessions_dir = logs / "sessions"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bundle = logs / f"{__app_name__}-log-bundle-{stamp}.zip"

    entries: list[Path] = []
    for name in (LOG_BASENAME, *[f"{LOG_BASENAME}.{i}" for i in range(1, BACKUP_COUNT + 1)]):
        p = logs / name
        if p.exists():
            entries.append(p)

    sessions: list[Path] = []
    if sessions_dir.exists():
        sessions = sorted((p for p in sessions_dir.glob("*.jsonl") if p.is_file()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
    added = 0
    for p in sessions[:max_sessions]:
        size = p.stat().st_size
        if added + size > MAX_SESSION_BYTES:
            break
        entries.append(p)
        added += size

    meta = {
        "app": __app_name__,
        "version": __version__,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "config": _safe_config(config),
        "files": [str(p) for p in entries],
    }
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for p in entries:
            zf.write(p, f"logs/{p.name}")
    return bundle

