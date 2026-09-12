# pqtl-interval-2018 Manifest Generator

This Store Family's Manifest Generator selects analyses from the
`gwas-catalog-ssf` Source Collection and emits a candidate release bundle
(`../releases/<family-release-id>/`).

It is a **thin family-specific wrapper** over the shared engine at
`generators/lib/source-formats/gwas-ssf-ragged/`. The engine is generic across all
molecular GWAS-SSF ragged stores; only this family's inputs differ, held here as:

- `config.yaml` - the analyte selection (GWAS Catalog accessions), the
  authoritative gene / cis-coordinate source (SomaScan SeqId -> gene -> GRCh38 via
  `../../../resources/somascan/sun-2018-analysis-targets.tsv`), the per-analysis
  metadata (N, ancestry, effect scale = sd, tissue), and the sparse-region
  filter policy (cis +/-1 Mb; significant trans p<=5e-8, merged +/-1 Mb;
  suggestive p<=1e-5, lead SNPs only; MHC analytes flagged).
- `generate.R` - a one-call driver that runs the shared engine with `config.yaml`
  and writes the release bundle.

Generate and validate the candidate bundle with:

```sh
pixi run Rscript families/pqtl-interval-2018/generators/generate.R --mode=emit
pixi run Rscript families/pqtl-interval-2018/generators/generate.R --mode=validate
```

Run sparse filtering with:

```sh
pixi run Rscript families/pqtl-interval-2018/generators/generate.R --mode=filter
```

For smoke tests, pass `--max-analyses=N` or `--only-analysis-id=GCST...`; partial
runs write `*.partial.tsv` reports and leave the release manifest untouched.
Full filtering downloads each `<GCST>.h.tsv.gz`, filters it to the sparse
regions, deletes the full download as it goes, writes checksums/sizes back into
`analyses.tsv`, and emits `sidecars/filter_summary.tsv` plus
`sidecars/sparse_regions.tsv`.

OpenGWASDB then builds the ragged store from that bundle via
`opengwasdb.layouts.ragged.build_ssf:build_ragged_from_ssf`; the shared
`build-store.py` wrapper records a small read-back report.
