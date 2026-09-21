# gwas-catalog-eur-hybrid (Phase B)

Phase B entry point for the EBI GWAS Catalog `hybrid__European` pool: the
`gwas-catalog-ssf` Source Collection's European-ancestry Hybrid Store Family
(issue #150).

```text
inventory.py       freeze + preflight for this family's Source Inventory (issue #151)
config-full.yaml   the full-release Phase B configuration
```

## The full release (issue #150)

`config-full.yaml` declares the release this family is working toward: every
ready Analysis in the frozen Source Inventory at
`resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv`, resolved
through AF-based ancestry assignment and phenotype-SD effect-scale estimation.
It is Phase B only — it never declares how to build a Store.

```sh
# 1. Freeze the inventory (writes resources/inventories/<snapshot-id>.{tsv,meta.yaml}).
pixi run inventory-freeze

# 2. Prove the frozen snapshot before spending hours on resolution.
pixi run preflight

# 3. Review the machine-readable report preflight wrote.
#    <work_root>/preflight/<snapshot-id>.json
```

Both commands default to `config-full.yaml`; pass `--config` to point them at a
different release configuration, `--cores` to lower the planned worker count,
`--inventory`/`--provenance` to preflight a different snapshot, or
`--snapshot-id`/`--out-dir`/`--candidates` to freeze a later one.

`inventory-freeze` needs the generated candidate table
(`resources/data/derived/store-candidates-analyses.tsv`), which is not tracked in
git: run `Rscript resources/scripts/ebi-studies.r` first, or pass `--candidates`
with this host's copy. Everything else it reads is either tracked or an
acquisition manifest under the mirror.

```sh
# A later snapshot, re-frozen after more acquisition, still accounting the same
# candidate pool:
pixi run python resources/generators/gwas-catalog-eur-hybrid/inventory.py freeze \
  --snapshot-id gwas-catalog-ssf-eur-hybrid-2026-10-01 \
  --base-manifest /data/opengwasdb/raw/ebi-gwas-catalog/eur-hybrid-download-manifest.tsv \
  --retry-manifest /data/opengwasdb/raw/ebi-gwas-catalog/eur-hybrid-download-retry-transient-manifest.tsv \
  --candidates resources/data/derived/store-candidates-analyses.tsv
```

Re-freezing does not change which Analyses an earlier snapshot selected; it
creates a new snapshot that the config must then be pointed at.

## Settled Decisions (issue #152)

Both Reference Resource decisions required before Phase B candidate generation (issue #153)
are finalized and ratified on empirical concordance evidence:

1. **Ancestry Extraction Method:** Retain Method A (full reference scan `ref_freqs.hg38.tsv.gz`,
   `extraction_panel: null`). `qc-panel-hg38` was evaluated on the preregistered 106-Analysis sample
   and rejected due to failing the genome-wide concordance gate (95.1% < 98.0% threshold) caused
   by overlap drop on non-standard arrays. Full evidence is in `docs/qc-panel-concordance-report.md`.
2. **Reference-AF Fallback Policy:** Adopt Option 3 (**Source-AF-Only**). `config-full.yaml` sets
   `effect_scale_validation.reference_resources: []`. Quantitative Analyses lacking usable source AF
   receive an explicit `skipped` resolution (`no_reference_resource_for_ancestry`) and are excluded from
   candidate release membership rather than estimated against an absent panel.

The earlier ten-Analysis pilots live in this directory as
`config-pilot-10.yaml` (case-control) and `config-quant-pilot-10.yaml`
(quantitative), and their Release Bundles are `stores/OGS-00004` and
`stores/OGS-00005`.

## QC Panel Concordance Study (issue #152)

The study was preregistered at `docs/spec/qc-panel-concordance-preregistration.md`
and the ratified final decision report is at `docs/qc-panel-concordance-report.md`.

```sh
# 1. Generate / re-generate the frozen stratified sample manifest (106 Analyses):
pixi run concordance-sample

# 2. Run the concordance study in a Herdr pane using up to 64 cores:
pixi run --environment dev concordance-run --cores 64 \
  --out-dir /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance

# 3. Review the generated comparison artifacts:
#    - /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance/concordance_results.json
#    - /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance/concordance_comparison.tsv
#    - /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance/concordance_report.md
```

### Reference-AF Fallback Policy

This release adopts an explicit **Source-AF-Only** policy:
- `config-full.yaml` sets `effect_scale_validation.reference_resources: []`.
- Quantitative Analyses lacking usable source AF receive an explicit `skipped`
  resolution with reason `no_reference_resource_for_ancestry` and are excluded
  from the candidate manifest.
- The absent panel `/data/opengwasdb/reference/ukb-hg38` is never treated as usable evidence.

## Pieces

- `resources/inventories/` — the frozen snapshot, sample manifest, and what their columns mean;
- `resources/generators/lib/source_inventory.py` — the freeze merge rule, the
  readiness vocabulary, and preflight;
- `resources/generators/lib/concordance_sampling.py` — deterministic stratified sampling;
- `resources/generators/lib/qc_panel_concordance.py` — concordance comparison engine & metrics;
- `docs/spec/qc-panel-concordance-preregistration.md` — locked preregistration document;
- `docs/qc-panel-concordance-report.md` — finalized decision & empirical concordance evidence report;
- `resources/scripts/download-ebi-gwas-catalog-eur-hybrid.py` — acquisition,
  which writes the status manifests the freeze merges.
