# MVP ancestry/META candidate emitter test suite

Test suite for `resources/generators/dbgap-mvp` (issue #221).

## Contracts and invariants covered

1. **Exact, disjoint partition**: the mapping's ancestry buckets are classified
   into EUR/AFR/AMR/EAS/META, the legacy European bucket reconciles as
   EUR + META, and an unknown ancestry or a count drift fails loudly.
2. **META is observed-only**: its emitted bundle has no `derived_from` and no
   `complete` recipe even though an EUR panel exists.
3. **Completion is reference-gated**: a qualified reference emits a
   lineage-linked completed child; a `hold` reference emits a recorded plan and
   no buildable bundle.
4. **Acceptance stays pending**: every candidate is `status: candidate`, has no
   `accepted_at`, and carries a `validation.yaml` whose `human_acceptance` is
   `pending` and whose warnings say it is not verified.
5. **Fail closed**: a missing or extra resolution row, and an attempted
   overwrite of an existing bundle, raise rather than emit.
6. Every emitted bundle passes `bundle.check()` (run inside the emitter).

## Running the suite

```sh
python3 tests/dbgap-mvp/test_generate_candidate.py
# or through the repo orchestrator
pixi run test-python
```
