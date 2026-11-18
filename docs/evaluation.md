# Evaluation

Two kinds of evidence: automated benchmarks of the toolkit (run in CI with `pytest`) and a manual
protocol for end-to-end answer quality with an agent.

## Automated benchmarks

All gold labels come from outside the code under test.

### Relationship discovery

| Dataset | Gold | Result |
| :--- | :--- | :--- |
| TPC-H (DuckDB `dbgen`, scale 0.01, no constraints declared) | the 9 single-column foreign keys in the TPC-H specification | precision 1.00, recall 1.00 |
| Trap warehouse ([examples/trapshop](../examples/trapshop/build_warehouse.py)) | 14 relationships known by construction: role-playing billing/shipping addresses, bridge, two self references, one-to-one shipments, SCD2 history; plus decoys | precision 1.00, recall 1.00, decoys rejected |

Both databases are generated at test time; nothing is downloaded. Reproduce:

```bash
pytest tests/test_profile_discovery.py -q
```

The decoys matter as much as the hits: `orders.promo_id` is named exactly like `promos.promo_id` but only
0% of its values exist there, so it is rejected and raised as a review question instead of becoming a join.

### check-sql

[`tests/fixtures/checksql_corpus.yaml`](../tests/fixtures/checksql_corpus.yaml) holds 70 queries against the
example semantic layer: wrong queries that must trigger specific rules, and correct queries (including
Snowflake `QUALIFY`/`GROUP BY ALL` and T-SQL `TOP`) that must not trigger forbidden rules. Every rule id is
covered. Reproduce: `pytest tests/test_checksql.py -q`.

### Extraction and exports

- The same schema written as Snowflake, Databricks, Postgres, T-SQL and BigQuery DDL produces identical
  snapshots (types, keys, foreign keys, nullability).
- The dbt export passes MetricFlow's semantic validator and `dbt parse` with dbt-core 1.12.7.
- The Ossie export validates against the vendored Apache Ossie 0.3.0.dev0 JSON schema.
- Context SQL generated for a glossary term is executed and compared with a hand-written reference query.

### Safety

Read-only enforcement (parser + SQLite authorizer + DuckDB read-only), timeouts, PII never written as
values, human edits never overwritten, installer real-path guards and strict CLI flags all have tests.

## Manual protocol: answer quality with and without the semantic layer

Automated benchmarks measure the toolkit. Answer quality depends on the agent, so measure it on your own
project with this protocol:

1. Write 20-50 business questions with reviewed answers (SQL + result), covering metrics with filters,
   multi-hop joins, many-to-many, history, currencies and at least 3 unanswerable questions.
2. **Baseline:** give the agent schema access only (information_schema or DDL) and ask each question.
3. **With mAutoOntology:** install the skill, build and review the semantic layer, then ask the same
   questions in a fresh session.
4. Score each answer: correct, wrong, or abstained ("I don't know" with a reason). Count abstentions on
   unanswerable questions as correct.
5. Report accuracy, wrong-answer rate and abstention rate for both runs, plus the number of check-sql
   repairs per answer. Repeat each question at least 3 times; LLM answers vary between runs.

Expect the largest gains on questions involving metric definitions, fan-out joins and default filters,
which matches the published findings in [research.md](research.md).
