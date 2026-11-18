"""Command line interface: ``mautoontology <command>``.

Every command supports ``--json`` so agents can consume results. Exit codes:
0 = success, 1 = findings/validation errors (or a refused action), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import SCHEMA_ID, __version__
from .model import EVIDENCE_DIR, PROFILE_FILE, SEMANTICS_DIR, SNAPSHOT_FILE, Project


def _emit(args: argparse.Namespace, payload: Any, text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    elif text is not None:
        print(text)


def _usage(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _semantics(root: Path) -> Path:
    return root / SEMANTICS_DIR


# --------------------------------------------------------------------- init
def cmd_init(args: argparse.Namespace) -> int:
    from .yamlio import ordered, write_file

    root = Path(args.root)
    config_path = _semantics(root) / "mautoontology.yaml"
    if config_path.exists() and not args.force:
        _emit(args, {"created": False, "path": str(config_path)}, f"{config_path} already exists (use --force to overwrite)")
        return 0
    doc = {
        "schema": SCHEMA_ID,
        "kind": "config",
        "project": {"name": args.name or root.resolve().name, "dialect": args.dialect, "default_domain": args.domain},
        "conventions": {},
    }
    write_file(config_path, ordered(doc))
    (_semantics(root) / EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
    gitignore = _semantics(root) / ".gitignore"
    if not gitignore.exists():
        gitignore.write_bytes(b".cache/\n")
    _emit(args, {"created": True, "path": str(config_path)}, f"Created {config_path}")
    return 0


# ------------------------------------------------------------------ extract
def cmd_extract(args: argparse.Namespace) -> int:
    from .extract import (
        extract_dbt,
        extract_ddl,
        extract_duckdb,
        extract_infoschema,
        extract_sqlite,
        infoschema_query,
    )
    from .snapshot import merge_snapshots

    root = Path(args.root)
    config_dialect = (Project.load(root).config.get("project") or {}).get("dialect")
    dialect = args.dialect or config_dialect or "ansi"
    if args.emit_infoschema:
        queries = infoschema_query(dialect, args.schema)
        _emit(args, queries, "\n\n".join(f"-- {name}\n{sql};" for name, sql in queries.items()))
        return 0
    snapshots, warnings = [], []
    for path in args.dbt or []:
        snap, warn = extract_dbt(path, args.dialect)
        snapshots.append(snap)
        warnings += warn
    if args.ddl:
        snap, warn = extract_ddl(args.ddl, dialect)
        snapshots.append(snap)
        warnings += warn
    if args.infoschema:
        snap, warn = extract_infoschema(args.infoschema, dialect)
        snapshots.append(snap)
        warnings += warn
    for path in args.sqlite or []:
        snapshots.append(extract_sqlite(path))
    for path in args.duckdb or []:
        snapshots.append(extract_duckdb(path))
    if not snapshots:
        print("error: give at least one source: --dbt, --ddl, --infoschema, --sqlite or --duckdb (or --emit-infoschema)", file=sys.stderr)
        return 2
    snapshot = merge_snapshots(snapshots)
    if args.dialect:
        snapshot.dialect = args.dialect
    elif snapshot.dialect == "ansi" and config_dialect:
        snapshot.dialect = config_dialect
    out = snapshot.save(_semantics(root) / EVIDENCE_DIR / SNAPSHOT_FILE)
    summary = {"snapshot": str(out), "dialect": snapshot.dialect, "datasets": len(snapshot.datasets),
               "columns": sum(len(d.columns) for d in snapshot.datasets),
               "declared_foreign_keys": sum(len(d.foreign_keys) for d in snapshot.datasets),
               "dbt_semantic_models": len((snapshot.semantic.get("dbt") or {}).get("semantic_models") or []),
               "warnings": warnings}
    text = (f"Extracted {summary['datasets']} datasets / {summary['columns']} columns ({snapshot.dialect}) -> {out}"
            + "".join(f"\n  warning: {w}" for w in warnings))
    _emit(args, summary, text)
    return 0


# ------------------------------------------------------------------ profile
def _load_snapshot(root: Path):
    from .snapshot import Snapshot

    path = _semantics(root) / EVIDENCE_DIR / SNAPSHOT_FILE
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found: run `mautoontology extract` first")
    return Snapshot.load(path)


def cmd_profile(args: argparse.Namespace) -> int:
    from . import profile as prof

    root = Path(args.root)
    if args.action == "run" and not args.db:
        return _usage("profile run needs --db <local SQLite/DuckDB file>")
    if args.action == "ingest" and not (args.plan and args.results):
        return _usage("profile ingest needs --plan <plan.json> and --results <results.json>")
    snapshot = _load_snapshot(root)
    profile_path = _semantics(root) / EVIDENCE_DIR / PROFILE_FILE
    if args.action == "run":
        result = prof.run(args.db, snapshot, timeout_s=args.timeout)
        prof.save_profile(result, profile_path)
        summary = {"profile": str(profile_path), "datasets": len(result["datasets"]), "inclusion_checks": len(result["inclusions"])}
        _emit(args, summary, f"Profiled {summary['datasets']} datasets, {summary['inclusion_checks']} inclusion checks -> {profile_path}")
        return 0
    if args.action == "emit":
        current = prof.load_profile(profile_path)
        stage = args.stage
        if stage == "auto":
            stage = "2" if current.get("datasets") else "1"
        dialect = args.dialect or snapshot.dialect
        if stage == "1":
            plan = prof.emit_stage1(snapshot, dialect, approx=args.approx, sample_pct=args.sample_pct)
        else:
            plan = prof.emit_stage2(snapshot, current, dialect, budget=args.budget)
        out = Path(args.out) if args.out else _semantics(root) / EVIDENCE_DIR / f"profile_plan_stage{stage}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes((json.dumps(plan, indent=2) + "\n").encode("utf-8"))
        text = (f"Wrote stage-{stage} plan with {len(plan['queries'])} read-only queries -> {out}\n"
                "Run each query with your warehouse access and save {query_id: [rows]} as JSON, then:\n"
                f"  mautoontology profile ingest --plan {out} --results <results.json>")
        _emit(args, {"plan": str(out), "stage": int(stage), "queries": len(plan["queries"]), "truncated": plan.get("truncated", False)}, text)
        return 0
    # ingest
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    raw = json.loads(Path(args.results).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        raw = {item["id"]: item.get("rows", []) for item in raw}
    current = prof.load_profile(profile_path)
    updated = prof.ingest(plan, raw, current if current.get("datasets") else None)
    prof.save_profile(updated, profile_path)
    missing = updated.get("missing_results") or []
    _emit(args, {"profile": str(profile_path), "missing_results": missing},
          f"Ingested stage-{plan.get('stage')} results -> {profile_path}" + (f" ({len(missing)} queries missing results)" if missing else ""))
    return 0


# ------------------------------------------------------------------ propose
def cmd_propose(args: argparse.Namespace) -> int:
    from .propose.run import run_propose

    summary = run_propose(args.root, domain=args.domain)
    data = summary.to_dict()
    text = (f"Entities: {len(summary.entities_created)} created, {len(summary.entities_updated)} refreshed; "
            f"relationships: {summary.relationships_accepted} accepted, {summary.relationships_rejected} rejected; "
            f"human edits kept: {summary.kept_human}; conflicts: {summary.conflicts}; "
            f"review questions: {summary.review_questions} (semantics/_proposals/review.yaml)"
            + (f"; dbt metrics adopted: {summary.adopted_metrics}" if summary.adopted_metrics else "")
            + "".join(f"\n  warning: {w}" for w in summary.warnings))
    _emit(args, data, text)
    return 0


# ----------------------------------------------------------------- validate
def cmd_validate(args: argparse.Namespace) -> int:
    from .snapshot import Snapshot
    from .validate import validate_project

    project = Project.load(args.root)
    drift = Snapshot.load(args.drift) if args.drift else None
    report = validate_project(project, drift_snapshot=drift)
    data = report.to_dict()
    lines = [f"{i['severity'].upper():7s} {i['code']} {i['where']}: {i['message']}" for i in data["issues"]]
    counts = data["counts"]
    lines.append(f"{'OK' if report.ok else 'FAILED'}: {counts['error']} errors, {counts['warning']} warnings, {counts['info']} info ({report.files} files)")
    _emit(args, data, "\n".join(lines))
    failed = not report.ok or (args.strict and counts["warning"] > 0)
    return 1 if failed else 0


# -------------------------------------------------------------------- index
def cmd_index(args: argparse.Namespace) -> int:
    from .index_md import build_index

    project = Project.load(args.root)
    text = build_index(project)
    path = project.semantics / "INDEX.md"
    path.write_bytes(text.encode("utf-8"))
    _emit(args, {"index": str(path), "entities": len(project.entities), "metrics": len(project.metrics)},
          f"Wrote {path} ({len(project.entities)} entities, {len(project.metrics)} metrics, ~{len(text) // 4} tokens)")
    return 0


# ------------------------------------------------------------------ context
def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def cmd_context(args: argparse.Namespace) -> int:
    from .context import build_context, render_markdown, search

    project = Project.load(args.root)
    if args.search:
        hits = search(project, args.search, limit=args.limit)
        _emit(args, hits, "\n".join(f"{h['score']:6.2f}  {h['kind']:9s} {h['id']}" for h in hits) or "no matches")
        return 0
    ctx = build_context(project, entities=_csv(args.entities), metrics=_csv(args.metrics), terms=_csv(args.terms),
                        question=args.question, base=args.base, dialect=args.dialect)
    if args.json:
        _emit(args, ctx)
    else:
        print(render_markdown(ctx), end="")
    return 0 if ctx.get("entities") else 1


# ---------------------------------------------------------------- check-sql
def _read_sql(args: argparse.Namespace) -> str:
    if args.sql:
        return args.sql
    if args.file and args.file != "-":
        return Path(args.file).read_text(encoding="utf-8")
    return sys.stdin.read()


def cmd_check_sql(args: argparse.Namespace) -> int:
    from .checksql import check_sql

    project = Project.load(args.root)
    result = check_sql(project, _read_sql(args), args.dialect)
    data = result.to_dict()
    lines = []
    for f in data["findings"]:
        lines.append(f"{f['severity'].upper():7s} {f['rule']} {f['message']}")
        if f.get("hint"):
            lines.append(f"        fix: {f['hint']}")
    lines.append("OK" if result.ok and not data["findings"] else ("OK with warnings" if result.ok else "FAILED"))
    _emit(args, data, "\n".join(lines))
    warnings = any(f["severity"] == "warning" for f in data["findings"])
    return 1 if (not result.ok or (args.strict and warnings)) else 0


# -------------------------------------------------------------------- query
def cmd_query(args: argparse.Namespace) -> int:
    from .checksql import check_sql
    from .readonly import detect_engine, run_query

    project = Project.load(args.root)
    sql = _read_sql(args)
    engine = detect_engine(args.db)
    result = check_sql(project, sql, args.dialect or engine)
    if not result.ok:
        _emit(args, {"executed": False, "check": result.to_dict()},
              "Refused: check-sql found errors (run `mautoontology check-sql` for details)")
        return 1
    rows = run_query(args.db, sql, timeout_s=args.timeout, max_rows=args.max_rows, engine=engine)
    payload = {"executed": True, "columns": rows.columns, "rows": rows.as_dicts(), "truncated": rows.truncated,
               "elapsed_s": rows.elapsed_s, "check": result.to_dict()}
    text_lines = ["\t".join(rows.columns)] + ["\t".join("" if v is None else str(v) for v in row) for row in rows.rows]
    if rows.truncated:
        text_lines.append(f"... truncated at {args.max_rows} rows")
    _emit(args, payload, "\n".join(text_lines))
    return 0


# ------------------------------------------------------------------- verify
def cmd_verify(args: argparse.Namespace) -> int:
    from .checksql import check_sql
    from .readonly import run_query

    project = Project.load(args.root)
    results, failed = [], 0
    for query_id, query in sorted(project.queries.items()):
        check = check_sql(project, query["sql"], query.get("dialect"))
        entry: dict[str, Any] = {"id": query_id, "check_ok": check.ok, "findings": [f.to_dict() for f in check.findings]}
        ok = check.ok
        if args.db and check.ok:
            rows = run_query(args.db, query["sql"], timeout_s=args.timeout)
            expected = query.get("expected") or {}
            entry["row_count"] = len(rows.rows)
            if "row_count" in expected and expected["row_count"] != len(rows.rows):
                ok = False
                entry["error"] = f"expected {expected['row_count']} rows, got {len(rows.rows)}"
            if "columns" in expected and [c.lower() for c in expected["columns"]] != [c.lower() for c in rows.columns]:
                ok = False
                entry["error"] = f"expected columns {expected['columns']}, got {rows.columns}"
        entry["ok"] = ok
        failed += 0 if ok else 1
        results.append(entry)
    text = "\n".join(f"{'PASS' if r['ok'] else 'FAIL'} {r['id']}" + (f" ({r.get('error')})" if r.get("error") else "") for r in results)
    _emit(args, {"ok": failed == 0, "results": results}, (text + "\n" if text else "") + f"{len(results) - failed}/{len(results)} verified queries pass")
    return 1 if failed else 0


# ------------------------------------------------------------------- export
def cmd_export(args: argparse.Namespace) -> int:
    from .export import TARGETS, export

    project = Project.load(args.root)
    targets = list(TARGETS) if args.to == "all" else [args.to]
    written = []
    for target in targets:
        written += [str(p) for p in export(project, target, Path(args.out) / target if args.to == "all" else Path(args.out))]
    _emit(args, {"written": written}, "\n".join(f"Wrote {p}" for p in written))
    return 0


# -------------------------------------------------------------------- skill
def cmd_skill(args: argparse.Namespace) -> int:
    from . import installer

    harnesses = list(installer.HARNESSES) if args.all or not any(getattr(args, h) for h in installer.HARNESSES) else [
        h for h in installer.HARNESSES if getattr(args, h)]
    if args.global_ and (args.project or args.dir):
        print("error: --global cannot be combined with --project or --dir", file=sys.stderr)
        return 2
    cwd = Path.cwd()
    if args.action == "status":
        out = {}
        for scope in ("project", "global"):
            targets = installer.resolve_targets(harnesses, scope, cwd, Path(args.dir) if args.dir else None)
            out[scope] = [{"harnesses": t.label, "path": str(t.path), "installed": (t.path / "SKILL.md").is_file()} for t in targets]
        text = "\n".join(f"{scope}: {e['harnesses']}: {'installed' if e['installed'] else 'not installed'} ({e['path']})"
                         for scope, entries in out.items() for e in entries)
        _emit(args, out, text)
        return 0
    scope = "global" if args.global_ else "project"
    targets = installer.resolve_targets(harnesses, scope, cwd, Path(args.dir) if args.dir else None)
    operation = installer.install if args.action == "install" else installer.uninstall
    try:
        for target in targets:  # preflight: refuse before anything is written
            operation(target.path, dry_run=True)
        results = [(t, operation(t.path, dry_run=args.dry_run)) for t in targets]
    except PermissionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    lines, payload = [], []
    for target, res in results:
        if args.action == "install":
            verb = "Would install" if args.dry_run else "Installed"
            lines.append(f"{verb} mautoontology ({target.label}) -> {target.path} [{len(res)} files]")
            payload.append({"harnesses": target.label, "path": str(target.path), "files": res})
        else:
            if not res.removed:
                lines.append(f"Not installed ({target.label}) at {target.path}")
            elif res.link_only:
                lines.append(f"{'Would remove' if args.dry_run else 'Removed'} link ({target.label}) {target.path} (link target untouched)")
            else:
                kept = f" (kept: {', '.join(res.kept)})" if res.kept else ""
                lines.append(f"{'Would remove' if args.dry_run else 'Removed'} mautoontology ({target.label}) <- {target.path}{kept}")
            payload.append({"harnesses": target.label, "path": str(target.path), "removed": res.removed, "kept": res.kept, "link_only": res.link_only})
    _emit(args, payload, "\n".join(lines))
    return 0


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mautoontology", allow_abbrev=False,
                                     description="Evidence-backed semantic layers (ontology-as-code) for any SQL warehouse.")
    parser.add_argument("--version", action="version", version=f"mautoontology {__version__}")
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument("--root", default=".", help="project root containing semantics/ (default: current directory)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common], allow_abbrev=False)

    p = add("init", "create semantics/mautoontology.yaml")
    p.add_argument("--name")
    p.add_argument("--dialect", default="ansi", help="sqlglot dialect: snowflake, databricks, bigquery, postgres, tsql, duckdb, ...")
    p.add_argument("--domain", default="core", help="default domain folder for new entities")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = add("extract", "build the catalog snapshot from dbt / DDL / information_schema / local databases")
    p.add_argument("--dbt", nargs="+", metavar="PROJECT_DIR")
    p.add_argument("--ddl", nargs="+", metavar="FILE_OR_DIR")
    p.add_argument("--infoschema", nargs="+", metavar="CSV_OR_JSON")
    p.add_argument("--sqlite", nargs="+", metavar="DB")
    p.add_argument("--duckdb", nargs="+", metavar="DB")
    p.add_argument("--dialect")
    p.add_argument("--emit-infoschema", action="store_true", help="print the catalog queries to run for --dialect and exit")
    p.add_argument("--schema", help="restrict --emit-infoschema to one schema")
    p.set_defaults(func=cmd_extract)

    p = add("profile", "profile data: emit SQL for your warehouse, ingest results, or run locally")
    p.add_argument("action", choices=["emit", "ingest", "run"])
    p.add_argument("--stage", choices=["auto", "1", "2"], default="auto")
    p.add_argument("--dialect")
    p.add_argument("--approx", action="store_true", help="use APPROX_COUNT_DISTINCT in stage 1 (cheaper on large tables)")
    p.add_argument("--sample-pct", type=float, help="sample stage-1 stats (percent of rows)")
    p.add_argument("--budget", type=int, default=400, help="maximum number of stage-2 queries")
    p.add_argument("--out", help="where to write the plan (emit)")
    p.add_argument("--plan", help="plan JSON (ingest)")
    p.add_argument("--results", help="results JSON {query_id: [rows]} (ingest)")
    p.add_argument("--db", help="local SQLite/DuckDB file (run)")
    p.add_argument("--timeout", type=float, default=60.0)
    p.set_defaults(func=cmd_profile)

    p = add("propose", "propose/refresh entities, keys, relationships and semantic types (never overwrites human edits)")
    p.add_argument("--domain")
    p.set_defaults(func=cmd_propose)

    p = add("validate", "validate schema, references, semantics, governance and verified queries")
    p.add_argument("--drift", metavar="SNAPSHOT_JSON", help="compare against a fresh snapshot")
    p.add_argument("--strict", action="store_true", help="fail on warnings too")
    p.set_defaults(func=cmd_validate)

    p = add("index", "write semantics/INDEX.md (compact catalog for agents)")
    p.set_defaults(func=cmd_index)

    p = add("context", "minimal connecting context (joins, filters, metrics, warnings) for a question")
    p.add_argument("--entities")
    p.add_argument("--metrics")
    p.add_argument("--terms")
    p.add_argument("--question", help="free-text question (lexical suggestions when no elements are given)")
    p.add_argument("--search", help="only rank elements for this text")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--base", help="entity whose grain the answer is at")
    p.add_argument("--dialect")
    p.set_defaults(func=cmd_context)

    for name, help_text, func in (("check-sql", "check candidate SQL against the semantic layer", cmd_check_sql),
                                  ("query", "check, then run read-only SQL on a local SQLite/DuckDB file", cmd_query)):
        p = add(name, help_text)
        p.add_argument("--sql")
        p.add_argument("--file", help="SQL file ('-' for stdin)")
        p.add_argument("--dialect")
        if name == "check-sql":
            p.add_argument("--strict", action="store_true", help="fail on warnings too")
        else:
            p.add_argument("--db", required=True)
            p.add_argument("--timeout", type=float, default=30.0)
            p.add_argument("--max-rows", type=int, default=1000)
        p.set_defaults(func=func)

    p = add("verify", "check (and optionally run) every verified query")
    p.add_argument("--db", help="local SQLite/DuckDB file to execute against")
    p.add_argument("--timeout", type=float, default=30.0)
    p.set_defaults(func=cmd_verify)

    p = add("export", "export to Apache Ossie, dbt MetricFlow, Snowflake, Databricks or Markdown")
    p.add_argument("--to", required=True, choices=["ossie", "dbt", "snowflake", "databricks", "markdown", "all"])
    p.add_argument("--out", default="semantics_export")
    p.set_defaults(func=cmd_export)

    p = add("skill", "install the agent skill into Claude Code / OpenCode / Codex / Antigravity")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    for harness in ("claude", "opencode", "codex", "antigravity"):
        p.add_argument(f"--{harness}", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--project", action="store_true", help="install into the current git project (default)")
    p.add_argument("--global", dest="global_", action="store_true", help="install into user-level skill directories")
    p.add_argument("--dir", help="install into this project directory")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_skill)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (OSError, ValueError, json.JSONDecodeError) as error:  # includes FileNotFoundError, TimeoutError
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # database / YAML errors: report them, never dump a traceback on users
        module = type(error).__module__.split(".")[0]
        if module in {"duckdb", "sqlite3", "ruamel", "sqlglot", "jsonschema"}:
            print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
