"""Installed skills: user-provided .md/.txt procedure documents loaded into the system prompt."""

import pytest

from winagent.agent import Agent
from winagent.skills import (MAX_SKILL_CHARS, load_skills, skills_section, user_skills_dir)


def test_load_skills_from_tmp_dir(tmp_path):
    (tmp_path / "crm-export.md").write_text("### When to use\nExport CRM invoices.\n", encoding="utf-8")
    (tmp_path / "weekly.txt").write_text("Make the weekly report.\n", encoding="utf-8")
    (tmp_path / "_ignored.md").write_text("template – must not load\n", encoding="utf-8")
    (tmp_path / "notes.txt.bak").write_text("wrong suffix – must not load\n", encoding="utf-8")
    (tmp_path / "empty.md").write_text("   \n", encoding="utf-8")

    skills = load_skills(extra_dirs=[tmp_path])
    # extra dir is searched; the real project/user dirs may contribute nothing in the test env
    names = {s.name for s in skills}
    assert "crm-export" in names and "weekly" in names
    assert "_ignored" not in names and "notes" not in names and "empty" not in names
    by_name = {s.name: s for s in skills}
    assert by_name["crm-export"].truncated is False
    assert by_name["crm-export"].path.endswith("crm-export.md")


def test_user_dir_overrides_project_name_clash(tmp_path):
    (tmp_path / "common.md").write_text("project version\n", encoding="utf-8")
    override = tmp_path / "user"
    override.mkdir()
    (override / "common.md").write_text("user version wins\n", encoding="utf-8")
    skills = load_skills(extra_dirs=[tmp_path, override])
    common = next(s for s in skills if s.name == "common")
    assert common.body == "user version wins"
    assert common.path.startswith(str(override))


def test_long_skill_is_truncated(tmp_path):
    (tmp_path / "big.md").write_text("x" * (MAX_SKILL_CHARS + 500), encoding="utf-8")
    skills = load_skills(extra_dirs=[tmp_path])
    big = next(s for s in skills if s.name == "big")
    assert big.truncated is True
    assert len(big.body) == MAX_SKILL_CHARS


def test_section_rendering_and_total_cap(tmp_path):
    (tmp_path / "a.md").write_text("skill A body\n", encoding="utf-8")
    skills = load_skills(extra_dirs=[tmp_path])
    section = skills_section([s for s in skills if s.name == "a"])
    assert "Installed skills" in section
    assert "### Skill: a" in section
    assert "skill A body" in section
    assert skills_section([]) == ""


def test_agent_includes_skills_in_system_prompt(config, backend, tmp_path, monkeypatch):
    import winagent.skills as skills_mod
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: tmp_path)
    (tmp_path / "pdf-export.md").write_text("### Steps\nExport via the Report menu.\n", encoding="utf-8")
    agent = Agent(config, backend, llm=None)
    sys_msg = agent._system_message()
    assert "### Skill: pdf-export" in sys_msg["content"]
    assert "Installed skills" in sys_msg["content"]


def test_no_skills_adds_nothing(config, backend, tmp_path, monkeypatch):
    import winagent.skills as skills_mod
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: tmp_path)
    agent = Agent(config, backend, llm=None)
    sys_msg = agent._system_message()
    assert "Installed skills" not in sys_msg["content"]


def test_user_skills_dir_is_under_user_config():
    d = user_skills_dir()
    assert d.name == "skills"
    assert d.parent.name in ("WinAgent", "winagent")


def test_save_skill_slugs_and_writes(tmp_path):
    from winagent.skills import save_skill
    path = save_skill("My CRM Export!", "### Steps\n1. do it\n", tmp_path)
    assert path == tmp_path / "my-crm-export.md"
    assert "### Steps" in path.read_text(encoding="utf-8")
    # overwriting works and keeps the same name
    assert save_skill("My CRM Export", "new body", tmp_path) == path
    assert path.read_text(encoding="utf-8").strip() == "new body"


def test_save_skill_rejects_dead_names_and_empty(tmp_path):
    from winagent.skills import save_skill
    with pytest.raises(ValueError):
        save_skill("_hidden", "body", tmp_path)   # would be ignored by the loader
    with pytest.raises(ValueError):
        save_skill("ok-name", "   ", tmp_path)    # empty content
    assert list(tmp_path.iterdir()) == []


def test_agent_picks_up_new_skill_without_rebuild(config, backend, tmp_path, monkeypatch):
    import winagent.skills as skills_mod
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: tmp_path)
    agent = Agent(config, backend, llm=None)
    assert "### Skill: fresh" not in agent._system_message()["content"]
    skills_mod.save_skill("fresh", "### Steps\nDo the thing.\n", tmp_path)
    # the very next request sees the installed skill – no agent rebuild needed
    assert "### Skill: fresh" in agent._system_message()["content"]
