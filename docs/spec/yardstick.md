# The yardstick

One fixed query workload, run at the finish of every epic that changes opengwasdb, or ogquery once it exists (opengwas/opengwasdb#487, #506), so that a performance trend
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
| OGS-00011 | Hybrid | gwas, Reference-Completed, with `hm3` | measured |
| OGS-00027 | Ragged | cis | measured |

`yardstick-4` is the workload of opengwas/opengwasdb-stores#503: one Store per GWAS Store Layout, every query type the engine's facade exposes, and a fixed set of scattered inputs, so that a trend across engine versions compares like with like. It changes what is measured, so every earlier result describes a different workload:

- OGS-00006 is dropped: OGS-00027 is the Ragged representative.
- No Rho Matrix is measured today, because no served Store holds one (it is opt-in, ADR 0025; the 10-Analysis Hybrid pilots OGS-00022 and OGS-00023 had one, and their directories are gone). `rho`, `rho_row` and `rho_matrix` stay in the code and are listed on every Store, so each records `not_applicable`; a Store that has a Rho Matrix joins the workload when one exists, with `rho` set to its ids.
- Every new measure below was added, and the full-Analysis queries stay as they were.
- `BUDGET_S` rises from 600 s to 1800 s, because the new measures add many timed reads; `over_budget` stays.
- The glossary term is **Yardstick** (CONTEXT.md): it records, it is not a gate.

`yardstick-3` measures OGS-00006 as Reference-Completed. On 2026-11-11 it was rebuilt to record its regions (`regions.tsv`, format 0.2.1) and completed only inside them (opengwas/opengwasdb#430, #224): 12,357,678 variants and 59,092,457 associations, 1,038,186 of them imputed. `yardstick-2` measured the Observed-Only Store. The identifiers are unchanged, so the timings stay comparable Store to Store, but a `yardstick-2` result and a `yardstick-3` one describe different contents. The other three Stores' entries are unchanged.

`yardstick-2` measures OGS-00011, which `yardstick-1` listed as pending while
its rebuild and completion ran (#371). It was added on 2026-11-01, once the
completed Store was published (#401). Its PheWAS ALID (row 100,000) is a panel
variant on the Dense axis, and its rsid and position (row 300,000) are an
off-panel variant in the Overflow, so the lookups read one component each. The
other three Stores' entries are unchanged.

Measures (a Store lists the ones that apply to it):

| Measure | What is timed |
|---|---|
| `open` | `query_store(path)` then `close()` |
| `phewas_rsid` | `phewas(rsid)` across all Analyses |
| `phewas_alid` | `phewas(alid)` |
| `phewas_position` | `range_phewas(chromosome, position, position)` |
| `analysis_full` | `analysis(analysis_id)`, every variant of one Analysis |
| `analysis_hm3` | `analysis(analysis_id, indexed_subset="hm3")`, only where the Store has `hm3` (a Dense Store, or a Hybrid's Dense Component); the record shape of opengwasdb's `measure_hm3_extract` |
| `region` | `range_phewas` over a fixed 1 Mb window |
| `region_100kb` | `range_phewas` over a 100 kb window with the same centre as `region` |
| `analyses_table`, `variants_table` | the facade's `analyses_table()` and `variants_table()`: every Analysis row, every variant row (`result_count` is the row count). `opengwasdb info` is a CLI command, not a query, and is not measured |
| `resolve` | `resolve(phewas(rsid))`, the rows of one PheWAS turned into human-readable form |
| `lookup_1x100`, `lookup_100x1`, `lookup_10x100`, `lookup_100x10`, `lookup_100x100` | `lookup(variants, analyses)` of V variants by A Analyses (`lookup_VxA`), from the Store's fixed lists (below) |
| `analysis_full_observed_only` | `analysis(analysis_id, observed_only=True)`, only on a Reference-Completed Store |
| `top_hits` | `top_hits(analysis_id=..., threshold=5e-8)` of one Analysis; `not_applicable` where the workload records no top hits for the Store |
| `range_by_analysis` | the cis query: every association of the Analyses whose trait position lies in a fixed window; Ragged only |
| `rho`, `rho_row`, `rho_matrix` | the three Rho Matrix reads: five Analyses' pairwise `rho`, one Analysis's row, and the full matrix; only where the Store has a Rho Matrix |
| `bytes_on_disk` | apparent bytes of every file under `store.opengwasdb`, from one walk (`du -sb`); the walk of the 62 GB OGS-00009 takes under a second, so the manifest figure is never needed |
| `peak_rss` | one `analysis_full` read in a fresh interpreter, RSS sampled every 5 ms from `/proc/self/statm` (the method of opengwasdb's `benchmarks/_rss.py` at the pinned revision, copied into `yardstick.measure` because that module is not importable) |

**Fixed, scattered inputs.** The workload fixes every input, so every run reads the same variants and Analyses. `tools/yardstick_inputs.py` picks them, read-only, through the facade, and the workload file records the result:

- `lookup.variants` and `lookup.analyses`: 100 of each, at evenly spaced rows of the Store's own variant and Analyses tables, so the variants spread over every chromosome. The smaller shapes take the first variant (1) or every tenth variant of the list (10), so they stay scattered, and the first 1 or 10 Analyses. On a Ragged Store, which holds an Analysis only inside its own window, variants scattered over the whole variant table meet almost no Analysis (`lookup_1x100` and `lookup_100x1` returned nothing on OGS-00027), so its variants are spread the same way over the variants the 100 picked Analyses hold.
- `region_100kb` and `region`: 100 kb and 1 Mb windows around the same centre.
- `top_hits`: one Analysis, threshold 5e-8, its `path`, `index` or `scan`, from the Store's `build.yaml` `post.top_hits`: a Store without the index is answered by a scan of every cell, so the two timings differ in kind. The measure's result carries the same `path`. The row count the call returned is `input_rows.top_hits`, the only place emptiness is read: a count of 0 means the Store has no top hits there, and the measure is `not_applicable`. `workload.check` accepts `path` only as `index` or `scan`.
- `cis` (Ragged only): a 1 Mb window around an Analysis's trait position, holding Analyses.
- `rho`: `false` for a Store without a Rho Matrix, or `{ids, analysis_id}` for one that has it. This declaration, with the layout, is all that decides `not_applicable`; no method is probed for.
- `input_rows`: for each measure that reads an input, keyed by measure name, the row count one read-only call returned when the workload was written. `workload.check` refuses an entry with no count for a measure it lists, and any count of 0 except `top_hits`: an input that returned nothing is wrong.

**Not applicable.** A query a layout or a Store refuses is recorded `not_applicable` with a `reason`, never `unavailable`: `analysis_hm3` and `range_by_analysis` outside the layouts that have them (an Indexed Variant Subset is Dense or a Hybrid's Dense Component; the cis query is Ragged), `top_hits` where the Store records none, and `rho`, `rho_row` and `rho_matrix` on a layout or a Store without a Rho Matrix. A query that fails for any other reason stays `unavailable` with its error.

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
`repetitions.default` is 5, `analysis_full` and `analysis_full_observed_only` are 3, `analyses_table` and `resolve` are 3, and `variants_table` is 1 (every variant row of OGS-00011, 47.9 million, is slow to read). A run was flagged `veeam_window: true`, never dropped, if
any part of it fell between 00:00 and 11:00 local time, when the nightly Veeam
snapshot covered `/data` and throttled disk I/O 8–10× (#390). That window is
history: since 2026-11-02 the backup skips `/data/opengwasdb` (#411), so a run
records `false` and prints no warning, and the flag marks only a result measured
before that date. The field stays in schema 1, and a committed result keeps the
value that was true of it, because a reader must be able to tell a point measured
under throttled I/O from one that was not. Each worker and the fixture build
are given the run's remaining 1800 s budget as a timeout; one that overruns is
killed with its whole process group (each runs in its own session: SIGTERM, a one-second grace, then SIGKILL, and it is reaped before the run goes on) and its measures are `unavailable` ("timeout after N s"). The result is
still written, and a run over budget exits 2. "Over budget" is decided on the unrounded elapsed time and carried as `over_budget`; `duration_s` is rounded for display.

## Result schema, version 1

A JSON object. A reader refuses a `schema` it does not know; a change of shape is
a new version.

| Field | Meaning |
|---|---|
| `schema` | `1` |
| `workload_id`, `workload_sha256` | the workload that ran |
| `opengwasdb` | `version`, and `revision`: the 40-hex commit of the engine that ran (#253: read from the install, not from the lock) |
| `ogstores` | `version`; `commit`: the 40-hex `HEAD` of the checkout that ran, because an editable install's version can be stale; `dirty`: whether that tree had uncommitted changes |
| `host` | `name`, `cpu_count` |
| `load_average` | `before` and `after`, each exactly three non-negative numbers: the 1, 5 and 15 minute averages |
| `started_utc`, `ended_utc` | `YYYY-MM-DDTHH:MM:SSZ` |
| `duration_s` | wall time of the whole run in seconds, rounded to 0.1, non-negative |
| `over_budget` | boolean: the unrounded wall time exceeded 1800 s (a run over budget exits 2) |
| `local_utc_offset_minutes` | the local zone the run started in |
| `veeam_window` | boolean: the run overlapped the nightly Veeam backup window (00:00–11:00 local) that throttled disk I/O until 2026-11-02; a run since records `false`, and a result measured before then keeps the value true of it |
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
- `peak_rss`: `baseline_mb`, `peak_mb`, `delta_mb`, `result_count`;
- `not_applicable`: a `reason` (the validator does not require it; the runner always writes it). New keys are added to the schema, never renamed or removed: og-dev-hub reads it through `yardstick.result`.

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
