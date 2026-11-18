# Workflow and platform playbooks

All commands accept `--root <project>` (default: current directory) and `--json`. Artifacts live in
`semantics/` inside the project:

| Path | Owner | Commit? |
| :--- | :--- | :--- |
| `semantics/mautoontology.yaml` | human | yes |
| `semantics/<domain>/<entity>.yaml`, `metrics.yaml` | generated + human | yes |
| `semantics/glossary.yaml`, `semantics/verified_queries/*.yaml` | human/agent | yes |
| `semantics/_evidence/` (snapshot, profile, relationships, generated.json) | toolkit | yes (aggregates only, no raw rows) |
| `semantics/_proposals/` (review questions, draft metrics) | toolkit | optional |
| `semantics/INDEX.md` | toolkit | yes |

## 1. Initialise

```bash
mautoontology init --dialect snowflake --name "<project>" --domain sales
```

Dialect names are sqlglot dialects: `snowflake`, `databricks`, `bigquery`, `postgres`, `redshift`, `tsql`
(SQL Server, Fabric, Synapse), `duckdb`, `sqlite`, `trino`, `mysql`, `oracle`, `clickhouse`, ...

## 2. Extract (combine every source you have)

| Project | Command |
| :--- | :--- |
| dbt | `dbt parse` (and ideally `dbt docs generate`), then `mautoontology extract --dbt .` |
| dbt without artifacts | `mautoontology extract --dbt .` (falls back to `schema.yml`; lower fidelity) |
| DDL files | `mautoontology extract --ddl ddl/ --dialect tsql` |
| Live warehouse | `mautoontology extract --emit-infoschema --dialect snowflake --schema ANALYTICS`, run the printed queries with your warehouse tool, save as CSV/JSON, then `mautoontology extract --infoschema columns.csv keys.csv` |
| Local file | `mautoontology extract --duckdb shop.duckdb` / `--sqlite app.db` |

Sources merge: `extract --dbt . --infoschema columns.csv` gives dbt descriptions, tests and lineage plus
authoritative warehouse types. Existing dbt `semantic_models`/`metrics` are adopted, not duplicated.

## 3. Profile

Profiling turns name guesses into evidence: uniqueness, inclusion ratios (share of child values found in a
parent key), null rates and value domains.

**Local SQLite/DuckDB:** `mautoontology profile run --db shop.duckdb` (both stages, read-only, timeouts).

**Warehouse (bring your own executor):**

```bash
mautoontology profile emit --stage 1 --approx            # one stats query per table
# run each query in the plan with your warehouse tool, save {"s1_001": [ {row}, ... ], ...}
mautoontology profile ingest --plan semantics/_evidence/profile_plan_stage1.json --results stage1.json
mautoontology profile emit --stage 2 --budget 200        # targeted: uniqueness, inclusion, value domains
mautoontology profile ingest --plan semantics/_evidence/profile_plan_stage2.json --results stage2.json
```

Cost control: `--approx` uses `APPROX_COUNT_DISTINCT`, `--sample-pct 5` samples stage 1, `--budget` caps
stage 2. Text columns never return min/max, and personal-data columns never return values.

Platform notes:

- **Snowflake:** run plans in a small warehouse; information_schema has no FK columns, so the key query is
  `SHOW PRIMARY KEYS / SHOW IMPORTED KEYS` (output is accepted as-is).
- **Databricks:** Unity Catalog constraints are informational; extract them, but profiling still decides.
- **BigQuery:** set the region in the emitted information_schema query (`region-eu`, ...).
- **SQL Server / Fabric / Synapse:** use `--dialect tsql`; exact distinct counts are emitted.

## 4. Propose, enrich, validate, review

```bash
mautoontology propose            # entities, keys, relationships, types, default filters; safe to re-run
mautoontology validate           # schema, references, semantics, governance, verified queries
mautoontology validate --drift fresh_snapshot.json   # after schema changes
```

Then enrich and review (see modeling-rules.md and review.md), re-run `validate`, and publish:

```bash
mautoontology index
mautoontology export --to all --out semantics_export
mautoontology verify --db shop.duckdb   # verified queries: check-sql + execution + expectations
```

## 5. Answer questions

```bash
mautoontology context --metrics net_revenue --entities customer --json
mautoontology check-sql --file answer.sql            # or --sql "..." or stdin
mautoontology query --db shop.duckdb --file answer.sql   # local only; refuses SQL with check-sql errors
```

`context --question "..."` suggests elements by lexical search when you cannot pick them from INDEX.md;
always confirm the suggestions match the question's meaning.
