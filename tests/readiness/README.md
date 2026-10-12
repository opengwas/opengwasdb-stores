# Store Release readiness reconciliation test suite

Test suite for `ogstores.readiness` (issue #217), which reconciles registered
Release Bundle membership, lifecycle and published evidence.

## Contracts and invariants covered

1. **Membership is separated, not collapsed**: intended (all manifest rows),
   admitted (not `exclude_from_build`), and excluded counts come from
   `analyses.tsv`; a malformed exclusion value fails loudly.
2. **Explicit ID-set differences**: a release whose published artifact predates
   a later composition change is diffed against a committed historical baseline
   (for example OGS-00011's 3,317-admitted artifact at `26fb1ec`) and reports
   the exact added/removed IDs.
3. **Evidence channels are distinct**: Release Status, source acquisition,
   observed build, Reference Completion, the required format-0.2.0 `by_variant`
   index, the optional HM3 Indexed Variant Subset, Store validation,
   registration, format/runtime, lineage and warnings. `missing` and `unknown`
   are not passes.
4. **Candidate-generation evidence is not Store validation**: a committed
   record whose validator is a generator is reported as a gap in Store
   validation, never as a clean pass.
5. **Incomplete lineage propagates**: a Reference-Completed child of a release
   with `incomplete_membership` is flagged.
6. **One Store seam**: the only way the view reads a published Store is the
   supported `opengwasdb info --format json` command; the reconciler never opens
   a Store file or `manifest.json`.

## Running the suite

```sh
python3 tests/readiness/test_readiness.py
# or through the repo orchestrator
pixi run test-python
```
