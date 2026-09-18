# metabolome-plasma-2023 Manifest Generator

This Store Family's Manifest Generator selects analyses from the
`gwas-catalog-ssf` Source Collection and emits a candidate release bundle
(`../releases/<family-release-id>/`).

Like `pqtl-interval-2018`, it is a **thin wrapper** over the shared engine at
`resources/generators/lib/source-formats/gwas-ssf-ragged/`, but it exercises two capabilities the
Sun pQTL pilot did not need:

- **No-cis sparse-region policy** (issue #26): metabolites have no single
  encoding gene, so `config.yaml` has no `inputs.analysis_targets`. Only
  significant-hit and suggestive-lead regions are retained; there is no cis
  window and no per-analysis gene target coordinates in `analyses.tsv`.
- **Multi-ancestry selection**: `selection.ancestry_groups` +
  `n_analyses_per_ancestry` pick a fixed-size slice per ancestry (African,
  East Asian, European, South Asian) rather than one ancestry per release.

This Source Collection's harmonised GWAS-SSF files carry genuine source
allele frequencies (unlike the Sun pilot's empty column), so this is also the
first release where reference-AF effect-scale validation (#16) runs with
`af_source: source` and AF-based ancestry assignment (#23/#25) runs with real
mixture fitting across more than one ancestry.

Generate and validate the candidate bundle with:

```sh
pixi run Rscript resources/generators/metabolome-plasma-2023/generate.R --mode=emit
pixi run Rscript resources/generators/metabolome-plasma-2023/generate.R --mode=validate
```

Run sparse filtering with:

```sh
pixi run Rscript resources/generators/metabolome-plasma-2023/generate.R --mode=filter
```

Then run reference-AF effect-scale validation and AF-based ancestry
assignment:

```sh
pixi run Rscript resources/generators/metabolome-plasma-2023/generate.R --mode=effect-scale

pixi run python resources/generators/lib/source-formats/gwas-ssf-ragged/ancestry-assign.py \
  --release-dir=families/metabolome-plasma-2023/releases/2023-chen-pilot-80
```

For smoke tests, pass `--max-analyses=N` or `--only-analysis-id=GCST...` to
`--mode=filter`; partial runs write `*.partial.tsv` reports and leave the
release manifest untouched.
