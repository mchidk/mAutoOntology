# Review loop and answering with provenance

## Running a review

1. Open `semantics/_proposals/review.yaml`. Questions are ranked by impact (missing grain, ambiguous
   relationships, name-matched keys whose data disagree, unknown currencies, conflicts).
2. Add your own findings from enrichment: ambiguous business words, metrics without a clear owner, PII.
3. Ask at most ~10 questions at a time, highest impact first, each answerable in one sentence. Offer a
   recommended answer with its evidence, e.g.:
   > `orders.promo_id` looks like a key of `promos.promo_id`, but 0% of its 15 distinct values exist there.
   > Recommendation: treat it as an external campaign code, not a relationship. Agree?
4. Apply each answer to the YAML: set `status: approved` + `reviewed_by` + `reviewed_at` only for elements
   the human approved; `rejected` or `deprecated` for elements they refused (keep them, so the generator
   does not propose them again); add a `rationale` quoting the decision.
5. Run `mautoontology validate`, then `mautoontology index`, and propose a commit or pull request. The diff
   is the audit trail.

## Answer template

Use this structure for every data answer:

```markdown
**Answer:** <the number or table, with units and currency>

- **Definition:** net_revenue (approved): sum of amount_cents * 0.01 where status not in (cancelled, returned)
- **Scope/filters:** soft-deleted orders excluded; 2024-01-01 to 2024-03-31; grouped by currency
- **Join path:** order -> customer (order__customer, approved)
- **Caveats:** amounts are per currency (no FX conversion); 2% of orders have no customer (orphans)

<details><summary>SQL (passed check-sql)</summary>

    SELECT ...

</details>
```

State plainly when something is unapproved, assumed, or excluded. If the user confirms the result,
offer to add it to `semantics/verified_queries/` with `verified_by` set to the user.

## Refusing well

Refuse (and say what is missing) instead of guessing when the entities are not connected by any
relationship, a business word cannot be resolved, or check-sql errors cannot be fixed from evidence.
Suggest the smallest addition that would make the question answerable (a relationship to confirm, a
metric definition to provide, a column to add).
