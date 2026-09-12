# GWAS-SSF Ragged Generator

Shared generator for molecular GWAS Catalog harmonised GWAS-SSF releases that
should be stored as ragged observed-only OpenGWASDB stores.

The generator is deliberately split into two fixed-input steps:

```sh
pixi run Rscript resources/generators/gwas-ssf-ragged/generate.R \
  --config=families/pqtl-interval-2018/generators/config.yaml \
  --mode=emit

pixi run Rscript resources/generators/gwas-ssf-ragged/generate.R \
  --config=families/pqtl-interval-2018/generators/config.yaml \
  --mode=filter
```

`emit` freezes the selected analyses and writes a release bundle. `filter`
downloads each full harmonised GWAS-SSF file transiently, writes a filtered
GWAS-SSF-shaped file containing the configured sparse regions, deletes the full
download, and stamps checksums/sizes back into `analyses.tsv`. Each Analysis's
download+filter is independent (own download, own output file, only a shared
read-only `targets` table), so `filter` runs them across a small pool of
forked workers (`parallel::mclapply`, issue #32) instead of one at a time —
configure the pool size with `filter.parallel_workers` in the family config or
`--parallel-workers=N` on the command line; it defaults to 4, a deliberately
conservative number chosen to avoid opening too many concurrent connections to
EBI's public FTP rather than maxing out available cores. Per-analysis
`download_seconds`/`filter_seconds`/`total_seconds` in
`sidecars/filter_summary.tsv` and per-analysis error handling are unchanged by
parallelising the run.

`emit` is the generator-to-build seam: there is no build mode. The Store is
built from the fixed input by the shared workflow (issue #103), whose
`build.command` names the `opengwasdb build-ragged-ssf` CLI and whose validate
phase reads the built Store back for metadata mismatches and association
statistics — the read-back the retired `build-store.py` adapter used to do:

```sh
pixi run release --configfile families/pqtl-interval-2018/releases/2018-sun-pilot-10/build.yaml
```

`refresh-artifacts` updates artifact paths in an existing release bundle after
changing `output.artifact_root` or `output.artifact_subdir`. `refresh-build`
re-writes `build.yaml` from the current config (for example after adding or
changing `reference_resources`/`effect_scale_validation`) without touching
`analyses.tsv`.

Run reference-AF effect-scale validation after `filter` and before the Store is
built (issue #16):

```sh
pixi run Rscript resources/generators/gwas-ssf-ragged/generate.R \
  --config=families/pqtl-interval-2018/generators/config.yaml \
  --mode=effect-scale
```

This uses the shared engine at `resources/lib/effect_scale_validation.R` to
choose an allele-frequency source per Analysis (source AF when usable,
otherwise a configured `reference_resources` fallback keyed by the assigned
ancestry), align alleles conservatively, compute implied phenotype-SD
diagnostics from standard error/N/MAF, and write
`sidecars/sd_estimation.tsv`. It updates `original_sd`/`original_sd_method` in
`analyses.tsv` only for Analyses that needed estimation (not
`declared_standardised` ones, which keep their source declaration and gain QC
evidence instead), and merges empirical `checks.effect_scale`/
`checks.sd_estimation` into `validation.yaml` without discarding the other
checks the workflow's validate phase and this stage separately maintain. A
release only needs this step when its config declares
`effect_scale_validation`; otherwise those checks remain `not_run`, as before.
See `docs/release-metadata-schema.md` for the config and sidecar field
reference, and `tests/effect-scale-validation/` for fixture coverage.

Run AF-based ancestry assignment after `filter` (issues #23, #25), for
releases whose Source Collection provides usable source allele frequencies:

```sh
pixi run python resources/generators/gwas-ssf-ragged/ancestry-assign.py \
  --release-dir=families/<family>/releases/<release>
```

For each Analysis with usable source AF, this builds a canonical `{alid: af}`
map from its retained filtered source rows and calls
`opengwasdb.ancestry.assign_ancestry` against the release's configured
`ancestry_assignment.reference_resource_id` (an ancestry-mixture Reference
Resource, distinct from the single-ancestry `reference_af` resources
effect-scale validation uses), writing `sidecars/ancestry.tsv`. Analyses
without usable source AF are left untouched at `source_trusted_no_af`, per
issue #11's settled trust-vs-validate policy — this stage never assigns
ancestry when there is nothing to validate against. It merges empirical
`checks.ancestry` into `validation.yaml` via the same shared
`resources/lib/release_yaml.py::merge_validation_yaml` helper the workflow's
validate phase introduced, so it never clobbers another
stage's checks. See `docs/release-metadata-schema.md` ("Ancestry assignment
configuration") and `tests/ancestry-assignment/` for fixture coverage.

The sparse policy implemented here is:

- cis windows: every target gene span plus/minus `cis_flank_bp`, retained in
  full. Only applies to Store Families that declare `inputs.analysis_targets`
  (a resolvable single encoding gene per Analysis, e.g. SomaScan proteomics).
- significant trans: non-cis variants with `p_value <= significant_p`, expanded
  plus/minus `trans_flank_bp`, merged per chromosome, retained in full.
- suggestive trans: non-cis and non-significant-trans variants with
  `significant_p < p_value <= suggestive_p`, kept as distance-pruned lead
  variants only.

A Store Family may additionally opt into unconditionally retaining a fixed QC
panel (issue #28/#29/#30), regardless of significance, by declaring a
`kind: qc_panel` entry in `reference_resources` (see
`reference-resources/qc-panel-hg38/`) and setting:

```yaml
filter:
  qc_panel:
    enabled: true
    resource_id: qc-panel-hg38
```

`filter` then keeps any source row whose chromosome/position matches the
panel in addition to whatever `cis`/`significant_trans`/`suggestive_trans`
regions that Analysis already qualifies for, recording matched positions in
`sidecars/sparse_regions.tsv` as `region_kind: qc_panel` (one row per
chromosome actually matched) and a `qc_panel_rows` count in
`sidecars/filter_summary.tsv`. This gives ancestry assignment and
reference-AF effect-scale validation a stable variant backbone independent of
how many significant hits a given Analysis has — useful for low-power
Analyses that otherwise gate out on `low_overlap`/`overlap` for lack of
retained variants, not for lack of genuine ancestry/scale evidence. It is
opt-in and purely additive: Store Families that do not configure
`filter.qc_panel` are unaffected. See `docs/release-metadata-schema.md`
("QC panel configuration") for the full field reference, including the
decision that effect-scale/ancestry stages treat QC-panel and signal-driven
retained rows uniformly rather than preferring one over the other.

A Store Family with no single encoding gene per Analysis (issue #26; for
example small-molecule metabolomics, which has no one gene to draw a cis
window around) simply omits `inputs.analysis_targets` from its config. The
generator then retains only the significant/suggestive regions above with
zero cis rows, does not require `fail_if_target_unresolved`-style target
resolution at all, and never emits the single-gene-target columns
(`trait_id`, `gene_id`, `gene_name`, `trait_chr`, `trait_bp`, `n`, `mhc`,
`target_resolution_method`, `n_target_rows`) — see
`docs/release-metadata-schema.md`'s `analyses.tsv` section and
`tests/no-cis-region-policy/` for fixture coverage. The existing
target-resolving families (pqtl-interval-2018) are unaffected; this is an
additive configuration, not a behavioural change to the cis+signals policy.

Large filtered files, transient downloads, and stores are written under the
configured `output.artifact_root` plus `output.artifact_subdir`. The release
bundle and small sidecars are tracked in this repository.
