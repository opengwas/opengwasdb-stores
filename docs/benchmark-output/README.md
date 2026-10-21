# Generated benchmark artifacts

This directory holds generated release-side benchmark artifacts. They are
**generated, never hand-edited**: re-run the command that produced one to
refresh it (CONTRIBUTING).

## HM3 release benchmark report (stores #212, epic #214)

`resources/scripts/release-benchmark-report.py` writes:

- `opengwasdb_hm3_release_benchmark.json` — the aggregate artifact: one HM3
  record per registered Store, taken verbatim from opengwasdb's
  `benchmarks._query_shapes.measure_hm3_extract`, plus the seven generic query
  shapes where a per-Store anchor is configured;
- `opengwasdb_hm3_release_benchmark.md` — the rendered report.

The repository's tracked runtime now provides Indexed Variant Subsets
(stores#218), but the report is still run under an explicit opengwasdb checkout
so its git revision and package fingerprint are recorded in the artifact:

```bash
pixi run -e dev python <stores>/resources/scripts/release-benchmark-report.py \
  --opengwasdb-checkout /path/to/opengwasdb --all --hm3-only \
  --output docs/benchmark-output/opengwasdb_hm3_release_benchmark.json \
  --report docs/benchmark-output/opengwasdb_hm3_release_benchmark.md
```

HM3 benchmark absence (`missing`, `not_applicable`, `unsupported`) is operational
evidence, not a Store validation failure; see
[`docs/spec/store-release-workflow.md`](../spec/store-release-workflow.md).

## HM3 pipeline proof (stores #213, epic #214)

`resources/scripts/hm3-pipeline-proof.py` runs a real-data end-to-end proof that
the maintained HapMap3 resource, release planning, bundle migration, overview,
validation and benchmark reporting work together. It writes:

- `opengwasdb_213_hm3_pipeline_proof.json` — the generated proof artifact (store
  and release identity, runtime and stores revisions, the planned Step argv and
  dry-run job list, the disclosed prerequisite-record relocation, subset
  counts/checksums, overview and validation evidence, and headline timings);
- [`docs/hm3-pipeline-proof.md`](../hm3-pipeline-proof.md) — the rendered report.

It runs against a fresh reflink copy of OGS-00009 under a dedicated artifact
root, so the authoritative Store is read-only, and executes the subset as a
planned Step through `workflow/release.py` on the capability runtime
(`opengwasdb` commit `1c2c85d`). The HM3 timing in the artifact is the core
`opengwasdb#272` seam, `benchmarks._query_shapes.measure_hm3_extract`, surfaced
by the stores#212 report.
