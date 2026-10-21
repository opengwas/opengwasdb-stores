# ukb-b Manifest Generator

This Store Family's Manifest Generator resolves analyses from the
`opengwas-gwas-vcf` Source Collection and keeps a working bundle in its work root,
`<artifact-root>/<output.store_id>/generate`; `--mode=emit-registry` emits the
candidate into `stores/<output.store_id>/` through the one emitter (#248).
`config-pilot-10.yaml` is registered as `OGS-00008`.

It is a **thin family-specific wrapper** over the shared engine at
`resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/`. The engine is generic
across dense-observed OpenGWAS-GWAS-VCF batches; only this family's inputs
differ, held here as:

- `config-pilot-10.yaml` - the candidate manifest to resolve
  (`families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv`, the
  existing hand-reverse-engineered bundle for this batch), the 10-Analysis
  selection, and release identity (`OGS-00008`).
- `generate.R` - a one-call driver that runs the shared engine with
  `config-pilot-10.yaml`.

## What this generator does (and does not do)

Per Analysis, it calls the OpenGWAS API's `gwasinfo` endpoint (via
`resources/generators/lib/metadata_resolvers/opengwas_api.R`, issue #49) and resolves
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
pixi run Rscript resources/generators/ukb-b/generate.R --mode=emit
pixi run Rscript resources/generators/ukb-b/generate.R --mode=validate
pixi run Rscript resources/generators/ukb-b/generate.R --mode=emit-registry
```

The legacy driver runs `config-pilot-10.yaml` (`ukb-b-pilot-10`, `OGS-00008`)
and keeps its working bundle in `<artifact-root>/OGS-00008/generate`;
`emit-registry` emits it into `stores/OGS-00008/` through the one emitter
(#248). The full ukb-b release comes from `generate_candidate.py` below; the
legacy full-batch `config.yaml` was removed in #248.

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
pixi run Rscript src/manifest_generation/providers/opengwas/download-opengwas-vcfs.R \
  --ids=ukb-b-10787,ukb-b-11842,ukb-b-19953 \
  --out-dir=/data/opengwasdb/wip/ukb-b-vcf

pixi run python3 -m manifest_generation.providers.opengwas.annotate \
  --release-dir="$(pixi run python3 -m manifest_generation.emit work-root OGS-00008)"
```

## Restored full membership (#219, current bundle shape)

The legacy R driver above writes a family-shaped bundle. The restored full
release is instead frozen and emitted through the current Release Bundle shape
by two pure-Python tools:

- `inventory.py` reconciles the **declared** intended selection
  (`families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv`, whose
  `inclusion_reason` cites the OpenGWAS IGD `data/ukb-b/manifest.tsv`) against
  the local VCF mirror and the old built baseline, then freezes a checksummed
  `resources/inventories/<snapshot>.{tsv,meta.yaml}`. It never treats a glob as
  intent, refuses to freeze a partial copy, and hashes with at most two reader
  threads using a resumable receipt.
- `generate_candidate.py` renders `release.yaml`, `build.yaml`, `analyses.tsv`
  and `validation.yaml` from that frozen inventory plus the recorded
  resolution, and emits them into `<registry-root>/<store-id>/` (default
  `stores/`) through the shared emitter (#248), which checks the bundle against
  the Release Bundle contract and writes `summary.yaml`. A re-run replaces an
  earlier candidate; a bundle that has been accepted is refused. Store Release
  Build does not build a candidate until it is accepted (#247).

```sh
python3 -m manifest_generation.providers.ukb_b.inventory reconcile \
  --source-dir /data/opengwasdb/raw/gwas-vcf/ukb-b \
  --built-baseline stores/OGS-00009/analyses.tsv

python3 -m manifest_generation.providers.ukb_b.inventory freeze \
  --source-dir /data/opengwasdb/raw/gwas-vcf/ukb-b \
  --snapshot-id ukb-b-restored-2026-10-11 \
  --upstream-batch-evidence resources/inventories/ukb-b-restored-2026-10-11.upstream-batches.json

python3 -m manifest_generation.providers.ukb_b.generate_candidate OGS-00017
```

The intended membership is anchored to the upstream batch count from the
unsecured `GET https://api.opengwas.io/api/batches`, saved as evidence beside
the inventory. Live `POST /api/gwasinfo` resolution needs a valid `OPENGWAS_JWT`;
when it is unavailable the emitter consumes the recorded resolution output
(snapshot `opengwas-api-r2026-08-10`) and says so in `validation.yaml`.
