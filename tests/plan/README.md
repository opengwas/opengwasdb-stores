# `plan()` golden argv

The primary Phase A test: `plan(bundle).argv` against a golden list, one file
per Store Release. Seven releases times roughly five steps is about thirty-five
assertions, and **none of them need a fixture Store** -- `plan()` opens no
Store and reads no `analyses.tsv` row, so its output is a string.

This is also the review artifact for a change to the seam: a change to
`plan()` shows up as a visible diff in every affected command line at once.

Every build-phase command is pointed at the **derived build manifest**
(`<artifact-root>/<store_id>/work/analyses.tsv`), not the bundle's audit
`analyses.tsv`. The derived manifest is produced by `ogstores.manifest` and
holds only rows the build is allowed to see, so an `exclude_from_build: true`
audit row never reaches `opengwasdb` (ADR 0025). Reference-Completed
(`complete-*`) commands consume only their parent Store and name no manifest.

Covered by `tests/plan/test_plan.py`:
- `OGS-00001` Ragged BESD observed-only golden test: exact step sequence (`build`, `validate`), argv with `--analyses` overlay (the derived build manifest) and the bundle-recorded `source_snapshot.besd_prefix` BESD source prefix, sibling inputs (`.esi`, `.epi`, `.besd`, derived build manifest), and outputs (`tests/plan/golden/OGS-00001.json`).
- `OGS-00002` Ragged Reference-Completed golden test: exact step sequence (`complete`, `validate`), parent Store input derived solely from `release.yaml` `derived_from` (`OGS-00001`), `--release-id` identity, `--ld-panel` and `--ancestry` options, and outputs (`tests/plan/golden/OGS-00002.json`).
- `OGS-00003` Dense observed-only golden test: exact step sequence (`build`, `top-hits`, `overview`, `validate`), argv, inputs, and outputs (`tests/plan/golden/OGS-00003.json`).
- `OGS-00004` Hybrid observed-only golden test: exact step sequence (`build`, `overview`, `validate`), argv, inputs, and outputs (`tests/plan/golden/OGS-00004.json`).
- `OGS-00005` Hybrid observed-only golden test: exact step sequence (`build`, `overview`, `validate`), argv, inputs, and outputs (`tests/plan/golden/OGS-00005.json`).
- `OGS-00006` Ragged SSF observed-only golden test: exact step sequence (`build`, `top-hits`, `validate`), argv, inputs, and outputs (`tests/plan/golden/OGS-00006.json`).
- `OGS-00007` Ragged SSF observed-only golden test: exact step sequence (`build`, `top-hits`, `validate`), argv, inputs, and outputs (`tests/plan/golden/OGS-00007.json`).
- Reference-Completion planning across all three layouts (`complete-dense`, `complete-hybrid`, `complete-ragged`): parent store resolution from `derived_from` without filesystem I/O or registry lookup at plan time, positionals (`source_path`, `dest_path`), `--release-id` child identity flag, and options passthrough.
- Inline index building policy: all three completion commands (`complete-dense`, `complete-hybrid`, `complete-ragged`) build top-hit indexes inline, so external `top_hits: true` post-steps are rejected with explicit errors.
- Hybrid top-hits policy: top hits are built inline during `build-hybrid` (for both the nested Dense Component and Ragged Overflow), so no post-build top-hits command is planned at the hybrid root.
- Ragged top-hits policy: `build-ragged-top-hits` is planned at the ragged root for SSF when `post.top_hits: true`; BESD builds top-hit indexes inline during `build-ragged-besd` and rejects external top-hits post-steps.
- BESD source resolution is a bundle fact: `plan()` requires a non-empty `source_snapshot.besd_prefix` in `release.yaml` and fails with a clear `ValueError` when it is missing or invalid.
- Negative assertions: no dense-root top-hits command is emitted for hybrid or ragged releases; no rho command is emitted for non-dense layouts (both observed and completed).
- Rejection of invalid post-processing steps: `top_hits: true` on Hybrid, Ragged BESD, and all Reference-Completed layouts; `rho: true` on Hybrid/Ragged (observed and completed); and `overview: true` on Ragged (observed and completed) raise explicit errors. The overview restriction follows the pinned store-format contract: Ragged's closed envelope excludes `overview.html`.
- Pass-through of unknown `build.options`/`complete.options` keys verbatim without interpretation.
- Generic `--reference-panel` and `--ld-panel` option flow from options with zero special handling.
- One Store Release identity: observed builds receive `--store-id <store-id> --release-id <store-id>` without consulting Store Family; completion commands receive the child `--release-id <store-id>` and preserve the source Store identity according to the upstream completion interface.
- Post-step filtering: conditional `top-hits` (dense / ragged SSF observed), `rho` (dense only), `overview` (Dense/Hybrid only), and `validate`.
- Post-processing defaults (issue #128, ADR 0027): `rho: false` and `validate: true` are identical across every Release Bundle, so `plan()` applies them when a Build Recipe omits them and `POST_DEFAULTS` holds only those two keys. A recipe that states either key still wins; `top_hits` and `overview` stay explicit because they vary. The suite asserts all seven committed `build.yaml` files carry only `top_hits`/`overview`, that the four Ragged releases still plan no overview step, and that removing the defaulted keys left every golden argv byte-identical.
- Shared post-step builder mechanism parameterised by planner-specific commands.
- Dispatch table keyed by `(layout, completion_state)` across all six valid pairs, plus command-keyed sub-dispatch table (`RAGGED_BUILD_DISPATCH`) for ragged layouts without branching or if/elif chains in shared code (ADR 0023).
- Derived build manifest seam: `plan()` resolves the `analyses` token (positional for Dense/Hybrid/SSF, `--analyses` flag for BESD) and the step's declared inputs to `<artifact-root>/<store_id>/work/analyses.tsv`, so Snakemake builds the filtered manifest before the builder runs; the bundle's audit `analyses.tsv` never appears in a build argv. Completion commands consume no analyses manifest.
- Artifact-root configuration: the Build Recipe carries no `artifacts` block; `plan()` renders the root its caller resolved through `paths.artifact_root()` (workflow config, environment variable, `ogstores.yaml`, built-in default). A stray `artifacts.root` is ignored, and overriding the root changes the rendered command line (issue #126).
- Verification of every planned step argv against the real pinned `opengwasdb` CLI.
- Pure function execution with tripwire proof for no filesystem or network I/O beyond path construction.

Run from repository root:
    pixi run python tests/plan/test_plan.py
