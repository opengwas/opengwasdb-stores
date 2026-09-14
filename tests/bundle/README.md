# Release Bundle checks

That `bundle.check()` rejects what the registry owns -- missing keys, a
`store_id` that disagrees with its directory, a malformed id, a declared file
that is absent, a bad checksum, a `derived_from` that does not resolve, an
illegal status transition -- and that it delegates the `analyses.tsv` contract
to `opengwasdb.model.analyses` rather than reimplementing it.

Also that Phase B's columns are asserted present and vocabulary-valid, and
that `check()` never opens a Store.

Covered by `tests/bundle/test_bundle.py`:
- All seven committed bundles (`OGS-00001`..`OGS-00007`) load and pass `check()`.
- Every registry failure class is asserted: missing keys in `release.yaml`/`build.yaml`/`analyses.tsv`, `store_id`/directory mismatch, malformed ID format, absent declared file/sidecar, bad checksum format, unresolvable/self-referential `derived_from`, illegal status transitions.
- Delegation of `analyses.tsv` validation to `opengwasdb.model.analyses`.
- Status-aware validation: `candidate` release without `validation.yaml` and with unresolved rows passes.
- `check()` and `load()` open no Store.
- Artifact paths in `paths.py` are pure functions of `store_id`.

Run from repository root:
    pixi run python tests/bundle/test_bundle.py
