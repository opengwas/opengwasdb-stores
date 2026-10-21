# dbGaP MVP release configs (phs002453, PMID 39024449)

MVP `phs002453` is one **Source Collection** under the dbGaP Source Provider
(ADR 0036). Its connector files are the collection's:

```text
connectors/source-metadata-table.tsv    the dbGaP MVP metadata adapter's output
connectors/acquisition-manifest.tsv     the acquisition's shared Acquisition Manifest
```

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
| OGS-00016 | *(not yet)* | `dense__pmid-39024449__Multi-ancestry` | 1,691 |

`expect_rows` is the retired partition's bound count, now a check on the
Selection (#290): freeze refuses a different number and preflight re-checks the
frozen inventory. The frozen snapshots are
`resources/inventories/dbgap-phs002453-{eur,afr,amr,eas}-2026-10-21.*`.

The EUR/AFR/AMR/EAS configs regenerate as **comparison-only** candidates into a
temporary registry; OGS-00012–16 are accepted and are never replaced.

**META needs a release-policy mode the pipeline does not have.** OGS-00016's
1,691 Analyses are all Assigned Ancestry empty with method `unassigned`, and
OGS-00014 includes 203 such rows; the shared `assigned_ancestry` Quality Check
would exclude them. A multi-ancestry Selection that records, but does not
require, a single super-population is a decision for #290's report, not
something these configs may fake with a super-population code. `config-candidate.yaml`
and `candidates.yaml` are the retired #221 generator's historical plan.

## Regeneration against the committed bundles

The comparison run of 2026-10-21 freezes and preflights the four Selections
clean, but the pipeline cannot reproduce a committed membership yet. It
excludes 799 rows the committed bundles include and adds none:

| Store | committed | pipeline | exclusions |
|---|---:|---:|---|
| OGS-00012 | 1,669 | 1,470 | `sd_failed` 199 |
| OGS-00013 | 1,368 | 1,187 | `sd_failed` 181 |
| OGS-00014 | 1,053 | 727 | `ancestry_unassigned` 203, `sd_failed` 123 |
| OGS-00015 | 241 | 148 | `sd_failed` 93 |

Both reasons are shared-pipeline gaps, reported in #290's report, never
something a config here may paper over:

- `phenotype_sd` gives `sd_failed` for the resolver's `scale_already_declared`,
  which is every quantitative Analysis whose scale the deposit declares (#300
  owns the declared-scale rule);
- `assigned_ancestry` excludes the 203 AMR rows the resolver measured
  `margin`/`proportion`, and would exclude META's whole bucket.

Resolving the same Selections also resumes nothing: the pipeline's resolver
settings differ from the #221 run's in three fingerprint fields, so every
record is re-resolved. #290's report gives the fields and the options.

## After #292 part 2

When OpenGWASDB's Column Map reader lands (#292 part 2), MVP should declare a
Column Map over dbGaP's own GIA files and delete the GWAS-SSF rewrite in the
acquisition. This is a note, not part of #290: the current release configs read
the acquisition's GWAS-SSF mirror because it is what the existing
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
