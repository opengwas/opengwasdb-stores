# Validation Record shape test suite

Test suite for the committed Validation Records (issue #135), governed by
[ADR 0022](../../docs/adr/0022-flat-opaque-store-ids.md) and
[ADR 0023](../../docs/adr/0023-the-registry-store-seam-is-a-command-line.md).

## Contracts and invariants covered

1. **One format.** Every `stores/OGS-*/validation.yaml` carries the shape
   `ogstores.register` writes (issue #119): `validator`, `build_environment`,
   `observed`, `checks`, `reports`, `warnings`, `errors`.
2. **Live provenance.** No record names the deleted `build-store.py` adapter as
   its validator; every record names `opengwasdb validate`, versioned by the
   `opengwasdb` revision its `build_environment` records.
3. **The three already-registered records are untouched.** OGS-00001..3 keep
   their observed measurements exactly, including the fabricated OGS-00003
   `n_associations` that issue #122 owns. This ticket migrates the container and
   must not make that value look more credible.
4. **Absence is recorded, not invented.** OGS-00004..7 record every measurement
   they do not have as `null`. The only value carried is the release-level
   verdict, as `observed.validate_status`, because it is already recorded as the
   record's own `status`. Measurements that exist in `sidecars/build_report.tsv`
   are deliberately not promoted: `register` never reads that report, and it
   describes a build the Validation Record did not observe.
5. **The verdict the master list publishes.** `observed.validate_status` agrees
   with the record's top-level `status` (issue #124).

The format is defined once in
[`docs/release-metadata-schema.md`](../../docs/release-metadata-schema.md#validationyaml).

## Running the suite

```sh
pixi run -e dev python3 tests/validation-record/test_validation_record.py
# or through the repo orchestrator
pixi run test-python
```
