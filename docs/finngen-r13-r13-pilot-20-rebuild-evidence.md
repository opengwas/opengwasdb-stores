# FinnGen R13 `r13-pilot-20` real workflow rebuild evidence (issue #101)

Status: **DONE** — `finngen-r13/r13-pilot-20` is the first real Store Release
rebuilt end to end through the shared fixed-input workflow, by one command, with
no Store-Family-specific build script involved. Both the observed Store and its
lineage-linked Reference-Completed child were rebuilt at production scale and
are `format_version` 1.0.

This is the evidence record for issue #101; it is written to be posted to the
issue.

## The one command

```bash
pixi run release --configfile families/finngen-r13/releases/r13-pilot-20/build.yaml
```

Nothing else was run by hand to produce the Stores. Both optional branches are
declared in that `build.yaml`: `rho` (built in place on the observed Store
before the overview is regenerated) and `reference_completion` (the
`r13-pilot-20-completed` child, built from the HGDP+1kGP hg38 EUR panel). The
only extra invocation was Snakemake's `--unlock` after the deliberate SIGKILL
interruption below, which is standard cleanup after an unclean kill.

The pinned `opengwasdb` revision is `6ef919e721d5b7368acad591264be50f79b77dfb`
(`build_environment.opengwasdb_commit` in both `validation.yaml` files).

## Result

| | Catalogued (before) | Rebuilt (after) |
|---|---|---|
| Observed `r13-pilot-20` | Dense Observed-Only, **0.1**, 21,230,615 variants, 20 Analyses, 4.0G | Dense Observed-Only, **1.0**, 21,230,615 variants, 20 Analyses, 5.1G |
| Child `r13-pilot-20-completed` | Dense Reference-Completed, **0.1**, 23,792,347 variants, 20 Analyses, 3.7G | Dense Reference-Completed, **1.0**, 23,792,347 variants, 20 Analyses, 4.9G |

The rebuilt observed Store lands as **`built`**, not `validated`, because
`finngen-r13-HEIGHT_IRN` fails effect-scale validation again — the expected
outcome (issue #99 decision 2). The child `validation.yaml` is `passed`.

## Scale evidence

Measured on the **resume** invocation (the observed build + rho + overview +
validate + child registration + completion + child rho + overview + validate),
because it re-ran the expensive build from scratch after the interruption:

- Wall clock: **1:29:42** (5,382 s)
- Peak resident set size: **44,807,644 kB ≈ 42.7 GiB**
  (`/usr/bin/time -v` maximum resident set size over the whole run)
- Observed Store size: **5,340,935,288 bytes** (`build-report.tsv`), 5.1G on
  disk; the catalogued 0.1 Store was 4,107,654,766 bytes
- Child Store size: 4.9G; the catalogued 0.1 child was 3,661,876,217 bytes

Per-phase wall times from the completion records (DAG order):

| Phase | Wall time |
|---|---|
| `build_observed_store` | 4,289.3 s (71 m 29 s) — 32 workers |
| `build_observed_rho` | 91 s |
| `regenerate_observed_overview` | 14 s |
| `validate_observed_release` | 107 s |
| `register_completed_release` | 13 s |
| `complete_store` | 625.3 s (10 m 25 s) — 32 workers, 53,293,883 cells imputed |
| `build_completed_rho` | 89 s |
| `regenerate_completed_overview` | 14 s |
| `validate_completed_release` | 104 s |

`build-report.tsv` records `n_analyses=20`, `n_variants=21,230,615`,
`status=passed`; `completion-report.tsv` records `n_variants=23,792,347`,
`n_imputed=53,293,883`. Both match the catalogued release exactly, so the
scale-up extrapolations in `pilot_report.md` can be refreshed against a
workflow-built Store rather than the retired adapter.

## Acceptance criteria

- **One command rebuilds the Store.** Yes — the command above rebuilt the
  observed Store from the acquired raw files with no other command run by hand.
- **20 Analyses, `opengwasdb validate` passes.** Yes. The build read-back
  asserts the Store carries exactly the 20 `analyses.tsv` Analysis ids;
  `validate_observed_release` records `store: passed`, and the child records
  `store: passed`.
- **Analysis set, per-Analysis metadata, variant axis match the catalogued
  release.** Analysis set identical (20/20). Variant axis identical
  (21,230,615 observed, 23,792,347 child). 80 metadata fields differ, all of
  them `ancestry_prop_*`: the issue #99 AF-mixture fit now reports a small AMR
  component (~0.6%) and a smaller EAS component (~0.77% vs 1.83%), with EUR
  rising from 0.9817 to 0.9862. Assigned Ancestry is still `EUR` with
  `af_assigned` for all 20 Analyses; every other compared field (labels, effect
  scale, SD method, sample-size fields, ontology mapping method) is unchanged.
- **Query probes return finite association statistics.** Yes, for both the
  binary (`finngen-r13-RX_PARACETAMOL_NSAID`, 21,230,615 finite) and
  quantitative (`finngen-r13-BMI_IRN`, 21,228,482 finite) probes — identical to
  the catalogued 0.1 Store.
- **Format 1.0 and declares its encoding.** Both Stores are
  `format_version: 1.0` with
  `encoding: {version: 1, z: {kind: int16_fixed, scale: 1024}, se: {kind: float16}}`.
  Association statistics were compared, not bytes: the median per-variant |Δz|
  is 0, the 99th percentile is 9.8e-4 (the int16 1/1024 quantum), Pearson r is
  0.99999996–0.99999997, and the maximum |Δz| (0.0155, only in the 75 cells
  beyond |z| = 37) is the *old* float16 Store's rounding error at that
  magnitude, which is exactly the accuracy the format move recovers.
- **`HEIGHT_IRN` effect-scale failure recorded; the run does not fail.** Yes:
  `validation.yaml` records `effect_scale: failed`, `sd_estimation: failed`,
  warning `finngen-r13-HEIGHT_IRN: empirical effect-scale status=failed
  (scale_inconsistent (median implied SD=0.663, dispersion=0.002, n=9720452))`,
  and the release lands `status: built` in `release.yaml`.
- **Rho runs on the real Store and the overview is regenerated afterwards.**
  Yes: `data.zarr/rho` exists on both Stores (`method=pleiodb-cml`,
  `n_analyses=20`, `n_variants_used=190,404` observed / `190,939` child,
  `window_bp=15,000`, `z_thresh=1.0`, `min_nulls=500`), the
  `regenerate_*_overview` phases depend on the rho records, and both
  `overview.html` files carry the `data-tab="rho"` Rho tab.
- **The Reference-Completed child is built through the completion branch.**
  Yes: `register_completed_release` wrote the child bundle with
  `lineage.derived_from: r13-pilot-20`, `store_layout: dense-reference-completed`
  and `completion_state: reference-completed`; `complete_store` built a
  *separate* Store at `<artifact-root>/finngen-r13/releases/r13-pilot-20-completed/store.opengwasdb`
  (the observed Store is only read), and the child's own rho, overview and
  validation ran in the same order.
- **Interrupted partway through at real scale and resumed.** See below.
- **A second full run with unchanged inputs is a no-op.** Yes:
  `Nothing to be done (all requested files are present and up to date)`.
- **Build report evidence captured.** See "Scale evidence".

## Interruption and resumption

The build phase (`opengwasdb build-dense-vcf`, 32 workers) was interrupted
partway through its serial Pass 1 union-axis scan — **28 m 23 s** in, well past
the point where the raw inputs had been read — by `SIGTERM` to the process
group. The interrupt left:

- **no `build_observed_store` completion record**;
- no `store.opengwasdb.partial` (Pass 1 writes it only after the axis scan).

The next invocation was the same one command. Snakemake re-scheduled the build
(and everything downstream) and the run completed: the resumed build took
4,289.3 s and produced a Store identical in Analysis set and variant axis to the
intended one. A partial Store therefore cannot masquerade as a completed phase
at real scale: the completion record, not the Store's mtime, is what the DAG
tracks.

## Defects found by the real run

1. **Snakemake deletes a rule's declared outputs before it runs the rule, which
   is not neutral for this workflow's in-place paths.** When
   `build_observed_store` was scheduled, Snakemake removed its declared output
   `store.opengwasdb/manifest.json` — from the *pre-existing* Store — before the
   phase started. The observed Store was then rebuilt anyway (and the backup was
   retained), but this contradicts `workflow/README.md`'s "an interrupted build
   leaves the previous Store untouched" and should be fixed (e.g. track only the
   completion record and treat the Store envelope as a phase side effect, as the
   observed `release.yaml` already is).
   The same deletion means `validate_observed_release` could not merge the
   previous `validation.yaml`: the pre-existing `reader_smoke_test`,
   `selection` and `metadata_resolution` checks and their old report pointers are
   gone from the rebuilt `validation.yaml`. The checks the workflow itself owns
   (`schema`, `files`, `store`, `ancestry`, `effect_scale`, `sd_estimation`) are
   all present and correct.
2. **`register_completed_release` rewrote the child's whole `release.yaml`
   (issue #101 fix).** Fixed in the same change: the phase now merges
   workflow-owned blocks and preserves a curator's `description`/`notes`
   block scalars, `status`, `created_at` and every other curated field, and the
   Snakefile no longer declares the child `release.yaml` as a rule output (the
   deletion above is why the merge could not have seen it otherwise). The child
   bundle's curated Finnish-founder ancestry and `HEIGHT_IRN` caveats survived
   the rebuild.
3. **The child bundle's legacy `build.yaml` is now stale.** It still describes
   the pre-#101 manual completion at
   `.../r13-pilot-20/store-completed.opengwasdb`; the workflow's child Store is
   at `.../r13-pilot-20-completed/store.opengwasdb` and its plan is the observed
   release's `build.yaml`. The file is not read by the workflow.

## Reproduce

Artifacts (external, not in Git, per ADR 0015):

- observed Store: `/data/opengwasdb/finngen-r13/releases/r13-pilot-20/store.opengwasdb`
- child Store: `/data/opengwasdb/finngen-r13/releases/r13-pilot-20-completed/store.opengwasdb`
- pre-rebuild 0.1 Stores and both pre-rebuild bundles (or rollback): retained
  under `/data/opengwasdb/backups/issue-101/`
- captured semantic baseline of the catalogued 0.1 Stores:
  `/data/opengwasdb/backups/issue-101/baseline-catalogued.json`

The small registry artifacts the rebuild wrote back into the repository are the
observed release's `validation.yaml` and `sidecars/`, and the child bundle's
`release.yaml`, `validation.yaml` and `sidecars/`. No Store or raw data is
tracked.
