# Research behind mAutoOntology

Each design decision traces to a published finding. Numbers are quoted from the sources listed in the
[README](../README.md#research).

## 1. Raw schemas are not enough for enterprise questions

- **Spider 2.0** (ICLR 2025) moved text-to-SQL evaluation to real enterprise workflows (Snowflake,
  BigQuery, dbt, thousands of columns). The best agent framework solved **21.3%** of tasks, against 91.2% on
  Spider 1.0 and 73.0% on BIRD. Enterprise schemas, dialects and business semantics are the hard part.
- **BIRD** (NeurIPS 2023) showed that value understanding and external knowledge drive the gap: ChatGPT
  reached 40.08% execution accuracy against 92.96% for humans.

*Consequence:* the agent must not reason from table and column names alone. mAutoOntology supplies grain,
keys, typed attributes, value domains, default filters, metric definitions and business vocabulary.

## 2. Most errors are schema linking and joins

- **DIN-SQL** (NeurIPS 2023) classified failures and found *schema linking* to be the largest category,
  followed by *JOIN* errors (missing or wrong tables/keys).
- The ACM Computing Surveys review by **Shi et al.** aggregates error analyses across systems and reaches
  the same ordering.

*Consequence:* joins are the most valuable thing to get right. mAutoOntology models relationships
explicitly (cardinality, optionality, role-playing, bridges, self-references, SCD2 as-of predicates),
accepts them only on evidence, and `check-sql` rejects joins that do not follow a declared relationship
(MAO004), cartesian products (MAO005), fan-out (MAO006) and chasm traps (MAO007).

## 3. A semantic representation raises accuracy; checking queries raises it further

- **Sequeda, Allemang & Jacob (2023):** on an enterprise insurance benchmark, zero-shot GPT-4 answered
  **16%** of questions correctly over the SQL schema and **54%** over a knowledge-graph representation.
- **Allemang & Sequeda (2024):** adding an *ontology-based query check* and LLM *repair* reached **72%**
  overall, including 8% "I don't know" answers instead of wrong ones, with a 20% error rate. Most remaining
  failures followed incorrect paths through the model.
- **dbt Labs (2026)** re-ran the same 11 questions (20 runs each): text-to-SQL vs semantic layer was
  32.7% vs 60.5% for 2023 GPT-4 and 64.5% vs 72.7% for current frontier models. After adding three dbt
  models to the project, accuracy rose to 90.0% vs **98.2%** and 84.1% vs **100%**: the quality of the
  model mattered more than the model generation.

*Consequences:*
1. The semantic layer is rich (not just relationships) and lives as code so it can be improved like code.
2. `check-sql` is a first-class step in the answering workflow: the agent repairs until there are no
   errors, using rule ids and hints (fan-out, missing metric filters, currency mixing, semi-additive sums).
3. Refusing is a valid answer. The skill instructs the agent to say "I don't know" when entities are
   unreachable or terms cannot be disambiguated.

## 4. Keys and joins can be discovered automatically, if evidence is combined well

- **Rostin et al. (WebDB 2009)** compute inclusion dependencies and filter spurious ones with cheap features.
- **Auto-BI (PVLDB 2023)** combines *local* join prediction with a *global* schema-graph optimisation and
  reaches F1 > 0.9 on more than 100,000 real BI models.

*Consequence:* mAutoOntology prunes candidates by type and name patterns (including role-playing and
shared-prefix conventions such as TPC-H's `o_custkey`), proves parent uniqueness and child inclusion with
profiling, scores each candidate from evidence only, and selects globally: one parent per column,
symmetric one-to-one pairs resolved to one direction, ambiguous value-only matches sent to human review.

## 5. Dimensional modelling semantics prevent silent wrong answers

Kimball's dimensional modelling vocabulary (grain, additive / semi-additive / non-additive facts, slowly
changing dimensions, fan and chasm traps) names exactly the mistakes LLMs make silently:

| Mistake | Model element | Guardrail |
| :--- | :--- | :--- |
| Summing a balance across days | `additivity: semi_additive`, `non_additive_over` | MAO011 |
| Summing an id or a price | `role: identifier`, `additivity: non_additive` | MAO012, V025 |
| Counting deleted or historical rows | `default_filters`, `history: scd2` | MAO009 |
| Adding EUR to USD | `money.currency_attr`, metric `unit` | MAO010, V065 |
| Double-counting through a 1:N join | relationship direction | MAO006, MAO007 |
| Using "revenue" inconsistently | metric `disambiguation`, glossary `maps_to` | MAO008, V072 |

## 6. Interoperability

Apache Ossie (incubating, formerly Open Semantic Interchange) defines a vendor-neutral semantic model
(datasets, fields, relationships, metrics, AI context). mAutoOntology's native schema uses the same shapes
and MetricFlow vocabulary so exports are direct mappings, and carries what Ossie's core does not model
(governance, evidence, additivity, default filters, history) in `nonportable_extensions`.
