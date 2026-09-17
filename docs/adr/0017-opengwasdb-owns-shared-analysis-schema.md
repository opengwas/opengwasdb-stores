# OpenGWASDB owns the shared analysis schema

OpenGWASDB owns the shared interpretation-bearing `analyses.tsv` core schema as
part of its store-format contract. The store registry emits Release Manifests
that conform to that core schema, and may add registry-only columns needed to
locate source files, record checksums, carry licence/publication provenance, or
explain inclusion decisions.

This keeps dependency direction one-way: the registry depends on OpenGWASDB's
schema contract, while OpenGWASDB never depends on the registry to interpret a
built store. Built stores carry their own Analysis metadata, and store-only
fields produced during or after the build, such as reference-completion quality,
do not need to exist in accepted Release Manifests.

Manifest Generators therefore resolve authoritative Analytical Metadata before a
build starts, validate the emitted manifest against the OpenGWASDB shared core
schema, and leave reusable source readers, SD estimation, ancestry assignment,
and statistical validation logic in OpenGWASDB.

## Registry-side vocabulary guards

Some shared-core columns carry a vocabulary or an absence rule the registry owns
but OpenGWASDB does not yet check. `assigned_ancestry` is normalised to the
ancestry-mixture Reference Resource's seven super-population codes, so a
free-text Source Ancestry Label is never stored there; and `n_cases`/
`n_controls` must be blank rather than `0` on a non-case-control Analysis. The
pinned `opengwasdb.model.analyses.validate_analyses()` accepts any
`assigned_ancestry` string and a zero count on a `total` Analysis, so
`bundle.check()` enforces both directly (issue #133). This guards
registry-owned semantics rather than duplicating the OpenGWASDB column schema,
and each check should be retired in favour of the upstream validator if
OpenGWASDB ever validates it.
