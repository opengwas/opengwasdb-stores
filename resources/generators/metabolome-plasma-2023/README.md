# metabolome-plasma-2023 Manifest Generator

This Store Family's Manifest Generator selects analyses from the
`gwas-catalog-ssf` Source Collection. It keeps a working bundle in its work root,
`<artifact-root>/<output.store_id>/generate`, and `--mode=emit-registry` emits
the candidate into `stores/<output.store_id>/` through the one emitter (#248).
`config-european.yaml` is `2023-chen-full-european`, registered as `OGS-00006`.
The other ancestries' configs, and the multi-ancestry `2023-chen-pilot-80`
config, were never registered and were removed in #248.

Like `pqtl-interval-2018`, it is a **thin wrapper** over the shared engine at
`resources/generators/lib/source-formats/gwas-ssf-ragged/`, but it exercises two capabilities the
Sun pQTL pilot did not need:

- **No-cis sparse-region policy** (issue #26): metabolites have no single
  encoding gene, so the config has no `inputs.analysis_targets`. Only
  significant-hit and suggestive-lead regions are retained; there is no cis
  window and no per-analysis gene target coordinates in `analyses.tsv`.
- **Per-ancestry selection**: the engine's `selection.ancestry_groups` +
  `n_analyses_per_ancestry` pick a fixed-size slice per ancestry. The
  multi-ancestry `2023-chen-pilot-80` config that used them was never
  registered and was removed in #248; `config-european.yaml` selects one
  ancestry (`selection.ancestry_group`).

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
  --release-dir="$(pixi run python3 resources/generators/lib/emit.py work-root OGS-00006)"
```

For smoke tests, pass `--max-analyses=N` or `--only-analysis-id=GCST...` to
`--mode=filter`; partial runs write `*.partial.tsv` reports and leave the
release manifest untouched.
