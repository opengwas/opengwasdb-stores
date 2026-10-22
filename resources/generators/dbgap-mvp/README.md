# dbGaP MVP release configs (phs002453, PMID 39024449)

MVP `phs002453` is one **Source Collection** under the dbGaP Source Provider
(ADR 0036). Its connector files are the collection's:

```text
connectors/source-metadata-table.tsv    the dbGaP MVP metadata adapter's output
connectors/acquisition-manifest.tsv     the acquisition's shared Acquisition Manifest
```

Every ready manifest row also carries the reformatting lineage ADR 0036 requires:
`original_location` names the published tar and the member inside it
(`<tar path>#<member>`), `original_sha256` and `original_bytes` are the extracted
member's own, and `reformat_tool` names the module that rewrote it with the
version recorded *at conversion*. A rewritten file whose extracted original is
gone is refused rather than given an invented identity.

The converter's version is never the version of the run that writes the
manifest (#290 review). `convert` writes a receipt under `GIA/convert-receipts/`
with its `module_sha256` and the executing `code_sha`, so a later acquisition's
files carry the code that wrote them. For the 6,022 files acquired before that,
`--conversion-history` reads the metadata-refresh run's own record: the two
files it converted carry that run's `code_sha` (`baf141c…`), and the other 6,020
carry `sha256:unrecorded` with the evidence searched, because no conversion-time
record names the code that wrote them.

The adapter is `manifest_generation.providers.dbgap.mvp.source_metadata_adapter`;
it joins the acquisition's `mapping.tsv` with the deposit's
`*.dbGaP.metadata.txt`. It writes the **post-split** `store_key`
(`dense__pmid-39024449__European`, `__African`, `__Hispanic-or-Latin-American`,
`__East-Asian`, `__Multi-ancestry`), so each ancestry bucket is one Selection on
a canonical column; `META` is deliberately imprecise and has no single
super-population. The shared `providers/dbgap/metadata.py` holds the tar/TOC and
metadata parsing both the acquisition and adapter use.

## The five Selections

| Store | Release config | `store_key` | `expect_rows` |
|---|---|---|---:|
| OGS-00012 | `release-eur.yaml` | `dense__pmid-39024449__European` | 1,669 |
| OGS-00013 | `release-afr.yaml` | `dense__pmid-39024449__African` | 1,368 |
| OGS-00014 | `release-amr.yaml` | `dense__pmid-39024449__Hispanic-or-Latin-American` | 1,053 |
| OGS-00015 | `release-eas.yaml` | `dense__pmid-39024449__East-Asian` | 241 |
| OGS-00016 | `release-meta.yaml` | `dense__pmid-39024449__Multi-ancestry` | 1,691 |

`expect_rows` is the retired partition's bound count, now a check on the
Selection (#290): freeze refuses a different number and preflight re-checks the
frozen inventory. The frozen snapshots are
`resources/inventories/dbgap-phs002453-{eur,afr,amr,eas,meta}-2026-10-21c.*`.

All five configs regenerate as **comparison-only** candidates into a temporary
registry; OGS-00012–16 are accepted and are never replaced.

Two ancestry policies are declared by these configs (#290, ADR 0036 "Clarified
by #290"):

- **OGS-00016 is `selection_superpopulation: multi-ancestry`.** The Selection
  names no single super-population, so the `assigned_ancestry` check is
  `not_applicable`, Assigned Ancestry is never assigned (blank, method
  `unassigned`), and nothing is excluded for ancestry. The measured fit stays in
  the Quality Check and ancestry sidecars.
- **OGS-00014 sets `keep_unassigned_ancestry_as_warning: true`**, keeping the
  203 Analyses the ancestry gates left unassigned (margin 200, proportion 3):
  blank ancestry, method `unassigned`, a warning-level `assigned_ancestry`
  result counted in `validation.yaml` and recorded per Analysis in the sidecar.
  A measured *different* super-population would still exclude.

`config-candidate.yaml` and `candidates.yaml` are the retired #221 generator's
historical plan.

## One id per Store (ADR 0037, #308)

Reference completion replaces a Store's Observed-Only Store under the same id,
so a Store never gets a second, `derived_from`-linked completed id. Each release
config emits one bundle under its one Store id, and the pipeline mints no
completed id. The release-config loader refuses a `completed_store_id`, a
`stores` block, a `complete` block or a `derived_from` as unknown keys.

`OGS-00018`–`OGS-00021` were reserved for ADR 0007's two-id completion of EUR,
AFR, AMR and EAS (one "lineage-linked completed" id each). ADR 0037 retires them
unused: no Store is allocated those ids, and none is ever reused. They appear
here, and in `config-candidate.yaml`'s comment, only as that record.

All five configs are `completion_state: observed_only`, and the pipeline does
not emit a reference-completed bundle: a `completion_reference` is a
declaration, and a `complete` block arrives only through the amendment below.
A Store whose reference is `qualified`/`active` is completed on the Store
Release Build side: the accepted bundle gains a `complete` block, the one
amendment ADR 0037 allows (#305), and the run completes that Store under its own
id. EUR is the first (owner decision 2026-10-21; OGS-00012 against the UKB EUR
panel). AFR and EAS are still pending, AMR is on hold and META is not
applicable, so OGS-00014 and OGS-00016 stay observed-only.
The retired generator's `<observed id>.completion-plan.yaml` receipt has
no pipeline equivalent.

## Regeneration against the committed bundles

Re-rendered from the reused pre-#284 records on 2026-10-21, all five Selections
now reproduce the committed membership exactly, with no exclusion:

| Store | committed | re-rendered |
|---|---:|---:|
| OGS-00012 | 1,669 | 1,669 |
| OGS-00013 | 1,368 | 1,368 |
| OGS-00014 | 1,053 | 1,053 |
| OGS-00015 | 241 | 241 |
| OGS-00016 | 1,691 | 1,691 |

`analyses.tsv` carries the pipeline's 42 columns instead of the committed 20–21
(none dropped), and the bundle gains `sidecars/`, `source_snapshot` and the
`validation.yaml` contract. Two existing columns also change, and only these:
`analysis_index` is written in the frozen inventory's order (ascending
accession) rather than the retired generator's bucket order, so 1,663 / 1,364 /
1,050 / 231 / 1,688 rows sit at a different position -- it is positional and
rewritten on every read (`opengwasdb` reassigns it from the row's position, and
the build manifest re-densifies it), so nothing keys on its value; and
`source_ancestry_label` carries the provider's public label (`European`,
`African`, `Hispanic or Latin American`, `East Asian`) instead of the
super-population code, which is now `assigned_ancestry`'s job as
[the spec](../../../docs/spec/source-connectors.md) says. The first regeneration also excluded 596
`scale_already_declared` rows, which #300's declared-scale rule now makes
`not_applicable`.

Resolving a Selection from the #221 records still re-resolves every record: the
pipeline's resolver settings differ from that run's in three fingerprint
fields. A run resolved once by the pipeline resumes everything afterwards (EAS:
241/241). #290's report gives the fields.

## After #292 part 2

The pinned `opengwasdb` already has the Column Map reader (opengwasdb#281). When
the pipeline integrates it (#292 part 2), MVP should declare a Column Map over
dbGaP's own GIA files and delete the GWAS-SSF rewrite in the acquisition. This
is a note, not part of #290: the current release configs read the
acquisition's GWAS-SSF mirror because it is what the existing
`opengwasdb.gwas-ssf` reader opens.

## Running

```sh
pixi run inventory-freeze --config resources/generators/dbgap-mvp/release-eur.yaml
pixi run preflight --config resources/generators/dbgap-mvp/release-eur.yaml
pixi run generate-candidate OGS-00012 \
  --config resources/generators/dbgap-mvp/release-eur.yaml --cores 16
```

The resolution root is deployment configuration; the five configs share the
Source Collection's Resolution Area.
