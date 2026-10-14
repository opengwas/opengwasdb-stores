# MVP ancestry/META candidate emitter test suite

Test suite for `resources/generators/dbgap-mvp` (issue #221).

## Contracts and invariants covered

1. **Exact, disjoint partition**: the mapping's ancestry buckets are classified
   into EUR/AFR/AMR/EAS/META, the legacy European bucket reconciles as
   EUR + META, and an unknown ancestry or a count drift fails loudly.
2. **META is observed-only**: its emitted bundle has no `derived_from` and no
   `complete` recipe even though an EUR panel exists.
3. **Completion is reference-gated honestly**: a qualified reference is never
   inferred from a `declared_status`, so every completed child is emitted as a
   pending, blocked definition; a missing suggested panel still produces a
   pending definition (no fabricated recipe).
4. **Acceptance stays pending**: every observed Store is `status: accepted` and
   has an `accepted_at` only so the dev release workflow may build it (#247),
   and every bundle carries a `validation.yaml` whose `human_acceptance` is
   `pending` and whose warnings say it is not verified. A completed child whose
   reference is pending/blocked stays `status: candidate` and is refused by
   `partition_buildable`.
5. **Planned metadata cross-check**: the four store manifests under the
   configured metadata root are read (metadata only) and their counts must match
   the mapping partition (combined European = EUR + META); a drift raises.
6. **Tracked definitions**: `write_candidate_definitions` records the intended
   membership/recipe with `in_stores: false`, `owner_ordering_approved: false`,
   planned manifest paths, and pending acceptance and resolution.
7. **Fail closed**: a missing or extra resolution row, a blank or wrong
   source-binding value (`source_analysis_id`/`source_file`/`source_genome_build`),
   a non-finite sample size, a cross-ancestry row, a same-count manifest with a
   swapped ID, a forged config digest, an attempted overwrite, a symlinked
   output path, or an output under the registry `stores/` tree all raise rather
   than emit.
8. **Safe writes**: the shared `safe_write` helper refuses a stale
   `.partial` temp, a symlinked final, an out-of-root target, and a silent
   overwrite.
9. **Provenance**: every emitted `release.yaml` carries a `source_snapshot` with
   the actual metadata receipt/candidate/manifest/resolution sha256 values
   (regular metadata only).
10. Every emitted bundle passes `bundle.check()` (run inside the emitter).

## Running the suite

```sh
python3 tests/dbgap-mvp/test_generate_candidate.py
# or through the repo orchestrator
pixi run test-python
```
