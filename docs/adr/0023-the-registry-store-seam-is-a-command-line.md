# The registry/store seam is a command line

The only thing this repository computes on the path from an accepted Release Bundle to a built Store Release is an `opengwasdb` command line. It does not read, rewrite, project, or validate a row of association or Analysis data, and it does not inspect a built store's internals.

`opengwasdb` consumes the registry's canonical `analyses.tsv` directly. A Release Bundle's `analyses.tsv` is passed to a builder as-is; the registry never materialises a derived builder manifest. Where a builder cannot yet consume it, the correct fix is to improve that command's interface in `opengwasdb` — not to add a projection layer here. Gaps are raised as `opengwasdb` issues and the affected release waits.

> Amended by [ADR 0025](0025-registry-filters-excluded-analyses.md): the registry now materialises exactly one derived artifact, `<artifact-root>/<store_id>/work/analyses.tsv`, which drops `exclude_from_build: true` audit rows from its own table. It still does not read, rewrite, project, or validate a row of Analysis data.

`build.yaml` names an `opengwasdb` CLI subcommand, never a Python entry point. The previous `builder.entrypoint` field named an internal `opengwasdb` module and function, which encoded the seam violation in the data model itself and forced typed dispatch, per-layout adapters, and registry-side knowledge of builder signatures. A subcommand name is a supported public interface; a dotted import path is not.

Build parameters are passed through verbatim, apart from the bounded planner exceptions recorded here and in [ADR 0031](0031-pure-planning-runtime-artifact-existence.md). `build.options` keys are `opengwasdb` flag names; other option semantics remain opaque. The registry does not know what `--min-cor` means and must not acquire a mirrored copy of `opengwasdb`'s parameter schema, which would drift. It composes arguments for its own facts: store identity, the bundle's `analyses.tsv` path, and artifact paths; the worker-count amendment below additionally reuses one declared option for validation. An unrecognised flag fails in the CLI's argument parser within milliseconds, before any expensive work begins.

**Amended 17 November 2026: validation worker forwarding (stores#358).** This
records the existing owner-authored acceptance contract in
[stores#358](https://github.com/opengwas/opengwasdb-stores/issues/358), opened
by Gibran Hemani (`explodecomputer`) on **22 October 2026**: the planner passes
the release's declared worker count to validation. It is a bounded exception to
opaque options, implemented by `ogstores.plan.declared_worker_count`, so the
build and its validation reuse one declared execution setting:

- A build-phase `validate` uses `build.options.n-workers`. A completion's
  validation uses `complete.options.n-workers` first and falls back to
  `build.options.n-workers`; this includes `completion-validate` for a release
  that completes its own Store.
- A missing or null option is absent, as in option rendering. If neither
  applicable block declares a count, validation gets no `--n-workers` flag and
  the engine's own default runs. The planner invents no count.
- The first applicable non-null count must be a positive integer. Booleans,
  floats, strings, zero and negative values are refused at planning time;
  an invalid completion count is not replaced by the build's count.
- Build and completion commands still receive their own options verbatim.
  Forwarding this count adds no filesystem access, new worker setting or
  general engine-option schema. The variant-reference path-equality check in
  ADR 0031 remains the other bounded exception.

This makes the seam a single pure function, `plan(bundle) -> list[Step]`, mapping a Release Bundle to a sequence of argv lists. That function is the entire adapter layer, and it is testable by comparing argv against golden values with no fixture stores. The master list is rendered from it, so the published build command is derived rather than maintained; what actually ran is a separate fact, recorded per step at execution time and compared against the planned argv before a release is registered.

Validation splits along the same line. This repository validates bundle structure, required files, path existence, checksums, release identity, and lineage — facts about the registry. Source formats, store contents, layout behaviour, query results, and scientific invariants are validated once, in `opengwasdb`, and this repository records the verdict rather than reproducing the check.
