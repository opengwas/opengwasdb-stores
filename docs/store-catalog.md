# Store catalog

A snapshot of every Store Release that has actually been **built** (a real
OpenGWASDB store exists on disk, not just an accepted or candidate release
bundle in the registry). Registry release bundles for these live under
`families/<store-family-id>/releases/<family-release-id>/`; the built store
artifacts themselves live outside this repository under
`/data/opengwasdb/`, symlinked for convenience under a single root:

```text
/data/opengwasdb/pilot/
├── eqtlgen-cis-pilot        -> /data/opengwasdb/eqtlgen-cis-pilot
├── finngen-r13              -> /data/opengwasdb/finngen-r13
├── gwas-catalog-eur-hybrid  -> /data/opengwasdb/gwas-catalog-eur-hybrid
└── metabolome-plasma-2023   -> /data/opengwasdb/metabolome-plasma-2023
```

New to these stores? [`query-walkthrough.html`](query-walkthrough.html) is a
hands-on command-line tour of all four -- what a store contains, the
`opengwasdb query-*` commands, and worked examples with real output.

The symlinks are a convenience for browsing only -- every registry
`artifacts.store_uri`/`artifact_root` path still points at the real location
under `/data/opengwasdb/<family>/...`, so nothing else needs to change if the
symlinks move or are removed.

### Every store here is `format_version` 0.1

None of these has been rebuilt since opengwasdb#114 moved the store format to
`1.0`. That matters for accuracy rather than for access: a 0.1 store stores `z`
as `float16`, whose spacing widens with |z|, so its p-values are least precise
exactly where they are steepest -- out by a factor of 1.82 at |z| = 47.8, and
by more above that. A 1.0 store stores `z` as `int16` fixed point at scale
1/1024, uniform to about 1.6% anywhere, with out-of-range values held exactly.

These stores stay **readable** by current builds, and `ogdb info` now reports
each store's encoding explicitly (`encoding: z=float16, se=float16` here). Two
things do change:

- they cannot be **reference-completed** by a build that writes 1.0 -- completion
  writes into its source's arrays, so it would have to stamp a version onto
  arrays that are not in it (opengwasdb ADR 0038 §4). It refuses instead;
- rebuilding is the only way to gain the accuracy, and it picks up every other
  build-time fix since these were built (opengwasdb#83, #106, #107, #109, #115).

Rebuilding them is tracked as opengwasdb#117. (This section added 2026-08-30;
the per-store numbers below are still the 2026-08-19 compilation, since nothing
has been rebuilt.)

Last updated 2026-08-19. Regenerate by re-reading each store's `manifest.json`
and the matching `families/*/releases/*/release.yaml` -- see "How this was
compiled" at the bottom.

## eqtlgen-cis-pilot

10 well-known genes' cis-eQTL summary statistics from the eQTLGen
Consortium's 2019 whole-blood cis-eQTL meta-analysis (31,684 samples), built
via `build_ragged_from_besd`. Proves the BESD -> Ragged pipeline end to end;
**not** the full eQTLGen cis-eQTL catalogue (19,250 genes). Ancestry: EUR
(eQTLGen's donor cohort; no approximation needed).

| Release | Layout | State | Variants | Analyses | Associations | Size | Status |
|---|---|---|---|---|---|---|---|
| `pilot-10` | Ragged | Observed-Only | 86,376 | 10 | 86,373 | 8.5M | built |
| `pilot-10-completed` | Ragged | Reference-Completed | 207,764 (121,388 new) | 10 | 86,373 obs + 106,653 imputed, 14,735 missing | 19M | validated |

LD panel: HGDP+1kGP hg38, EUR. `min_cor=0.7`.

## finngen-r13

20-Analysis onboarding trial from FinnGen R13's public manifest (17
case-control endpoints + all 3 inverse-rank-normalised quantitative
endpoints), built via the native `opengwasdb.finngen-r13` Source Reader
Capability -- no format conversion or liftover (source is already GRCh38).
**Known caveat**: `finngen-r13-HEIGHT_IRN` fails empirical effect-scale
validation (implied SD 0.665 vs declared-standardised); the pilot assessment
recommends NO-GO on the full R13 collection until resolved. Ancestry:
completed against EUR as the nearest available approximation -- FinnGen is a
Finnish-founder population with no FIN-specific panel in this registry;
downstream LD-sensitive use of imputed cells should account for that.

| Release | Layout | State | Variants | Analyses | Cells imputed | Size | Status |
|---|---|---|---|---|---|---|---|
| `r13-pilot-20` | Dense | Observed-Only | 21,230,615 | 20 | -- | 4.0G | built |
| `r13-pilot-20-completed` | Dense | Reference-Completed | 23,792,347 (2,561,732 new) | 20 | 53,293,883 imputed, 177,267 failed, 24,445 off-panel missing | 3.7G | validated |

LD panel: HGDP+1kGP hg38, EUR. `min_cor=0.7`. Dense Rho Matrix (20x20
pairwise genetic correlation) built on the completed release.

## gwas-catalog-eur-hybrid

Hybrid-layout (Dense Component + Ragged Overflow, ADR 0026) releases from the
GWAS-Catalog-SSF `hybrid__European` candidate pool
(`resources/data/derived/store-candidates-analyses.tsv`, 6,035 Analyses
across 1,007 publications), via `build_hybrid_from_vcf_manifest`. Two
sibling pilots split by effect type -- case-control needs no phenotype-SD
estimation (`log_or`, `binary_trait`); quantitative needs real reference-AF
SD estimation (`resources/lib/effect_scale_validation.R`). **Known caveat**
(opengwasdb#86, open): the Dense/Hybrid VCF-manifest builder drops
`sample_size_kind`/`sample_size_scope`/`original_effect_scale` from the
built store and fabricates `ancestry_assignment_method=af_assigned`
regardless of the manifest's real value -- both `validation.yaml`s correctly
report `passed_with_warnings`, not a clean pass. Ancestry: EUR (direct
match; `--ancestry European` was required at the CLI due to
opengwasdb#98's ancestry string-match gap -- worked around via a panel
symlink, not fixed in code).

**eur-hybrid-pilot-10** (case-control, 10 Analyses):

| Release | State | Variants (panel + off-panel) | Overflow assoc. | Dense cells imputed | Size | Status |
|---|---|---|---|---|---|---|
| `eur-hybrid-pilot-10` | Observed-Only | 20,531,956 (9,847,807 + 10,684,149) | 27,810,834 | -- | 4.1G | built |
| `eur-hybrid-pilot-10-completed` | Reference-Completed | 22,809,242 (12,719,981 + 10,089,261) | 25,087,345 | 33,037,123 | 4.5G | validated |

**eur-hybrid-quant-pilot-10** (quantitative, 10 Analyses):

| Release | State | Variants (panel + off-panel) | Overflow assoc. | Dense cells imputed | Size | Status |
|---|---|---|---|---|---|---|
| `eur-hybrid-quant-pilot-10` | Observed-Only | 14,763,864 (9,847,807 + 4,916,057) | 6,562,223 | -- | 3.3G | built |
| `eur-hybrid-quant-pilot-10-completed` | Reference-Completed | 17,291,528 (12,719,981 + 4,571,547) | 5,636,468 | 50,210,079 | 3.8G | validated |

Both completions fold real off-panel observations that crossed onto the
newly-extended Dense panel back in rather than leaving them duplicated or
silently shadowed by a lower-fidelity LD-imputed guess (opengwasdb#99,
fixed): 2,723,489 crossovers folded for the case-control release
(2,687,338 previously imputed), 925,755 for the quantitative release
(902,943 previously imputed). LD panel: HGDP+1kGP hg38, EUR. Dense Rho
Matrices built on both completed releases' Dense Components.

## metabolome-plasma-2023

Full single-ancestry slices of the Chen et al. 2023 plasma metabolome atlas
(PMID 36635386), via the shared `gwas-ssf-ragged` generator and
`build_ragged_from_ssf`. No single encoding gene per Analysis (small-molecule
metabolomics), so no cis window -- sparse region policy retains full
significant-trans windows (±1Mb) plus distance-pruned suggestive-lead
variants only (`sig-trans-1mb_suggestive-leads`). Ancestry-matched 1:1 to a
real HGDP+1kGP hg38 continental panel per release (no approximation).
**Reference-Completed siblings do not exist for this family**:
`complete_ragged_store` only enumerates LD blocks via a per-Analysis cis
window (`trait_chr`/`trait_bp`), which a no-gene-target family has none of,
so completion is a complete no-op (opengwasdb#102, open -- real feature
work, not yet attempted).

| Release | Ancestry | Variants | Analyses | Associations | Size | Status |
|---|---|---|---|---|---|---|
| `2023-chen-full-south-asian` | SAS | 1,336,174 | 971 | 6,653,533 | 145M | built (`passed_with_warnings`) |
| `2023-chen-full-african` | AFR | 7,067,555 | 1,034 | 31,986,793 | 733M | built (`passed_with_warnings`) |
| `2023-chen-full-east-asian` | EAS | 1,176,410 | 1,038 | 7,380,542 | 134M | built (`passed`) |
| `2023-chen-full-european` | EUR | 12,086,426 | 1,400 | 58,053,989 | 1.3G | built (`passed_with_warnings`) |

`passed_with_warnings` on 3 of the 4 reflects opengwasdb#101 (fixed): the
builder now drops any `(analysis, variant)` cell where two source rows
canonicalized to the same variant with genuinely conflicting z-scores,
rather than silently keeping an arbitrary one. Counts: 1 conflicting pair
(south-asian), 22 (african), 108 (european) -- east-asian had none. Also
required opengwasdb#100 (fixed): the builder no longer hard-requires a
`trait_id` manifest column, which this no-gene-target family correctly
omits by this registry's own documented family-shape convention (see
`docs/release-metadata-schema.md`).

## ukb-b

UK Biobank GWAS-VCF analyses from the OpenGWAS `ukb-b` batch (2,514
case-control + quantitative Analyses), built via `build_dense_from_vcf_manifest`
(`opengwasdb.v0.1_dense_vcf_two_pass`). Ancestry: EUR (UKB's cohort; no
approximation needed). **Known caveat** (opengwasdb#14, open): the real
VCF-manifest builder never consumes manifest-supplied
`stored_effect_scale`/`sample_size_kind`/`n_cases`/`n_controls`, so every
built `ukb-b` store -- including the completed release below -- genuinely
lacks these required `analyses.tsv` columns, even though
`dense-observed-vcf-c128-resolved` (candidate, not built) has already
resolved them at the manifest level. This is a pre-existing, family-wide gap,
not something either release below introduces.

| Release | Layout | State | Location | Variants | Analyses | Size | Status |
|---|---|---|---|---|---|---|---|
| `dense-observed-vcf-c128` | Dense | Observed-Only | `/data/opengwasdb/wip/ukb-b-c128.opengwasdb` | 9,847,701 | 2,514 | -- | candidate |
| `dense-observed-vcf-c128-completed-issue34` | Dense | Reference-Completed | `/data/opengwasdb/stores/ukb-b-c128-completed.opengwasdb` | 11,192,757 (1,345,056 new) | 2,514 | 71G | built (`failed`, opengwasdb#14) |
| `dense-observed-vcf-pilot-10` | Dense | Observed-Only | `/data/opengwasdb/wip/` | -- | 10 | -- | built |

`dense-observed-vcf-c128-completed-issue34`: 11,266,156,810 cells imputed,
18,645,778 imputation-failed, 2,766,547,727 left off-panel-missing, against a
UKB-specific rebuilt hg38 EUR LD panel (`/data/opengwasdb/reference/ukb-hg38`,
opengwasdb-stores#34 -- distinct from the shared HGDP+1kGP panel other
families in this catalog use). Built 2026-08-05, elapsed ~7.7h; not formally
registered as a Store Release until 2026-08-19, when its `analyses.tsv` also
needed migrating in place onto the current unified schema (opengwasdb#103,
fixed) after ADR-0034 landed out from under it. `opengwasdb validate` reports
`valid` -- the `failed` status above is entirely the inherited opengwasdb#14
metadata gap, not a completion or migration defect.

## Other real stores not under `/data/opengwasdb/pilot`

Excluded from the consolidation above by user request (scoped to families
registered earlier in this working session); listed here for completeness.
`ukb-b` (above) is also outside that consolidation, but gets its own section
since it now has a fully registered, real, built Reference-Completed release.

- **`pqtl-interval-2018/2018-sun-pilot-100`** -- a real, built Ragged store
  (100-Analysis SomaScan pQTL pilot, PMID 29875488, European), but its
  artifact lives at `/data/opengwasdb/stores/2018-sun-pilot-100/store/...`,
  not at the path its own `build.yaml` `artifacts.store_uri` declares (a
  path-drift issue, same shape as one fixed for `metabolome-plasma-2023`
  this session but not yet fixed here). It also predates ADR-0034 (carries
  a legacy `traits.tsv.gz`), so it cannot be Reference-Completed with
  current tooling (`scripts/migrate_store_to_analyses_tsv.py` explicitly
  excludes Ragged stores, ADR-0030).

## How this was compiled

Every number above was read directly from each store's own `manifest.json`
(`provenance.n_analyses`/`n_variants`/`n_associations`, and
`provenance.completion`/`provenance.hybrid` for completion-specific counts;
note that from `format_version` 1.0 the per-layout `provenance.*.dtype` key is
named `se_dtype`, and the plane encodings are in the manifest's own `encoding`
block rather than inferred from a dtype),
cross-referenced against the matching `families/*/releases/*/release.yaml`
for description/ancestry/status, and store directory sizes via `du -sh`. Not
derived from `build_report.tsv`/`validation.yaml` sidecars alone, since those
reflect the build-time snapshot rather than the artifact's current state.
