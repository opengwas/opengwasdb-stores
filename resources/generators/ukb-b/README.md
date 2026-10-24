# ukb-b Release Configs and Connectors

ukb-b is one **Source Collection** (`opengwas-gwas-vcf`) of 2,514 UK Biobank
GWAS-VCF Analyses: the restored membership #219 froze as
`ukb-b-restored-2026-10-11`. Since #289 it is on the shared pipeline (ADR 0036),
so this directory holds **inputs only**: two connector files, two release
configs, and the history the accepted bundles cite.

```text
connectors/source-metadata-table.tsv    the ukb-b adapter's output (2,514 rows)
connectors/acquisition-manifest.tsv     the ukb-b writer's output (2,514 rows)
receipts/                               the recorded identity evidence it reuses
release-full.yaml                       OGS-00009's content: all 2,514 Analyses
release-probe-10.yaml                   the ten-Analysis probe before the full resolve
```

## The connectors

Both are written from what this host has, never from a network call: the
OpenGWAS API token expired on 2026-08-21, so `POST /api/gwasinfo` is unavailable
and the metadata comes from the provider JSON sidecar beside each VCF.

```sh
# The Source Metadata Table: the declared selection's intent + provider JSONs.
python3 -m manifest_generation.providers.ukb_b.source_metadata_adapter \
  --declared-selection resources/inventories/ukb-b-restored-2026-10-11.declared-selection.tsv \
  --source-dir /data/opengwasdb/raw/gwas-vcf/ukb-b \
  --out resources/generators/ukb-b/connectors/source-metadata-table.tsv

# The Acquisition Manifest: the recorded identity/integrity receipts, reused
# rather than a fresh 425 GB read.
python3 -m manifest_generation.providers.ukb_b.write_acquisition_manifest \
  --declared-selection resources/inventories/ukb-b-restored-2026-10-11.declared-selection.tsv \
  --source-dir /data/opengwasdb/raw/gwas-vcf/ukb-b \
  --checksums resources/generators/ukb-b/receipts/ukb-b-restored-2026-10-11.checksums.json \
  --integrity resources/generators/ukb-b/receipts/ukb-b-restored-2026-10-11.integrity.json \
  --verification resources/generators/ukb-b/receipts/verify-deep.json \
  --out resources/generators/ukb-b/connectors/acquisition-manifest.tsv
```

All three receipts are required: the writer refuses an absent, shallow, empty or
partial verification report rather than treating it as "nothing failed"
(`docs/spec/source-connectors.md`).

`receipts/` holds **copies** of the three receipts #219 wrote under
`/data/opengwasdb/work/epic232/219/` (the checksums receipt is 605 KB and names
each VCF's path, size, mtime and sha256; the integrity receipt is the
`bgzip -t`/`tabix -l` result per file; the verification report is the deep
re-verification's, with nothing to report). They are committed because a
committed manifest's lineage must name files that outlive the run: the checksum
receipt's sha256 `0bf039ff…` is the one
`resources/inventories/ukb-b-restored-2026-10-11.receipt.json` already records,
and `connectors/acquisition-manifest.meta.yaml` records all three.

`docs/spec/source-connectors.md` specifies both files and both writers ("The
ukb-b adapter", "The ukb-b acquisition writer"). In short:

- the table's intent is the **declared selection**, and every declared Analysis
  must have a provider JSON whose `id` and `batch` match it; the table carries
  the provider's trait label, its `European` label (translated to `EUR`), the
  study design, and the total sample size with the case/control counts for the
  1,846 binary traits;
- the manifest's availability is the **recorded identity**: 2,514 VCFs with
  their sizes, sha256s and Tabix indexes, each with its provider JSON as the
  sidecar. A file that is gone, truncated, re-checksummed, missing its index or
  corrupt is a non-ready row with the reason, never a silent member;
- the manifest's `<out-stem>.meta.yaml` pass lineage names the three receipts
  (and their sha256) the manifest was written from.

The mirror itself is never copied into the repository and never re-read to write
a connector: `connectors/acquisition-manifest.meta.yaml` records the receipts'
identity, and `resources/inventories/ukb-b-restored-2026-10-22d.meta.yaml`
records the frozen snapshot's.

## The release configs

`release-full.yaml` is **OGS-00009's content**: all 2,514 Analyses of the frozen
snapshot, the `opengwasdb.gwas-vcf` reader on the `hg19` source assembly against
the GRCh38 ancestry reference, and the maintained `hm3` Indexed Variant Subset.
Its method tiers declare `sd` / `declared_standardised` for **both** study
designs, as the accepted bundle does -- every ukb-b trait is on the SD scale,
the 1,846 binary traits included -- and `sample_size_kind` is the one field that
differs between them (`case_control` / `total`).

`release-probe-10.yaml` is the bounded probe: ten Analyses (five case-control,
five quantitative) from #219's own probe set, resolved first because the
GWAS-VCF reader has never read a real ukb-b file at the pinned opengwasdb and
the GRCh37 source needs a liftover chain against the GRCh38 reference.

```sh
# Freeze the Selection from the two connectors (or prove the frozen one is ours).
pixi run inventory-freeze --config resources/generators/ukb-b/release-full.yaml
# Prove the frozen snapshot before any association row is read.
pixi run preflight --config resources/generators/ukb-b/release-full.yaml
# The whole pipeline, into a temporary registry: never over stores/OGS-00009.
pixi run generate-candidate OGS-00009 \
  --config resources/generators/ukb-b/release-full.yaml \
  --registry-root <scratch>/stores --work-root <scratch>/work \
  --resolution-root <scratch>/resolution --cores 16
```

The frozen snapshots are `resources/inventories/ukb-b-restored-2026-10-22d.*`
(the full Selection) and `ukb-b-probe-10-2026-10-22d.*` (the probe's ten). The
earlier unshipped pairs are superseded: `-2026-10-22` was the first freeze,
`-2026-10-22c` added the adapter's First Author column, and `-2026-10-22d`
re-froze because the manifest's pass lineage now records how many Analyses the
deep verification verified and refused. A
changed connector, a changed Selection or a new freeze is a **new snapshot id**;
a frozen snapshot is never overwritten. `ukb-b-restored-2026-10-11.*` stays
untouched: it is OGS-00009's accepted record, in the retired local format.

## Ancestry, SD and the accepted bundle

- **Ancestry** is measured by the shared resolver, not taken from the label.
  The provider labels all 2,514 Analyses `European`, and OGS-00009 records that
  as a `source_fallback`. A candidate records what the resolver actually
  measured, so a measured non-EUR or unassigned Analysis is a finding about the
  reference, not a regression.
- **Effect scale.** Every Analysis declares the SD scale (`units: SD`), so the
  release declares `original_sd_method: declared_standardised` and estimates no
  phenotype SD; no reference-AF resource is declared and none is needed.
- **Traits.** The provider supplies no ontology term, so Trait Ontology Mapping
  runs the shared step (#286) against the Canonical Trait Mapping Table;
  OGS-00009's accepted bundle records every row `unmapped`.

## Criterion 1: the full comparison against OGS-00009 (#289)

The 2,514-Analysis resolve was run once, against the final code, **into a
temporary registry** -- never over `stores/OGS-00009`:

```sh
generate-candidate OGS-00009 --config resources/generators/ukb-b/release-full.yaml \
  --cores 24 \
  --registry-root /data/opengwasdb/work/289/stores \
  --work-root /data/opengwasdb/work/289/work \
  --resolution-root /data/opengwasdb/work/289/res
```

It took about **14 minutes** at 24 workers (4.8 CPU-hours summed; every
Analysis read a median 66,947 rows, bounded by the resolver's 50,000-site
ancestry bound, and peaked at 71 MB), with `opengwasdb 0.3.0` at `d405de0` and
the declared `hg19 → hg38` chain on every record. Evidence:
`/data/opengwasdb/work/289/` (`run.log`, the bundle under `stores/OGS-00009/`,
`work/OGS-00009/resolver/resolution_receipt.json`, and the records under
`res/opengwas-gwas-vcf/checkpoint/`).

**Result: 2,514 Analyses resolved (0 failures, 0 controlled failures), 2,514
admitted, 0 excluded, `bundle.check()` clean**, and `build.yaml` equal to
`stores/OGS-00009/build.yaml` document for document -- the observed `build`
block, the `complete-dense` block against `ukb-hg38-eur-ld`, `post` and the
`hm3` subset.

| Comparison | Result |
|---|---|
| Membership | 2,514 = 2,514; no Analysis missing, extra or excluded on either side, so there are no exclusions to explain by reason. |
| `analyses.tsv`, 28 of the 34 accepted columns | Identical on every row: `analysis_id`, source ids and labels, `source_file`, `checksum`, `size_bytes`, `source_genome_build`, licence, consortium, First Author, `source_ancestry_label`, `assigned_ancestry`, `original_effect_scale`, `original_sd`, `original_sd_method`, `stored_effect_scale`, `sample_size_kind`, `sample_size_scope`, `sample_size`, `n_cases`, `n_controls`, `analysis_group_id`, `exclude_from_build`, `analysis_index`, ... |
| `analyses.tsv`, the other columns | The five that differ, and the eight added, are the by-design differences listed below. |
| `build.yaml` | **Equal** to the accepted one. |
| `validation.yaml` | The pipeline ran what #219 could not: `schema`, `files`, `ancestry`, `effect_scale`, `sd_estimation` and `reference_overlap` are all `passed`, where the accepted bundle records `ancestry`, `effect_scale` and `sd_estimation` `not_run` and a `reader_smoke_test` `not_run`. The completion reference is `qualified`/`active` and not blocked. |
| `release.yaml` | `status: candidate` (the accepted bundle is `accepted`), `derived_from: null` in both, the same label and description family; the snapshot id, notes and `generator` block name the shared pipeline instead of the retired bespoke generator. |
| Ancestry assigned | **Identical**: all 2,514 `EUR`, and measured rather than copied -- `gate_reason ok` on every row, dominant proportion 0.940-1.000 (median 0.998), NNLS residual at most 0.0062 (limit 0.06), EAF orientation `passed` (r 0.740-0.999, median 0.966). |
| Phenotype SD | Every row `skipped / scale_already_declared`: the tier declares the SD scale, so nothing is estimated. Matches the accepted `declared_standardised` with a blank `original_sd`. |
| Case/control counts | 1,846 rows `case_control` with their counts, 668 `total` without, exactly the accepted declaration. |
| Trait mappings | 553 rows mapped by the Canonical Trait Mapping Table (353 distinct terms) and 1,961 `unmapped`; the accepted bundle is `unmapped` on all 2,514. |

The by-design differences, each as the probe found it:

- `ancestry_assignment_method`: `source_fallback` in the accepted bundle,
  `af_assigned` here -- the pipeline measures what the bundle recorded, and the
  measurements agreed on every row.
- `inclusion_reason`: the accepted wording cites the declared selection; the
  pipeline's member vocabulary is `selected_ready_source`.
- `trait_ontology_id`, `trait_ontology_label`, `trait_ontology_mapping_method`
  (553 rows): mapped where the accepted bundle has no mapping; a gain, not a
  regression, and the remaining 1,961 stay `unmapped` exactly as accepted.
- The eight added columns the pipeline writes and the accepted bundle predates:
  `source_reader_capability` (`opengwasdb.gwas-vcf`), `source_url` (blank, as
  accepted), `downloaded_file` (the basename), `info_score_threshold` and
  `maf_threshold` (literal `NaN`, no declaration), and the
  `imputation_score_column`/`_kind`/`_provenance` triple (blank, undeclared).

## Retired paths

ukb-b's bespoke path is gone (#289):

- `providers/ukb_b/inventory.py` and its suite, replaced by the shared freeze
  (`tests/freeze/test_ukb_b_reconciliation.py` ports the failures it caught);
- `providers/ukb_b/generate_candidate.py` and its suite, replaced by the shared
  pipeline (#340 ported its completion-reference logic first);
- the R path -- `generate.R`, `lib/opengwas_gwas_vcf_dense.R`,
  `lib/source-formats/opengwas-gwas-vcf-dense/generate.R` and
  `lib/metadata_resolvers/opengwas_api.R` -- with their suites
  (`tests/opengwas-gwas-vcf-dense`, `tests/metadata-resolvers/opengwas-api`).

`lib/metadata_resolvers/canonical_trait_table.R` and `ontology_contract.R`
went with #350: they lost their generator with the R path, and curation's
round-trip proof (`tests/curation/test_promotion.py`, `test_e2e_pipeline.py`) no
longer drives the R resolver -- it maps a promoted row through the pipeline's
own `manifest_generation.trait_ontology_mapping`.

`config-candidate.yaml` and `config-pilot-10.yaml` stay unchanged as the
history OGS-00009's and OGS-00008's `generator.commands` cite, and
`successor-identity.yaml` as the retired-id record.
