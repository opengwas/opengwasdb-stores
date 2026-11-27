# Generated benchmark artifacts

This directory holds generated release-side benchmark artifacts. They are
**generated, never hand-edited**: re-run the command that produced one to
refresh it (CONTRIBUTING).

## HM3 release benchmark report (stores #212, epic #214)

`tools/benchmarks/release-benchmark-report.py` writes:

- `opengwasdb_hm3_release_benchmark.json` — the aggregate artifact: one HM3
  record per registered Store, taken verbatim from opengwasdb's
  `benchmarks._query_shapes.measure_hm3_extract`, plus the seven generic query
  shapes where a per-Store anchor is configured;
- `opengwasdb_hm3_release_benchmark.md` — the rendered report.

The repository's tracked runtime now provides Indexed Variant Subsets
(stores#218), but the report is still run under an explicit opengwasdb checkout
so its git revision and package fingerprint are recorded in the artifact:

```bash
pixi run -e dev python <stores>/tools/benchmarks/release-benchmark-report.py \
  --opengwasdb-checkout /path/to/opengwasdb --all --hm3-only \
  --output docs/benchmark-output/opengwasdb_hm3_release_benchmark.json \
  --report docs/benchmark-output/opengwasdb_hm3_release_benchmark.md
```

While `--all` aborts on OGS-00004 (#518), a rebuilt Store is benchmarked with
`--store <id>` into a report dated in its name, never into the aggregate's
paths. The aggregate stays stale for those Stores until #518 is fixed and `--all` is run
again. The stopgap reports are:

- `opengwasdb_514_canaries_benchmark_2026-11-25.{json,md}`: #514's three 0.3.0 canaries.
- `opengwasdb_532_dense_rho_benchmark_2026-11-26.{json,md}`: the seven Dense Stores that
  gained Rho (OGS-00009, -12, -13, -14, -15, -16, -17), measured at engine e97f07b5.
- `opengwasdb_532_ogs00011_benchmark_2026-11-26.{json,md}`: the Hybrid OGS-00011, measured in
  a later run at the same engine. Its bundle's first Analysis is not in the Store, so its HM3
  anchor is the Store's first, which the report names.
- `opengwasdb_532_ogs00032_benchmark_2026-11-27.{json,md}` and
  `opengwasdb_532_ogs00033_benchmark_2026-11-27.{json,md}`: the rebuilt and completed
  Hybrids OGS-00032 and OGS-00033, one report each.

Each report's Markdown is generated from its JSON with the tool's `render_markdown`; none is
hand-merged. The #532 reports were recorded at load, not on a quiet host: the shapes harness
waits for a 1-minute load below 3 before each timing, which this shared host does not reach
for days, so `--max-start-load 0` (passed through to the harness) records at the current load,
and each Store's `load_average` (1, 5 and 15 minutes, before and after) sits in the JSON and
the report. The loads were 42 to 80. For example, the Dense report:

```bash
pixi run -e dev python tools/benchmarks/release-benchmark-report.py \
  --opengwasdb-checkout /path/to/opengwasdb@e97f07b5 \
  --store OGS-00009 --store OGS-00012 --store OGS-00013 --store OGS-00014 \
  --store OGS-00015 --store OGS-00016 --store OGS-00017 --reps 5 --max-start-load 0 \
  --output docs/benchmark-output/opengwasdb_532_dense_rho_benchmark_2026-11-26.json \
  --report docs/benchmark-output/opengwasdb_532_dense_rho_benchmark_2026-11-26.md
```

Timings taken at load are inflated and noisy; compare them with a quiet-host run only
in kind. The seven shapes' medians are in each Store's `shapes.timings` in the JSON.

HM3 benchmark absence (`missing`, `not_applicable`, `unsupported`) is operational
evidence, not a Store validation failure; see
[`docs/spec/store-release-workflow.md`](../spec/store-release-workflow.md).

## HM3 pipeline proof (stores #213, epic #214)

`tools/benchmarks/hm3-pipeline-proof.py` runs a real-data end-to-end proof that
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

## Observed-view check, OGS-00009 → OGS-00010 (ADR 0037, #302)

`tools/benchmarks/observed-view-check.py` writes
`ogs00009_ogs00010_observed_view_check.json`: the prototype check that every
observed cell of the Observed-Only Store survives reference completion
unchanged. It maps the whole variant axis, then compares decoded Z, SE, EAF and
the imputed flag over seeded random and edge-stratum chunk-rows:

```bash
pixi run python tools/benchmarks/observed-view-check.py \
  --parent OGS-00009 --child OGS-00010 --n-random 1000 --seed 20261021 \
  --out docs/benchmark-output/ogs00009_ogs00010_observed_view_check.json
```

It also writes a per-band `.jsonl` beside `--out`; that file is not tracked.
