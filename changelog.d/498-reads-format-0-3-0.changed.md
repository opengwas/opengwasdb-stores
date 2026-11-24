- **This repository reads a format-0.3.0 Store Release (issue #498, epic opengwasdb#450).**
  The pin moves to the epic's branch (`orch/450-ogquery-store-reader`, `7446e750`) and
  returns to `main` when opengwasdb#480 merges; hosted releases stay 0.2.x until their
  rebuild, and every change below still reads one.
  - `ogstores.audit reconcile` reads a 0.3.0 Hybrid: its Dense Component carries no Variant
    Axis of its own, so `axis.dense_axis_rows` is `null` there; and it checks the ALID
    sidecar through the engine (`alid_sidecar_unsound`) instead of reporting
    `alid_search_index_absent` for the fixed-width pair a 0.3.0 release does not carry.
  - The yardstick measures a 0.3.0 Observed-Only Store, which ships no `index.sqlite`;
    the missing-index refusal now applies to a release the engine says ships one.
  - `tools/benchmarks/observed-view-check.py` maps axes through the engine's ALID index
    rather than the `variant_alid_*` files.
  - The query walkthrough picks TSV columns by name (the positional `cut -f` broke when
    `beta` joined the TSV) and finds an rsID's ALID through the Store Reader.
  - The audit suites build their fixtures in both layouts, 0.2.x and 0.3.0.
