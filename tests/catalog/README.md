# Analysis Catalog test suite

Test suite for `analysis_catalog` (#323, epic #313), governed by
[ADR 0039](../../docs/adr/0039-analysis-catalog.md) and
[the spec](../../docs/spec/analysis-catalog.md).

There is one seam: `analysis_catalog.build.build_catalog(registry, artifact_root)`.
Tests write fixture Release Bundles into a temporary registry, generate into a
temporary Artifact Root, and assert by SQL on the produced database and on the
files under `catalog/`. Expected values are literals.

Run from the repository root:

```sh
pixi run -e dev test --suite catalog
```
