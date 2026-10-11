# Indexed Variant Subset runtime tests

Focused coverage for stores#209 (epic #214), the parts that are not pure
planning:

- `ogstores.resources` resolves a declaration's `resource_id` under
  `resources/reference-resources/<id>/resource.yaml`, requires the declared
  `genome_build`/`location`/`checksums` to agree, and hashes the maintained ALID
  list (sha256, byte size, row count). Every mismatch is a refusal.
- a run that declares a subset needs an `opengwasdb` providing
  `build-indexed-subset`; the repository pin deliberately predates it until
  #197, so the entry point refuses with the required capability rather than
  failing mid-build. A no-declaration run is never gated on the runtime.

Fixtures are tiny and hermetic; the capability probe is exercised against a
stub executable, so no real HapMap3 data or newer opengwasdb is required.

```sh
pixi run python tests/indexed-subsets/test_indexed_subsets.py
```
