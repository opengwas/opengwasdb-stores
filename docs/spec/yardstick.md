# The yardstick

One fixed query workload, run at the finish of every epic that changes opengwasdb, or ogquery once it exists (opengwas/opengwasdb#487, #506), so that a performance trend
compares like with like (#390). opengwasdb's `benchmarks/` holds one-off
harnesses; the yardstick is the single workload this repository re-runs.
It is the one recurring measurement, of GWAS and LD Stores alike (#504):
opengwasdb's `scripts/ld/bench/latency.py`, `serving.py` and the other bench
scripts stay one-off investigations (HTTP targets, serving memory), and
nothing schedules them.

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

`yardstick-6` adds the LD section of opengwas/opengwasdb-stores#504 (below, "The LD section"): one LD Store and one Genotype Store, measured in the same run and written to the same result file, under its own `ld` key. The GWAS section is `yardstick-4`'s unchanged, Stores, inputs and measures alike, so its timings compare with `yardstick-4` results as they stand. `yardstick-5` was the same section measuring OGL-00001, which has no Partner Rows; it was committed (opengwas/opengwasdb-stores@6d3c354b) but never run for a committed result, and the id is not reused.

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

## The LD section

The workload's `ld` key, and the result's (#504). It measures the two kinds the
LD Store Registry records (docs/spec/ld-store-registry.md), both HGDP+1kGP EUR
k = 10 with Partner Rows, so the kind is the only difference:

| ld_id | Kind | Partner Rows | Status |
|---|---|---|---|
| OGL-00006 | `ld_store` | format 0.2.0, `t_partner` 0.01 | measured |
| OGL-00007 | `genotype_store` | sidecar, `t_partner` 0.01 | measured |

OGL-00006 is the EUR LD Store with its Partner Rows (status `reference`), not
OGL-00001, the served one: OGL-00001 is format 0.1.0 and has none, so it
answers neither `proxies` nor `clump` (owner, 27 November 2026). OGL-00006 is
an overlay whose `near/` and `far/` link into OGL-00001, so it reads the same
pairs.

**Where a Store is.** The workload names an LD Store by `ld_id` and `kind`
alone. `workload.resolve_ld` reads the registry through
`ld_registry.record.valid_records` (`ld_registry.record.LD_ROOT`) and takes
each Store's `location` and `far_bp`; no path to an LD Store is written in this
package. The runner resolves every entry before it measures anything, and
refuses the whole workload (`WorkloadError`) on an `ld_id` with no record, a
`kind` its record contradicts, or a region wider than the Store's `far_bp`.

**Queries**, each through `ogquery.ld.open` (an LD Store read by
`StoreReader`, a Genotype Store by `GenotypeReader`, ogquery's `genotype`
extra); nothing in the yardstick imports the engine for LD
(docs/architecture/engine-imports-baseline.yaml). The sizes are the ticket's
table, in the shapes of opengwasdb's `scripts/ld/bench/latency.py` (ogld#8):

| Measure | What is timed | `result_count` |
|---|---|---|
| `open` | `ogquery.ld.open(location)` then `close()` | — |
| `matrix_10`, `matrix_500` | `matrix(alids)` of 10 or 500 variants | the matrix's variants |
| `region_1mb`, `region_3mb` | `region(chromosome, centre, width / 2)`, 1 or 3 Mb wide | the region's variants |
| `proxies_1`, `proxies_10`, `proxies_100` | `proxies(alids, r2=0.8, kb=10000)` of 1, 10 or 100 variants | proxy rows |
| `clump` | one call is `clump(alids, abs(z), r2=0.01, kb=3000)` of each of opengwasdb#403's 37 Analyses in turn | clump rows over the 37; the record adds `analyses`, `hits` and `leads` |
| `bytes_on_disk` | the Store's size on disk, comparable across kinds: `bytes` is every file a query of the Store can read, links followed and each real file counted once (`ld.bytes_read`), plus, for a Genotype Store, `genotype_bytes`, the pgen, psam and pvar files its provenance pins (`ld_registry.writer.genotype_bytes`). `store_bytes` is the registry's own figure (its records' `measured.store_bytes`, `ogstores.store_size.store_size_on_disk`): a Store's own files, no link followed. The two differ exactly where a Store reads files it does not hold: OGL-00006's `store_bytes` leave out the OGL-00001 `near/` and `far/` and the scratch `partners/` its links read, and its `bytes` count them; a Genotype Store's `bytes` count the pgens beside its sidecar | — |
| `peak_rss` | one `region_3mb` read in a fresh interpreter, sampled as the GWAS `peak_rss` is; `query` names it | the region's variants |

`matrix_*` and `proxies_*` records also carry `absent`: requested ALIDs the
Variant Set does not hold.

**Fixed inputs.** `python -m yardstick.ld pick <location>` draws the lists
once, read-only, and the workload records them. `matrix.variants` is 500
variants at seeded rows (seed 8) of chr22, latency.py's chr22 draw: the same
draw over the same Variant Set picks the same variants, so `matrix_500` is
ogld#8's. `matrix_10` is every 50th of them. Each `proxies.variants` list is a
genome-wide draw of 1, 10 or 100 rows, at the first seed from 8 whose draw has
a proxy (`proxies.seeds`): seed 8's one variant has none, and an empty answer
measures nothing. The regions are centred on chr22:25,000,000, latency.py's
first centre. OGL-00006 and OGL-00007 hold one Variant Set, so both are read
with the same variants.

**The clump's input** is outside every repository:
`$OPENGWASDB_LD_403_ANALYSES`, else the workload's `clump.default`
(`/data/opengwasdb/work/ogld/realdata/403-analyses`), holding `manifest.json`
and one `<store>__<analysis_id>` directory per Analysis it names, each with its
`hits.tsv`. The workload pins it: `clump.sha256` is
`workload.clump_sha256`, over the manifest's and every hits file's sha256 in
name order. `workload.clump_input` refuses, before anything is measured, a
directory that is missing or lacks `manifest.json`; a manifest naming another
number of Analyses than `clump.analyses`; directories that are not exactly the
manifest's set; an Analysis without its hits file, or one whose header is not
`alid`, `z`; and any input whose digest is not the pinned one, so a substitute
is never measured. The preflight and the worker each read the input once
(`workload.read_clump`): the worker hashes the very bytes it parses and refuses
them, `unavailable` with a named reason, if their digest is not the one the
preflight checked, so a file replaced between the two is never measured under
the old checksum. Both check the header with one parser
(`workload.hits_header`); the worker reads each row as exactly those two
fields, never by position alone.

**Not applicable.** Nothing opens a Store before its timed worker, so `open`'s
first read is the Store's first open. After timing it, the worker opens the
Store its queries run on and asks ogquery's own rule,
`ogquery.ld.format.claims_partner_rows`, of that Store's provenance: a
recorded `t_partner` and, for an LD Store, format 0.2.0 or later, or a
Genotype Store's sidecar. A Store that claims no Partner Rows cannot answer
`proxies` or `clump` (ld-store §13), so the worker records those four
`not_applicable` with that reason. A worker that never runs says nothing: when
the budget is spent first, every row is `unavailable`, naming the budget. A
Store whose `location` does not exist, or that ogquery cannot open, has every
measure `unavailable`, never skipped.

**Method.** As the GWAS Stores', through the same loop (`runner._workers`):
each Store's timed measures in one fresh worker (`python -m yardstick.worker
ld`), its peak RSS in another (`ld_rss`), both within the run's budget; one
first read kept apart, then `ld.repetitions` warm reads (`default` 5, `clump`
3, because one clump reads the 37 Analyses' 1.6 million hits). The read-only
guard is the GWAS Stores': the top-level entries of `location` before the
first worker, compared after each. Resolving the registry and
hashing the clump input are part of the run: they count in `duration_s` and
the budget.

## Read-only guarantee

On a release that ships an `index.sqlite`, `query_store` opens it read-write, so a
missing index would be created beside the Store. Every 0.2 release ships one; from
format 0.3.0 it holds only `completion_quality`, so an Observed-Only release, and a
Hybrid's outer release, ship none and the Store Reader never opens one (opengwasdb
ADR 0102; `runner.carries_index` is the engine's `ships_index`, held to it by a test).
Before a Store's first worker starts the runner records the
Store directory's top-level entries and `index.sqlite`'s size and mtime. A Store
that ships an `index.sqlite` and has none is not opened: every measure is `unavailable` ("index.sqlite
missing; refusing to let the query path create it"). Such an `index.sqlite` must still be present before each worker (the timed one and the
RSS one); one that ships none must still have none afterwards (a new `index.sqlite` is a new top-level entry); a violation stops that Store's remaining workers, whose measures are
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
| `ld` | optional, present when the workload has an LD section (#504): `repetitions`; `ogquery`, `{version}` of the reader that ran; `inputs`, what the run read (`matrix` and `proxies` with their seeds and each list's `variants_sha256`, the sha256 of its ALIDs one a line; `region` with its widths; `clump` with its `path`, `source` (`env` or `default`), `analyses`, `hits`, `manifest_sha256` and `sha256`, over the manifest's and every hits file's digest in name order); and `stores`, one object per LD Store, below |

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

An LD Store object has `ld_id`, `kind` (the registry's `ld_registry.record.KINDS`),
`label`, `location`, `status` (`measured`) and `measures`, keyed by the LD
measure names above, each record of the same shape as a GWAS one. The LD
query names are `yardstick.result.LD_TIMED`, not `TIMED`: og-dev-hub draws
every `TIMED` name as a GWAS Store measure, and adding the `ld` key changes
nothing a reader of `stores` reads. It is a new key, not a new shape of an
old one, so the schema stays version 1 and every earlier result stays valid.

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
shape is a refusal, never a crash (#390). An `ld` section is checked the same way, by path (`ld.stores[OGL-00006].clump
lacks result_count`), and more strictly than the GWAS records, whose committed
shape it keeps: the section must name its reader (`ogquery.version`); `inputs` must hold
`matrix` (with `chromosome`, `seed`, `count` and a 64-hex `variants_sha256`),
`region` (with `chromosome`, `centre` and `widths_bp`), `proxies` (with `r2`,
`kb`, and a seed and a 64-hex `variants_sha256` for each list) and `clump`
(with a `path`, `analyses`, `hits` and a 64-hex `sha256`); each Store's
`ld_id` must be an LD Store Registry id, and it needs `label` and
`location`; a measure name outside the LD names is refused; every numeric
field of a measured record must be non-negative, and its sizes and counts
(`store_bytes`, `genotype_bytes`, `absent`, `analyses`, `hits`, `leads`)
whole numbers; and a `not_applicable` must give its `reason`. A Store id that
is not text is refused, never a crash, when the result is checked against a
workload. With a `workload` (`yardstick.workload.load()`) it also
requires every workload Store, with the same status, and every measure that
Store lists, and, when the workload has an LD section, every LD Store, of the
workload's `kind`, with every LD measure (one completeness check serves both). The project site's collector reads results through it. Load a
workload with `yardstick.workload.load()`; `check()` raises `WorkloadError`.
