# Dense SE codec re-encode — 2026-10-24 to 2026-10-26

This report records the one-time, pre-release in-place re-encode authorised by
[opengwasdb#326](https://github.com/opengwas/opengwasdb/issues/326). It does not
weaken the rule that published Stores are immutable after the project's first
release.

The forward codec fix and lossless staged-swap implementation were merged in
`opengwasdb` PR #330 (`c769a712d31cd7fe64e656aca57a9eff25d34f3f`). The
release-aware command was merged in `opengwasdb-stores` PR #370
(`a757ead98a710b2a84224989156a5c05dd0aad89`) and ran the pinned implementation
revision `5fc07666202d43500d5e48bbc48f1e4c9e34ef0f`.

## Method

Each Store was processed separately with its release lock held, no active
publication or partial-completion marker, temporary space on `/data`, and 16
Zarr codec-pipeline workers. Only `data.zarr/se` and
`data.zarr/se_exception_index` were selected. For each array, the command staged
a replacement on the same filesystem, compared every decoded chunk, and only
then performed the rename swap. Its atomic record is
`<artifact-root>/<store-id>/records/se-reencode.json`.

## Measured results

The byte columns cover the two re-encoded arrays, not the whole Store.

| Store | Finished (UTC) | Bytes before | Bytes after | Bytes saved | Chunks compared | Full decoded equality | `validate` |
|---|---:|---:|---:|---:|---:|---:|---:|
| OGS-00009 | 2026-10-25 | 8,333,951,999 | 5,523,892,775 | 2,810,059,224 | 447,689 | yes | pass |
| OGS-00012 | 2026-10-24 | 21,091,589,895 | 17,200,731,807 | 3,890,858,088 | 554,954 | yes | pass |
| OGS-00013 | 2026-10-25 | 31,778,958,120 | 25,887,822,261 | 5,891,135,859 | 813,433 | yes | pass |
| OGS-00014 | 2026-10-25 | 21,646,108,403 | 17,299,241,242 | 4,346,867,161 | 516,566 | yes | pass |
| OGS-00015 | 2026-10-24 | 1,356,507,820 | 1,043,354,617 | 313,153,203 | 37,360 | yes | pass |
| OGS-00016 | 2026-10-26 | 34,438,167,740 | 27,118,365,372 | 7,319,802,368 | 754,111 | yes | pass |
| OGS-00017 | 2026-10-26 | 31,340,172,327 | 25,296,311,425 | 6,043,860,902 | 948,250 | yes | pass |
| **Total** | | **149,985,456,304** | **119,369,719,499** | **30,615,736,805** | **4,072,363** | **yes** | **pass** |

Every post-run `opengwasdb validate --format json` invocation exited zero and
returned exactly `{"errors": [], "ok": true, "warnings": []}`.

## Observed-view checks

- OGS-00009: passed after the re-encode against the retained Observed-Only
  parent at `replaced/20261023T190459Z/store.opengwasdb`; all mismatch counts
  were zero.
- OGS-00017: passed after the re-encode against the retained Observed-Only
  parent at `replaced/20261024T220316Z/store.opengwasdb`; all mismatch counts
  were zero.
- OGS-00013, OGS-00015 and OGS-00016 are Observed-Only, so this check does not
  apply.
- OGS-00012 and OGS-00014 passed their completion-time observed-view checks,
  but their Observed-Only parents were not retained. The check therefore could
  not be re-run after the re-encode. Their new `se` and `se_exception_index`
  arrays were nevertheless compared exhaustively with the old arrays and were
  decoded-equal, so the values consumed by the already-passing observed-view
  relation are unchanged. This is the only residual evidence limitation.

## Derived outputs

The affected bundles' checked-in Validation Records, `stores.tsv`, and
`STORES.md` do not currently carry Store byte measurements. There was therefore
no size-derived registry value to regenerate. The measured post-run values are
reported above and remain bound to the per-Store operational records.
