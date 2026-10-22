# Source connectors and freeze

How a Source Collection's connector files become a frozen Source Inventory.

Governed by [ADR 0036](../adr/0036-manifest-generation-is-one-pipeline-fed-by-source-connectors.md):
a source plugs into Manifest Generation through files, never through code the
pipeline calls. This specification covers the two connector files that meet at
**freeze** -- the **Source Metadata Table** (what the collection intends) and
the **Acquisition Manifest** (what acquisition produced) -- the **Selection** a
release config declares, and the frozen Source Inventory freeze writes (#283).
The release config is specified in [release-config.md](release-config.md)
(#287). How a collection's files are read -- the Source Reader Capability and
the Column Map -- is [Reading the source files](#reading-the-source-files) (#292).

| Contract | Owner |
|---|---|
| Source Metadata Table | `src/manifest_generation/source_metadata_table.py` |
| Acquisition Manifest | `src/manifest_generation/acquisition_manifest.py` |
| Connector file shape (reader, writer, errors) | `src/manifest_generation/connector_tsv.py` |
| Selection | `src/manifest_generation/selection.py` |
| Freeze and the frozen Source Inventory | `src/manifest_generation/freeze.py` |
| Inventory columns and readiness vocabulary | `src/manifest_generation/source_inventory.py`, documented in [`resources/inventories/README.md`](../../resources/inventories/README.md) |

## Reading the source files

Each Analysis's `source_file` is read by an OpenGWASDB Source Reader Capability
(`source_reader_capability`). A tabular collection with no dedicated reader is
read through `opengwasdb.column-map` and a **Column Map**, a TOML file naming
which provider column supplies each field (opengwasdb ADR 0063). The release
config names the map ([`column_map`](release-config.md#column_map)), and the
resolver and each build command take it as `--column-map PATH|NAME`: a file
path, or the name of a built-in map. A source on another genome build is
passed `--chain-file`, the declared liftover chain
([`ancestry_assignment`](release-config.md#reference-resources)). The binding,
the commands that take the flag and the checksum contract are in the
[release metadata schema](../release-metadata-schema.md#column-map-binding).

## Connector files

Both connector files are machine-written, tab-separated, UTF-8, with a header
row and no quoting. They share one reader and one writer:

- an unknown column, a missing required column, or a column named twice is
  refused, by the reader and by the writer alike;
- a ragged row, a value padded with whitespace and an empty file are refused,
  never trimmed or skipped;
- values are carried verbatim;
- one refusal names every problem found, up to twenty.

Every refusal is a `FreezeError` (an `InventoryError`), with one subclass per
input: `SourceMetadataTableError`, `AcquisitionManifestError`,
`SelectionError`, and `ReconciliationError` for freeze's own checks.

## Source Metadata Table

One row per intended Analysis, in one canonical set of columns whatever the
Source Provider, written by the collection's metadata adapter. Its rows are the
intended membership. Only `source_analysis_id` is required; every other column
may be absent from the header, and a blank cell means the source supplied no
value -- never zero and never a default.

| Column | Required | Vocabulary or form | Description |
|---|---:|---|---|
| `source_analysis_id` | Yes | unique | Source Analysis ID: the upstream accession (`GCST…`). Becomes the Source Inventory's `analysis_id`. |
| `source_trait_label` | No | text | Source Trait Label, verbatim. Becomes the inventory's `trait`. Blank when the source supplies none (one GWAS Catalog study does). |
| `source_trait_id` | No | text | Source Trait ID, when the source has one (a FinnGen phenocode). |
| `source_trait_ontology_id` | No | text | The source's own ontology term(s) for the Trait, verbatim: a CURIE, a URI, or a comma-separated list. Normalising it is Trait Ontology Mapping's job (#286). |
| `source_trait_ontology_label` | No | text | The source's label for that term. Refused without `source_trait_ontology_id`. |
| `source_ancestry_label` | No | text | Source Ancestry Label, verbatim provenance (`European`, `Multiple/Mixed`). Never Assigned Ancestry. |
| `source_superpopulation` | No | `ogstores.bundle.SUPERPOPULATIONS`: `AFR` `AMR` `EAS` `EUR` `MID` `NAF` `SAS` | The super-population the source declares, or its label's translation through the tracked `source_label_map.tsv`. Blank for a label `source_label_imprecise.tsv` lists as deliberately imprecise. A Selection may name it; it never sets Assigned Ancestry. |
| `study_design` | No | `quantitative`, `case-control` | The trait type. The release config keys its method tiers by it (`defaults.by_study_design`), and the inventory carries it. |
| `declared_effect_scale` | No | OpenGWASDB `StoredEffectScale`: `sd`, `log_or`, `log_hazard` | An effect scale the source declares. Blank when it declares none: the release's method tier decides. |
| `sample_size` | No | non-negative decimal | Source total sample size. Becomes the inventory's `sample_size`. |
| `n_cases`, `n_controls` | No | non-negative decimal | Case and control counts. Refused unless `study_design` is `case-control`: a zero count where counts do not apply is the absence-vs-zero confusion `bundle.check()` refuses (#133). |
| `source_genome_build` | No | `GRCh37`, `GRCh38` | Genome build of the source coordinates, when the source states it per Analysis, in the spelling every Release Bundle's `source_genome_build` uses. A collection-wide build is the release config's default instead. |
| `publication_pmid` | No | positive integer | Publication Metadata. Becomes the inventory's `publication_pmid`. |
| `publication_doi`, `first_author`, `consortium` | No | text | Publication Metadata. |
| `license` | No | text | Source License, when the source states it per Analysis. A collection-wide licence is the release config's default instead. |
| `store_key` | No | text | A provider-side curation's proposed Store grouping (GWAS Catalog: `ebi-studies.r`'s `store_key`). Present so a Selection can name it. |
| `analysis_id` | No | unique | The Store's Analysis ID, when it is not the source's (#288). Becomes `analyses.tsv`'s `analysis_id` and the id the sidecars name; blank means `source_analysis_id`. Resolution, the resolver manifest and the Resolution Records are always keyed by `source_analysis_id`, so declaring this renames nothing the resolver reads. A Source Collection whose ids are namespaced because a bare source id is not unique across sources declares its namespace here (FinnGen: `finngen-r13-<phenocode>`). Uniqueness is the *effective* id -- the declared value, else `source_analysis_id` -- so a declared value that collides with another row's source id is refused too; `bundle.check()` would refuse the bundle they render. |
| `analysis_group_id` | No | text | The Analysis's Store grouping (`analyses.tsv`'s `analysis_group_id`), when it is per Analysis (#288). Blank means the release config's collection-wide `defaults.analysis_group_id`. FinnGen groups by its provider manifest's `category`. |
| `target_assay_id` | No | text | Target annotation for an Analysis of one molecular target: the assay identifier (Sun 2018: the SomaScan SeqId, `5227-60`). |
| `target_symbol` | No | text | The target's gene symbol (`PDK1`). |
| `target_ensembl_gene_id` | No | `ENSG` + 11 digits, optional `.version` | The target's Ensembl gene. |
| `target_chromosome`, `target_start`, `target_end` | No | `1`–`22`, `X`, `Y`, `MT`; positive integers, start ≤ end | The target gene's span on the source build. All three or none. A cis region policy keeps a flank around it ([release config](release-config.md#region_policy)). |
| `target_mhc` | No | `true`, `false` | Whether the target gene overlaps the MHC (chr6:25–34 Mb). |
| `target_is_aggregate` | No | `true`, `false` | `true` for an aggregate assay that measures several targets at once (a SomaScan SeqId for a protein family): its Analysis Label is `target_assay_id`, which it then needs, never one member gene; the symbol, gene and span are those its coordinates map to. |
| `target_resolution_method` | No | text | How the target was resolved (`somascan_db_seqid`, `somascan_db_gene_name`, `ensembl_hgnc_symbol`). |
| `target_rows` | No | positive integer | How many target-table rows the target was resolved from. |
| `tissue`, `context` | No | text | The measured tissue and assay context (`plasma`, `SomaScan`), as in `analyses.tsv`. |

The target columns are annotation, never a Trait Ontology Mapping: a gene is
not the Trait (issue #141). They render into `analyses.tsv` (`trait_chr` and
`trait_bp` from the span's chromosome and start, `mhc`,
`target_resolution_method`, `n_target_rows`, and the Analysis Label: the
`target_symbol` of a single target, the `target_assay_id` of an aggregate) and
into `sidecars/analysis_targets.tsv`, one row per targeted Analysis (#300).

The two Store-identity columns are the only ones that do not reach the frozen
Source Inventory: the inventory, the resolver manifest and the Resolution
Records are keyed by `source_analysis_id`, and the bundle's `analyses.tsv` and
its sidecars report the declared identity instead (#288). Both are optional,
so a collection that declares neither keeps the pipeline's defaults.

### The GWAS Catalog adapter

`src/manifest_generation/providers/gwas_catalog/source_metadata_adapter.py`
converts `src/manifest_generation/providers/gwas_catalog/ebi-studies.r`'s candidate table, unchanged, and
loads the result back through the canonical loader; a table it refuses is
removed.

| Candidate column | Canonical column |
|---|---|
| `STUDY.ACCESSION` | `source_analysis_id` |
| `DISEASE.TRAIT` | `source_trait_label` |
| `MAPPED_TRAIT_URI`, `MAPPED_TRAIT` | `source_trait_ontology_id`, `source_trait_ontology_label` |
| `ancestry_group` | `source_ancestry_label`, and its `source_label_map.tsv` translation as `source_superpopulation` |
| `study_design`, `sample_size` | the same |
| `n_cases`, `n_controls` | the same for `case-control`; see below for `quantitative` |
| `PUBMED.ID`, `FIRST.AUTHOR` | `publication_pmid`, `first_author` |
| `store_key` | `store_key` |

Its translations, and only these:

- **Missing values.** `ebi-studies.r` writes with `data.table::fwrite`'s
  default `na = ""`, so an R `NA` is an empty field and an empty string is
  `""`; both read as empty, the only missing value. A literal `NA` or `nan` is
  carried, and the canonical loader refuses it where a number is due.
- **Quantitative case counts.** The candidate table writes `0`/`0` for a
  resolved quantitative study. Those become blank, because a zero count is a
  real count (#133). A non-zero count on a quantitative study, or any count on
  an Analysis with no `study_design`, is refused.
- **Padding** is trimmed with `str.strip`, as the candidate-table freeze
  trimmed it: a few GWAS Catalog trait labels carry trailing non-breaking
  spaces.
- **Super-population** (`source_ancestry.load_source_label_translation`). A
  label `source_label_map.tsv` lists becomes its code. A label
  `source_label_imprecise.tsv` lists -- `Multiple/Mixed`, `NR/Unknown`,
  `Other`, `Asian (unspecified)`, `META`, each with its reason -- leaves
  `source_superpopulation` blank on purpose. Any other label is refused by
  name, so a new or misspelt label is never silently blank: map it, or list it
  as imprecise with a reason. Both files live in the
  `ukb-ancestry-mixture-hg38` Reference Resource.
- **Collection-wide facts** -- the GRCh38 build of EBI's harmonised files and
  the EBI GWAS Catalog terms of use -- are not written; the release config's
  defaults supply them (#287).

Example (abridged):

```text
source_analysis_id  source_trait_label  source_ancestry_label  source_superpopulation  study_design  sample_size  n_cases  n_controls  publication_pmid  store_key
GCST000028          Type 2 diabetes     European               EUR                     case-control  2931         1464     1467        17463246          hybrid__European
GCST90006908        Anti-human herpes…  European               EUR                     quantitative  8735                             33204752          hybrid__European
```

### The dbGaP MVP adapter

`src/manifest_generation/providers/dbgap/mvp/source_metadata_adapter.py`
joins the acquisition's Analysis/candidate binding (`mapping.tsv`) with the
deposit's own `*.dbGaP.metadata.txt`, and writes the canonical table. dbGaP is
a host, not one collection (ADR 0036), so this adapter is the MVP Source
Collection's; a later dataset shares only `providers/dbgap/metadata.py`.

| Deposit or mapping value | Canonical column |
|---|---|
| `analysis_id` | `source_analysis_id` |
| `Analyzed variable` | `source_trait_label` |
| `dbgap_trait_code` | `source_trait_id` |
| member ancestry (`EUR`, `AFR`, `AMR`, `EAS`, `META`) | `source_ancestry_label`: `European`, `African`, `Hispanic or Latin American`, `East Asian`, `META` |
| that label through `source_label_map.tsv`, or blank for `META` | `source_superpopulation` |
| `Phenotypic trait type` | `study_design` |
| `Sample size` | `sample_size`, and `n_cases`/`n_controls` for a case-control study only |
| the post-split bucket | `store_key`: `dense__pmid-39024449__European`, `__African`, `__Hispanic-or-Latin-American`, `__East-Asian`, `__Multi-ancestry` |

`META` is deliberately imprecise: the cross-ancestry bucket has no single
super-population, so its `source_superpopulation` is blank and it never maps
to one. The adapter writes the post-split `store_key` rather than the legacy
candidate key that folded META into the European bucket, so five Selections
can each name one bucket on a canonical column.

Counts are validated by study design and absence is never a zero (#290 review):
a case-control Analysis must record both counts in the deposit and in the
mapping, and the two must agree; a quantitative Analysis must record neither,
and a non-zero count there is refused. A count the deposit does not record is
absent -- `n_cases`/`n_controls` stay blank -- never written as `0`. Every other
deposit value that disagrees with the mapping is refused, and so is an unknown
ancestry in a member name, as the GWAS Catalog adapter does (#133, #290).

### SomaScan target annotation

`src/manifest_generation/providers/gwas_catalog/somascan_targets.py` adds
target annotation to the GWAS Catalog's table for a SomaScan pQTL collection
(#300). It writes the table's rows for the Analyses the tracked target table
(`resources/reference-resources/somascan/sun-2018-analysis-targets.tsv`)
names, with their target columns: `target_assay_id`, the source's own SeqId;
the gene and span when the mapped rows name one gene, none when they name
several (a complex such as AMP kinase) or none; `target_mhc`;
`target_is_aggregate` when SomaLogic's `somascan-targets.tsv` flags the assay
(`somascan_is_multiple`) or the trait label lists several member symbols;
`target_resolution_method`; and `target_rows`, the mapped rows counted.

### The FinnGen adapter

`src/manifest_generation/providers/finngen/source_metadata_adapter.py` converts
FinnGen's published `finngen_R13_manifest.tsv` -- one row per endpoint, with
`phenocode`, `phenotype`, `category`, `num_cases`, `num_controls` -- into the
canonical table (#288). Its classification is the retired R metadata
resolver's, and it keeps it exactly:

| Provider column | Canonical column |
|---|---|
| `phenocode` | `source_analysis_id`, and `source_trait_id` |
| `phenocode` | `analysis_id`, as `finngen-r13-<phenocode>` |
| `category` | `analysis_group_id` |
| `phenotype` | `source_trait_label` |
| `category`, `num_cases`, `num_controls` | `study_design`, `sample_size`, `n_cases`, `n_controls` |
| -- (`Finnish` for every endpoint) | `source_ancestry_label`, and its `source_label_map.tsv` translation as `source_superpopulation` |

Its translations, and only these:

- **Endpoint classification.** `category` equal to `Quantitative endpoints`
  (trimmed, case-insensitively) is a `quantitative` endpoint whose total
  `sample_size` is `num_cases`; the inverse-rank-normalised traits are
  standardised over the whole cohort, so any `num_controls` upstream reports on
  such a row is not a case count and is not carried. Any other category with a
  positive `num_cases` and a positive `num_controls` is `case-control`, with
  both counts and their sum as `sample_size`. Anything else -- a quantitative
  endpoint with no positive `num_cases`, any other endpoint without both
  positive counts, a non-numeric count -- is refused by phenocode, so an
  endpoint the manifest cannot describe honestly fails the adapter rather than
  shrinking the release.
- **No declared effect scale.** The provider manifest has none, so the release
  config's `defaults.by_study_design` tiers decide `stored_effect_scale`, and
  the quantitative tier says `declared_standardised` for the IRN traits.
- **Collection-wide facts** -- the GRCh38 build, the FinnGen public-data
  licence and the FinnGen consortium -- are not written; the release config's
  defaults supply them (#287).
- **Padding** is trimmed, as the GWAS Catalog adapter trims it.
- **The Source Ancestry Label** `Finnish` is carried verbatim and translated
  through the tracked map, so `source_superpopulation` is `EUR`; a map without
  that label refuses it by name rather than leaving the cell blank.

Example:

```text
source_analysis_id  source_trait_label                    source_trait_id     source_ancestry_label  source_superpopulation  study_design    sample_size  n_cases  n_controls
BMI_IRN             Body mass index, inverse-rank norm…   BMI_IRN             Finnish                EUR                     quantitative    362216
AB1_ACTINOMYCOSIS   Actinomycosis                         AB1_ACTINOMYCOSIS   Finnish                EUR                     case-control    433889       132      433757
```

## Acquisition Manifest

What acquisition produced for a Source Collection: one row per Analysis it
acquired a file for, or tried to and could not. It records availability, never
intent. A row with a non-ready `status` records *why* acquisition has no usable
file for that Analysis -- an explained absence (see Freeze).

| Column | Required | Description |
|---|---:|---|
| `source_analysis_id` | Yes, every row | The Analysis the file was acquired for: its claim. Unique. |
| `status` | Yes, every row | The readiness vocabulary of `source_inventory.py`: `ok` and `already_present` are ready; `missing_remote_harmonised_yaml`, `header_rejected`, `metadata_rejected`, `data_absent_upstream`, `data_failed`, `yaml_failed`, `dry_run` and `error` record why there is no usable file. An unknown status is refused. |
| `location` | Column yes; value on every ready row | The exact local path acquisition wrote. Becomes the inventory's `data_file`. |
| `bytes` | Column yes; value on every ready row | The file's size in bytes, with no leading zero. Becomes `data_bytes`. On a refused row it is the size the run holds the Analysis to (see the FinnGen writer below). |
| `sha256` | Column yes; value on every ready row | The file's lower-case sha256. On a refused row it is the digest the run holds the Analysis to, so the refusal does not erase its own pin. |
| `source_url` | No | Where the file was fetched from. Becomes `data_url`. |
| `sidecar_location`, `sidecar_url`, `sidecar_bytes` | No | A per-Analysis metadata file acquisition fetched with it (GWAS-SSF `-meta.yaml`). Become `yaml_file`, `yaml_url`, `yaml_bytes`. |
| `error` | No | The acquisition error, when there was one. |
| `original_location`, `original_sha256`, `reformat_tool` | No; all three or none | Lineage of a file acquisition changed: the original's path and sha256 and the tool, with its version, that changed it. Only on a ready row. Acquisition changes a file in two cases: a structural change no reader or Column Map can cope with, such as splitting one file into one per Analysis, and filtering it to a release's region policy (#300, below). |
| `original_bytes` | No; only with lineage | The original's size. It stands in for `original_sha256` only for an original filtered before acquisition checksummed it, and since deleted: Chen 2023's files, filtered by the retired R generator, whose `filter_summary.tsv` recorded each original's size and no checksum. |
| `region_policy` | No; only with lineage | The region policy a region-filtered file was filtered to: one JSON object with sorted keys and no spaces, naming `region_policy_id` and `association_coverage` (`signals_only` or `cis_plus_signals`), `trans_flank_bp` and `suggestive_lead_min_distance_bp` (whole bases), `significant_p` and `suggestive_p` (in (0, 1]), the QC panel's `resource_id` and `sha256` (every policy keeps one, ADR 0036), and for a cis policy `cis_flank_bp` and the `target` span used (`chromosome`, `start`, `end`). A policy missing any of them, or a cis flank or target on a signals-only one, is refused. |

A duplicate `source_analysis_id` is refused, and so is a file -- a `location`
or `sidecar_location` -- claimed by two rows.

**One row per Analysis is one file per Analysis.** A file several Analyses
share, such as eQTLGen's BESD file set, cannot be recorded today: it is refused
as a duplicate file. #291 extends the manifest when eQTLGen moves onto the
pipeline.

### Region-filtered acquisition

A release whose config declares a `region_policy` ([release config](release-config.md#region_policy))
is acquired filtered, for scale (ADR 0036, clarified by #300):

```sh
python3 -m manifest_generation.region_filtered_acquisition \
  --config <release config> --originals <originals' Acquisition Manifest> \
  --out-dir <filtered files> --out <Acquisition Manifest> [--workers N] [--keep-originals]
```

It reads the Acquisition Manifest of the downloaded originals, checks each
original's sha256 against it, filters the original with the shared region
filter (`manifest_generation.region_filter`) into `<out-dir>/<id>.filtered.tsv.gz`,
records the filtered file's location, size and sha256 with its lineage
(`original_location`, `original_sha256`, `reformat_tool`, `region_policy`), and
syncs the manifest. Only then does it delete the original: a failure to record
leaves the original in place. An Analysis the originals' manifest records as
unavailable stays an explained absence. A re-run resumes only a run that is
this one: before anything is skipped, filtered or deleted, the plan and the
summary beside the manifest (below) must both be present with the sha256s its
run record binds, and every Analysis the output manifest records must still
be a ready original of this run, filtered
from that original (location and sha256), to this run's region policy, QC
panel and target, into a filtered file that still has its recorded size and
sha256. Any difference refuses the run, naming each Analysis, and deletes
nothing; otherwise an original an interrupted run left behind is deleted.
`--keep-originals` only reads the originals and never deletes one: for
originals a built release still cites, such as Sun 2018's (OGS-00007), so the
lineage names them where they are kept. A cis policy refuses the run if an Analysis has no
target span in the Source Metadata Table.

The filter is the retired R `gwas-ssf-ragged` generator's, reproduced row for
row: every row within `cis_flank_bp` of the target (cis only); every row within
`trans_flank_bp` of a row outside the cis window with `p_value <= significant_p`,
the flanks merged; of the other rows with `significant_p < p_value <= suggestive_p`,
the strongest first, each kept only when every lead already kept on its
chromosome is more than `suggestive_lead_min_distance_bp` away (equal p-values in
file order); and every row at a QC-panel position, whatever its p-value, so
Analysis Resolution can still measure ancestry and phenotype SD. A row with no
position or p-value is dropped. Kept rows are the source's own lines, with its
header: the filter selects and never reformats.

Before a filtered file's row is recorded, its plan and counts go beside the
manifest, as the R filter recorded them, and the run record is rewritten to
bind them:

- `<manifest-stem>.sparse-regions.tsv`, one row per region kept, in R's
  columns (`analysis_id`, `region_id` = `<id>__<cis|sig_trans|suggestive|qc_panel>__<nnn>`,
  `region_kind`, `chromosome`, `start`, `end`, `source_genome_build`,
  `target_id`, `target_label`, `lead_variant_id`, `pvalue_threshold`,
  `n_variants_retained`, `region_policy_id`): the cis window and each merged
  significant-trans range with the rows kept in it, each suggestive lead as a
  one-base region named by its `variant_id` (else `rsid`), and the QC-panel rows
  kept on each chromosome, first to last;
- `<manifest-stem>.filter-summary.tsv`, one row per filtered file:
  `analysis_id`, `status`, `error`, `source_url`, `downloaded_bytes`,
  `input_rows`, `retained_rows`, `retained_fraction`, `cis_rows`,
  `significant_trans_regions`, `significant_trans_rows`, `suggestive_leads`,
  `qc_panel_rows`, `output_file`, `output_bytes`, `checksum_algorithm`,
  `checksum` -- R's columns without its per-run attempt and timing ones.

`<manifest-stem>.meta.yaml` records the run: the originals' manifest and its
sha256, the filter tool, the policy, and under `evidence` each of the two files'
`path` and `sha256`. Freeze checksums it into the inventory's provenance, and a
candidate carries both files' selected rows into its bundle
([schema](../release-metadata-schema.md)).

**A release reads only files filtered to its own policy** (#282 review).
Preflight refuses, before resolution, a frozen acquisition that is not the
release config's, and rendering refuses it again however the stages are run
(`region_filtered_acquisition.region_lineage_problems`). Every ready row of the
frozen Acquisition Manifest must record, as its `region_policy`, the config's
policy, its QC panel's `resource_id` with the sha256 the panel has now, and
for a cis policy the target span the frozen Source Metadata Table gives the
Analysis. The pass lineage the freeze bound must name both the plan and the
summary, each still with the sha256 its run record binds, and the summary must
cover every ready Analysis. A changed flank, threshold, QC panel or target
therefore refuses the files filtered under the old one, naming each Analysis
and what differs: their rows are the old policy's, so they are re-acquired
and re-frozen. A policy declared over files nobody filtered is refused the
same way, and a config with no `region_policy` refuses region-filtered files
and their evidence.

### Files the retired R generator filtered

OGS-00006 (Chen 2023) was built from files the R `gwas-ssf-ragged` generator
filtered, deleting each original.
`src/manifest_generation/providers/gwas_catalog/legacy_ragged_acquisition.py`
writes the Acquisition Manifest of its filtered files as they are, with
lineage from its `sidecars/filter_summary.tsv`: `original_location` (the URL R
downloaded), `original_bytes` and no `original_sha256` (R recorded none), R's
policy, and the tool. A file whose sha256 is the one R recorded is the R
generator's output. One whose sha256 differs is accepted only when the
reviewed list of re-materialisations
(`resources/generators/gwas-catalog-ragged/ogs-00006-rematerialised-2026-09-15.tsv`)
names its (recorded, on-disk) sha256 pair -- the 198 files
`materialise-gwas-ssf-ragged.R` re-created from `sparse_regions.tsv` on 15 Sep
2026, each holding R's rows plus the multi-allelic siblings of some of its
leads -- and its tool then says so. Any other difference is refused, naming
the Analysis, and nothing is written. Its run record checksums the list and,
as `evidence`, the bundle's own `sparse_regions.tsv` and `filter_summary.tsv`,
which a regenerated candidate carries unchanged.

### Pass lineage

An acquisition writer may record the passes a manifest was written from in
`<manifest-stem>.meta.yaml` beside it (`acquisition_manifest.pass_lineage_path`).
Freeze checksums that file into the inventory's provenance when it exists.

The GWAS Catalog's writer,
`src/manifest_generation/providers/gwas_catalog/write_acquisition_manifest.py`,
overlays the EBI downloader's per-pass status manifests -- earliest first; a
later pass wins for every `analysis_id` it covers and may not regress a ready
Analysis -- and records each pass's path, sha256, size, row count, readiness
counts and overrides there. It drops the download manifests' study columns
(intent, the table's job) and per-run `seconds`.

Example (abridged):

```text
source_analysis_id  status                          location                                          bytes     sha256     source_url                                    sidecar_location                          sidecar_bytes
GCST000028          ok                              /data/…/17463246-GCST000028-EFO_0001360.h.tsv.gz  35279386  fd598567…  https://ftp.ebi.ac.uk/…/…EFO_0001360.h.tsv.gz  /data/…/…EFO_0001360.h.tsv.gz-meta.yaml  811
GCST000803          missing_remote_harmonised_yaml
```

### The FinnGen acquisition writer

`src/manifest_generation/providers/finngen/acquire.py` has two interfaces. The
canonical one is the writer a release config's connectors name:

```sh
python3 -m manifest_generation.providers.finngen.acquire \
  --manifest <provider endpoint manifest> --source-dir <dir> --out <acquisition manifest>
```

It fetches every listed endpoint's file into `--source-dir` (resumable: `.part`
files and HTTP Range; a complete file is checksummed and kept, never refetched)
and writes the manifest above, plus `<manifest-stem>.meta.yaml` pass lineage. An
endpoint upstream no longer serves is `data_absent_upstream`; a failed transfer
is `data_failed`; the manifest is written either way and the run exits non-zero,
so an unattended retry reuses every completed file.

The second is a **compatibility mode** the R era used, because its generator
emitted a working bundle before acquisition ran; it stays because the recorded
receipts of OGS-00003 and OGS-00017 cite it (#288):

```sh
python3 -m manifest_generation.providers.finngen.acquire --release-dir=<dir>
```

It reads `<dir>/analyses.tsv`, fetches each row's `source_url` into its
`source_file` with the same downloader, writes the rows back with `checksum` and
`size_bytes` filled, writes `<dir>/sidecars/downloads.tsv` with the download
evidence, merges `checks.files` into `<dir>/validation.yaml`, and records the
same acquisition as the shared Acquisition Manifest at
`<dir>/acquisition-manifest.tsv`. The two interfaces are exclusive: combining
`--release-dir` with `--manifest`, `--source-dir` or `--out` is refused.

**A recorded digest is identity evidence.** In the canonical mode, the manifest
at `--out` is the pin, read from every row that carries a digest whether that row
is ready or refused. An existing file whose bytes no longer match is refused by
name -- status `data_failed`, `location` empty, the error naming the Analysis and
both digests -- and never re-hashed into a fresh row. A fresh download is held to
the same pin and refused *before* promotion if it does not match. A refused row
keeps the pin in `bytes` and `sha256`, so the next retry is held to it rather
than accepting the bytes the previous run refused; only a ready row's `bytes` and
`sha256` describe a file on disk. In compatibility mode a row's own `checksum` is
the pin, as the R emitter leaves it blank on a fresh emit: a mismatch refuses the
bytes before promotion, leaving the staged `.part` file and the recorded checksum
untouched.

### The dbGaP MVP acquisition writer

The dbGaP MVP acquisition
(`src/manifest_generation/providers/dbgap/mvp/acquire_dbgap_phs002453.py`,
`manifest`) writes one manifest for the whole Source Collection, not one per
candidate store key (#290). A mapped Analysis whose converted file is absent
is an explained absence with status `data_absent_upstream` and the path it
would have had. `--recorded-manifests` lets it reuse the sha256 the
acquisition recorded while converting, re-checking each file's size, so a
metadata-only reproduction does not re-read the corpus; without it the writer
hashes the files itself.

Every ready row carries the reformatting lineage ADR 0036 requires (#290
review). The original is a member of a published tar, so `original_location` is
`<tar path>#<member>`; `original_sha256` and `original_bytes` are the extracted
member's own, cross-checked against the deposit's table of contents (the tar
itself is deleted after extraction, so only its recorded md5 survives, and it
identifies the container rather than these bytes); and `reformat_tool` names
the code that actually rewrote the file, never the code writing the manifest
(#290 review). `convert` records its own `module_sha256` and the executing
commit in a receipt under `GIA/convert-receipts/`, which a later `manifest` run
reads; for files acquired before that, `--conversion-history` reads a
conversion run's own record (`parent-run-summary.json`'s `code_sha` and
`refresh-receipt.tsv`'s per-Analysis action), so a file it converted carries
that commit and a file it only re-canonicalised carries
`sha256:unrecorded` with the evidence searched, because no conversion-time
record names that code. A rewritten file whose extracted original is gone is
refused by name rather than given an invented identity.

## Selection

A release config declares exactly one form in its `selection` block:

```yaml
selection:
  all: true                        # every row of the Source Metadata Table
```

```yaml
selection:
  analyses: [GCST000028, GCST90006908]   # an explicit list
```

```yaml
selection:
  where:                           # a rule: AND over canonical columns
    store_key: hybrid__European    #   a value is equality
    study_design: [quantitative, case-control]   # a list is membership
```

Any form may also carry `expect_rows: N`, the Selection's intended size. It is
a bound, not a filter: `freeze` refuses a Selection that takes a different
number of rows, by name, and preflight re-checks the frozen inventory against
it. It is recorded in the provenance with the rule, so a changed bound is a
reviewed re-freeze under a new snapshot id (#290).

Rule values are strings or integers, compared with the cell's text; a blank
cell matches nothing. A rule column must be canonical and carried by the table.
Refused: a list naming an Analysis the table does not intend or naming one
twice, a Selection that selects nothing, two forms at once, and an empty
Selection. There is no negation, range or OR: anything a rule cannot say is
said with a list.

**The legacy grammar.** Today's generator configs carry a `selection` block in
an older grammar (`store_key`, `ancestry_group`, `study_design`,
`n_analyses`, `mode`, `pubmed_id`, `binary_count`, `probe_ids`,
`fail_if_target_unresolved`). It is recognised and refused by name, never read
as a rule: #287 migrates every release config to the forms above.

## Freeze

```sh
python3 -m manifest_generation.freeze \
  --source-metadata-table T --acquisition-manifest M \
  --selection CONFIG --snapshot-id ID --source-collection-id C --out-dir DIR
```

reads only the two connector files and the Selection. It lists no directory
and opens no source file, so an unrecorded file on disk changes nothing. It
fails (`ReconciliationError`) on:

- a selected Analysis with no Acquisition Manifest row -- no file and no
  recorded reason there is none;
- an Acquisition Manifest row no Analysis in the table claims;

and a duplicate Analysis or file is refused when either connector loads.

A selected Analysis whose row has a non-ready `status` is an **explained
absence**: it stays in the inventory as a non-ready row with its `error`, as
every Source Inventory has kept unavailable inputs, and never becomes a release
member. A row claimed by an Analysis the Selection leaves out is counted, not
frozen.

It writes four files into `DIR` and never overwrites any of them -- a
re-freeze is a new snapshot id:

| File | Content |
|---|---|
| `ID.tsv` | The Source Inventory: one row per selected Analysis, sorted by `analysis_id`, in the inventory columns. `publication_pmid`, `trait`, `study_design` and `sample_size` come from the table, the rest from the manifest. |
| `ID.source-metadata.tsv` | The frozen Source Metadata Table: the selected rows, verbatim, in the original's columns. |
| `ID.acquisition-manifest.tsv` | The frozen Acquisition Manifest: the selected Analyses' rows, verbatim, in the original's columns. |
| `ID.meta.yaml` | The provenance, written last. |

Later generation reads the frozen connectors, never the originals:
`freeze.load_frozen_connectors(ID.meta.yaml)` checks each against its recorded
sha256 and loads it, and the frozen files alone reproduce the inventory.

### Provenance

The sidecar keeps the counts preflight re-checks, computed by
`source_inventory.inventory_counts` for both freezes, and records:

| Key | Description |
|---|---|
| `snapshot_id`, `source_collection_id`, `frozen_at` | Identity. |
| `frozen_by` | `manifest_generation.freeze`. |
| `selection` | The Selection declaration, as written in the release config. |
| `rows`, `readiness_counts`, `ready_rows`, `ready_bytes`, `study_design_counts`, `ready_study_design_counts`, `duplicate_content_groups` | The inventory's counts. Duplicate-content groups -- ready Analyses whose files share one sha256 -- are reported for review, never collapsed. |
| `inventory_tsv_sha256` | Binds the sidecar to `ID.tsv`. |
| `source_metadata_table.frozen` | `path` (relative to the sidecar), `sha256`, `bytes`, `rows` of the frozen table. |
| `source_metadata_table.original` | `path`, `sha256`, `bytes`, `rows` of the table freeze read. A `path` inside the checkout is recorded relative to it, so a committed snapshot names the same file in every checkout (`freeze.recorded_path`, #300); any other is absolute. |
| `acquisition_manifest.frozen` | The same, for the frozen manifest. |
| `acquisition_manifest.original` | `path`, `sha256`, `bytes`, `rows`, `readiness_counts` of the manifest freeze read, and `pass_lineage`: the `path` and `sha256` of its pass lineage, or null when it has none. |
| `acquisition_manifest.unselected_entries` | Rows claimed by Analyses the Selection leaves out. |
| `lineage` | One entry per selected reformatted file: `analysis_id` and its lineage. |

A release config drives this freeze (`pixi run inventory-freeze --config
<release config>`, or the candidate entry point's `freeze` stage, #287): it
freezes the config's Selection into `source_collection.inventory.directory`
and, when the snapshot already exists, writes nothing and refuses one frozen
for another Source Collection or Selection. The candidate-table freeze this
replaced is gone (#287); the inventories it wrote stay as history. The ukb-b
reconciliation goes in #289.

Preflight (`pixi run preflight --config <release config>`) requires a ready
row's `location`, `bytes` and `sha256`. The Acquisition Manifest makes
`source_url` and the sidecar columns optional, so a ready row without a source
URL is a preflight warning (its `analyses.tsv` `source_url` is blank), and a
ready row with no recorded sidecar is counted, not failed. A recorded sidecar
must still exist, and a build it declares (`genome_assembly`, as a GWAS-SSF
`-meta.yaml` does) must be the release's: the Analysis's `source_genome_build`,
else the release config's default. A sidecar declaring no build is counted.
Harmonisation is not a preflight requirement: the GWAS Catalog downloader gates
it at acquisition and records a failure as `metadata_rejected`.

### Preflighting several Selections together

`pixi run preflight --config A --config B ...` preflights each release config,
then checks that the Selections of their one Source Collection partition it:
they name the same collection, no Analysis is selected twice, and each
Selection's declared `expect_rows` matches its frozen inventory. The retired
dbGaP MVP partition proved exactly this for its five ancestry buckets (#290);
the check now follows the Selections rather than a written-out partition.

Completeness is proved against the collection, not against the Selections
(#290 review): the command reads the Source Collection's checksummed intent --
the whole-collection Source Metadata Table and Acquisition Manifest the frozen
snapshot's provenance records -- and fails, naming each one, on an Analysis no
Selection covers and on a selected Analysis the collection does not intend. An
`expect_rows` bound cannot see a row that has been added to the collection, and
a set of Selections that is merely disjoint is not a partition; a preflight
that cannot read those originals fails rather than reporting a partial result
as complete.

## Pilot prefixes

Pilot configs selected a deterministic prefix with `n_analyses`. The Selection
grammar has no such form: a migrated pilot writes the prefix out as an
`analyses` list ([release config](release-config.md#pilot-prefixes), #287).
