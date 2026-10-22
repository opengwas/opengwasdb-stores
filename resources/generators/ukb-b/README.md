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
identity, and `resources/inventories/ukb-b-restored-2026-10-22.meta.yaml`
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

The frozen snapshots are `resources/inventories/ukb-b-restored-2026-10-22.*`
(the full Selection) and `ukb-b-probe-10-2026-10-22.*` (the probe's ten). A
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

## Retired paths

`providers/ukb_b/inventory.py` and its suite retired with #289: the shared
freeze replaces it, and `tests/freeze/test_ukb_b_reconciliation.py` ports the
missing-file, unclaimed-file and duplicate cases it caught. The R generator
(`generate.R`, `lib/opengwas_gwas_vcf_dense.R`,
`lib/source-formats/opengwas-gwas-vcf-dense/generate.R` and the OpenGWAS
metadata resolvers) and `providers/ukb_b/generate_candidate.py` retire with the
rest of ukb-b's bespoke path; until then `config-candidate.yaml` and
`config-pilot-10.yaml` stay unchanged as the history OGS-00009's and
OGS-00008's `generator.commands` cite.
