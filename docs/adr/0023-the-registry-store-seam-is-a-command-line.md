# The registry/store seam is a command line

The only thing this repository computes on the path from an accepted Release Bundle to a built Store Release is an `opengwasdb` command line. It does not read, rewrite, project, or validate a row of association or Analysis data, and it does not inspect a built store's internals.

`opengwasdb` consumes the registry's canonical `analyses.tsv` directly. A Release Bundle's `analyses.tsv` is passed to a builder as-is; the registry never materialises a derived builder manifest. Where a builder cannot yet consume it, the correct fix is to improve that command's interface in `opengwasdb` — not to add a projection layer here. Gaps are raised as `opengwasdb` issues and the affected release waits.

> Amended by [ADR 0025](0025-registry-filters-excluded-analyses.md): the registry now materialises exactly one derived artifact, `<artifact-root>/<store_id>/work/analyses.tsv`, which drops `exclude_from_build: true` audit rows from its own table. It still does not read, rewrite, project, or validate a row of Analysis data.

`build.yaml` names an `opengwasdb` CLI subcommand, never a Python entry point. The previous `builder.entrypoint` field named an internal `opengwasdb` module and function, which encoded the seam violation in the data model itself and forced typed dispatch, per-layout adapters, and registry-side knowledge of builder signatures. A subcommand name is a supported public interface; a dotted import path is not.

Build parameters are passed through verbatim. `build.options` keys are `opengwasdb` flag names, rendered onto the command line without interpretation. The registry does not know what `--min-cor` means and must not acquire a mirrored copy of `opengwasdb`'s parameter schema, which would drift. The only arguments this repository composes are the ones that are its own facts: store identity, the bundle's `analyses.tsv` path, and artifact paths. An unrecognised flag fails in the CLI's argument parser within milliseconds, before any expensive work begins.

This makes the seam a single pure function, `plan(bundle) -> list[Step]`, mapping a Release Bundle to a sequence of argv lists. That function is the entire adapter layer, and it is testable by comparing argv against golden values with no fixture stores. The master list is rendered from it, so the published build command is derived rather than maintained; what actually ran is a separate fact, recorded per step at execution time and compared against the planned argv before a release is registered.

Validation splits along the same line. This repository validates bundle structure, required files, path existence, checksums, release identity, and lineage — facts about the registry. Source formats, store contents, layout behaviour, query results, and scientific invariants are validated once, in `opengwasdb`, and this repository records the verdict rather than reproducing the check.
