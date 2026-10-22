# Store Release readiness reconciliation test suite

Test suite for `ogstores.readiness` (issue #217), which reconciles registered
Release Bundle membership, lifecycle and published evidence.

## Contracts and invariants covered

1. **Membership is separated, not collapsed**: manifest (all `analyses.tsv`
   rows), admitted (not `exclude_from_build`), and excluded counts; a malformed
   exclusion value fails loudly. Manifest is not intended membership: an
   authoritative Source Inventory is reported separately and stays `unknown`
   where none is recorded.
2. **Explicit ID-set differences, two kinds**: a committed-bundle baseline
   difference (for example OGS-00011's 3,317-admitted artifact at `26fb1ec`)
   and a built-ID reconciliation against the published Store's Analysis IDs
   read through the supported core API. The two are never interchanged.
3. **Evidence channels are distinct**: Release Status, source acquisition,
   intended membership, observed build, built membership, Reference Completion,
   the required format-0.2.0 `by_variant` index (not expected on a 0.1.0
   baseline), the optional HM3 Indexed Variant Subset, Store validation,
   registration, format/runtime, lineage and warnings. `missing` and `unknown`
   are not passes.
4. **Candidate-generation evidence is not Store validation**: a committed
   record whose validator is a generator is reported as a gap in Store
   validation, never as a clean pass.
5. **Incomplete lineage propagates**: a Reference-Completed child of a release
   with `incomplete_membership` is flagged, and a built-ID read on an
   incomplete parent keeps its runtime identity.
6. **The interim single-id state is expected, not a mismatch** (issue #307,
   ADR 0037): a release that completes its own Store declares
   `reference_completed` while the Observed-Only Store is what is published, so
   the format/runtime channel passes and the Reference Completion channel
   reports it as declared-and-pending; a two-id child, which publishes only a
   completed Store, still fails on the same difference. Such a release has an
   observed build of its own, so its observed-build channel is not
   `not_applicable`. A ledger `completion_verified: true` beside an
   Observed-Only Store fails: evidence and artifact must agree. The interim
   rule is `bundle.is_completion_pending`, the same predicate the master list's
   pending marker uses.
7. **Supported core seam**: manifest metadata comes from
   `opengwasdb info --format json`; built IDs come from
   `read_analyses(open_store(path).analyses_path)`. A support probe
   (`core_reporting_support`) fails loudly if that public surface changes, and
   the reconciler never constructs `analyses.tsv`, joins a filename, or touches
   a storage array.

8. **Validation Record completeness is its own channel** (issue #326): a
   committed Store Validation Record that records `null` for any of the
   Analysis, variant or association count, `association_coverage` or
   `reference_assembly` is `missing`, and the channel names each null field.
   A release with no Store validation record (none, or candidate-generation
   evidence) is `missing` too.

## Running the suite

```sh
python3 tests/readiness/test_readiness.py
# or through the repo orchestrator
pixi run test-python
```
