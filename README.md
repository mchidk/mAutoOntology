# mAutoOntology

**Auto-build an evidence-backed semantic layer over any data project, keep it as reviewable YAML in git, and check every SQL answer against it before it reaches a human.**

Works on dbt, Snowflake, Databricks, BigQuery, Postgres, SQL Server/Fabric, Redshift, Trino, DuckDB and SQLite. It ships as an agent skill (Claude Code, OpenCode, Codex, Antigravity) plus a small, deterministic Python toolkit (`mautoontology`).

**Author:** Michael Christensen · **License:** Apache-2.0

---

## Why

**The goal: an agent that answers data questions with the *reviewed* meaning of every term, the *right* joins, and SQL that is checked before anyone sees a number.**

LLMs writing SQL against real warehouses fail in predictable ways, and the research is clear about which:

| Finding | Source |
| :--- | :--- |
| Enterprise text-to-SQL is far from solved: the best agent solved **21.3%** of Spider 2.0's enterprise tasks, against 91.2% on Spider 1.0. | Lei et al., ICLR 2025 [3] |
| Picking the wrong tables/columns (schema linking) and wrong **joins** are the largest error classes in published error analyses. | Pourreza & Rafiei 2023 [4]; Shi et al. 2025 [5] |
| Giving the LLM a knowledge-graph/semantic representation raised GPT-4 accuracy from **16% to 54%** on enterprise insurance questions. | Sequeda, Allemang & Jacob 2023 [1] |
| Adding an **ontology-based query check plus repair** raised it to **72%** (including 8% honest "I don't know"). | Allemang & Sequeda 2024 [2] |
| A semantic layer still helps frontier models: **64.5% → 72.7%**; once the *model* was improved with 3 more dbt models, **90.0% → 98.2%** and **84.1% → 100%**. Modeling quality is what moves the needle. | dbt Labs, 2026 [6] |
| Keys and joins can be discovered automatically and reliably (F1 > 0.9 on 100K+ real BI models) by combining local join evidence with a global view of the schema graph. | Lin, He & Chaudhuri, VLDB 2023 [7]; Rostin et al. 2009 [8] |

mAutoOntology is built around exactly those findings:

| Research says | mAutoOntology does |
| :--- | :--- |
| Schema linking and joins cause most errors [4, 5] | Relationships come **only from evidence** (declared keys, dbt tests, inclusion ratios, proven uniqueness, name patterns), with role-playing, bridges, self-references, SCD2 as-of joins and fan-out direction modelled explicitly. |
| A semantic representation beats raw schema [1, 6] | A rich model, not just relationships: grain, keys, typed attributes (money + currency + minor units, time grains, enums, PII), additivity (incl. semi-additive), default filters (soft delete, current SCD2 row), metrics, segments, glossary with **disambiguation**, verified queries. |
| Query check + repair is what gets you to ~72% [2] | `check-sql` is the flagship: 16 rules with stable ids and concrete repair hints (fan-out, chasm traps, wrong join keys, missing metric filters, currency mixing, semi-additive sums, identifiers aggregated, PII). The agent is not allowed to answer until it passes. |
| Modeling quality decides accuracy [6] | Ontology-as-**code**: YAML in your repo, reviewed in pull requests, refreshed without ever overwriting human edits, validated with 40+ checks. |
| Automatic discovery works with local + global evidence [7, 8] | Pruned candidates → inclusion/uniqueness profiling → evidence scores → global selection (one parent per column, symmetric 1:1 resolved, ambiguity sent to review). |

---

## What makes it different

- **Evidence, not vibes.** Every key and relationship carries its evidence (`declared_fk`, `dbt_test`, `inclusion 700/700`, `uniqueness`, `name_match`). The model never invents numeric confidence.
- **Git-native, zero infrastructure.** No database, no services, no API keys. The semantic layer is a folder of YAML files that diff cleanly and are approved in PRs. The audit trail is `git log`.
- **Humans stay in charge.** Generated elements are fingerprinted; anything a human edits, comments, approves, rejects or deletes is never touched again. Disagreements surface as review questions.
- **Adopt, then overlay.** Existing dbt Semantic Layer metrics are imported as the source of truth, never duplicated.
- **Warehouse-safe.** For live warehouses the toolkit *emits* budgeted, sampled, dialect-correct SQL (every statement verified to be a single SELECT, identifiers safely quoted) and your agent runs it with access it already has. No credentials pass through mAutoOntology. Local SQLite runs with `mode=ro`, an authorizer and timeouts; local DuckDB runs read-only with external file/network access disabled.
- **Privacy by default.** Personal-data columns are detected by name (any case style) and by value shape; their values are never profiled, written to YAML, exported or returned without a warning.
- **Interoperable.** Exports to Apache Ossie (validated against the official schema), dbt MetricFlow (validated with MetricFlow and `dbt parse`), Snowflake semantic views, Databricks metric views and Markdown docs with an ER diagram.

---

## Quick start (5 minutes, no warehouse needed)

```bash
pip install "mautoontology[duckdb] @ git+https://github.com/mchidk/mAutoOntology"   # or: pip install -e ".[dev]" in a checkout
cd examples/trapshop
python build_warehouse.py trapshop.duckdb        # a web-shop warehouse full of modelling traps
```

The `semantics/` folder in the example is a reviewed semantic layer. Try the guardrail:

```bash
mautoontology check-sql --sql "SELECT SUM(o.amount_cents) FROM orders o JOIN order_items i ON i.order_id = o.order_id"
```

```text
ERROR   MAO006 measures of order are aggregated after joining order_item (one order row per many order_item rows); totals are inflated
        fix: Aggregate order_item in a subquery/CTE grouped by the order key first, or use COUNT(DISTINCT ...) for counts.
WARNING MAO008 SUM(o.amount_cents) is ambiguous between metrics (gross_revenue: status <> 'cancelled'; net_revenue: status NOT IN ('cancelled', 'returned'))
        fix: Pick the metric the question means and apply its filter explicitly.
WARNING MAO009 orders read without its default filter 'not_deleted' (soft delete)
        fix: Add: COALESCE(o.is_deleted, FALSE) = FALSE  (omit only if the question explicitly includes these rows)
WARNING MAO010 SUM(o.amount_cents) adds amounts in different currencies (o.currency)
        fix: GROUP BY o.currency, filter to one currency, or convert with an FX rate first.
INFO    MAO015 o.amount_cents is stored in minor units (scale 0.01)
        fix: Multiply by 0.01 to report major units.
FAILED
```

Get exactly the context an agent needs for "revenue by country":

```bash
mautoontology context --terms revenue --entities customer
```

Build a semantic layer from scratch on the same warehouse:

```bash
mkdir /tmp/shop && cd /tmp/shop
mautoontology init --dialect duckdb --name shop
mautoontology extract --duckdb /path/to/trapshop.duckdb
mautoontology profile run --db /path/to/trapshop.duckdb
mautoontology propose        # 13 entities, 14 relationships (incl. role-playing, bridge, SCD2), 4 review questions
mautoontology validate && mautoontology index
```

---

## Use it with your agent

```bash
mautoontology skill install --dir /path/to/your/project      # Claude Code, OpenCode, Codex/Antigravity project skills
mautoontology skill install --global --claude                 # or user-level
```

Then ask your agent things like *"Build the semantic layer for this dbt project"* or *"What was net revenue per market last quarter?"*. The [skill](src/mautoontology/skill/SKILL.md) makes the agent:

1. **Build:** orient → `extract` → `profile` → `propose` → enrich descriptions, metrics, glossary from evidence → `validate` → ask the human ranked review questions → `index` / `export` → open a PR.
2. **Answer:** read `INDEX.md` → `context` → write SQL → `check-sql` and repair until clean → run read-only → answer with metric definitions, filters, SQL and caveats.

The skill forbids inventing tables, joins or metrics, approving its own work, running anything but read-only SELECTs, or copying personal data.

---

## Platform playbooks

| Project | Extract | Profile |
| :--- | :--- | :--- |
| **dbt** | `dbt parse` → `extract --dbt .` (tests, docs, lineage, semantic models; `schema.yml` fallback) | via the warehouse dialect below |
| **Snowflake** | `extract --emit-infoschema --dialect snowflake` → run → `extract --infoschema cols.csv keys.json` (accepts `SHOW IMPORTED KEYS`) | `profile emit --approx` → run → `profile ingest` |
| **Databricks** | same with `--dialect databricks` (Unity Catalog informational constraints included) | same |
| **BigQuery / Postgres / Redshift / Trino** | same with the matching dialect | same |
| **SQL Server / Fabric / Synapse** | `--dialect tsql` (also parses `GO`-separated DDL) | same (exact distinct counts) |
| **DDL files** | `extract --ddl ddl/ --dialect <d>` | from any of the above |
| **Local DuckDB / SQLite** | `extract --duckdb f.duckdb` / `--sqlite f.db` | `profile run --db f.duckdb` |

Sources merge: dbt descriptions + tests + information_schema types in one snapshot. Details: [workflow guide](src/mautoontology/skill/references/workflow.md).

---

## The semantic model (ontology-as-code)

```text
semantics/
├── mautoontology.yaml          # project, dialect, conventions
├── sales/order.yaml            # one entity per file: grain, keys, attributes, relationships, filters
├── sales/metrics.yaml          # simple / ratio / derived metrics with filters, time, disambiguation
├── glossary.yaml               # business words -> metrics/entities/segments
├── verified_queries/*.yaml     # question -> SQL pairs a human confirmed (few-shot + regression tests)
├── INDEX.md                    # compact catalog the agent reads first (generated)
├── _evidence/                  # snapshot, profile, relationship decisions, fingerprints (generated)
└── _proposals/                 # review questions and draft metrics (generated)
```

```yaml
# semantics/sales/order.yaml (excerpt)
schema: mautoontology/v1
kind: entity
id: order
entity_type: fact
source: {dataset: orders}
grain: [order_id]
default_filters:
  - {id: not_deleted, sql: "COALESCE(is_deleted, FALSE) = FALSE", reason: soft_delete}
attributes:
  - {id: amount_cents, role: measure, semantic_type: money, money: {currency_attr: currency}, scale: 0.01, aggregation: sum, additivity: additive}
  - {id: status, role: dimension, values: [shipped, placed, cancelled, returned], values_complete: true}
relationships:
  - id: order__billing_address
    to: address
    role: billing
    on: [{from: billing_address_id, to: address_id}]
    cardinality: many_to_one
    status: approved
    reviewed_by: data-team
    evidence:
      - {kind: inclusion, score: 1.0, detail: 700/700 distinct values found in parent}
      - {kind: uniqueness, ref: addresses(address_id), score: 1.0}
      - {kind: name_match, score: 0.75, detail: role-playing (billing)}
```

The full contract is the JSON Schema [`mautoontology.v1.schema.json`](src/mautoontology/schema/mautoontology.v1.schema.json); the [modeling rules](src/mautoontology/skill/references/modeling-rules.md) explain every field. Other agents and tools can read the YAML directly, use `--json` on every command, or consume an export.

---

## Commands

| Command | What it does |
| :--- | :--- |
| `init` | Create `semantics/mautoontology.yaml`. |
| `extract` | Build the catalog snapshot from `--dbt`, `--ddl`, `--infoschema`, `--duckdb`, `--sqlite` (merged); `--emit-infoschema` prints catalog queries. |
| `profile emit / ingest / run` | Two-stage profiling: emit dialect-correct SQL for your warehouse and ingest results, or run locally. |
| `propose` | Propose/refresh entities, keys, relationships, types and filters; never overwrites human edits; writes review questions. |
| `validate [--drift snapshot.json] [--strict]` | Schema, references, semantics (keys, additivity, currency, synonyms), governance, verified queries, drift. |
| `index` | Write `semantics/INDEX.md`. |
| `context` | Minimal connecting subgraph for entities/metrics/terms: join SQL, metric SQL, filters, warnings, similar verified queries. |
| `check-sql [--strict]` | Check SQL against the semantic layer (16 rules, repair hints). |
| `query --db` | Check, then run read-only SQL on a local SQLite/DuckDB file. |
| `verify [--db]` | Check (and execute) every verified query against its expectations. |
| `export --to ossie\|dbt\|snowflake\|databricks\|markdown\|all` | Interoperability exports. |
| `skill install\|uninstall\|status` | Install the agent skill safely (real-path guards, scoped uninstall, strict flags). |

All commands accept `--json` and `--root`. Exit codes: `0` ok, `1` findings/validation errors, `2` usage error.

---

## Evaluation (honest, reproducible)

| What | Result | How |
| :--- | :--- | :--- |
| Relationship discovery, **TPC-H** (constraints stripped; gold = the 9 specified foreign keys) | precision **1.00**, recall **1.00** | `pytest tests/test_profile_discovery.py` |
| Relationship discovery, **trap warehouse** (14 gold relationships incl. role-playing, bridge, 2 self-references, 1:1, SCD2; decoys) | precision **1.00**, recall **1.00**; decoys rejected | same |
| **check-sql** seeded corpus (70 wrong and right queries, incl. USING/NATURAL joins, CTE fan-out, UNNEST, Snowflake and T-SQL syntax) | every expected rule fires, no forbidden rule fires | `pytest tests/test_checksql.py` |
| Same schema as Snowflake, Databricks, Postgres, T-SQL and BigQuery DDL | identical snapshots | `pytest tests/test_extract.py` |
| dbt export | passes MetricFlow semantic validation and `dbt parse` (dbt-core 1.12.7) | `pytest tests/test_export.py` (with `dbt-core` installed) |
| Ossie export | valid against the official Apache Ossie 0.3.0.dev0 JSON schema | `pytest tests/test_export.py` |

Gold labels come from outside the code (the TPC-H specification and the trap warehouse's construction), never from the heuristics they test. These numbers measure the toolkit, not end-to-end answer accuracy; for that, [docs/evaluation.md](docs/evaluation.md) describes a with/without-semantic-layer agent protocol. The fixtures are small and synthetic: real warehouses will produce more review questions, which is the intended behaviour.

---

## Design decisions

- **YAML files, not a database.** Review, history, blame and rollback come from git; agents and humans read the same files; there is nothing to deploy. The JSON in `_evidence/` holds aggregates only (no raw rows, no personal values).
- **Emit SQL, don't hold credentials.** Agents already have warehouse access (MCP servers, CLIs). Profiling is two-stage and budgeted so cost tracks uncertainty.
- **The model writes meaning, the toolkit writes facts.** Names, descriptions, metrics and glossary come from the agent and the human; keys, relationships and evidence scores come from data and declared metadata.
- **Conservative by default.** Unknown integers are dimensions, not measures; identifiers are never summed; prices are non-additive; balances are semi-additive; a relationship whose parent is not a key is an error.

**Known limits:** query-log join mining and an MCP server are planned for 1.1; Snowflake and Databricks exports follow the documented specs but are not executed against those platforms in CI; MetricFlow has single-column entities, so composite keys and role-playing joins are documented in the export notes rather than silently dropped.

---

## Research

1. J. Sequeda, D. Allemang, B. Jacob. *A Benchmark to Understand the Role of Knowledge Graphs on Large Language Model's Accuracy for Question Answering on Enterprise SQL Databases.* 2023. [arXiv:2311.07509](https://arxiv.org/abs/2311.07509)
2. D. Allemang, J. Sequeda. *Increasing the LLM Accuracy for Question Answering: Ontologies to the Rescue!* 2024. [arXiv:2405.11706](https://arxiv.org/abs/2405.11706)
3. F. Lei et al. *Spider 2.0: Evaluating Language Models on Real-World Enterprise Text-to-SQL Workflows.* ICLR 2025. [arXiv:2411.07763](https://arxiv.org/abs/2411.07763)
4. M. Pourreza, D. Rafiei. *DIN-SQL: Decomposed In-Context Learning of Text-to-SQL with Self-Correction.* NeurIPS 2023. [arXiv:2304.11015](https://arxiv.org/abs/2304.11015)
5. L. Shi et al. *A Survey on Employing Large Language Models for Text-to-SQL Tasks.* ACM Computing Surveys, 2025. [arXiv:2407.15186](https://arxiv.org/abs/2407.15186)
6. J. Ganz, B. Perigaud. *Semantic Layer vs. Text-to-SQL: 2026 Benchmark Update.* dbt Labs, 2026. [docs.getdbt.com](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026)
7. Y. Lin, Y. He, S. Chaudhuri. *Auto-BI: Automatically Build BI-Models Leveraging Local Join Prediction and Global Schema Graph.* PVLDB 16(10), 2023. [arXiv:2306.12515](https://arxiv.org/abs/2306.12515)
8. A. Rostin, O. Albrecht, J. Bauckmann, F. Naumann, U. Leser. *A Machine Learning Approach to Foreign Key Discovery.* WebDB 2009.
9. R. Kimball, M. Ross. *The Data Warehouse Toolkit*, 3rd ed. Wiley, 2013 (grain, additivity, slowly changing dimensions, fan and chasm traps).
10. Apache Ossie (incubating), formerly Open Semantic Interchange. [github.com/apache/ossie](https://github.com/apache/ossie)

More detail on how each finding maps to a feature: [docs/research.md](docs/research.md).

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
pytest                                                          # ~215 tests, ~35 s (TPC-H is generated locally by DuckDB)
pip install dbt-core && pytest tests/test_export.py             # also validates the dbt export with MetricFlow
```

See [CHANGELOG.md](CHANGELOG.md). The vendored Apache Ossie schema is covered by [NOTICE](NOTICE).
