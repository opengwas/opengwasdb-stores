# dbGaP MVP test suite (#290)

MVP `phs002453` is the dbGaP Source Provider's pilot collection on the shared
pipeline. These tests sit at the four seams the ticket agreed:

| File | Seam |
|---|---|
| `test_dbgap_metadata.py` | The shared `providers/dbgap/metadata.py`: tar tables of contents, `*.dbGaP.metadata.txt` parsing, sample sizes. Moved here from the acquisition suite when the plumbing became a helper. |
| `test_source_metadata_adapter.py` | The MVP adapter: the acquisition mapping and the deposit's metadata become the canonical Source Metadata Table, with literal expected values; `META` is imprecise. |
| `test_selection_partition.py` | Five disjoint Selections on the adapter's `store_key`, the `expect_rows` bound, and the multi-config partition check. |
| `test_resolution_import.py` | Seeding the Source Collection's Resolution Area from pre-#284 records (the MVP's five accepted record sets). |
| `test_end_to_end.py` | Two MVP-shaped Analyses through `freeze`, `prepare`, `resolve` (the hermetic fake resolver in `tests/phase-b-candidate/fixtures/`), `render` and `emit` into a temporary registry, under 5 s. |

The acquisition's own suite (`tests/dbgap-phs002453/`) still covers extract,
map, convert and the shared Acquisition Manifest writer. The retired #221
generator, partition and pilot have no tests because they no longer exist.

Run from the repository root:

```sh
python3 tests/dbgap-mvp/test_dbgap_metadata.py
python3 tests/dbgap-mvp/test_source_metadata_adapter.py
python3 tests/dbgap-mvp/test_selection_partition.py
python3 tests/dbgap-mvp/test_resolution_import.py
python3 tests/dbgap-mvp/test_end_to_end.py
```
