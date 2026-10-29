# The yardstick

One fixed benchmark, run at the end of every epic, so that a performance trend
compares like with like (#390). opengwasdb's `benchmarks/` holds one-off
harnesses; the yardstick is the single workload this repository re-runs.

Run it with `pixi run yardstick`. It writes
`docs/_yardstick/<UTC start, YYYYMMDDTHHMMSSZ>-<opengwasdb version>.json`. It
is read-only on the Artifact Root: Stores are opened for reading, and the
fixture build and worker files go under `$TMPDIR`.

## Workload

`resources/yardstick/workload.yaml` names the Stores, the fixed identifiers used
on each, the measures and the repetitions. Changing it changes what every later
result means, so a change bumps `workload_id`, and this section says why.
`workload_sha256` in each result pins the file it ran.

| Store | Layout | Coverage | Status |
|---|---|---|---|
| OGS-00009 | Dense | gwas, Reference-Completed, with `hm3` | measured |
| OGS-00027 | Ragged | cis | measured |
| OGS-00006 | Ragged | signals_only | measured |
| OGS-00011 | Hybrid | full | pending: its live `store.opengwasdb.partial` belongs to the rebuild run (#371) |

Measures (a Store lists the ones that apply to it):

| Measure | What is timed |
|---|---|
| `open` | `query_store(path)` then `close()` |
| `phewas_rsid` | `phewas(rsid)` across all Analyses |
| `phewas_alid` | `phewas(alid)` |
| `phewas_position` | `range_phewas(chromosome, position, position)` |
| `analysis_full` | `analysis(analysis_id)`, every variant of one Analysis |
| `analysis_hm3` | `analysis(analysis_id, indexed_subset="hm3")`, only where the Store has `hm3` (Dense); the record shape of opengwasdb's `measure_hm3_extract` |
| `region` | `range_phewas` over a fixed 1 Mb window |
| `bytes_on_disk` | apparent bytes of every file under `store.opengwasdb`, from one walk (`du -sb`); the walk of the 62 GB OGS-00009 takes under a second, so the manifest figure is never needed |
| `peak_rss` | one `analysis_full` read in a fresh interpreter, RSS sampled every 5 ms from `/proc/self/statm` (the method of `benchmarks/_rss.py`) |

A fixed query that returns no rows is refused: a measure on an empty result
measures nothing, so it is recorded `unavailable` with its error.

`fixture_build` times `opengwasdb build-dense-vcf` on a 2-variant GWAS-VCF
(`yardstick.fixture`). There is no Ragged build: the Ragged fixture builders are
in opengwasdb's tests and are not importable from here.

## Method

Each Store runs in its own fresh process; its peak RSS in another. A timed
measure reads once (`first_read_ms`, kept apart), then `reps` warm times; the
median and p95 are over the warm reads. `repetitions.default` is 5 and
`analysis_full` is 3. A run is flagged `veeam_window: true`, never dropped, if
any part of it fell between 00:00 and 11:00 local time (the nightly Veeam
snapshot throttles disk I/O). A run that exceeds 600 s still writes its result
and exits 2.

## Result schema, version 1

A JSON object. A reader refuses a `schema` it does not know; a change of shape is
a new version.

| Field | Meaning |
|---|---|
| `schema` | `1` |
| `workload_id`, `workload_sha256` | the workload that ran |
| `opengwasdb` | `version`, and `revision`: the 40-hex rev `pixi.lock` pins |
| `ogstores` | `version` |
| `host` | `name`, `cpu_count` |
| `load_average` | `before` and `after`, each `[1, 5, 15]` minute |
| `started_utc`, `ended_utc` | `YYYY-MM-DDTHH:MM:SSZ` |
| `duration_s` | wall time of the whole run, non-negative |
| `local_utc_offset_minutes` | the local zone the Veeam test used |
| `veeam_window` | boolean |
| `repetitions` | as in the workload |
| `stores` | one object per workload Store, below |
| `fixture_build` | optional: `{status: measured, layout, seconds}` or `{status: unavailable, error}` |

A Store object has `store_id`, `layout`, `coverage` and `status`. A `pending`
Store has a `reason`. A `measured` Store has `measures`, an object keyed by
measure name, each with a `status` of `measured`, `unavailable` (with an
`error`) or `not_applicable`. A measured record holds:

- timed measures: `first_read_ms`, `median_ms`, `p95_ms`, `reps`, and
  `result_count` (rows returned; absent for `open`); `analysis_hm3` adds
  `subset` and `analysis_id`;
- `bytes_on_disk`: `bytes` and `source` (`walk` or `manifest`);
- `peak_rss`: `baseline_mb`, `peak_mb`, `delta_mb`, `result_count`.

No timing, byte or size field is negative.

## Validator

`yardstick.result.validate(result, workload=None)` raises `yardstick.result.ResultError`
on the first problem. With a `workload` (`yardstick.workload.load()`) it also
requires every workload Store, with the same status, and every measure that
Store lists. The project site's collector reads results through it. Load a
workload with `yardstick.workload.load()`; `check()` raises `WorkloadError`.
