"""Skill installer safety: real-path guards, link handling, scoped uninstall, preflight, strict flags."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mautoontology import installer
from mautoontology.cli import main

from .conftest import REPO


def _link(target, link):
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    values = {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "XDG_CONFIG_HOME": str(home / ".config"),
              "CODEX_HOME": str(home / ".codex"), "HOME": str(home), "USERPROFILE": str(home)}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    return home, project


def test_install_status_uninstall_project(env, capsys, monkeypatch):
    home, project = env
    monkeypatch.chdir(project)
    assert main(["skill", "install", "--all"]) == 0
    for rel in (".claude/skills/mautoontology", ".opencode/skills/mautoontology", ".agents/skills/mautoontology"):
        assert (project / rel / "SKILL.md").is_file()
        assert sorted(p.name for p in (project / rel / "references").iterdir()) == [
            "check-sql-rules.md", "modeling-rules.md", "review.md", "workflow.md"]
    out = capsys.readouterr().out
    assert "Codex, Antigravity" in out  # shared .agents directory is installed once
    assert main(["skill", "status", "--claude"]) == 0
    assert "project: Claude Code: installed" in capsys.readouterr().out
    (project / ".claude/skills/mautoontology/notes.md").write_text("mine", encoding="utf-8")
    assert main(["skill", "uninstall", "--claude"]) == 0
    assert "kept: notes.md" in capsys.readouterr().out
    assert (project / ".claude/skills/mautoontology/notes.md").read_text(encoding="utf-8") == "mine"
    assert not (project / ".claude/skills/mautoontology/SKILL.md").exists()


def test_global_install_uses_env_overrides(env, capsys, monkeypatch):
    home, project = env
    monkeypatch.chdir(project)
    assert main(["skill", "install", "--global", "--all"]) == 0
    assert (home / ".claude/skills/mautoontology/SKILL.md").is_file()
    assert (home / ".config/opencode/skills/mautoontology/SKILL.md").is_file()
    assert (home / ".codex/skills/mautoontology/SKILL.md").is_file()
    assert (home / ".gemini/antigravity/skills/mautoontology/SKILL.md").is_file()


def test_dry_run_writes_nothing(env, monkeypatch):
    _, project = env
    monkeypatch.chdir(project)
    assert main(["skill", "install", "--all", "--dry-run"]) == 0
    assert not (project / ".claude").exists()


def test_refuses_to_install_into_the_source_checkout(monkeypatch, capsys):
    monkeypatch.chdir(REPO)
    before = (REPO / ".claude/skills/mautoontology/SKILL.md").read_bytes()
    assert main(["skill", "install", "--claude"]) == 1
    assert "resolves into the mAutoOntology source" in capsys.readouterr().err
    assert (REPO / ".claude/skills/mautoontology/SKILL.md").read_bytes() == before
    assert not (REPO / ".claude/skills/mautoontology/references").exists()


def test_refuses_links_that_resolve_into_the_source(env, tmp_path, capsys):
    home, _ = env
    _link(REPO, home / ".claude" / "skills" / "mautoontology")
    assert main(["skill", "install", "--global", "--claude"]) == 1
    assert "resolves into the mAutoOntology source" in capsys.readouterr().err
    alias = tmp_path / "alias"
    _link(REPO, alias)
    assert main(["skill", "uninstall", "--dir", str(alias)]) == 1
    assert (REPO / ".claude/skills/mautoontology/SKILL.md").is_file()


def test_never_writes_or_deletes_through_a_linked_skill_dir(env, tmp_path, capsys):
    home, _ = env
    other = tmp_path / "other-checkout"
    other.mkdir()
    (other / "SKILL.md").write_text("someone else's skill", encoding="utf-8")
    skill_dir = home / ".claude" / "skills" / "mautoontology"
    _link(other, skill_dir)
    assert main(["skill", "install", "--global", "--claude"]) == 1
    assert "symlink/junction" in capsys.readouterr().err
    assert main(["skill", "uninstall", "--global", "--claude"]) == 0
    assert "Removed link" in capsys.readouterr().out
    assert not os.path.lexists(skill_dir)
    assert (other / "SKILL.md").read_text(encoding="utf-8") == "someone else's skill"


def test_global_and_dir_conflict(env, capsys):
    assert main(["skill", "install", "--global", "--dir", "x"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_preflight_refuses_before_any_write(env, tmp_path, monkeypatch):
    home, project = env
    monkeypatch.chdir(project)
    _link(REPO, home / ".config" / "opencode" / "skills" / "mautoontology")  # second target is unsafe
    assert main(["skill", "install", "--global", "--claude", "--opencode"]) == 1
    assert not (home / ".claude" / "skills" / "mautoontology").exists()  # first target untouched


def test_module_entry_point():
    result = subprocess.run([sys.executable, "-m", "mautoontology", "--version"], capture_output=True, text=True, cwd=REPO)
    assert result.returncode == 0 and "mautoontology" in result.stdout
