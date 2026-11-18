---
name: mautoontology
description: Build and use an evidence-backed semantic layer (ontology-as-code) over dbt, Snowflake, Databricks or any SQL project, and check every SQL answer against it before returning results.
---

Read and follow the canonical [mAutoOntology skill](../../../src/mautoontology/skill/SKILL.md) and its references ([workflow](../../../src/mautoontology/skill/references/workflow.md), [modeling rules](../../../src/mautoontology/skill/references/modeling-rules.md), [check-sql rules](../../../src/mautoontology/skill/references/check-sql-rules.md), [review](../../../src/mautoontology/skill/references/review.md)).

Use the `mautoontology` CLI from this checkout (`pip install -e .`); run every SQL answer through `mautoontology check-sql` before returning it.
