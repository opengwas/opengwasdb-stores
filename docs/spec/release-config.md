# Release config

The one connector file written per Store Release (#287).

Governed by [ADR 0036](../adr/0036-manifest-generation-is-one-pipeline-fed-by-source-connectors.md):
a source plugs into Manifest Generation through four files. Three belong to the
Source Collection -- the Acquisition Manifest, the Source Metadata Table and the
Column Map ([source connectors](source-connectors.md)) -- and the fourth, the
release config, belongs to one Store Release. It names everything the shared
pipeline needs that is not the Source Collection's: the release's identity, the
Selection, the Build Recipe shape, the release policy and its Quality Check
overrides, the Reference Resources, and the collection-wide defaults.

| Contract | Owner |
|---|---|
| Schema and loader | `src/manifest_generation/release_config.py` (`load_release_config`) |
| Selection grammar | `src/manifest_generation/selection.py` ([spec](source-connectors.md#selection)) |
| Quality Check names and thresholds | `src/manifest_generation/quality_checks.py` |
| Indexed Variant Subset default | `src/manifest_generation/indexed_subsets.py` |

Loading is a gate. An unknown key at any level, a missing required field, a
value outside its vocabulary, a Reference Resource reference that names no
declared resource (or one of the wrong kind), and the legacy Selection grammar
are refused, each by name. Nothing is ignored and no required fact is
defaulted. A relative path is relative to the repository root.

A config states `release_config_version: 1`. A file without it predates this
schema: the generator configs that produced the committed bundles
(`config-full.yaml`, `config-pilot-10.yaml`, ...) are kept unchanged as the
history their bundles' `generator.commands` cite, and are refused here by that
key.

## Keys

| Key | Required | Description |
|---|---:|---|
| `release_config_version` | Yes | `1`. |
| `label` | Yes | Source-natural release name, written to `release.yaml`. Never an identifier. |
| `access_posture` | No | `public`, `controlled` or `embargoed`; `public` only when the key is absent, so an explicit `false` or blank is refused. |
| `description` | Yes | Prose for `release.yaml`. |
| `notes` | No | Prose; the candidate's own evidence notes follow it in `release.yaml`. |
| `source_collection` | Yes | The Source Collection and its files (below). |
| `selection` | Yes | The Selection, in #283's grammar: exactly one of `all: true`, `analyses: [...]` or `where: {...}`. |
| `build` | Yes | The Build Recipe shape `build.yaml` carries (below). |
| `indexed_subsets`, `indexed_subsets_opt_out` | No | Dense and Hybrid releases declare the maintained `hm3` Indexed Variant Subset by default; `[]` with a reason opts out; a Ragged release declares none (stores #210). |
| `defaults` | Yes | Collection-wide Analytical Metadata defaults and the method tiers (below). |
| `release_policy` | Yes | Membership policy inputs (below). |
| `quality_checks` | No | Quality Check threshold overrides: a registered check's name to its threshold for this release (#285). |
| `reference_resources` | Yes | The Reference Resources the run uses (below). |
| `ancestry_assignment` | Yes | What Analysis Resolution measures ancestry against (below). |
| `effect_scale_validation` | No | Phenotype-SD evidence settings (below). |
| `trait_ontology_mapping` | Yes | The Canonical Trait Mapping Table (below). |
| `runtime` | No | `cores` (default 1, capped at 64 by preflight) and `min_free_gb` (default 0). |
| `output` | Yes | `work_root`: the root of the Store Release's Work Root, absolute or repository-relative like every path here. The resolution root is deployment configuration and `output.resolution_root` is refused (#284). |

### `source_collection`

| Key | Required | Description |
|---|---:|---|
| `source_collection_id` | Yes | One path segment; names the Resolution Area. |
| `connectors.source_metadata_table`, `connectors.acquisition_manifest` | No; both or neither | The connector files freeze reads. Absent when the config names only an already frozen inventory. |
| `inventory.snapshot_id` | Yes | The frozen Source Inventory's snapshot id. |
| `inventory.directory` | Yes | Where it is: `<directory>/<snapshot_id>.tsv`, `.meta.yaml` and the two frozen connector files ([freeze](source-connectors.md#freeze)). |

The pipeline's `freeze` stage writes the inventory from the connectors when it
does not exist, and otherwise checks that the existing one was frozen for this
Source Collection and this Selection. A frozen inventory is never overwritten:
a re-freeze is a new snapshot id.

### `build`

`layout`, `completion_state`, `command` and `post` are required, `options` is
not. `command` names an `opengwasdb` subcommand and `options` are its flags,
passed through verbatim (ADR 0023): `options` is opaque by design, and its keys
are not checked here. `post` is the registry's own vocabulary -- `top_hits`,
`overview`, `rho` and `validate`, each `true` or `false`, the steps
`ogstores.plan` plans -- so an unknown key or a non-boolean is refused. An
`indexed_subsets` entry takes only the Release Bundle contract's keys
(`name`, `resource_id`, `variant_list`, `reference_assembly`, `checksum`,
`options`). The recipe carries no artifact root
(#126). Manifest Generation reads two options itself:
`source-reader-capability` (the reader the resolver uses; default
`opengwasdb.gwas-ssf`) and `variant-reference` (the Variant Reference the
resolver measures overlap against).

### `defaults`

| Key | Required | Description |
|---|---:|---|
| `by_study_design` | Yes | One method tier per `study_design`, each with `stored_effect_scale`, `original_effect_scale`, `original_sd_method` and `sample_size_kind`. `stored_effect_scale`, `original_sd_method` and `sample_size_kind` must be in OpenGWASDB's vocabularies (`opengwasdb.model.enums`, imported, not copied); `original_effect_scale` has no closed vocabulary. A ready Analysis whose design has no tier fails preflight. |
| `source_genome_build` | No | `GRCh37` or `GRCh38`. |
| `license`, `publication_doi`, `consortium` | No | Text. |
| `sample_size_scope` | No | OpenGWASDB's `SampleSizeScope`; default `analysis_level`. |
| `analysis_group_id` | No | Default blank. |
| `info_score_threshold` | No | The INFO floor in [0,1] a declared Analysis requests; default 0.6 (#175). |
| `maf_threshold` | No | The MAF floor in [0,0.5] each Analysis requests; absent is no MAF filter (#176). |

`source_genome_build`, `license`, `publication_doi` and `consortium` are
collection-wide facts: an Analysis whose Source Metadata Table row carries a
value keeps it, and one whose cell is blank takes the default. A metadata
adapter never writes a collection-wide fact into the table (#283).

### `release_policy`

| Key | Required | Description |
|---|---:|---|
| `selection_superpopulation` | Yes | The super-population code (`AFR` `AMR` `EAS` `EUR` `MID` `NAF` `SAS`) the Selection expects. The `assigned_ancestry` Quality Check tests each Assigned Ancestry against it; it never moves an Analysis (ADR 0036). |
| `store_composition.require_maf_filtered` | No | The Store Composition Rule (ADR 0033). Needs a positive `defaults.maf_threshold`. |
| `trust_source_ancestry_without_af` | No | `true` opts in to #287's decision for sources with no allele frequencies (Sun 2018): an Analysis whose Resolution Record shows ancestry unmeasurable for that reason takes its Source Metadata Table `source_superpopulation` as Assigned Ancestry (`source_trusted_no_af`), a warning-level `assigned_ancestry` result. The record shows it only under a MAF request, so the opt-in needs `defaults.maf_threshold` (0 filters nothing): the resolver then counts every row without a usable frequency (`maf_rows_missing`), and absence is every row it read (`canonical_rows_observed`) with no ancestry site. Default `false`: such an Analysis is `ancestry_unassigned`. A measured ancestry never takes the source label, and frequencies the fit could not read (palindromic sites, unusable standard errors) are not absent. |
| `imputation_score_declarations` | No | The reviewed per-Analysis INFO score declarations (#175). The table is the Source Collection's, so Selections may share it: a declaration may name an Analysis outside the Selection when the collection intends it (the original Source Metadata Table the snapshot's provenance checksums), and only the Selection's apply. A declaration the collection does not intend is refused, and so is one outside the Selection when that table is not on the host. |
| `maf_filter_exempt_genotyping_technologies` | No | Technologies exempt from the MAF floor (#176). |
| `maf_filter_exempt_analyses` | No | The reviewed per-Analysis MAF-floor exemptions (#176). |
| `effect_placeholder_exclusions` | No | The reviewed effect-placeholder listing the `effect_placeholder_rows` Quality Check reads (#285). |

### Reference Resources

`reference_resources` lists each resource the run uses: `resource_id`, `kind`
and `location` are required; `location_kind`, `version`, `status`,
`fine_group_map`, `ancestry`, `super_populations`, `genome_build` and
`variant_id_convention` are optional. A `resource_id` declared twice is
refused. Three blocks use them, by id:

- `ancestry_assignment.reference_resource_id` (required) names the ancestry
  mixture the resolver measures against: an `ancestry_mixture` resource, with a
  `fine_group_map`.
  `maf_floor` (default 0.01), `extraction_panel` (default none) and `gates`
  (`tau` 0.50, `delta` 0.20, `n_min` 5000, `residual_max` 0.06) are passed to
  the resolver as written.
- `effect_scale_validation.reference_resources` lists `{ancestry, resource_id}`
  reference-AF resources for phenotype-SD estimation. `maf_min`, `maf_max` and
  `dispersion_max` are recorded in `sidecars/sd_estimation.tsv`;
  `dispersion_max` (default 0.5) marks a high-dispersion estimate.
- `trait_ontology_mapping.reference_resource_id` (required) names the
  Canonical Trait Mapping Table, a `trait_ontology_mapping` resource whose
  `location` is its resource directory (#286).

Preflight requires every named resource to be present.

## Example

The OGS-00011 release (`resources/generators/gwas-catalog-eur-hybrid/release-eur-hybrid-full.yaml`),
abridged:

```yaml
release_config_version: 1
label: eur-hybrid-full
access_posture: public
description: >
  Every ready Analysis in the hybrid__European candidate pool of the EBI GWAS
  Catalog harmonised GWAS-SSF Source Collection, ...

source_collection:
  source_collection_id: gwas-catalog-ssf
  inventory:
    snapshot_id: gwas-catalog-ssf-eur-hybrid-2026-10-20
    directory: resources/inventories

selection:
  where:
    store_key: hybrid__European

build:
  layout: hybrid
  completion_state: observed_only
  command: build-hybrid
  options:
    variant-reference: /data/opengwasdb/reference/gnomad-v4-common-hg38/EUR-variants.txt.gz
    n-workers: 24
    checkpoint: true
    source-reader-capability: opengwasdb.gwas-ssf
    source-assembly: hg38
  post:
    top_hits: false
    overview: true

defaults:
  source_genome_build: GRCh38
  license: EBI GWAS Catalog terms of use
  sample_size_scope: analysis_level
  info_score_threshold: 0.6
  maf_threshold: 0.005
  by_study_design:
    quantitative:
      stored_effect_scale: sd
      original_effect_scale: sd
      original_sd_method: estimated_from_source_maf
      sample_size_kind: total
    case-control:
      stored_effect_scale: log_or
      original_effect_scale: log_or
      original_sd_method: binary_trait
      sample_size_kind: case_control

release_policy:
  selection_superpopulation: EUR
  store_composition:
    require_maf_filtered: true
  imputation_score_declarations: resources/generators/gwas-catalog-eur-hybrid/score-declarations.tsv
  maf_filter_exempt_genotyping_technologies:
    - Whole genome sequencing
    - Exome-wide sequencing
  maf_filter_exempt_analyses: resources/generators/gwas-catalog-eur-hybrid/maf-filter-exemptions.tsv
  effect_placeholder_exclusions: resources/generators/gwas-catalog-eur-hybrid/effect-placeholder-exclusions.tsv

reference_resources:
  - resource_id: ukb-ancestry-mixture-hg38
    kind: ancestry_mixture
    location: /data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz
    fine_group_map: /data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv
  - resource_id: canonical-trait-mapping-efo
    kind: trait_ontology_mapping
    location: resources/reference-resources/canonical-trait-mapping-efo

ancestry_assignment:
  reference_resource_id: ukb-ancestry-mixture-hg38
  maf_floor: 0.01
  extraction_panel: null
  gates: {tau: 0.50, delta: 0.20, n_min: 5000, residual_max: 0.06}

effect_scale_validation:
  maf_min: 0.01
  maf_max: 0.5
  dispersion_max: 0.5
  reference_resources: []

trait_ontology_mapping:
  reference_resource_id: canonical-trait-mapping-efo

runtime:
  cores: 64
  min_free_gb: 50

output:
  work_root: /data/opengwasdb/work/gwas-catalog-eur-hybrid
```

## From a pre-#287 generator config

| Pre-#287 key | Release config |
|---|---|
| `source.source_collection_id` | `source_collection.source_collection_id` |
| `source.inventory.{snapshot_id,path,provenance_path}` | `source_collection.inventory.{snapshot_id,directory}` |
| `source.inventory.freeze_inputs`, `source.candidates` | Gone: acquisition's passes become the Acquisition Manifest, and the candidate table becomes the Source Metadata Table, before freeze (`source_collection.connectors`). |
| `source.store_key`, `source.ancestry_group`, a legacy `selection:` block | `selection` in #283's grammar, and `release_policy.selection_superpopulation` |
| `source.imputation_score_declarations`, `source.maf_filter_exempt_*`, `source.effect_placeholder_exclusions` | `release_policy.*` |
| `store_composition` | `release_policy.store_composition` |
| `defaults.ancestry_assignment_method` | Gone: the method is the release policy's outcome, never a default. |
| `ancestry_assignment.enabled`, `effect_scale_validation.enabled` | Gone: every Analysis is resolved (ADR 0036). |
| `effect_scale_validation.{min_overlap_variants,sd_tolerance,warning_multiplier}` | Gone: read by no Python code; the resolver owns the estimate. |
| `release_kind`, `association_coverage` | Gone: read by no code (ADR 0029). |
| `--canonical-trait-mapping-table` | `trait_ontology_mapping.reference_resource_id` |

## Pilot prefixes

A pre-#287 pilot selected a deterministic prefix with `n_analyses` ("the first
10 case-control Analyses of the pool"). The Selection grammar adds no prefix
form: a migrated pilot writes the Analyses out as an `analyses:` list. A prefix
depends on the order of the Source Metadata Table, which a re-run metadata
adapter may change, so it would select different Analyses from the same
config; a list selects the same ones until a reviewer edits it, and the
reviewer sees exactly which.
