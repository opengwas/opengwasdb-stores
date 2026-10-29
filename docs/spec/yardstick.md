# The yardstick

One fixed benchmark, run at the end of every epic, so that a performance trend
compares like with like (#390). opengwasdb's `benchmarks/` holds one-off
harnesses; the yardstick is the single workload this repository re-runs.

Run it with `pixi run yardstick`. It writes
`docs/_yardstick/<UTC start, YYYYMMDDTHHMMSSZ>-<opengwasdb version>.json`. It
is read-only on the Artifact Root: Stores are opened for reading, and the
fixture build and worker files go to a scratch directory under
`$YARDSTICK_TMPDIR`, else `$TMPDIR`, else `/data/opengwasdb/work/yardstick-tmp`
-- never the host's `/tmp`, because everything this project writes goes under
`/data/opengwasdb` (CONTRIBUTING, Worktrees). A run removes the scratch
directory it made.

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
| OGS-00011 | Hybrid | gwas | pending: its live `store.opengwasdb.partial` belongs to the rebuild run (#371) |

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
| `peak_rss` | one `analysis_full` read in a fresh interpreter, RSS sampled every 5 ms from `/proc/self/statm` (the method of opengwasdb's `benchmarks/_rss.py` at the pinned revision, copied into `yardstick.measure` because that module is not importable) |

A fixed query that returns no rows is refused: a measure on an empty result
measures nothing, so it is recorded `unavailable` with its error. Every call is
checked, the first read included, and so is the `peak_rss` read; a failure to
walk the Store (`bytes_on_disk`) is `unavailable` too, never a partial total.

## Read-only guarantee

`query_store` opens `index.sqlite` read-write, so a missing index would be
created beside the Store. Before a Store's first worker starts the runner records the
Store directory's top-level entries and `index.sqlite`'s size and mtime. A Store
with no `index.sqlite` is not opened: every measure is `unavailable` ("index.sqlite
missing; refusing to let the query path create it"). `index.sqlite` must still be present before each worker (the timed one and the
RSS one); a violation stops that Store's remaining workers, whose measures are
`unavailable`. The comparison runs in a `finally`, so it happens whatever a
worker does; a worker that exits non-zero, times out or prints malformed JSON
makes its measures `unavailable` with the error and never aborts the run. After
the workers it compares: a new top-level entry or a changed `index.sqlite` is recorded in the
result's `guard_violations` and the run exits 3.

`fixture_build` times `opengwasdb build-dense-vcf` on a 2-variant GWAS-VCF
(`yardstick.fixture`). There is no Ragged build: the Ragged fixture builders are
in opengwasdb's tests and are not importable from here.

## Method

Each Store runs in its own fresh process; its peak RSS in another. A timed
measure reads once (`first_read_ms`, kept apart), then `reps` warm times; the
median and p95 are over the warm reads. The `ogstores` identity carries the commit as well as the version: an editable
install reports the version it was built from, which is not necessarily the code
that ran (#386), and `dirty` marks a run on an uncommitted tree as one that
cannot be reproduced.
`repetitions.default` is 5 and
`analysis_full` is 3. A run is flagged `veeam_window: true`, never dropped, if
any part of it fell between 00:00 and 11:00 local time (the nightly Veeam
snapshot throttles disk I/O). Each worker and the fixture build
are given the run's remaining 600 s budget as a timeout; one that overruns is
killed with its whole process group (each runs in its own session: SIGTERM, a one-second grace, then SIGKILL, and it is reaped before the run goes on) and its measures are `unavailable` ("timeout after N s"). The result is
still written, and a run over budget exits 2. "Over budget" is decided on the unrounded elapsed time and carried as `over_budget`; `duration_s` is rounded for display.

## Result schema, version 1

A JSON object. A reader refuses a `schema` it does not know; a change of shape is
a new version.

| Field | Meaning |
|---|---|
| `schema` | `1` |
| `workload_id`, `workload_sha256` | the workload that ran |
| `opengwasdb` | `version`, and `revision`: the 40-hex rev `pixi.lock` pins |
| `ogstores` | `version`; `commit`: the 40-hex `HEAD` of the checkout that ran, because an editable install's version can be stale; `dirty`: whether that tree had uncommitted changes |
| `host` | `name`, `cpu_count` |
| `load_average` | `before` and `after`, each exactly three non-negative numbers: the 1, 5 and 15 minute averages |
| `started_utc`, `ended_utc` | `YYYY-MM-DDTHH:MM:SSZ` |
| `duration_s` | wall time of the whole run in seconds, rounded to 0.1, non-negative |
| `over_budget` | boolean: the unrounded wall time exceeded 600 s (a run over budget exits 2) |
| `local_utc_offset_minutes` | the local zone the Veeam test used |
| `veeam_window` | boolean |
| `repetitions` | as in the workload |
| `stores` | one object per workload Store, below |
| `guard_violations` | optional list of `{store_id, problem}`: what the runner found changed beside a Store (empty on a clean run) |
| `fixture_build` | optional: `{status: measured, layout, seconds}` or `{status: unavailable, error}` |

A Store object has `store_id`, `layout`, `coverage` and `status`. A `pending`
Store has a `reason`. A `measured` Store has `measures`, an object keyed by
measure name, each with a `status` of `measured`, `unavailable` (with an
`error`) or `not_applicable`. A measured record holds:

- timed measures: `first_read_ms`, `median_ms`, `p95_ms`, `reps`, and
  `result_count` (rows returned, every call non-empty; required except for `open`), `first_result_count` (rows of the first read; `warm_result_count` on `analysis_hm3`); `analysis_hm3` carries the fields of
  opengwasdb's `measure_hm3_extract` at the pin: `subset` (`hm3`), `detail`,
  `analysis_id`, `warm_result_count` (rows of the first read), and, read from the
  subset's own metadata, `subset_n_variants`, `subset_n_analyses`,
  `subset_requested_count`, `subset_resolved_count`, `subset_absent_count`,
  `subset_reference_assembly` and `subset_input_sha256`; the validator requires
  every one of these, with non-negative integer counts;
- `bytes_on_disk`: `bytes` and `source` (`walk` or `manifest`);
- `peak_rss`: `baseline_mb`, `peak_mb`, `delta_mb`, `result_count`.

No timing, byte or size field is negative.

## Validator

`yardstick.result.validate(result, workload=None)` raises `yardstick.result.ResultError`
on the first problem. It refuses: a wrong `schema`; a Store object without
`store_id`, `layout`, `coverage` or `status`; a timed record other than `open`
without `result_count`; an `analysis_hm3` record lacking any
`measure_hm3_extract` field above; a measured `fixture_build` without `layout`; a revision that is
not 40 lower-case hex characters; a timestamp that is not `YYYY-MM-DDTHH:MM:SSZ`;
a load array that is not exactly three non-negative numbers; a missing `first_result_count`; and any negative timing, byte or size.
Every nested value is checked before it is read: the result object, `opengwasdb`,
`ogstores`, `host`, `load_average`, `fixture_build`, `stores` and each Store,
its `measures` and each measure record must be of the shape the schema names,
and one that is not is a `ResultError` naming its path (`stores[0] must be an
object`) rather than an exception escaping the validator. A value of the wrong
shape is a refusal, never a crash (#390). With a `workload` (`yardstick.workload.load()`) it also
requires every workload Store, with the same status, and every measure that
Store lists. The project site's collector reads results through it. Load a
workload with `yardstick.workload.load()`; `check()` raises `WorkloadError`.
