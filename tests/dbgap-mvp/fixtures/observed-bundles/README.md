# Observed-bundle goldens for the MVP generator (#308)

One directory per observed Store id, holding `release.yaml`, `build.yaml` and
`analyses.tsv` exactly as the generator emitted them from the fixture in
`tests/dbgap-mvp/test_generate_candidate.py` **before** #308 changed it: the base
tree is commit `d4f620f` (ADR 0037's single-id completion not yet applied).

They are the pinned bytes for seam 2 of #308: with a pending reference the
observed bundle must not change at all, so `TestOneBundlePerStore` compares the
current emit against them instead of running the old code on every test run (a
depth-1 CI checkout has no such commit, and a test may not need the network).

Two placeholders stand in for what the fixture's **random temp directory**
decides rather than what the code decides:

| Placeholder | Replaces |
|---|---|
| `{fixture}` | the fixture's temp directory, e.g. in `source_file` and `source_snapshot` |
| `{fixture-digest}` | `output_metadata_content_sha256`, `metadata_receipt_sha256`, `metadata_fingerprint_sha256`, `source_metadata_snapshot_sha256`, `resolution_sha256`, `planned_manifest_sha256` |

Those six digests hash a tree (or a table) keyed by absolute paths, so they
differ per temp directory. Every other byte here came from the base code.

Regenerate them from the base tree when the fixture itself changes:

```sh
pixi run -e dev python3 tests/dbgap-mvp/test_generate_candidate.py --write-goldens
```
