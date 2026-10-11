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

The tracked pin predates Indexed Variant Subsets (stores#197 owns the format
bump), so the report is run under an explicit opengwasdb checkout, whose git
revision and package fingerprint are recorded in the artifact:

```bash
pixi run -e dev python <stores>/resources/scripts/release-benchmark-report.py \
  --opengwasdb-checkout /path/to/opengwasdb --all --hm3-only \
  --output docs/benchmark-output/opengwasdb_hm3_release_benchmark.json \
  --report docs/benchmark-output/opengwasdb_hm3_release_benchmark.md
```

HM3 benchmark absence (`missing`, `not_applicable`, `unsupported`) is operational
evidence, not a Store validation failure; see
[`docs/spec/store-release-workflow.md`](../spec/store-release-workflow.md).
