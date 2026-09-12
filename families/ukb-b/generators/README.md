# ukb-b Manifest Generator

This Store Family's Manifest Generator resolves analyses from the
`opengwas-gwas-vcf` Source Collection and emits a candidate release bundle
(`../releases/<family-release-id>/`).

It is a **thin family-specific wrapper** over the shared engine at
`resources/generators/opengwas-gwas-vcf-dense/`. The engine is generic
across dense-observed OpenGWAS-GWAS-VCF batches; only this family's inputs
differ, held here as:

- `config.yaml` - the candidate manifest to resolve
  (`../releases/dense-observed-vcf-c128/analyses.tsv`, the existing
  hand-reverse-engineered bundle for this batch), source/output paths, and
  release identity.
- `generate.R` - a one-call driver that runs the shared engine with
  `config.yaml`.

## What this generator does (and does not do)

Per Analysis, it calls the OpenGWAS API's `gwasinfo` endpoint (via
`resources/lib/metadata_resolvers/opengwas_api.R`, issue #49) and resolves
`stored_effect_scale`, `original_effect_scale`, `original_sd_method`,
`sample_size_kind`, `sample_size`, and `n_cases`/`n_controls` — replacing
the candidate manifest's blank/family-default values for these fields
(previously reverse-engineered by hand from `explodecomputer/opengwasdb`'s
`data/ukb-b/manifest.tsv`, which does not distinguish continuous from
binary UKB-b traits). An Analysis the API returns no usable metadata for
gets `exclude_from_build: true` and an explanatory `inclusion_reason`,
never a silently defaulted `0`.

**Scope boundary — read before treating this as "the ieu-a-7 fix":** this
generator makes the *release manifest* correct. It does not make
`opengwasdb` build a store using these values. As of the currently pinned
`opengwasdb` commit, `opengwasdb.layouts.dense.build_vcf`
(`stored_effect_scale = read_vcf_study_type(row.file_path)`) and
`opengwasdb.layouts.hybrid.build` call `read_vcf_study_type()` on the raw
VCF `##SAMPLE` header **unconditionally** — there is no code path where a
manifest-supplied value is even considered. The builder-side half is
tracked at `explodecomputer/opengwasdb#14`; until that lands, a store built
from this release's manifest would still silently ignore the resolved
values and fall back to the (often wrong) VCF header. `ieu-a-7` itself is
not a ukb-b analysis (it belongs to the unrelated `ieu-a` batch) and is not
part of this batch.

It also does not download or filter the source GWAS-VCFs (the 2,514 files
live on a separate host this registry has no direct access to) — only
`derive`-stage metadata resolution.

## Usage

Requires an OpenGWAS API JWT (see <https://api.opengwas.io/>) in the
`OPENGWAS_JWT` environment variable — for example via a local, gitignored
`.Renviron` (`OPENGWAS_JWT=...`). Never commit this token.

```sh
pixi run Rscript families/ukb-b/generators/generate.R --mode=emit
pixi run Rscript families/ukb-b/generators/generate.R --mode=validate
```

For smoke tests, pass `--max-analyses=N` or
`--only-analysis-id=ukb-b-...,ukb-b-...`; both request only a subset of the
already-loaded candidate manifest, so no partial-write handling is needed
(unlike `gwas-ssf-ragged`'s `--mode=filter`, there is no separate
long-running download step here).

Each run calls the live OpenGWAS API (batched at up to 100 ids per
request, per the API's own flat-cost pricing tier) and writes a fresh
`sidecars/derivations.tsv` recording the resolved scale/sample-size
evidence (or the unresolved reason) per Analysis.

After the source VCFs are locally available, run the shared one-pass annotation
stage. It uses OpenGWASDB's GWAS-VCF reader to extract AF and SE together, then
feeds that extraction to OpenGWASDB's ancestry and phenotype-SD modules:

```sh
pixi run Rscript resources/scripts/download-opengwas-vcfs.R \
  --ids=ukb-b-10787,ukb-b-11842,ukb-b-19953 \
  --out-dir=/data/opengwasdb/wip/ukb-b-vcf

pixi run python resources/generators/opengwas-gwas-vcf-dense/annotate.py \
  --release-dir=families/ukb-b/releases/dense-observed-vcf-c128-resolved
```

## Building the Store

The generator produces the fixed input; `--mode=emit` is its generator-to-build
seam. There is no build mode: the Store is built and validated from that fixed
input by the shared workflow (issue #103):

```sh
pixi run release --configfile families/ukb-b/releases/dense-observed-vcf-c128-resolved/build.yaml
```
