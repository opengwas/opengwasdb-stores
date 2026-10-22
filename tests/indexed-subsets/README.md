# Indexed Variant Subset runtime tests

Focused coverage for stores#209 (epic #214), the parts that are not pure
planning:

- `ogstores.resources` resolves a declaration's `resource_id` under
  `resources/reference-resources/<id>/resource.yaml`, requires the declared
  `genome_build`/`location`/`checksums` to agree, and hashes the maintained ALID
  list (sha256, byte size, row count). Every mismatch is a refusal.
- a run that declares a subset needs an `opengwasdb` providing
  `build-indexed-subset`; the repository's tracked current-format runtime
  provides it (stores#218), and the entry point still refuses with the required
  capability rather than failing mid-build when it is absent. A no-declaration
  run is never gated on the runtime.

Fixtures are tiny and hermetic; the capability probe is exercised against a
stub executable, so no real HapMap3 data or newer opengwasdb is required.

```sh
pixi run python tests/indexed-subsets/test_indexed_subsets.py
```

`test_generator_defaults.py` covers the generator-side half (stores #210): the
shared resolver every Dense and Hybrid `build.yaml` writer calls reads the one
maintained `hapmap3-hg38` resource, a Ragged layout resolves to nothing, and a
reviewed opt-out is an explicit empty list plus a written reason. The R mirror
(`resources/generators/lib/indexed_subsets.R`) has no R writer suite left to
exercise it: FinnGen's retired with its R path (#288) and ukb-b's, the last one,
with its own (#289).

```sh
pixi run python tests/indexed-subsets/test_generator_defaults.py
```
