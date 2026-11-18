"""Install the mAutoOntology agent skill into Claude Code, OpenCode, Codex and Antigravity.

Safety rules (each covered by tests):

* targets are compared by *real* path in both directions, so symlinks,
  junctions and ``--dir`` aliases can never point an install or uninstall back
  into the source package or checkout;
* a skill directory that is itself a symlink/junction is never written through;
  uninstall removes only the link;
* install writes ``SKILL.md`` and replaces the bundle's ``references/`` folder
  wholesale (so renamed references never linger); keep your own notes next to,
  not inside, ``references/``;
* uninstall removes only the files the bundle installs (``SKILL.md`` and
  ``references/``) and keeps anything else, reporting it;
* every target is checked before the first one is written.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

SKILL_NAME = "mautoontology"
HARNESSES = ("claude", "opencode", "codex", "antigravity")
LABELS = {"claude": "Claude Code", "opencode": "OpenCode", "codex": "Codex", "antigravity": "Antigravity"}


@dataclass
class Target:
    path: Path
    harnesses: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return ", ".join(LABELS[h] for h in self.harnesses)


def bundle_dir() -> Path:
    return Path(str(resources.files("mautoontology").joinpath("skill")))


def protected_root() -> Path:
    """The source checkout (when running from one) or the installed package directory."""
    package = Path(__file__).resolve().parent
    checkout = package.parent.parent
    if (checkout / "pyproject.toml").is_file() and (checkout / "src" / "mautoontology").is_dir():
        return checkout
    return package


def bundle_files() -> list[str]:
    root = bundle_dir()
    files = ["SKILL.md"]
    refs = root / "references"
    if refs.is_dir():
        files += sorted(f"references/{p.name}" for p in refs.iterdir() if p.is_file())
    return files


def find_git_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def global_dir(harness: str, home: Path, env: dict[str, str]) -> Path:
    if harness == "claude":
        return Path(env.get("CLAUDE_CONFIG_DIR", "").strip() or home / ".claude") / "skills" / SKILL_NAME
    if harness == "opencode":
        return Path(env.get("XDG_CONFIG_HOME", "").strip() or home / ".config") / "opencode" / "skills" / SKILL_NAME
    if harness == "codex":
        return Path(env.get("CODEX_HOME", "").strip() or home / ".agents") / "skills" / SKILL_NAME
    if harness == "antigravity":
        return home / ".gemini" / "antigravity" / "skills" / SKILL_NAME
    raise ValueError(f"unsupported harness {harness}")


def project_dir(harness: str, root: Path) -> Path:
    if harness == "claude":
        return root / ".claude" / "skills" / SKILL_NAME
    if harness == "opencode":
        return root / ".opencode" / "skills" / SKILL_NAME
    return root / ".agents" / "skills" / SKILL_NAME


def resolve_targets(harnesses: list[str], scope: str, cwd: Path, project: Path | None = None,
                    home: Path | None = None, env: dict[str, str] | None = None) -> list[Target]:
    env = dict(os.environ if env is None else env)
    home = home or Path.home()
    root = (cwd / project).resolve() if project else (find_git_root(cwd) or cwd.resolve())
    targets: dict[str, Target] = {}
    for harness in harnesses:
        path = global_dir(harness, home, env) if scope == "global" else project_dir(harness, root)
        key = os.path.normcase(str(path.resolve()))
        targets.setdefault(key, Target(path=path)).harnesses.append(harness)
    return list(targets.values())


def realpath_nearest(path: Path) -> Path:
    """Real path of ``path`` following links in every existing component."""
    current = path.absolute()
    missing: list[str] = []
    while True:
        try:
            return Path(os.path.realpath(current, strict=True)).joinpath(*reversed(missing))
        except (FileNotFoundError, NotADirectoryError, OSError):
            if current.parent == current:
                return current.joinpath(*reversed(missing))
            missing.append(current.name)
            current = current.parent


def _inside(child: Path, parent: Path) -> bool:
    try:
        rel = os.path.relpath(os.path.normcase(str(child)), os.path.normcase(str(parent)))
    except ValueError:  # different drives on Windows
        return False
    return rel == "." or (not rel.startswith("..") and not os.path.isabs(rel))


def _assert_outside_source(target: Path, action: str) -> None:
    source = realpath_nearest(protected_root())
    real = realpath_nearest(target)
    if _inside(real, source) or _inside(source, real):
        raise PermissionError(
            f"refusing to {action} {target}: it resolves into the mAutoOntology source ({source}). "
            "Target another project with --dir <path>, or remove the symlink/junction if the target links here."
        )


def _is_link(path: Path) -> bool:
    """True for symlinks and Windows junctions (on every supported Python version)."""
    if path.is_symlink():
        return True
    try:
        info = os.lstat(path)
    except OSError:
        return False
    reparse_tag = getattr(info, "st_reparse_tag", 0)
    return bool(reparse_tag) and reparse_tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def install(target: Path, dry_run: bool = False) -> list[str]:
    _assert_outside_source(target, "install")
    if _is_link(target):
        raise PermissionError(f"refusing to install through {target}: it is a symlink/junction. Remove the link or target another directory.")
    files = bundle_files()
    if dry_run:
        return files
    source = bundle_dir()
    target.mkdir(parents=True, exist_ok=True)
    staging = target / f".references-staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        for rel in files[1:]:
            shutil.copyfile(source / rel, staging / Path(rel).name)
        refs = target / "references"
        if _is_link(refs):
            refs.unlink() if refs.is_symlink() else os.rmdir(refs)
        else:
            shutil.rmtree(refs, ignore_errors=True)
        os.replace(staging, refs)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    skill = target / "SKILL.md"
    if skill.is_symlink():
        skill.unlink()
    shutil.copyfile(source / "SKILL.md", skill)
    return files


@dataclass
class UninstallResult:
    removed: bool
    kept: list[str] = field(default_factory=list)
    link_only: bool = False


def uninstall(target: Path, dry_run: bool = False) -> UninstallResult:
    _assert_outside_source(target, "uninstall")
    if _is_link(target):
        if not dry_run:
            target.unlink() if target.is_symlink() else os.rmdir(target)
        return UninstallResult(removed=True, link_only=True)
    skill, refs = target / "SKILL.md", target / "references"
    existed = skill.exists() or refs.exists()
    kept = sorted(p.name for p in target.iterdir() if p.name not in {"SKILL.md", "references"}) if target.is_dir() else []
    if existed and not dry_run:
        if skill.exists() or skill.is_symlink():
            skill.unlink()
        if _is_link(refs):
            refs.unlink() if refs.is_symlink() else os.rmdir(refs)
        else:
            shutil.rmtree(refs, ignore_errors=True)
        with contextlib.suppress(OSError):
            target.rmdir()
    return UninstallResult(removed=existed, kept=kept)


def is_source_location(path: Path) -> bool:
    return realpath_nearest(path) == realpath_nearest(protected_root())
