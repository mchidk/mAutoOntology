# Modeling rules

The schema is `mautoontology/v1` (JSON Schema shipped in the package). Every file has `schema` and `kind`
(`config`, `entity`, `metrics`, `glossary`, `verified_queries`). Ids are `snake_case`.

## Entities (`kind: entity`, one file per entity)

- `source.dataset` is the physical table/view; `source.dbt` the dbt unique id when known.
- `grain` lists the columns that identify one row. State it in words in `grain_description` when it is not
  obvious ("one row per order line"). An entity without a grain is a review question, not a guess.
- `entity_type`: `fact` (measures + foreign keys), `dimension`, `bridge` (many-to-many link), `snapshot`
  (periodic, semi-additive), `history` (SCD2), `event`.
- `history`: `scd2` (with `valid_from`, `valid_to`, `current`) or `snapshot` (`snapshot_at`).
- `default_filters` apply to every query unless the question explicitly includes those rows (soft deletes,
  current SCD2 version, test data). Give each a `reason`.
- `segments` are named, reusable predicates ("nordic customers"). Prefer a segment over repeating a WHERE.

## Attributes

| role | meaning | rules |
| :--- | :--- | :--- |
| `identifier` | keys and references | never `sum`/`avg`; count with `count_distinct` |
| `dimension` | group/filter by | add `values` only when the domain is complete and not personal data |
| `time` | dates/timestamps | `time.grains`, `time.timezone` |
| `measure` | numbers to aggregate | `aggregation` + `additivity` are required in practice |
| `text` | free text | never group by |

- `additivity`: `additive` (sum anywhere), `semi_additive` (balances, stock: never sum across
  `non_additive_over`), `non_additive` (prices, rates, ratios: average or recompute).
- Money: `semantic_type: money` with `money.currency` (fixed ISO code) or `money.currency_attr` (column).
  Minor units (cents) get `scale: 0.01`.
- `pii`: `direct_identifier`, `contact`, `sensitive`, `quasi_identifier`. Keep PII attributes, but never
  list their values.

## Relationships (on the many/child side)

```yaml
relationships:
  - id: order__billing_address
    to: address
    role: billing                 # role-playing: several relationships to the same entity
    on: [{from: billing_address_id, to: address_id}]   # composite keys: several pairs
    cardinality: many_to_one      # or one_to_one; many_to_many uses `through: <bridge entity>`
    optional: false               # true -> LEFT JOIN from child to parent
    as_of: {at: ordered_at}       # temporal join into an SCD2 entity
```

The parent side must be a key of the parent (validation error V044 otherwise). Do not add a relationship
because names match; add it because the toolkit accepted it or the human confirms it, and cite why.

## Metrics (`kind: metrics`)

- `simple`: `entity`, `agg` (`sum`, `avg`, `min`, `max`, `count`, `count_distinct`, `median`), `expr` (SQL
  over the entity's columns, `*` for row count), optional `filter`, `time`, `grains`.
- `ratio`: `numerator`, `denominator` (metric ids). `derived`: `expr` over metric ids plus `inputs`.
- When two metrics share a measure (gross vs net), give both a `disambiguation` that says when to use each,
  and make one the default meaning of the everyday word through synonyms or a glossary term.
- `unit: <currency attribute>` declares "amounts are in the row currency; always group by it".
- Define a metric only from evidence: an existing dbt/BI definition, a verified query, documentation or the
  human. Draft suggestions in `_proposals/metrics.yaml` are starting points, not facts.

## Glossary (`kind: glossary`)

`id`, `definition`, `synonyms`, `maps_to` (`metric:`, `entity:`, `attribute:entity.attr`,
`segment:entity.segment`, `relationship:`), `disambiguation`. One business word must not silently mean two
things: validation reports clashing synonyms (V072).

## Verified queries (`kind: verified_queries`)

Question -> SQL pairs a human confirmed, with `dialect`, `uses`, optional `expected` (`columns`,
`row_count`) and `verified_by`. They are few-shot examples for answering, regression tests (`verify`) and
are exported to Snowflake `verified_queries`.

## Governance on every element

`status` (`draft` -> `proposed` -> `approved`, or `deprecated`/`rejected`), `origin` (`generated`, `human`,
`dbt`, ...), `owner`, `reviewed_by`, `reviewed_at`, `rationale`, `evidence` (kinds: `declared_fk`,
`dbt_test`, `inclusion`, `uniqueness`, `name_match`, `profile`, `verified_query`, `human`, `doc`, ...).

The generator only refreshes elements nobody touched. Anything you or a human edit, approve, reject or
delete is preserved; if the generator later disagrees, `_proposals/review.yaml` lists a conflict.
