"""Installable "skills": user-provided instruction documents for the agent.

A *skill* is a plain Markdown (or .txt) file describing a specialized procedure the agent
should follow – e.g. "how our company's CRM export works", "how to use this specific CAD
tool", "the exact steps for the weekly report".  Drop files into a skills directory and
they are loaded into the system prompt automatically, so no code change is needed:

* ``<project>/skills/``          – next to the WinAgent package (shared with the team/PC)
* user config dir ``/skills``    – ``%APPDATA%/WinAgent/skills`` on Windows
                                   (``$XDG_CONFIG_HOME/winagent/skills`` or ``~/.config/winagent/skills`` elsewhere)

Rules:

* only ``*.md``, ``*.markdown`` and ``*.txt`` files are loaded;
* files whose name starts with ``_`` or ``.`` are ignored (use ``_example.md`` as a template);
* skills are loaded at most once per agent start; each file is truncated to
  ``MAX_SKILL_CHARS`` characters and the whole section to ``MAX_TOTAL_SKILL_CHARS`` so a
  skill can never blow up the context window;
* the section is inserted into the system prompt before the Environment section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

SKILL_SUFFIXES = (".md", ".markdown", ".txt")
MAX_SKILL_CHARS = 4000       # per skill file
MAX_TOTAL_SKILL_CHARS = 16000  # combined size of the whole skills section body


@dataclass(frozen=True)
class Skill:
    name: str          # file name without suffix
    path: str          # absolute path (shown in the UI/logs)
    body: str          # the file content (possibly truncated)
    truncated: bool = False


def _project_skills_dir() -> Path:
    # winagent/skills.py -> package root -> project root/skills
    return Path(__file__).resolve().parent.parent / "skills"


def user_skills_dir() -> Path:
    """Per-user skills directory (created on demand when saving a skill)."""
    try:
        from .config import default_config_dir
        return default_config_dir() / "skills"
    except Exception:  # pragma: no cover - config import should never fail
        return Path.home() / "skills"


def skills_dirs(extra: Optional[Iterable[Path]] = None) -> list[Path]:
    dirs = [_project_skills_dir(), user_skills_dir()]
    for d in extra or ():
        if d not in dirs:
            dirs.append(d)
    return dirs


def _is_skill_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in SKILL_SUFFIXES:
        return False
    if path.name.startswith(("_", ".")):
        return False
    return True


def load_skills(extra_dirs: Optional[Iterable[Path]] = None) -> list[Skill]:
    """Read every skill file from all skills directories (later dirs win on name clashes)."""
    found: dict[str, Skill] = {}
    for d in skills_dirs(extra_dirs):
        try:
            entries = sorted(d.iterdir()) if d.is_dir() else []
        except OSError as exc:
            log.warning("Could not read skills dir %s: %s", d, exc)
            continue
        for path in entries:
            if not _is_skill_file(path):
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError as exc:
                log.warning("Could not read skill %s: %s", path, exc)
                continue
            if not raw:
                continue
            truncated = len(raw) > MAX_SKILL_CHARS
            name = path.stem
            # a later directory (more specific: user dir) overrides the same-named skill
            found[name] = Skill(name=name, path=str(path), body=raw[:MAX_SKILL_CHARS], truncated=truncated)
            log.info("Loaded skill %r from %s (%d chars%s)", name, path, len(raw),
                     ", truncated" if truncated else "")
    return list(found.values())


def skills_section(skills: list[Skill]) -> str:
    """Render the installed skills as a system-prompt section ('' when none are installed)."""
    if not skills:
        return ""
    parts = [
        "## Installed skills (specialized procedures installed by the user)",
        "These skills are procedures the user explicitly installed for THIS agent. When the "
        "user's request matches a skill, follow its steps (they take priority over generic habits); "
        "if a skill and the user's explicit current request conflict, the user's current request wins. "
        "If you need a skill for the task but it is not listed, say so in task_complete.",
    ]
    total = 0
    for s in skills:
        budget = MAX_TOTAL_SKILL_CHARS - total
        body = s.body
        truncated = s.truncated
        if budget < 400:
            break
        if len(body) > budget:
            body = body[:budget]
            truncated = True
        total += len(body)
        parts.append(f"### Skill: {s.name}{' [truncated]' if truncated else ''}\n{body}")
    return "\n\n".join(parts)
