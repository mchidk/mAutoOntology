"""Shared fixtures: generated warehouses (never committed) and an isolated copy of the example project."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "trapshop"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location("build_warehouse", EXAMPLE / "build_warehouse.py")
warehouse = importlib.util.module_from_spec(_spec)
sys.modules["build_warehouse"] = warehouse
_spec.loader.exec_module(warehouse)


@pytest.fixture(scope="session")
def trap_db(tmp_path_factory) -> Path:
    return warehouse.build(tmp_path_factory.mktemp("trap") / "trap.duckdb")


@pytest.fixture(scope="session")
def tpch_db(tmp_path_factory) -> Path:
    return warehouse.build_tpch(tmp_path_factory.mktemp("tpch") / "tpch.duckdb")


@pytest.fixture()
def example_project(tmp_path, trap_db) -> Path:
    """A writable copy of examples/trapshop (reviewed semantic layer) with its warehouse."""
    root = tmp_path / "trapshop"
    shutil.copytree(EXAMPLE / "semantics", root / "semantics")
    shutil.copy(trap_db, root / "trapshop.duckdb")
    return root


@pytest.fixture(scope="session")
def example_project_ro(tmp_path_factory, trap_db) -> Path:
    """Session-wide read-only copy of the example (do not modify in tests)."""
    root = tmp_path_factory.mktemp("example") / "trapshop"
    shutil.copytree(EXAMPLE / "semantics", root / "semantics")
    shutil.copy(trap_db, root / "trapshop.duckdb")
    return root
