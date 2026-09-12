# Release Bundle checks

That `bundle.check()` rejects what the registry owns -- missing keys, a
`store_id` that disagrees with its directory, a malformed id, a declared file
that is absent, a bad checksum, a `derived_from` that does not resolve, an
illegal status transition -- and that it delegates the `analyses.tsv` contract
to `opengwasdb.model.analyses` rather than reimplementing it.

Also that Phase B's columns are asserted present and vocabulary-valid, and
that `check()` never opens a Store.

Empty until `src/ogstores/bundle.py` is implemented.
