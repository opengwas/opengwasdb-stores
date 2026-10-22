# Legacy generators onto the one emitter (R side)

Tests for `resources/generators/lib/emit_registry.R` and each legacy R
generator's `--mode=emit-registry` (#248). A legacy generator keeps its
family-shaped working bundle in its work root,
`<artifact-root>/<output.store_id>/generate`. `emit-registry` projects the
current Release Bundle contract from that working bundle and the config's
`build:` block, and emits it through `src/manifest_generation/emit.py`.

Covered by `tests/emit-registry/run_tests.R`:
- `generation_work_root()` is `<artifact-root>/<id>/generate`, resolved by
  Python under `OPENGWASDB_ARTIFACT_ROOT`; a config without `output.store_id`
  has none, and says so.
- `emit_registry()` emits a working bundle as a candidate:
  - the commands are recorded in order: the working bundle's own log, then
    `also_ran`, then this emit;
  - the version is the generator script's sha256;
  - the label and source snapshot come from the config;
  - the Build Recipe comes from the `build:` block, with the default hm3
    subset on a Dense release;
  - `analyses.tsv` is unchanged and the sidecars are carried over;
  - `summary.yaml` is written, and `validated_at` is the emit's time.
- Emitting over a bundle that has been accepted is refused, and the bundle is
  untouched.
- No legacy generator emits through `--mode=emit-registry` any more, so this
  suite drives the shared helper directly: the R `gwas-ssf-ragged` and
  `gwas-ssf-hybrid` generators retired with #300, FinnGen's R path with #288,
  ukb-b's `opengwas-gwas-vcf-dense` -- this projection's last R driver -- with
  its R path (#289), and the BESD generator with #291, eQTLGen now emitting
  through the shared pipeline (`tests/eqtlgen-pipeline`).

```sh
pixi run test --suite "emit-registry (R)"
```
