- **This repository reads a format-0.3.0 Store Release (issue #498, epic opengwasdb#450).**
  Hosted releases stay 0.2.x until their rebuild, and every change below still reads one.
  - `ogstores.audit reconcile` reads a 0.3.0 Hybrid. Its Dense Component carries no Variant
    Axis of its own, so `axis.dense_axis_rows` is `null` there. The ALID sidecar is checked
    through the engine (new inconsistency kind `alid_sidecar_unsound`); what a sidecar does
    not count (`duplicate_alid_groups`, `duplicate_alid_extra_rows`, `distinct_alids`,
    `alid_index.rows_entries`, `.sorted`, `.agrees_with_table`) is `null`, not 0. The 0.2
    report is unchanged. Nothing in this repository, the catalog or the release workflow
    reads those fields (checked: `src`, `tools`, `tests`, `docs`, the receipts under
    `docs/data`), which is why this is `changed`, not `breaking`.
  - The yardstick measures a 0.3.0 Observed-Only Store, which ships no `index.sqlite`, and
    guards the `index.sqlite` the engine says a release ships (`ships_index`), a Hybrid's
    Dense Component's included.
  - `tools/benchmarks/observed-view-check.py` maps axes through the engine's ALID index
    rather than the `variant_alid_*` files.
  - The query walkthrough picks TSV columns by name (the positional `cut -f` broke when
    `beta` joined the TSV) and finds an rsID's ALID through the Store Reader.
  - The audit suites build their fixtures in both layouts, 0.2.x and 0.3.0.
