"""Configuration handling for WinAgent.

The configuration is a plain JSON file (``config.json``).  Environment
variables (``WINAGENT_API_KEY``, ``WINAGENT_API_BASE_URL``, ``WINAGENT_MODEL``)
override the values found in the file so that secrets never have to be written
to disk if the user prefers not to.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

APP_DIR_NAME = "WinAgent"

ENV_OVERRIDES = {
    "WINAGENT_API_KEY": "api_key",
    "WINAGENT_API_BASE_URL": "api_base_url",
    "WINAGENT_MODEL": "model",
    "WINAGENT_BACKEND": "backend",
    "WINAGENT_LOG_LEVEL": "log_level",
}

TOOL_PROTOCOLS = ("auto", "native", "json")
BACKENDS = ("auto", "windows", "fake")
SCREENSHOT_FORMATS = ("jpeg", "png")


def default_config_dir() -> Path:
    """Directory where user-specific config/logs live."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / APP_DIR_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "winagent"
    return Path.home() / ".config" / "winagent"


def default_config_path() -> Path:
    """Resolve the config file location.

    Priority: ``WINAGENT_CONFIG`` env var -> ``./config.json`` (if present)
    -> ``<user config dir>/config.json``.
    """
    env = os.environ.get("WINAGENT_CONFIG")
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / "config.json"
    if local.exists():
        return local
    return default_config_dir() / "config.json"


@dataclass
class Config:
    # --- LLM connection -----------------------------------------------------
    api_base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: int = 2048
    request_timeout: float = 120.0
    max_retries: int = 3
    extra_headers: dict[str, str] = field(default_factory=dict)

    # --- Agent behaviour -----------------------------------------------------
    tool_protocol: str = "auto"          # auto | native | json
    max_steps: int = 40                  # max tool-calling rounds per task
    vision_enabled: bool = True          # send screenshots as images
    auto_screenshot_after_action: bool = True
    action_delay: float = 0.6            # seconds to wait after UI actions before screenshot
    max_images_in_context: int = 3       # older screenshots are dropped from context
    max_history_messages: int = 80
    confirm_dangerous_actions: bool = True
    allow_shell_commands: bool = True
    allow_file_write: bool = True
    stop_hotkey: str = "ctrl+alt+esc"    # global emergency-stop hotkey (Windows only)
    mouse_failsafe: bool = True          # moving the mouse to the top-left corner aborts the task
    response_language: str = "auto"      # auto | fa | en | ...
    extra_system_prompt: str = ""

    # --- Screenshots -----------------------------------------------------------
    screenshot_max_width: int = 1280
    screenshot_grid: bool = True
    screenshot_grid_spacing: int = 100
    screenshot_show_cursor: bool = True
    screenshot_format: str = "jpeg"      # jpeg | png
    screenshot_jpeg_quality: int = 70

    # --- Misc --------------------------------------------------------------------
    backend: str = "auto"                # auto | windows | fake
    minimize_gui_while_running: bool = True
    log_level: str = "INFO"

    # ------------------------------------------------------------------------
    def chat_completions_url(self) -> str:
        base = self.api_base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def models_url(self) -> str:
        base = self.api_base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        return f"{base}/models"

    def validate(self) -> list[str]:
        """Return a list of human readable problems (empty when valid)."""
        problems: list[str] = []
        if not self.api_base_url.strip():
            problems.append("API base URL is empty.")
        elif not self.api_base_url.strip().lower().startswith(("http://", "https://")):
            problems.append("API base URL must start with http:// or https://.")
        if not self.model.strip():
            problems.append("Model name is empty.")
        if self.tool_protocol not in TOOL_PROTOCOLS:
            problems.append(f"tool_protocol must be one of {TOOL_PROTOCOLS}.")
        if self.backend not in BACKENDS:
            problems.append(f"backend must be one of {BACKENDS}.")
        if self.screenshot_format not in SCREENSHOT_FORMATS:
            problems.append(f"screenshot_format must be one of {SCREENSHOT_FORMATS}.")
        if self.max_steps < 1:
            problems.append("max_steps must be >= 1.")
        if self.screenshot_max_width < 320:
            problems.append("screenshot_max_width must be >= 320.")
        if not (0.0 <= self.temperature <= 2.0):
            problems.append("temperature must be between 0 and 2.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def copy(self) -> "Config":
        return Config.from_dict(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f.name: f for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in (data or {}).items():
            if key not in known:
                continue  # silently ignore unknown keys (forward compatibility)
            kwargs[key] = _coerce(value, known[key].type, known[key].default)
        return cls(**kwargs)


def _coerce(value: Any, type_hint: Any, default: Any) -> Any:
    """Best-effort coercion of JSON values into the dataclass field types."""
    hint = str(type_hint)
    try:
        if hint in ("bool", "<class 'bool'>"):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if hint in ("int", "<class 'int'>"):
            return int(value)
        if hint in ("float", "<class 'float'>"):
            return float(value)
        if hint in ("str", "<class 'str'>"):
            return "" if value is None else str(value)
        if hint.startswith("dict"):
            return dict(value) if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return default
    return value


def apply_env_overrides(cfg: Config) -> Config:
    for env_name, attr in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value:
            setattr(cfg, attr, value)
    return cfg


def load_config(path: str | Path | None = None, *, env: bool = True) -> Config:
    """Load config from ``path`` (or the default location).

    A missing file yields default settings; a corrupt file raises ``ValueError``.
    """
    cfg_path = Path(path) if path else default_config_path()
    cfg = Config()
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Config file {cfg_path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Config file {cfg_path} must contain a JSON object.")
        cfg = Config.from_dict(data)
    if env:
        apply_env_overrides(cfg)
    return cfg


def save_config(cfg: Config, path: str | Path | None = None) -> Path:
    cfg_path = Path(path) if path else default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, indent=2, ensure_ascii=False)
    os.replace(tmp, cfg_path)
    return cfg_path
