"""Repository hygiene gates: names, links, schema, versions and committed artifacts."""

from __future__ import annotations

import json
import re
import subprocess

from jsonschema import Draft202012Validator

import mautoontology
from mautoontology.model import load_schema

from .conftest import REPO

TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".sql", ".cfg", ".ini"}
# Built from pieces so this file does not match itself.
FORBIDDEN = re.compile("|".join(["nvi" + "dia", "onto" + "forge", "auto[ _-]?" + "ontology"]), re.IGNORECASE)
PRODUCT = re.compile("m" + "auto" + "ontology", re.IGNORECASE)


def _committable_files():
    """Files git would commit: tracked plus untracked-but-not-ignored."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout
    return [REPO / line for line in out.splitlines() if line and (REPO / line).is_file()]


def test_no_forbidden_names_anywhere():
    offenders = []
    for path in _committable_files():
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"LICENSE", ".gitignore", ".gitattributes"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if FORBIDDEN.search(PRODUCT.sub("", line)):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()[:100]}")
    assert offenders == []


def test_markdown_links_resolve():
    broken = []
    for path in _committable_files():
        if path.suffix != ".md":
            continue
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for target in re.findall(r"(?<!!)\[[^\]]+\]\(([^)\s]+)\)", text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            file_part = target.split("#")[0]
            if file_part and not (path.parent / file_part).exists():
                broken.append(f"{path.relative_to(REPO)} -> {target}")
    assert broken == []


def test_schemas_are_valid_json_schemas():
    Draft202012Validator.check_schema(load_schema())
    Draft202012Validator.check_schema(load_schema("ossie-0.3.0.dev0.schema.json"))


def test_versions_are_consistent():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE).group(1)
    assert mautoontology.__version__ == version
    assert f"## {version}" in (REPO / "CHANGELOG.md").read_text(encoding="utf-8")


def test_skill_frontmatter_and_adapters():
    canonical = (REPO / "src/mautoontology/skill/SKILL.md").read_text(encoding="utf-8")
    assert canonical.startswith("---\nname: mautoontology\ndescription: ")
    for harness in (".claude", ".opencode", ".agents"):
        adapter = (REPO / harness / "skills/mautoontology/SKILL.md").read_text(encoding="utf-8")
        assert adapter.startswith("---\nname: mautoontology\n")
        assert "../../../src/mautoontology/skill/SKILL.md" in adapter


def test_example_evidence_contains_no_personal_values():
    profile = json.loads((REPO / "examples/trapshop/semantics/_evidence/profile.json").read_text(encoding="utf-8"))
    for dataset in profile["datasets"].values():
        for name, column in dataset.get("columns", {}).items():
            if any(word in name for word in ("email", "name", "phone")):
                assert "values" not in column and "min" not in column, name


def test_no_databases_or_caches_would_be_committed():
    files = _committable_files()
    assert not [p for p in files if p.suffix in {".duckdb", ".db", ".sqlite", ".pyc"}]
    assert not [p for p in files if ".venv" in p.parts or "__pycache__" in p.parts]
