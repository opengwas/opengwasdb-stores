# pqtl-interval-2018 Manifest Generator

This Store Family's Manifest Generator selects analyses from the
`gwas-catalog-ssf` Source Collection and emits a candidate release bundle
(`../releases/<family-release-id>/`).

It is a **thin family-specific wrapper** over the shared engine at
`resources/generators/gwas-ssf-ragged/`. The engine is generic across all
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

OpenGWASDB builds the ragged store from that bundle's fixed input (the filtered
GWAS-SSF files and `analyses.tsv`). The retired `gwas-ssf-ragged/build-store.py`
wrapper used to do it and record a small read-back report; since issue #103 the
Store is built by the shared workflow, whose `build.command` names the same
`opengwasdb build-ragged-ssf` CLI and whose validate phase performs the
read-back:

```sh
pixi run release --configfile families/pqtl-interval-2018/releases/2018-sun-pilot-10/build.yaml
```

`--mode=emit` is the generator-to-build seam: it freezes the fixed input. The
workflow, not the generator, owns building and validating the Store.
