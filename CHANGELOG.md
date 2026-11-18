# Changelog

All notable changes to mAutoOntology.

**Author:** Michael Christensen

## 1.0.0 - 2026-11-18

First public release: a complete redesign around evidence, ontology-as-code and query checking.

### Semantic layer

- `mautoontology/v1` JSON Schema covering:
  - entities with grain, keys, `entity_type` (fact, dimension, bridge, snapshot, history, event), SCD2 and snapshot history, default filters and segments
  - typed attributes: role, semantic type, time grains, enum domains, money with currency and scale, additivity including semi-additive, PII classes
  - relationships: cardinality, optionality, role-playing, composite keys, bridges, self references, SCD2 as-of joins
  - simple, ratio and derived metrics with filters and disambiguation
  - glossary, verified queries, and per-element governance (status, owner, reviewer, evidence)
- YAML files as the source of truth, written round-trip-safe with ruamel.yaml (comments and key order survive).

### Toolkit

- `extract` works from:
  - dbt `manifest.json` and `catalog.json`: tests, docs, lineage, semantic models and metrics, with a `schema.yml` fallback
  - DDL in any sqlglot dialect, including T-SQL `GO` batches and `COMMENT ON`
  - information_schema exports and Snowflake `SHOW ... KEYS` output
  - local DuckDB and SQLite files
- Two-stage `profile`:
  - emits budgeted, sampled, dialect-correct SQL for any warehouse ("bring your own executor") and ingests the results
  - runs locally against SQLite/DuckDB, read-only and with timeouts
  - never reads text min/max or personal values
- `propose` works only from evidence:
  - candidate keys and relationships with inclusion/uniqueness evidence, then global selection
  - semantic typing, SCD2 and soft-delete detection, bridge many-to-many
  - adoption of dbt metrics
  - ranked review questions
  - fingerprint-based merge that never overwrites human edits
- `validate`: 40+ checks covering schema, references, key/cardinality consistency, additivity, currency, PII, synonyms, cyclic metrics, verified queries and drift.
- `check-sql`: 16 rules with stable ids and repair hints, analysed scope by scope with sqlglot (USING/NATURAL joins, composite keys, column lineage through CTEs and derived tables, every UNION branch). They cover fan-out, chasm traps, joins that don't follow a declared relationship, cartesian products, missing metric filters, missing default filters, currency mixing, semi-additive sums, aggregated identifiers, PII output and minor units.
- `context`, `index`, `query`, `verify`, and `export` to Apache Ossie 0.3.0.dev0 (schema-validated), dbt MetricFlow (validated with MetricFlow and `dbt parse`), Snowflake semantic views, Databricks metric views and Markdown with a Mermaid ER diagram.
- `skill install|uninstall|status` for Claude Code, OpenCode, Codex and Antigravity. Safety rules:
  - real-path guards in both directions
  - never writes through linked skill directories
  - scoped uninstall
  - preflight before any write
  - strict flags with abbreviations disabled

### Skill

- Agent workflows for building/refreshing a semantic layer and for answering questions with mandatory `check-sql` repair.
- References: platform playbooks, modeling rules, check-sql repair recipes, review and answer templates.

### Evaluation

- Relationship discovery: precision and recall 1.00 on TPC-H (constraints stripped) and on the trap warehouse (role-playing, bridge, self references, SCD2, decoys).
- 70-query check-sql corpus, five-dialect DDL parity, export validation, review-regression and safety tests (~215 tests on Python 3.11, 3.12 and 3.14).
