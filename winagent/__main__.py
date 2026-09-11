"""Command line entry point.

    python -m winagent                 # start the GUI
    python -m winagent --cli           # interactive terminal chat
    python -m winagent --task "open notepad and type hello"
    python -m winagent --demo          # GUI with the simulated desktop backend
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __app_name__, __version__
from .config import Config, load_config, save_config


def _setup_logging(level: str) -> None:
    """Shared rotating-file + console logging (also used by the GUI)."""
    from .logging_setup import setup_logging
    setup_logging(level, console=True)


def _cli_loop(config: Config, backend_kind: str, task: str | None, max_turns: int | None = None) -> int:
    from .agent import Agent, AgentEvents
    from .backends import create_backend

    def confirm(call, reason: str) -> bool:
        print(f"\n[!] The agent wants to run {call.name}:\n{reason}")
        try:
            return input("Allow? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    def ask_user(question: str, options: list[str]):
        print(f"\n[?] {question}")
        if options:
            print("   options: " + " | ".join(options))
        try:
            return input("> ").strip()
        except EOFError:
            return None

    events = AgentEvents(
        on_status=lambda m: print(f"   .. {m}", file=sys.stderr),
        on_thought=lambda t: print(f"   (thinking) {t[:300]}"),
        on_assistant_text=lambda t: print(f"\nagent> {t}\n"),
        on_tool_start=lambda c: print(f"   -> {c.name} {c.arguments}"),
        on_tool_end=lambda r: print(f"   {'OK ' if r.ok else 'ERR'} {r.summary()}"),
        on_error=lambda m: print(f"   ERR {m}", file=sys.stderr),
        ask_user=ask_user,
        confirm=confirm,
    )
    agent = None
    try:
        backend = create_backend(backend_kind, stop_hotkey=config.stop_hotkey,
                                 on_emergency_stop=lambda: agent and agent.stop())
    except Exception as exc:
        print(f"Desktop backend error: {exc}", file=sys.stderr)
        return 4
    agent = Agent(config, backend, events=events)
    print(f"{__app_name__} v{__version__} – model {config.model} @ {config.api_base_url} – backend {backend.name}")
    if task:
        outcome = agent.run(task)
        print(f"\n[{outcome.status}] {outcome.message}")
        return 0 if outcome.status in ("completed", "answered") else 1
    print("Type a task (or 'quit').")
    turns = 0
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in ("quit", "exit", "q"):
            break
        if text.lower() in ("/reset", "/new"):
            agent.reset()
            print("(conversation cleared)")
            continue
        outcome = agent.run(text)
        print(f"[{outcome.status} · {outcome.steps} steps · {outcome.duration:.1f}s]")
        turns += 1
        if max_turns and turns >= max_turns:
            break
    backend.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="winagent", description=f"{__app_name__} – LLM-powered Windows computer-use agent")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--api-key", help="override API key")
    parser.add_argument("--api-base-url", help="override API base URL (OpenAI compatible)")
    parser.add_argument("--model", help="override model name")
    parser.add_argument("--backend", choices=["auto", "windows", "fake"],
                        help="auto/windows: real desktop (Windows only); fake: simulated demo, not your screen")
    parser.add_argument("--cli", action="store_true", help="interactive terminal mode instead of the GUI")
    parser.add_argument("--task", help="run one task headless and exit")
    parser.add_argument("--demo", action="store_true", help="GUI with the simulated desktop (safe on any OS)")
    parser.add_argument("--init-config", action="store_true", help="write a default config.json and exit")
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    args = parser.parse_args(argv)

    if args.init_config:
        path = Path(args.config) if args.config else Path.cwd() / "config.json"
        if path.exists():
            print(f"{path} already exists.")
            return 1
        save_config(Config(), path)
        print(f"Default config written to {path}. Edit api_key / api_base_url / model.")
        return 0

    try:
        config = load_config(args.config)
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    if args.api_key:
        config.api_key = args.api_key
    if args.api_base_url:
        config.api_base_url = args.api_base_url
    if args.model:
        config.model = args.model
    if args.log_level:
        config.log_level = args.log_level
    _setup_logging(config.log_level)

    backend_kind = args.backend or ("fake" if args.demo else config.backend)
    config_path = Path(args.config) if args.config else None

    if args.cli or args.task:
        return _cli_loop(config, backend_kind, args.task)

    try:
        from .gui.main_window import run_gui
    except ImportError as exc:
        print(f"GUI dependencies missing ({exc}). Install with: pip install PySide6", file=sys.stderr)
        return 3
    return run_gui(config, config_path=config_path, backend_override=args.backend or ("fake" if args.demo else None))


if __name__ == "__main__":
    sys.exit(main())
