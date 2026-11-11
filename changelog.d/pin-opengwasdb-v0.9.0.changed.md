- **The `opengwasdb` engine moves from `v0.7.4` (`f2ddb0f`) to `v0.9.0` (`ea81c4a`).** What it brings:
  - **A Ragged Store keeps the regions its bundle retains.** `build-ragged-ssf --regions TABLE` validates a bundle's `sidecars/sparse_regions.tsv` and writes it into the Store as `regions.tsv`, declared in `manifest.json` (#459, opengwas/opengwasdb#430).
  - **Completion happens only inside each Analysis's regions**, read from that file: its cis region, the table's cis row or else the Trait position ± `--cis-window-bp`, and its `significant_trans` rows. A source whose `association_coverage` is not `cis` must hold `regions.tsv`, and neither `complete-ragged` nor `check-observed-view` takes a regions flag any more.
  - **A Ragged Store carrying the table is stamped `format_version` 0.2.1**, and `0.2.0` otherwise. The baseline stays 0.2.0, so `engine-update`'s probe and the completion-parent gate accept both; only a Ragged Store carrying `regions.tsv` reads as 0.2.1. Existing Stores gain the table, and the stamp, only when rebuilt.

  The release guard's `REQUIRED_FORMAT_VERSION` does not move.
