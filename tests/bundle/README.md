# Release Bundle checks

That `bundle.check()` rejects what the registry owns -- missing keys, a
`store_id` that disagrees with its directory, a malformed id, a declared file
that is absent, a bad checksum, a `derived_from` that does not resolve, an
illegal status transition -- and that it delegates the `analyses.tsv` contract
to `opengwasdb.model.analyses` rather than reimplementing it.

Also that `assigned_ancestry` is vocabulary-valid wherever it appears (that it
is present is a Manifest Generation rule, `tests/generation-rules`), and
that every Analysis column retired by the pinned upstream model is rejected,
naming both the column and Store Release without copying the upstream list into
this repository. `check()` accumulates all errors without raising and never
opens a Store or inspects a Release Artifact. BESD source provenance is checked for the
bundle-recorded `source_snapshot.besd_prefix` required since `908797f`, without
probing that external prefix. Current Ragged bundles use `post.overview: false`
and pass unchanged under the contract fixed by `6092fee`. Build Recipes pass
without an `artifacts` block, and declaring one is rejected because issue #126
moved the artifact root to deployment configuration.

Covered by `tests/bundle/test_bundle.py`:
- Every committed bundle is discovered dynamically; every registered Store
  Release loads and passes `check()` (the original seven Trial releases
  `OGS-00001`..`OGS-00007`, plus the later accepted and pending bundles).
- Every registry failure class is asserted: missing keys in `release.yaml`/`build.yaml`/`analyses.tsv`, `store_id`/directory mismatch, malformed ID format, absent declared file/sidecar, bad checksum format, unresolvable/self-referential `derived_from`, illegal status transitions.
- Delegation of `analyses.tsv` validation to `opengwasdb.model.analyses`.
- Required Analysis columns are asserted to enforce non-blank values on standard
  and new ragged-BESD releases, with blank-overlay tolerance strictly scoped to
  named legacy releases OGS-00001 and OGS-00002.
- Retired-column rejection follows the pinned upstream
  `RETIRED_ANALYSIS_COLUMNS`, including a patched sentinel that proves there is
  no registry-owned duplicate list.
- OGS-00007's Trait Ontology Mapping is the source-provided EFO term and trait
  label from its tracked target-evidence sidecar, never a gene identifier; the
  seven-member 14-3-3 aggregate `GCST90240123` is identified by its SomaScan
  SeqId. A gene-shaped `trait_ontology_id` and an Ensembl authority name in
  `trait_ontology_label` are rejected.
- Status-aware validation: `candidate` release without `validation.yaml` and with unresolved rows passes.
  Any release Store Release Build will not build may leave required values
  blank, disclosed as a tolerated gap cited `not buildable` (#250).
- Manifest Generation's rules are not the contract's: a bundle missing
  `assigned_ancestry`, or with an incoherent INFO or out-of-range MAF request
  on an excluded row, passes `check()` (#250).
- Buildable statuses (#247): every Release Status has a verdict; only
  `accepted`, `built` and `validated` are buildable. A candidate's refusal
  names its status and how to accept it, a superseded or withdrawn release is
  never offered acceptance, a missing or unknown status is refused, and
  `partition_buildable`/`refuse_unbuildable` name every refused bundle.
- Column Map (#292, ADR 0036): a bundle with an Analysis read through
  `opengwasdb.column-map`, by its own row or inherited from the Build Recipe,
  binds `build.yaml` `column_map: {name, sha256, file}` and carries
  `column_maps/<file>`. The checks refuse:
  - a missing binding or file;
  - a checksum mismatch;
  - a map nothing reads through;
  - an unbound carried file;
  - a linked or non-directory `column_maps`, or a linked map;
  - a malformed binding;
  - `column-map` in either `options` block.

  Every defect is reported in one pass. A built-in (`gwas-ssf`, `finngen-r13`)
  is bound without `file` and reported as a tolerated gap (its sha256 is
  unverifiable before the pin bump). A test fails once opengwasdb ships
  built-in maps, retiring the copied list. A bundle on a dedicated reader needs
  neither binding nor file. The map is checked only as TOML with
  `column_map_version` and a matching `name`; OpenGWASDB validates the rest.
- Artifact-root separation: `artifacts` is neither required nor allowed in a
  Build Recipe; `paths.artifact_root()` owns deployment placement.
- `check()` and `load()` open no Store.
- Artifact paths in `paths.py` are pure functions of `store_id`.

Run from repository root:
    pixi run bundle-check
    pixi run python tests/bundle/test_bundle.py
