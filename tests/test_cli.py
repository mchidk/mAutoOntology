"""CLI end-to-end flow on the trap warehouse, exit codes and strict argument parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mautoontology.cli import main


def run(capsys, *argv):
    code = main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_full_flow(tmp_path, trap_db, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    db = trap_db
    assert run(capsys, "init", "--root", root, "--dialect", "duckdb", "--name", "shop")[0] == 0
    assert run(capsys, "extract", "--root", root, "--duckdb", db)[0] == 0
    assert run(capsys, "profile", "run", "--root", root, "--db", db)[0] == 0
    code, out, _ = run(capsys, "propose", "--root", root, "--json")
    assert code == 0 and json.loads(out)["relationships_accepted"] == 14
    code, out, _ = run(capsys, "validate", "--root", root, "--json")
    assert code == 0 and json.loads(out)["counts"]["error"] == 0
    assert run(capsys, "index", "--root", root)[0] == 0
    assert (root / "semantics" / "INDEX.md").is_file()
    code, out, _ = run(capsys, "context", "--root", root, "--entities", "order,customer", "--json")
    assert code == 0 and json.loads(out)["join_plan"]["steps"][0]["relationship"] == "order__customer"

    bad = "SELECT SUM(o.amount_cents) FROM orders o JOIN order_items i ON i.order_id = o.order_id"
    code, out, _ = run(capsys, "check-sql", "--root", root, "--sql", bad)
    assert code == 1 and "MAO006" in out
    good = "SELECT COUNT(*) AS n FROM orders WHERE COALESCE(is_deleted, FALSE) = FALSE"
    assert run(capsys, "check-sql", "--root", root, "--sql", good)[0] == 0
    code, out, _ = run(capsys, "query", "--root", root, "--db", db, "--sql", good, "--json")
    assert code == 0 and json.loads(out)["rows"] == [{"n": 2940}]
    code, out, _ = run(capsys, "query", "--root", root, "--db", db, "--sql", bad)
    assert code == 1 and "Refused" in out
    code, out, _ = run(capsys, "export", "--root", root, "--to", "all", "--out", tmp_path / "out", "--json")
    assert code == 0 and len(json.loads(out)["written"]) >= 5


def test_warehouse_emit_and_ingest(tmp_path, trap_db, capsys):
    from mautoontology.readonly import run_query

    root = tmp_path / "proj"
    run(capsys, "extract", "--root", root, "--duckdb", trap_db, "--dialect", "snowflake")
    code, out, _ = run(capsys, "profile", "emit", "--root", root, "--json")
    plan_path = json.loads(out)["plan"]
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    assert plan["stage"] == 1 and plan["dialect"] == "snowflake"
    duck_plan = run(capsys, "profile", "emit", "--root", root, "--dialect", "duckdb", "--out", tmp_path / "p1.json")
    plan = json.loads((tmp_path / "p1.json").read_text(encoding="utf-8"))
    results = {q["id"]: run_query(trap_db, q["sql"]).as_dicts() for q in plan["queries"]}
    (tmp_path / "r1.json").write_text(json.dumps(results, default=str), encoding="utf-8")
    assert run(capsys, "profile", "ingest", "--root", root, "--plan", tmp_path / "p1.json", "--results", tmp_path / "r1.json")[0] == 0
    code, out, _ = run(capsys, "profile", "emit", "--root", root, "--dialect", "duckdb", "--json")
    assert json.loads(out)["stage"] == 2
    assert duck_plan[0] == 0


def test_emit_infoschema_and_missing_inputs(tmp_path, capsys):
    code, out, _ = run(capsys, "extract", "--root", tmp_path, "--emit-infoschema", "--dialect", "snowflake")
    assert code == 0 and "information_schema.columns" in out and "SHOW IMPORTED KEYS" in out
    code, _, err = run(capsys, "extract", "--root", tmp_path)
    assert code == 2 and "at least one source" in err
    code, _, err = run(capsys, "propose", "--root", tmp_path)
    assert code == 1 and "extract" in err


def test_verify_against_the_example(example_project, capsys):
    code, out, _ = run(capsys, "verify", "--root", example_project, "--db", example_project / "trapshop.duckdb")
    assert code == 0 and "3/3 verified queries pass" in out


@pytest.mark.parametrize("argv", [
    ["check-sql", "--sqll", "SELECT 1"],        # typo
    ["skill", "install", "--dry-ru"],            # abbreviations are not accepted
    ["export", "--to", "tableau"],              # invalid choice
    ["frobnicate"],                              # unknown command
])
def test_strict_argument_parsing(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    from mautoontology import __version__

    assert __version__ in capsys.readouterr().out
