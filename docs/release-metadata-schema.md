# Release Bundle schema

A Store Release is defined by a small, self-contained **Release Bundle**: one
directory per release under `stores/`, whose files record identity, membership,
the Build Recipe, and (once built) validation evidence. The bundle is the
output of Phase B and the fixed input of Phase A; see
[`docs/spec/store-release-workflow.md`](spec/store-release-workflow.md) for the
workflow that consumes it and the ADRs it is governed by.

`bundle.check()` in `src/ogstores/bundle.py` is the **executable** definition of
a valid Release Bundle. It accumulates every registry-side error it can find
and never raises for invalid bundle content, so an author can run it and fix
everything in one pass:

```sh
pixi run bundle-check        # discovers and checks every stores/<id>/ bundle
```

A Release Bundle structure is:

```text
stores/OGS-00042/
  release.yaml      identity, lineage, status, source snapshot, generation log, prose
  build.yaml        the recipe: an opengwasdb subcommand and its flags
  analyses.tsv      one row per Analysis; opengwasdb owns the shared schema
  summary.yaml      generated review view derived only from analyses.tsv
  validation.yaml   evidence, written back by the register step
  sidecars/         optional generated evidence (ancestry, SD, sparse regions)
```

The `store_id` is the opaque `OGS-` identifier (ADR 0022). It is the directory
name here, the directory name under the artifact root, and the `release_id`
written into the built Store's manifest. There is no Store Family tier: the
`OGS-` id is the only identifier (ADR 0028).

Large Release Artifacts live outside this repository, at a path that is a pure
function of the identifier:

```text
<artifact-root>/OGS-00042/
  source/                   acquired or filtered source files
  work/                     checkpoints, scratch, logs
  work/analyses.tsv         derived build manifest (exclude_from_build rows dropped)
  records/<step>.json       one per executed step
  store.opengwasdb          the Store Release
  store.opengwasdb.partial  transient staged destination
<artifact-root>/by-label/   generated symlinks
```

The artifact root is **deployment configuration, not a bundle field**: a bundle
is immutable once accepted, so committing an absolute path into it would bind
that bundle to one machine. `paths.artifact_root()` resolves it, highest
precedence first, from a workflow `--config artifact_root=`, the
`OPENGWASDB_ARTIFACT_ROOT` environment variable, the tracked `ogstores.yaml`,
then the built-in default (`/data/opengwasdb/stores`). A Build Recipe must not
declare an `artifacts` block (issue #126).

## A minimal valid Release Bundle

Following only this document, the three files below produce a bundle that
passes `bundle.check()`:

```text
stores/OGS-99001/release.yaml
```

```yaml
store_id: OGS-99001
label: example-release
access_posture: public
status: candidate
created_at: '2026-01-01T00:00:00Z'
source_snapshot_id: example-source-2026-01-01
description: 'A minimal Release Bundle authored from the schema documentation.'
generator:
  commands:
    - Rscript resources/generators/example/generate.R --mode=emit
```

```text
stores/OGS-99001/build.yaml
```

```yaml
store_id: OGS-99001
layout: dense
completion_state: observed_only
build:
  command: build-dense-vcf
  options:
    source-reader-capability: opengwasdb.example
    source-assembly: hg38
post:
  top_hits: true
  overview: true
```

```text
stores/OGS-99001/analyses.tsv   (tab-separated; header plus at least one row)
```

```text
analysis_id	source_label	analysis_label	source_file	checksum	checksum_algorithm	source_genome_build	license	trait_ontology_mapping_method	assigned_ancestry	ancestry_assignment_method	original_effect_scale	original_sd_method	stored_effect_scale	sample_size_kind	sample_size_scope	sample_size
EXAMPLE-0001	Example trait	Example trait	/data/opengwasdb/example/source.tsv.gz	e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855	sha256	GRCh38	Example terms of use	unmapped	EUR	source_trusted_no_af	sd	declared_standardised	sd	total	analysis_level	1000
```

That covers every column this document marks Yes. A `candidate` release needs
no `validation.yaml`; a `built` or `validated` release does (see
[`validation.yaml`](#validationyaml)). A `reference_completed` release must also
declare `derived_from: OGS-00042` in `release.yaml` and use a `complete` block
instead of `build`.

## Pipeline stages

Phase B — what produces a bundle — is described here only in outline; the
canonical command log for each release is recorded in
`release.yaml:generator.commands`. A Manifest Generator conventionally has five
stages:

| Stage | Purpose | Typical inputs | Outputs |
|---|---|---|---|
| `discover` | Snapshot available upstream analyses and files. | Source inventory, GWAS Catalog studies table, source `meta.yaml`, provider APIs. | Raw candidate records and source-file locations. |
| `select` | Apply the Store Release's inclusion rules. | Candidate records, generator config, priority lists, publication/analyte filters. | Selected Analysis set for a proposed Store Release. |
| `derive` | Add authoritative analytical metadata and generated evidence. | Selected rows, provider metadata resolvers, source files, OpenGWASDB readers, reference resources, Ensembl, checksums. | Resolved `analyses.tsv` columns and sidecar evidence. |
| `emit` | Write a reproducible release bundle. | Resolved rows, sidecars, build choices, generator provenance. | `release.yaml`, `analyses.tsv`, `build.yaml`, `validation.yaml`, sidecars. |
| `accept` | Check that the bundle is complete enough to build. | Release bundle, schema checks, lightweight source/readability checks. | Updated `validation.yaml` and release status. |

Metadata resolution and summary-statistics reading are orthogonal concerns. A
Source Collection chooses a provider-specific metadata resolver for study tables,
APIs, or endpoint manifests, and separately declares an OpenGWASDB Source Reader
Capability for source data. The resolved metadata shape is common even when the
provider and source format differ.

## `release.yaml`

Registry identity and provenance. Never read by `opengwasdb`. Required for an
accepted Release Bundle; a candidate may leave lifecycle timestamps null.

The file trims to identity, lineage, Release Status, creation time, source
snapshot identity, the generation command log, and the prose that explains the
Store Release (issue #136, [ADR 0029](adr/0029-release-identity-file-trimmed.md)).
Issue #137 removed the one-time `migration` provenance block
([ADR 0030](adr/0030-artifact-path-is-the-identifier-migration-retired.md)); the
published artifact path is now a pure function of the identifier.

| Field | Required | Description |
|---|---:|---|
| `store_id` | Yes | Opaque `OGS-` 5-digit identifier (ADR 0022): the directory name under `stores/`, and must match it. |
| `label` | Yes | Source-natural display name; provenance, never an identifier (ADR 0022). |
| `access_posture` | No | Declared availability category: `public`, `controlled`, or `embargoed`. The one store-level fact the retired Store Family tier genuinely declared (ADR 0028); every committed bundle declares it, but it is descriptive rather than structurally required. |
| `status` | Yes | Registry lifecycle state: `candidate`, `accepted`, `built`, `validated`, `superseded`, or `withdrawn`. |
| `derived_from` | No | Parent `store_id` when this release derives from another release. Required for a Reference-Completed release, and the parent must be a registered Release Bundle. |
| `created_at` | Yes | Timestamp when the candidate bundle was generated. |
| `accepted_at` | No | Timestamp when the bundle was accepted as the release input record. |
| `source_snapshot_id` | Yes | Dated or provider-native source snapshot used for this release, for example a GWAS Catalog studies-table release date. |
| `source_snapshot` | No | Source snapshot identity detail. For a monolithic BESD release: `besd_prefix` and `source_genome_build` (issue #134 records this as a known integrity gap, not a checksum equivalent). For a provider-manifest release: `manifest_url`, `manifest_sha256`, `manifest_size_bytes`, `manifest_etag`, `manifest_last_modified`. |
| `generator` | Yes | How the bundle was produced; a mapping. |
| `generator.version` | No | Generator package version, git commit, or script hash. A value beginning `sha256:` must carry a valid 64-hex digest. |
| `generator.commands` | Yes | The **executed** command log: a non-empty list of non-empty strings, the commands that produced the bundle, in the order they ran. A log, not a prediction (issue #136). |
| `description` | Yes | Short human-readable release description. |
| `notes` | No | Free-text release notes. |

`bundle.check()` requires exactly the keys marked Yes above and rejects a
`generator` that is not a mapping or whose `commands` is empty. Release Status
transitions are a separate, optional check (`validate_status_transition`): the
legal lifecycle graph is `candidate -> accepted -> built -> validated ->
{superseded, withdrawn}` with `withdrawn` reachable from every state. See the
workflow specification's "Status lifecycle and transitions".

For a BESD build (`build-ragged-besd`), `source_snapshot.besd_prefix` is
required and must be a non-empty string; its referenced files are deliberately
not inspected, because monolithic BESD source identity is an unverified host
path prefix (issue #134, a known integrity gap).

## `analyses.tsv`

One row per Analysis, tab-separated. This is the membership record: the exact
set of Analyses in the release. The shared interpretation-bearing core schema is
owned by OpenGWASDB (ADR 0017); the registry may add registry-only columns
needed to locate source files, record checksums, carry licence/publication
provenance, or explain inclusion decisions. `bundle.check()` delegates the
shared contract to the pinned `opengwasdb.model.analyses` module and adds
registry-owned vocabularies on top.

Use empty strings for unknown optional values in TSV. Values are resolved after
applying generator config defaults, even when that repeats store-level metadata.

### Column classes

| Class | Owner | Where used | Examples |
|---|---|---|---|
| Shared core | OpenGWASDB | Release manifests and built stores. These columns carry interpretation-bearing Analysis metadata. | `analysis_id`, `analysis_label`, ontology fields, ancestry fields, effect-scale fields, sample-size fields, Attribution Metadata (`license`, `publication_doi`, `publication_pmid`, `consortium`, `first_author`). |
| Registry-only | Store registry | Release manifests only. These columns locate source inputs, record provenance, or explain inclusion. | `source_analysis_id`, `source_label`, `source_file`, `source_bundle_id`, `checksum`, `checksum_algorithm`, `size_bytes`, `analysis_group_id`, `inclusion_reason`, `exclude_from_build`, `trait_ontology_mapping_method`. |
| Store-only | OpenGWASDB | Built stores only. These columns are produced during or after the build and therefore do not appear in accepted release manifests. | `completed_against`, reference-completion quality rollups, store artifact diagnostics. |

Builders may ignore registry-only columns after using them to locate inputs.
Store-only columns must not be required before the build has run.
`exclude_from_build` is the exception that is enforced rather than merely
ignored: the registry materialises a derived build manifest with every excluded
row removed and points the builder at it, so `opengwasdb` never sees an excluded
row at all ([ADR 0025](adr/0025-registry-filters-excluded-analyses.md)).

`bundle.check()` requires these columns to be present in the header:

```text
analysis_id  stored_effect_scale  sample_size_kind  sample_size_scope
sample_size  original_effect_scale  original_sd_method
ancestry_assignment_method  assigned_ancestry
```

The first eight are OpenGWASDB's required set; `assigned_ancestry` is added by
the registry. `bundle.check()` structurally enforces exactly these header
columns, the retired-column list, and the ancestry, case/control-count, and
checksum rules below. The `Required` column in the reference table is the
broader authoring convention for a complete bundle — columns marked Yes that
`check()` does not itself demand (`analysis_label`, `source_label`, `source_file`,
`checksum`, `source_genome_build`, `license`, `trait_ontology_mapping_method`)
are what a reviewer and the pinned OpenGWASDB schema expect to see populated.
Every required column must also be non-empty on every row, except
that a `candidate` release and the two legacy trial releases `OGS-00001` and
`OGS-00002` may leave required Analysis values blank pending resolution (issue
#134; see "Blank required values" below).

`bundle.check()` **rejects** any column named by the pinned upstream
`RETIRED_ANALYSIS_COLUMNS` list: `phenotype_id`, `phenotype_label`, `trait_id`,
`gene_id`, `gene_name`. The upstream schema is the single retired-column
contract; the registry does not keep its own copy. A Trait Ontology Mapping
carries an ontology term and its trait label, never a gene identifier with an
authority name standing in for the label: gene/target identity is annotation
(`analysis_label` for a single-target Analysis; `trait_chr`/`trait_bp` and the
target sidecar for the target itself). `bundle.check()` rejects a gene- or
protein-shaped `trait_ontology_id` (Ensembl, HGNC, Entrez/NCBI Gene, UniProt)
and the matching authority name in `trait_ontology_label` (issue #141).

### Column reference

| Field | Required | Description |
|---|---:|---|
| `analysis_id` | Yes | Stable registry Analysis ID. Usually source-derived unless the source lacks stable IDs. |
| `source_analysis_id` | No | Upstream analysis identifier, such as a GCST accession or OpenGWAS ID, when the Source Collection provides one. |
| `source_label` | Yes | Upstream trait or phenotype label preserved as source provenance. Registry-only; kept separate from `analysis_label` even when both hold the same source text. |
| `analysis_label` | Yes | Free-text, non-unique display label for the Analysis, carried into the built store. Typically the same source text as `source_label`; for a single-target gene-centric Analysis it is the resolved gene symbol, and for an aggregate assay it is the SomaScan SeqId, SomaLogic's stable assay identifier (issue #141). |
| `trait_ontology_label` | No | Human-readable trait label from the ontology that defines `trait_ontology_id`, such as an EFO/MONDO/OBA/GO term name or a source-local analyte vocabulary term. Never an identifier-authority name such as `Ensembl`: an authority name describes the vocabulary, not the Trait. Named `trait_ontology_name` before OpenGWASDB ADR 0034. |
| `trait_ontology_id` | No | Ontology or controlled-vocabulary identifier for the analysed Trait, when available. CURIE format, for example `EFO:0001073`; blank when unmapped. Never a gene or protein identifier: `bundle.check()` rejects Ensembl, HGNC, Entrez/NCBI Gene, and UniProt identifiers, bare or authority-qualified (issue #141). Not required to be unique. |
| `trait_ontology_mapping_method` | Yes | Controlled value describing how `trait_ontology_id`/`trait_ontology_label` were resolved: `source_provided`, `canonical_table_lookup`, or `unmapped`. The #130 `external_authority_lookup` value is retired: gene/target identity is annotation, not a Trait Ontology Mapping (issue #141). Registry-only. |
| `source_file` | Yes | Source file or filtered source file consumed by the builder. Omitted in legacy monolithic BESD releases (`OGS-00001` and `OGS-00002`), where source identity is currently recorded only as an unverified path prefix (a known integrity gap tracked in issue #134). |
| `source_bundle_id` | No | Identifier for a multi-file Source Bundle when one file is insufficient. |
| `checksum` | Yes | Checksum for `source_file` or source bundle manifest. Omitted in legacy monolithic BESD releases (`OGS-00001` and `OGS-00002`; known integrity gap tracked in issue #134). When both `checksum` and `checksum_algorithm` are present, `bundle.check()` validates the digest length for `md5` (32), `sha1` (40), or `sha256` (64) hex; an unsupported algorithm is rejected. |
| `checksum_algorithm` | Yes | Algorithm used for `checksum`, for example `sha256`. |
| `size_bytes` | No | File size in bytes. |
| `source_genome_build` | Yes | Genome build of source coordinates for this Analysis. |
| `license` | Yes | Licence or usage terms after applying source defaults and row overrides. Attribution Metadata: carried into the built store, since a store that cannot be legally used or cited has failed self-containment even when its statistics are perfectly interpretable. |
| `publication_doi` | No | DOI for compact bibliographic provenance. |
| `publication_pmid` | No | PMID for compact bibliographic provenance. |
| `consortium` | No | Consortium or provider label when DOI/PMID is not enough for provenance. |
| `first_author` | No | First author for compact bibliographic/attribution provenance. |
| `source_ancestry_label` | No | Upstream ancestry/population label, preserved as provenance. |
| `assigned_ancestry` | Yes | Registry-normalised ancestry used for store inclusion and routing, from the ancestry mixture's super-population vocabulary (`AFR`, `AMR`, `EAS`, `EUR`, `MID`, `NAF`, `SAS`). Empty means unassigned. A free-text Source Ancestry Label such as `European` is **not** valid here; `source_ancestry_label` preserves that provenance separately. `bundle.check()` rejects any other value. |
| `ancestry_assignment_method` | Yes | Controlled value: `af_assigned`, `source_fallback`, `source_trusted_no_af`, or `unassigned`. |
| `original_effect_scale` | Yes | Controlled value for upstream effect units, such as `sd`, `cm`, `logOR`, or another approved vocabulary item. |
| `original_sd` | No | Source-provided or estimated phenotype SD on the original scale. Empty for binary traits or unavailable values. |
| `original_sd_method` | Yes | Controlled value describing `original_sd`: `declared_standardised`, `source_provided`, `estimated_from_source_maf`, `estimated_from_reference_maf`, `estimated_from_beta_distribution`, `binary_trait`, or `unavailable`. |
| `stored_effect_scale` | Yes | Controlled value for the effect scale stored by OpenGWASDB for this Analysis: `sd`, `log_or`, or `log_hazard`. |
| `sample_size_kind` | Yes | `total`, `case_control`, `effective`, or `variant_level`. Accepted build rows must not use `unknown`. |
| `sample_size_scope` | Yes | `analysis_level` or `variant_level`. Use `variant_level` when N differs per SNP in the source file. |
| `sample_size` | Yes | Scalar study N for this Analysis. When source N differs per SNP, use the study's maximum N. |
| `n_cases` | No | Case count for binary traits, or event count for time-to-event traits. Required when `stored_effect_scale = log_or` or `log_hazard`; blank (not `0`) when the Analysis is not case-control. |
| `n_controls` | No | Control count for binary traits, or non-event/comparison count for time-to-event traits when reported by the source. Same absence rule as `n_cases`. |
| `analysis_group_id` | No | Grouping key for analyses sharing a publication, analyte panel, phenotype batch, or source bundle. |
| `inclusion_reason` | No | Short reason this Analysis was selected. |
| `exclude_from_build` | No | `true` only for rows retained for audit but intentionally skipped by the build. The registry honours it at build time: it materialises a derived build manifest (`<artifact-root>/<store-id>/work/analyses.tsv`) with every `true` row removed and points the builder at that, so `opengwasdb` never sees an excluded row. The row itself stays in the committed bundle, with its `inclusion_reason`, as the audit record of why the Analysis is absent. See [ADR 0025](adr/0025-registry-filters-excluded-analyses.md). |
| `ancestry_prop_*` | No | Optional family of columns for estimated reference ancestry proportions. |

Some generators add release-specific columns beyond this table, such as the
`gwas-ssf-ragged` generator's single-gene-target columns (`trait_chr`,
`trait_bp`, `n`, `mhc`, `target_resolution_method`, `n_target_rows`) for
proteomics releases whose Analyses each have one resolvable encoding gene. A
release with no such target (for example small-molecule metabolomics, issue
#26) omits these columns entirely rather than filling them with placeholder or
`NA` values — their absence is how a reviewer tells the two shapes apart. These
columns remain release-specific with no shared-core equivalent.

The target-resolution sidecar retains the source trait mapping and raw Ensembl
fields as evidence; they are not duplicate Analysis columns.

### Accepted build rows

Accepted build rows must have usable sample-size metadata. Analyses with unknown
sample size may appear in Source Inventories, candidate diagnostics, or review
sidecars, but should not be included as buildable rows in an accepted
`analyses.tsv`.

Case/control-style counts are required when `stored_effect_scale = log_or` or
`stored_effect_scale = log_hazard`. For time-to-event traits, `n_cases` is the
event count and `n_controls` is the non-event or comparison count reported by
the source. When the Analysis is not case-control (`sample_size_kind` is not
`case_control`), both counts must be blank rather than `0`: zero is a real
count, and writing it where the counts do not apply is exactly the
absence-vs-zero confusion CONTRIBUTING.md rules out. `bundle.check()` rejects a
non-empty count on a non-case-control Analysis (issue #133).

`assigned_ancestry` is one controlled vocabulary on every Analysis, regardless
of `ancestry_assignment_method`: the ancestry-mixture reference's
super-population codes (`AFR`, `AMR`, `EAS`, `EUR`, `MID`, `NAF`, `SAS`) or
empty for unassigned. It is never a free-text Source Ancestry Label
(`European`, `East Asian`, and so on). The tracked translation from the
candidates table's free-text labels to these codes is
`resources/reference-resources/ukb-ancestry-mixture-hg38/source_label_map.tsv`,
read by both the Ancestry Assignment stage and the manifest generators so the
two cannot drift apart. `bundle.check()` rejects any value outside the
vocabulary, so the split cannot silently reappear.

### Blank required values (issue #134)

`bundle.check()` normally fails a row that leaves a required column blank.
Two deliberate exemptions exist, both narrow:

- **Candidate releases.** A `candidate` may legitimately leave values blank
  pending resolution; this is the one status where an unresolved row is the
  expected state rather than a defect.
- **Legacy monolithic BESD releases.** `OGS-00001` and `OGS-00002` are exempt
  because their required Analytical Metadata is unpopulated in upstream BESD
  sources and cannot be resolved without external study metadata. The exemption
  is scoped to exactly those two named releases. Any new release — including any
  future ragged-BESD or completed release — must provide valid required values
  or fail `bundle.check()`.

This is a known integrity gap, tracked by open issue #134, not an equivalent
alternative to the per-Analysis checksums and source identity enforced for other
formats.

## `build.yaml`

The Build Recipe describes *how* the accepted manifest becomes an OpenGWASDB
store. It is the only input to `plan()`, and every value in it is either a
registry fact or an `opengwasdb` flag. It does not record *where* artifacts land
(issue #126).

| Field | Required | Description |
|---|---:|---|
| `store_id` | Yes | Must equal the bundle's `store_id` and its directory name. |
| `layout` | Yes | `dense`, `ragged`, or `hybrid`. |
| `completion_state` | Yes | `observed_only` or `reference_completed`. |
| `build` | Yes for `observed_only` | The observed-only build block. |
| `complete` | Yes for `reference_completed` | The completion block. |
| `build.command` / `complete.command` | Yes | An `opengwasdb` CLI subcommand; never a Python import path. Supported values are keyed by phase in `plan.COMMANDS`: `build-dense-vcf`, `build-hybrid`, `build-ragged-ssf`, `build-ragged-besd`; `complete-dense`, `complete-hybrid`, `complete-ragged`. |
| `build.options` / `complete.options` | No | `opengwasdb` flag names passed through verbatim; see "The passthrough rule". |
| `post` | Yes | Post-processing choices: `top_hits`, `rho`, `overview`, `validate`. `rho` defaults off and `validate` defaults on (`plan.POST_DEFAULTS`); a recipe states only what it chooses differently. `top_hits` and `overview` are always explicit because they vary per release. |

A Build Recipe must **not** declare an `artifacts` block: `bundle.check()`
rejects it, because the artifact root is deployment configuration (issue #126).
It must also not declare the superseded family-first layout: the top-level
`store_family_id`, `family_release_id`, and `store_layout` keys, or the nested
`builder`, `source`, `normalisation`, `effects`, and `shape` blocks. Those are
read by no code. Builder inputs that remain real flags go in `options` (for
example `source-reader-capability`, `source-assembly`). Reference Resources,
effect-scale validation, ancestry assignment, and QC-panel configuration are
**Manifest Generator configuration**, not Build Recipe fields; see "Manifest
Generator configuration" below.

```yaml
store_id: OGS-00042
layout: dense
completion_state: observed_only
build:
  command: build-dense-vcf
  options:
    source-reader-capability: opengwasdb.finngen-r13
    source-assembly: hg38
    n-workers: 8
post:
  top_hits: true
  overview: true
```

A Reference-Completed release is the same file with a `complete` block instead
of `build`. It carries no parent path: the parent is `release.yaml`'s
`derived_from`, and its artifact path is a pure function of that ID.

```yaml
store_id: OGS-00043
layout: dense
completion_state: reference_completed
complete:
  command: complete-dense
  options:
    ld-panel: /data/opengwasdb/reference/ld/hgdp1kgp-hg38
    ancestry: EUR
    n-workers: 16
post:
  top_hits: true
  overview: true
```

### The passthrough rule

`options` keys are `opengwasdb` flag names. `plan()` renders `key: value` as
`--key value`, booleans as bare flags (`--key` for true, `--no-key` for false),
lists by repeating the flag, and never interprets a key. The registry does not
know what `--min-cor` means and must not acquire a copy of `opengwasdb`'s
parameter schema (ADR 0023).

An unrecognised or invalid flag fails in the CLI argument parser in
milliseconds, before any expensive work. `plan()` composes only the arguments
that are the registry's own facts — store identity, the derived build manifest
path, and artifact paths. Everything else comes from `options`.

### Post-processing support

Post-steps are conditional on `post` and on the selected command's Store-format
support. `plan()` raises rather than planning a step the subcommand cannot
support:

| Step | Supported by | Notes |
|---|---|---|
| `top_hits` | `build-dense-vcf`, `build-ragged-ssf`; built inline by `build-hybrid`, `build-ragged-besd`, and every `complete-*` | Ragged and Hybrid releases often declare `top_hits: false`. |
| `rho` | Dense only (via `complete-dense` or `build-dense-vcf`) | Defaults off. |
| `overview` | Dense and Hybrid (`regenerate-overview`) | Ragged's closed Store envelope excludes `overview.html`, so Ragged releases declare `overview: false`. |
| `validate` | Every release | Defaults on. |

## `validation.yaml`

Release-level acceptance and build validation summary. `register` is the only
writer (issue #119); it assembles the record from the step records and the
`opengwasdb validate` verdict. This table is the one definition of the format;
the workflow specification explains how `register` produces it rather than
restating the fields. A measurement the build did not report is written as
`null` — absence is recorded, never guessed or defaulted (issue #135).

A `built` or `validated` release must carry a `validation.yaml` whose `status`
is one of `not_run`, `passed`, `passed_with_warnings`, or `failed`;
`bundle.check()` rejects any other value. A `candidate`, `accepted`,
`superseded`, or `withdrawn` release needs none.

| Field | Required | Description |
|---|---:|---|
| `status` | Yes | `not_run`, `passed`, `failed`, or `passed_with_warnings`. The release-level verdict. |
| `validated_at` | No | Timestamp of the latest validation run. |
| `validator.name` | No | The validator that produced the verdict. `register` writes `opengwasdb validate`; a deleted generator adapter is never named (issue #135). |
| `validator.version` | No | Validator version, git commit, or script hash. `register` writes `opengwasdb@<commit>`. |
| `build_environment.opengwasdb_version` | No | `opengwasdb` package version the record was produced against. |
| `build_environment.opengwasdb_commit` | No | `opengwasdb` revision the record was produced against. |
| `build_environment.python_version` | No | Python version of the registering environment. |
| `build_environment.platform` | No | Platform string of the registering environment. |
| `observed.format_version` | Yes | OpenGWASDB store format version the build reported, or `null` when it was not recorded. |
| `observed.n_analyses` | Yes | Analysis count the build reported, or `null` when it was not recorded. |
| `observed.n_variants` | Yes | Variant count the build reported, or `null` when it was not recorded. |
| `observed.n_associations` | Yes | Association count the build reported, or `null` when it was not recorded. |
| `observed.store_bytes` | Yes | Store size in bytes the build reported, or `null` when it was not recorded. |
| `observed.build_elapsed_s` | Yes | Summed step elapsed seconds, or `null` when it was not recorded. |
| `observed.validate_status` | Yes | The validate verdict the release-level `status` is derived from. |
| `observed.variant_reference` | No | Variant-reference provenance (issue #148): `provided` when the artifact already existed (a skipped `variant-reference` pre-stage, or a build option naming an existing panel with no declared pre-stage, as OGS-00004/OGS-00005 do), `extracted` when the workflow ran `extract-variant-reference`, and absent when the release uses no variant reference. |
| `checks.schema` | Yes | Whether required files and fields conform to OpenGWASDB's shared core schema and this registry's release-bundle requirements. |
| `checks.files` | Yes | Whether referenced source or filtered files exist and match checksums. |
| `checks.reader_smoke_test` | No | Whether OpenGWASDB can read a small sample from each source file or bundle. |
| `checks.ancestry` | No | `not_run` when the release has not opted into `ancestry_assignment` (or opted in but every Analysis was skipped for lacking usable source AF). Otherwise reflects the AF-based ancestry sidecar evidence: `passed` when every attempted Analysis cleared its gates with no source/assigned mismatch, `passed_with_warnings` when at least one attempted Analysis failed a gate or disagreed with its source-declared ancestry, and never a bare `passed` implying validation ran when it did not. |
| `checks.effect_scale` | No | `not_run` when the release has not opted into `effect_scale_validation`, or when it has opted in but reflects only controlled-vocabulary validity. Once a release opts in, this must reflect the empirical reference-AF/source-AF SD-estimation sidecar outcome across attempted Analyses (`passed`, `passed_with_warnings`, or `failed`), not merely that declared vocabulary values are valid. |
| `checks.sd_estimation` | No | `not_run` when SD-estimation was not attempted. Otherwise `passed` only when the sidecar is internally consistent (every attempted, warned, or failed Analysis has a matching sidecar row with required fields populated) and no attempted Analysis has status `failed`; `passed_with_warnings` when at least one Analysis has status `warning`, or `skipped` for a reason that should be reviewed (for example `no_reference_resource_for_ancestry`); `failed` otherwise. |
| `checks.sparse_regions` | No | Whether ragged region sidecars match filtered files. |
| `reports` | No | URIs or paths to detailed reports. |
| `warnings` | No | List of non-blocking warnings. Reference-AF effect-scale warnings should name the Analysis and reason, for example low reference-AF overlap, an allele mismatch, unstable implied SD, a missing reference resource for the assigned ancestry, or scale inconsistency versus the declared effect scale. |
| `errors` | No | List of blocking errors. |

The record's top-level `status` is the release-level verdict, and the generated
master list publishes that value and no other. A per-check entry in `checks`
describes one check and cannot override the record: a record reads
`status: failed` precisely when a check failed, and publishing the passing check
in its place is the "wrong answer that looks like a right answer" CONTRIBUTING
names as the worst outcome.

## `summary.yaml`

Every bundle carries a generated `summary.yaml` review view, written by the
`index` rule and checked for drift in CI. It is derived solely from
`analyses.tsv` and is not declared metadata or an input to building a Store
Release: constants stay verbatim, varying identifiers become `mixed (n)`,
varying quantities become `min-max`, varying URLs become their common
host/path prefix, and a missing column, an empty table, or any empty value
renders as `NA` (zero is not absence). The same values are published in the
master list. See `bundle.summarise()` and [ADR 0014](adr/0014-release-manifest-bundles.md).

## Sidecars

Generated evidence lives under `sidecars/` inside the bundle. Sidecars are
Phase B outputs; `bundle.check()` does not validate their fields, so a Manifest
Generator is responsible for emitting them consistently.

### Ancestry sidecar

Suggested path: `sidecars/ancestry.tsv`. One row per Analysis when ancestry
assignment was attempted or source ancestry required mapping.

| Field | Required | Description |
|---|---:|---|
| `analysis_id` | Yes | Registry Analysis ID matching `analyses.tsv`. |
| `source_analysis_id` | No | Upstream analysis identifier, when the Source Collection provides one. |
| `source_ancestry_label` | No | Upstream ancestry label. |
| `assigned_ancestry` | No | Final assigned ancestry used for routing. |
| `ancestry_assignment_method` | Yes | Same controlled value as `analyses.tsv`. |
| `ancestry_reference_id` | No | Reference panel/catalogue used for AF-based assignment or MAF fallback. |
| `af_overlap` | No | Number or proportion of variants overlapping the reference panel. |
| `dominant_superpop` | No | Dominant reference super-population. |
| `dominant_proportion` | No | Estimated dominant ancestry proportion. |
| `runner_up_margin` | No | Difference between dominant and runner-up proportions. |
| `nnls_residual` | No | Residual from mixture fitting, when used. |
| `gate_reason` | No | Pass/fail or exclusion reason from the ancestry assignment gate. |
| `ancestry_prop_*` | No | Optional family of columns for estimated reference ancestry proportions. |
| `source_assigned_mismatch` | No | `true` when source label and AF-based assignment disagree. |
| `ancestry_notes` | No | Free-text notes for review dashboards. |

### SD-estimation sidecar

Suggested path: `sidecars/sd_estimation.tsv`. One row per Analysis whenever
effect-scale validation was attempted, passed, warned, failed, or explicitly
skipped. Skips are always explicit and reasoned; an Analysis with no
source-provided or reference-derivable allele frequencies, no configured
reference resource for its assigned ancestry, or a non-quantitative
`stored_effect_scale` (`log_or`, `log_hazard`) still gets a row, with
`status = skipped` and a `skip_reason`.

| Field | Required | Description |
|---|---:|---|
| `analysis_id` | Yes | Registry Analysis ID matching `analyses.tsv`. |
| `source_analysis_id` | No | Upstream analysis identifier, when the Source Collection provides one. |
| `status` | Yes | `passed`, `warning`, `failed`, or `skipped`. |
| `skip_reason` | No | Reason code when `status = skipped`, for example `non_quantitative_effect_scale`, `no_retained_variants`, `no_reference_resource_for_ancestry`, or `low_overlap`. |
| `af_source` | No | `source`, `reference`, or empty when no AF source was used. Source-provided AF is preferred whenever usable; reference AF is a fallback used only when source AF is missing, empty, or otherwise unusable. |
| `ancestry_reference_id` | No | Reference Resource `resource_id` used for reference MAF, when `af_source = reference`. |
| `original_sd` | No | Resolved phenotype SD written to `analyses.tsv`. Left as the source-declared value (unchanged) for `original_sd_method = declared_standardised`; populated from the estimate for `estimated_from_source_maf` / `estimated_from_reference_maf` when diagnostics pass. |
| `original_sd_method` | Yes | Same controlled value as `analyses.tsv`. |
| `n_variants_considered` | No | Number of retained source rows examined for this Analysis. |
| `n_variants_overlapping` | No | Number of considered variants with a same-position AF value available. |
| `n_variants_excluded_ambiguous` | No | Number of overlapping variants excluded because the allele pair was palindromic/strand-ambiguous. |
| `n_variants_excluded_mismatch` | No | Number of overlapping variants excluded because the alleles did not correspond to the reference in either orientation, or were not a usable single-nucleotide pair. |
| `n_variants_excluded_missing_af` | No | Number of source rows excluded because the source-provided allele-frequency value was missing or unusable (`af_source = source` only). |
| `n_variants_excluded_maf` | No | Number of safely aligned variants excluded for falling outside `maf_min`/`maf_max`. |
| `n_variants_retained` | Yes | Number of variants actually used to compute the implied-SD summary. |
| `maf_min` | No | Minimum MAF bound used for variant selection. |
| `maf_max` | No | Maximum MAF bound used for variant selection. |
| `implied_sd_median` | No | Robust (median) summary of per-variant implied phenotype SD, computed from standard error, sample size, and MAF. |
| `sd_dispersion` | No | Robust dispersion diagnostic (median absolute deviation over median) for implied-SD estimates across retained variants. |
| `estimator_version` | No | Estimator package version, git commit, or script hash. |
| `sd_notes` | No | Free-text notes for audit or review dashboards, including the reason for a `warning`/`failed` status. |

### Sparse-region sidecar

Suggested path: `sidecars/sparse_regions.tsv`. One row per retained region for
ragged stores.

| Field | Required | Description |
|---|---:|---|
| `analysis_id` | Yes | Registry Analysis ID matching `analyses.tsv`. |
| `region_id` | Yes | Stable region identifier within the Analysis. |
| `region_kind` | Yes | Controlled value such as `cis`, `significant_trans`, `suggestive_trans`, `qc_panel`, or `manual`. `qc_panel` rows (issue #28/#30) mark the fixed QC panel's positions retained unconditionally, regardless of significance; unlike the signal-driven kinds, `pvalue_threshold`/`lead_variant_id`/`target_id` are not applicable and are left blank. |
| `chromosome` | Yes | Chromosome name in source coordinates. |
| `start` | Yes | 1-based inclusive region start. |
| `end` | Yes | 1-based inclusive region end. |
| `source_genome_build` | Yes | Genome build of the region coordinates. |
| `target_id` | No | Gene, analyte, or other target defining the region. |
| `target_label` | No | Human-readable target label. |
| `lead_variant_id` | No | Lead variant for signal-defined regions. |
| `pvalue_threshold` | No | Threshold used to define signal-derived regions. |
| `n_variants_retained` | No | Number of variants retained in the filtered source file for this region. |
| `region_policy_id` | Yes | Named sparse-region policy declared in generator configuration. |

### General derivation sidecar

Suggested path: `sidecars/derivations.tsv`. Use only when compact
`analyses.tsv` fields need additional evidence that does not belong in a
specialised sidecar.

| Field | Required | Description |
|---|---:|---|
| `analysis_id` | Yes | Registry Analysis ID matching `analyses.tsv`. |
| `field` | Yes | Manifest field being explained. |
| `value` | No | Resolved value written to the manifest. |
| `method` | Yes | Controlled or script-local method name. |
| `evidence` | No | Compact evidence string or URI. |
| `notes` | No | Free-text notes for audit. |

## Manifest Generator configuration

The blocks below are **Phase B generator configuration**, not Release Bundle
fields. They are recorded here because they determine the sidecar and
`validation.yaml` evidence a generator writes into a bundle. A generator's
config also carries its selection and output settings; the four blocks that
affect release metadata are documented here.

### Metadata resolvers

A metadata resolver is a small, independently callable module owned by one
Source Collection (`gwas-catalog-ssf`, `opengwas-gwas-vcf`, ...) that resolves
the interpretation-bearing fields `derive` cannot safely infer from the raw
summary-statistics file alone: `stored_effect_scale`, `sample_size_kind`,
`sample_size`, `n_cases`, and `n_controls`. Resolvers read the provider's own
authoritative study/analysis metadata (a study table row, an API response, an
endpoint manifest) — never the summary-statistics file itself, which may
disagree with or omit this metadata (see issue #15's `ieu-a-7` example, where a
GWAS-VCF `##SAMPLE` header declares `StudyType=Continuous` for a study the
source API correctly reports as case-control).

Conventionally, resolver modules live under
`resources/generators/lib/metadata_resolvers/`, one file per Source Collection,
named for the resolved field set they share:
`resolve_<source_collection>_metadata(...)`.

Input is whatever raw metadata unit the provider exposes for one Analysis (free
text, a parsed API record, ...); shape is provider-specific and not part of this
contract. Output is always a single resolved record with these fields:

| Field | Required | Description |
|---|---:|---|
| `resolution_status` | Yes | `resolved` or `unresolved`. Never a silent default — an Analysis whose metadata can't be established gets an explicit `unresolved` record, not a defaulted value that looks plausible. |
| `resolution_notes` | No | Free-text reason when `resolution_status = unresolved`. |
| `stored_effect_scale` | No | Same controlled vocabulary as `analyses.tsv`: `sd`, `log_or`, or `log_hazard`. `NA` when `resolution_status = unresolved`. |
| `sample_size_kind` | No | Same controlled vocabulary as `analyses.tsv`: `total`, `case_control`, `effective`, or `variant_level`. `NA` when `resolution_status = unresolved`. |
| `sample_size` | No | Scalar study N. `NA` when `resolution_status = unresolved`. |
| `n_cases` | No | Case (or event) count. `NA` when not applicable (`sample_size_kind != case_control`) or unresolved. |
| `n_controls` | No | Control (or non-event) count. `NA` when not applicable or unresolved. |

A caller (a `derive` stage, or a curation script such as `ebi-studies.r`) may
adapt this generic record onto its own column names, but the resolver itself
must not know which downstream shape it feeds — this is what lets `derive` stay
agnostic to which provider it is talking to.

Three implementations exist:

- `resources/generators/lib/metadata_resolvers/gwas_catalog_ssf.R`
  (`resolve_gwas_catalog_ssf_metadata()`) resolves GWAS Catalog's free-text
  "INITIAL SAMPLE SIZE" study field: a study whose parsed components include any
  `cases`/`controls` counts resolves as `case_control` with
  `stored_effect_scale = log_or`; otherwise it resolves as `total` with
  `stored_effect_scale = sd`; a field with no parseable numeric component
  resolves as `unresolved`.
- `resources/generators/lib/metadata_resolvers/opengwas_api.R`
  (`resolve_opengwas_api_metadata()`, issue #49), for the `opengwas-gwas-vcf`
  Source Collection. It resolves a study's `ncase`/`ncontrol`/`sample_size`
  from the OpenGWAS API's own "gwasinfo" record rather than the source GWAS-VCF
  file's `##SAMPLE` header, which is not authoritative. A usable
  `ncase`/`ncontrol` pair resolves `sample_size_kind` as `case_control`;
  otherwise a usable `sample_size` resolves it as `total`; a record with neither
  resolves as `unresolved`. `stored_effect_scale` primarily follows the API's
  `unit` field (`SD` -> `sd`, `log odds`/`logOR` -> `log_or`, a hazard-ratio
  unit -> `log_hazard`), falling back to `log_or`/`sd` from `ncase`/`ncontrol`
  presence only when `unit` is absent or unrecognised (issue #50: the `ukb-b`
  batch reports every trait on the SD scale, so inferring scale from
  case/control-count presence alone is wrong for that batch).
- `resources/generators/lib/metadata_resolvers/finngen_manifest.R`
  (`resolve_finngen_manifest_metadata()`, issue #57), for FinnGen's public
  endpoint manifest. Binary endpoint rows carry `num_cases` and `num_controls`
  and resolve to `log_or`/`case_control`. FinnGen encodes its
  inverse-rank-normalised quantitative endpoints with
  `category = Quantitative endpoints`, the total N in `num_cases`, and
  `num_controls = 0`; those rows resolve to `sd`/`total` without
  misrepresenting that total as a case count. Any other shape is explicit
  `unresolved`.

### Trait ontology mapping resolver

A separate resolver family, also under
`resources/generators/lib/metadata_resolvers/`, resolves Trait Ontology Mapping
(see `CONTEXT.md`) rather than the effect-scale/sample-size fields above — a
different output shape. `resources/generators/lib/metadata_resolvers/canonical_trait_table.R`
(`resolve_trait_ontology_mapping()`) returns:

| Field | Required | Description |
|---|---:|---|
| `resolution_status` | Yes | `resolved` or `unresolved`. |
| `trait_ontology_id` | No | `NA` when `resolution_status = unresolved`. |
| `trait_ontology_label` | No | `NA` when `resolution_status = unresolved`. |
| `trait_ontology_mapping_method` | Yes | `source_provided`, `canonical_table_lookup`, or `unmapped`. |
| `resolution_notes` | No | Free-text reason when `unmapped`. |

Resolution order: (1) if the Source Collection already supplies an ontology
ID (e.g. GWAS Catalog's `MAPPED_TRAIT_URI`), pass it through as
`source_provided`; (2) otherwise, exact-match the Analysis's trait label
against the curated Canonical Trait Mapping Table Reference Resource
(`resources/reference-resources/canonical-trait-mapping-efo/`) as
`canonical_table_lookup`; (3) otherwise `unmapped`, leaving
`trait_ontology_id`/`trait_ontology_label` blank rather than guessing. There is
no gene-authority path: a gene's Ensembl ID is not a Trait Ontology Mapping, so
a gene-centric Analysis whose source supplies no ontology term stays
`unmapped`/blank rather than being given a gene id (issue #141). A
source-provided term is kept even when the ontology has deprecated it, and is
recorded as `source_provided`; a plausible-looking invented replacement would be
worse than the genuine obsolete term. See
[ADR 0021](adr/0021-trait-ontology-mapping-lookup-lives-in-registry.md) for why
this lookup lives in this repo rather than OpenGWASDB.

### Reference Resource declaration

Each item in a generator config's `reference_resources` list describes one
auxiliary build-time resource (see [ADR 0011](adr/0011-reference-resources-are-build-resources.md)).
The canonical form of a declaration is a `resource.yaml` under
`resources/reference-resources/<resource-id>/`; a generator config may repeat
the fields it needs inline. A reference-AF resource used for effect-scale
validation should declare at least:

| Field | Required | Description |
|---|---:|---|
| `resource_id` | Yes | Stable ID for this Reference Resource, referenced from `effect_scale_validation.reference_resources` or `ancestry_assignment.reference_resource_id`. |
| `kind` | Yes | `reference_af` for single-ancestry allele-frequency lookup resources used by effect-scale validation; `ancestry_mixture` for multi-ancestry mixture-frequency resources used by AF-based ancestry assignment (issue #23); `qc_panel` for the fixed QC variant panel unconditionally retained regardless of significance (issue #28/#29); `trait_ontology_mapping` for a curated trait-label-to-ontology-term lookup; `hybrid_dense_panel` for a Hybrid release's Dense Component reference panel; other kinds (for example LD panels) may reuse the same declaration shape. |
| `ancestry` | Yes for `reference_af` | Ancestry/population label the resource represents, matching the controlled `assigned_ancestry` vocabulary. For `ancestry_mixture` resources, which cover multiple super-populations at once, use `ancestry: multi` and list the covered super-populations in `super_populations` instead. Not applicable for `trait_ontology_mapping`. |
| `super_populations` | Yes for `ancestry_mixture` | List of super-population labels the mixture reference can assign to (for example `AFR, AMR, EAS, EUR, MID, NAF, SAS`). |
| `genome_build` | Yes, except `trait_ontology_mapping` | Genome build of the resource's coordinates. Not applicable for `trait_ontology_mapping`, which has no genomic coordinates. |
| `variant_id_convention` | Yes, except `trait_ontology_mapping` | Variant identifier/allele convention used by the resource, for example `chr:pos_ref_alt`, or `chr:pos:A1:A2 (A1 = min(allele1, allele2))` for the canonical ALID convention `opengwasdb.ancestry` uses. |
| `allele_columns` | No | Column-name mapping the resource uses for effect/other allele and frequency, for example `{effect: EA, other: OA, freq: EAF}`. |
| `location` | Yes | External path or URI for the resource, outside this repository — except `kind: qc_panel` and `kind: trait_ontology_mapping`, both small enough to track directly in this repository, so `location` is a repo-relative path instead. |
| `location_kind` | No | How to interpret `location`, for example `external_directory`, `external_file`, or `tracked_file` for a small resource tracked directly in this repository. |
| `fine_group_map` | Yes for `ancestry_mixture` | External path to the fine-ancestry-group -> super-population map (one row per fine group in the resource) used to aggregate a fitted mixture to super-population composition. |

Reference-AF and ancestry-mixture resources are recorded here for provenance;
the resource data itself lives under the configured artifact root or another
external location, never inside this repository (ADR 0015).

### Effect-scale validation configuration

A generator config's `effect_scale_validation` block configures the reference-AF
effect-scale validation stage (issue #16) for one release:

| Field | Required | Description |
|---|---:|---|
| `enabled` | Yes | Whether this release opts into empirical effect-scale validation. |
| `reference_resources` | Yes when enabled | List of `{ancestry, resource_id}` pairs mapping an assigned ancestry to a declared Reference Resource `resource_id`. An ancestry without an entry here produces an explicit `skipped` sidecar row rather than silently omitting evidence. |
| `maf_min` / `maf_max` | Yes when enabled | Reference-MAF bounds a variant must fall within to be used for implied-SD estimation. |
| `min_overlap_variants` | Yes when enabled | Minimum number of safely aligned, in-bounds variants required before an Analysis is scored rather than skipped as `low_overlap`. |
| `sd_tolerance` | Yes when enabled | Maximum `abs(median implied SD - 1)` for a declared-standardised Analysis to pass. |
| `warning_multiplier` | Yes when enabled | Multiplier applied to `sd_tolerance` defining the boundary between a `warning` and a `failed` scale-inconsistency status. |
| `dispersion_max` | Yes when enabled | Maximum robust dispersion (median absolute deviation over median implied SD) before the result is downgraded to `warning` regardless of the central estimate. |

A generator config may set defaults for these values and override them per
release, per the source's molecular or population-scale GWAS tolerances. When
the block is absent or `enabled: false`, the release has not opted into
empirical effect-scale validation, and `validation.yaml`
`checks.effect_scale`/`checks.sd_estimation` should read `not_run` rather than
imply a pass.

### Ancestry assignment configuration

A generator config's `ancestry_assignment` block configures the AF-based
ancestry assignment stage (issue #23) for one release. Unlike
`effect_scale_validation` (which maps one Reference Resource per assigned
ancestry, since each reference-AF panel is single-ancestry), a single
ancestry-mixture Reference Resource covers every super-population at once, so
this block references exactly one Reference Resource rather than an
ancestry-keyed list:

| Field | Required | Description |
|---|---:|---|
| `enabled` | Yes | Whether this release opts into AF-based ancestry assignment. |
| `reference_resource_id` | Yes when enabled | `resource_id` of the declared `kind: ancestry_mixture` Reference Resource to fit against. |
| `maf_floor` | No | Minimum reference-wide MAF for a variant to be used in the mixture fit; defaults to `0.01`. Lower for tiny fixture panels where all variants are informative. |
| `gates.tau` | Yes when enabled | Minimum dominant super-population proportion required to admit an assignment. |
| `gates.delta` | Yes when enabled | Minimum margin of the dominant super-population's proportion over the runner-up. |
| `gates.n_min` | Yes when enabled | Minimum number of allele-frequency sites overlapping the reference required before fitting. Ragged/sparse releases (signal-derived regions only, not a full-genome scan) typically need a much lower `n_min` than a dense, full-GWAS release. |
| `gates.residual_max` | Yes when enabled | Maximum NNLS mixture-fit residual before the assignment is gated out as unreliable. |

Only Analyses with usable source allele frequencies attempt AF-based
assignment; Analyses without usable source AF keep
`ancestry_assignment_method: source_trusted_no_af` untouched, per issue #11's
settled trust-vs-validate policy. When an assignment is attempted but fails a
gate, `ancestry_assignment_method` becomes `unassigned` (not left at its
prior value), because the prior value would otherwise misleadingly imply
validation was never attempted.

### QC panel configuration

A generator config's `filter.qc_panel` block configures unconditional retention
of the fixed QC variant panel (issue #28/#29/#30) for one release. Unlike the
signal-driven sparse regions (`cis`/`significant_trans`/`suggestive_trans`), the
QC panel is retained regardless of an Analysis's `p_value` distribution, so
ancestry assignment and reference-AF effect-scale validation have a stable
variant backbone even for Analyses with few or no genome-wide-significant hits:

| Field | Required | Description |
|---|---:|---|
| `enabled` | Yes | Whether this release retains the QC panel in every Analysis's filtered file. |
| `resource_id` | Yes when enabled | `resource_id` of the declared `kind: qc_panel` Reference Resource to retain (see "Reference Resource declaration"). |

QC-panel retention is purely additive: a release that does not configure
`filter.qc_panel` behaves exactly as before, and enabling it for
one release never changes another's filtered output. Retained QC-panel rows
are recorded in the sparse-region sidecar with `region_kind: qc_panel` (one
row per chromosome the panel actually matched in that Analysis's source file,
spanning the matched positions), distinct from the signal-driven region
kinds, and `sidecars/filter_summary.tsv` gets a `qc_panel_rows` count per
Analysis (`0` for releases that do not configure a panel).

Effect-scale validation (issue #16) and ancestry assignment (issue #23/#25)
deliberately do not distinguish QC-panel rows from signal-driven rows when
choosing which retained variants to use for AF/SD computations — both
continue to consume every retained row with usable allele-frequency data
uniformly. Retaining the QC panel already directly grows that pool for
low-power Analyses; preferring QC-panel rows exclusively would need to
discard perfectly good signal-driven rows for well-powered Analyses, for no
demonstrated ascertainment benefit. This can be revisited if evidence of an
ascertainment problem emerges, but is not assumed by default.

## Acceptance rule of thumb

A release bundle is acceptable when `analyses.tsv` is enough for OpenGWASDB to
build the intended store, `release.yaml` and `build.yaml` are enough to explain
what the store is and how to rebuild it, and sidecars are enough to audit any
non-obvious derived metadata without blocking low-effort source ingestion. Run
`pixi run bundle-check` to confirm it.
