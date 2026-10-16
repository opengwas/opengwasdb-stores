# OpenGWASDB Store Operations

This context describes the planning and operational language for expected and produced OpenGWASDB store assets.

## Language

**Store Registry**:
The authoritative record of intended and produced Store Releases, including their identity, source inputs, build recipes, release lineage, and operational priority.
OpenGWASDB's former Analysis Catalogue model is superseded by this registry boundary and by OpenGWASDB-owned store metadata carried with each built Store Release.
_Avoid_: catalogue, service catalogue

**Access Posture**:
The declared availability category for a Source Collection or Store Release, such as public, controlled, or embargoed.
_Avoid_: access control, authorisation

**Build Priority**:
The ordering of what should be produced next, based on value, readiness, cost, risk, and strategic fit. No longer registry metadata: it is decided in the issue tracker (ADR 0028).
_Avoid_: scheduler state, job priority

**Source Collection**:
A homogeneous upstream set of summary-statistics inputs with one Source Format and one Source Reader Capability, recorded with its Source Provider, provenance, location, access constraints, and validation state. Several Store Releases can come from one Source Collection, each through its own Selection.
_Avoid_: input folder, data dump

**Source Provider**:
The organisation or repository Source Collections come from, such as the GWAS Catalog, FinnGen, OpenGWAS or dbGaP. Acquisition and the metadata adapter are written once per Source Provider where it delivers consistently -- every GWAS Catalog collection shares them -- and once per Source Collection where it does not: dbGaP is a host whose datasets each look different, so each is its own Source Collection (MVP `phs002453` is one) with its own adapter, sharing only common dbGaP plumbing. A Column Map is always per Source Collection.
_Avoid_: host, repository (as a separate concept), source

**Source Inventory**:
The frozen reconciliation, for one Selection, of the Analyses a Source Collection intends (its Source Metadata Table) against the files acquisition produced (its Acquisition Manifest): each selected Analysis with its source IDs, location, checksum, size and readiness. Freezing fails loudly on a selected Analysis with no file, a file no Analysis claims, or a duplicate; a directory listing is never the intent.
_Avoid_: release manifest, store manifest, discovered file list

**Acquisition Manifest**:
What acquisition produced for a Source Collection: each file fetched, extracted or converted, with its location, size and checksum. It records what is available, never what is intended. A file acquisition had to reformat before any reader could open it -- a structural change a Column Map cannot express, such as splitting one file into one per Analysis -- also records the original it came from, with that original's location and checksum and the tool that reformatted it, so a Release Bundle traces back to what the provider published.
_Avoid_: download log, source inventory

**Selection**:
The subset of a Source Collection's intended Analyses that one release takes, declared in its release config by rule or by list -- all of them, a Trial Store Release's handful, or one ancestry group. Freezing requires only the selected Analyses, and the Source Inventory records the Selection it was frozen for, so a subset is visible in the bundle rather than implied by which metadata was used. One Selection gives one candidate Release Bundle: a Source Collection split into several Store Releases is several Selections, which share Resolution Records but never route an Analysis from one Store Release to another.
_Avoid_: sample, pilot set, partial acquisition, routing

**Source Metadata Table**:
The per-Analysis source metadata a Manifest Generator reads, in one canonical set of columns whatever the Source Collection: Source Analysis ID, Source Trait Label, source ancestry, sample size, trait type and declared effect scale, genome build, publication and licence. A source-specific adapter writes it from the provider's own metadata -- an API, a manifest, a study table -- and it is frozen with the Source Inventory, so generation reads no live source and a metadata change is a reviewed re-freeze rather than a silent difference between runs.
_Avoid_: candidate table, candidates table, metadata resolver output

**Source Bundle**:
A named group of source files shared by one or more Analyses when a single `source_file` column is insufficient.
_Avoid_: store release, source collection

**Source Format**:
The structural format of upstream summary-statistics inputs, such as GWAS-VCF, GWAS-SSF, BESD, or custom tabular.
_Avoid_: source collection, store layout

**Source Reader Capability**:
The OpenGWASDB capability assigned to a Source Collection for reading and normalising all analyses in that collection. For tabular sources it is OpenGWASDB's generic tabular reader together with the collection's Column Map; a dedicated reader remains only for a Source Format a Column Map cannot describe, such as GWAS-VCF or BESD.
_Avoid_: source format, build recipe

**Column Map**:
The declaration, one per Source Collection, of how OpenGWASDB's generic tabular reader reads that collection's files: which provider column supplies each required and optional field, and the collection's conventions -- missing-value tokens, effect type, p-value form, which allele the effect and frequency refer to, chromosome spelling and genome build. An undeclared required field or an unknown key is a refusal, never a guess. It travels in the Release Bundle, checksummed, so Analysis Resolution and Store Release Build read every file the same way. Files that need different Column Maps belong to different Source Collections.
_Avoid_: header mapping, schema, reader config

**Manifest Generation**:
The workflow that turns a frozen Source Inventory into a candidate Release Bundle. A Manifest Generator selects and resolves the Analyses, applies the release policy, and emits the bundle into the Store Registry with Release Status `candidate`. It never builds, validates or publishes a Store, and it meets Store Release Build only at the Release Bundle (ADR 0035).
_Avoid_: Phase B, candidate generation, build phase

**Manifest Generator**:
An orchestration program that discovers or selects upstream inputs from a Source Collection and emits a candidate Release Bundle.
_Avoid_: selector, manifest script

**Analysis Resolution**:
OpenGWASDB's pass over one Analysis's source file that measures what release decisions need -- ancestry fit, allele alignment against a reference, phenotype SD, allele-frequency and imputation-quality state -- and records the measurements in a Resolution Record. Every Analysis Manifest Generation admits is resolved, without exception: a Source Format OpenGWASDB cannot yet resolve is a gap in OpenGWASDB, and its Source Collections wait for it rather than entering a Release Bundle on declared values. Manifest Generation decides from the record and never computes these statistics itself (ADRs 0012, 0017).
_Avoid_: metadata resolution (fetching source metadata is not resolution)

**Resolution Record**:
What Analysis Resolution measured for one Analysis's source file under one set of references and settings. It belongs to the Analysis, kept once per Source Collection, not to a Store Release: every Selection that takes the Analysis reuses a matching record, and a changed file, reference or setting makes a new one. Each candidate Release Bundle names the records its decisions came from.
_Avoid_: resolver output, QC report

**Resolution Area**:
Where a Source Collection's Resolution Records are kept, `<resolution-root>/<source_collection_id>/`: shared by every Selection of the collection, resolved into by one run at a time, and never read by Store Release Build. Its kept records are write-once; a candidate's resolution receipt names the ones it used. The resolution root is deployment configuration, like the Artifact Root, never a release config value.
_Avoid_: shared records dir, resolver cache

**Quality Check**:
A named release-policy test of one Resolution Record measurement against a threshold, with its consequence fixed when the check is defined: an exclusion (the Analysis stays in the Release Manifest as an `exclude_from_build` audit row with the check's reason) or a warning (the Analysis stays in and the finding is recorded). OpenGWASDB measures; Manifest Generation holds the threshold, a shared default a release may override and records, and records every Analysis's measurement whether it passes or not. A Quality Check decides membership, so it runs before the Release Bundle exists; checks on a built Store are Store Release Build's validation and cannot change membership.
_Avoid_: QC panel (a variant extraction panel, not a check), validation (of a built Store)

**Store Release Build**:
The workflow that turns an accepted Release Bundle into a built, validated and published Store Release, run through `pixi run release`. It builds only a bundle whose Release Status allows a build (`accepted`, `built` or `validated`), and it never regenerates a bundle (ADR 0035).
_Avoid_: Phase A, release pipeline, build phase

**Release Bundle**:
The Store Registry directory `stores/<store-id>/` that Manifest Generation and Store Release Build meet at, and the only thing they share: `release.yaml` (identity, lineage, Release Status and provenance), `build.yaml` (the Build Recipe), `analyses.tsv` (the Release Manifest), `validation.yaml` (the Validation Record), and sidecars. Manifest Generation emits it as a candidate, a human accepts it, and Store Release Build consumes it. A candidate and an accepted Release Bundle are the same thing at different Release Statuses.
_Avoid_: Release Manifest Candidate, candidate bundle (as a separate kind), release directory

**Release Manifest**:
The membership table of a Release Bundle, its `analyses.tsv`: the concrete Analyses and Traits from one Source Collection for one Store Release, with their source files, checksums, metadata and per-Analysis build inputs, including the `exclude_from_build` audit rows.
_Avoid_: study list, input list, release bundle (for the whole directory)

**Analysis**:
One statistical analysis of one Trait, producing associations between that Trait and variants. Every Analysis in a Release Manifest has a stable registry analysis ID and source or ontology metadata describing the analysed Trait where available.
_Avoid_: dataset, trait, phenotype

**Source Analysis ID**:
The analysis identifier supplied by the upstream Source Collection and preserved in the Release Manifest as provenance.
_Avoid_: analysis ID, trait ID

**Trait**:
A measured or derived biological outcome analysed against genetic variants. Every Trait in a Release Manifest has a trait ID, assigned by the registry when a stable upstream ID is unavailable.
_Avoid_: phenotype, analysis

**Source Trait ID**:
The trait identifier supplied by the upstream Source Collection and preserved in the Release Manifest as provenance.
_Avoid_: trait ID, analysis ID

**Build Recipe**:
A declarative description of the release shape to produce from an accepted Release Bundle, including layout, completion mode, indexing, validation, and execution parameters.
_Avoid_: pipeline, script, release manifest

**Reference Resource**:
An auxiliary build-time resource such as an LD reference panel or genome reference used by a Build Recipe without becoming the Source Collection for a Store Release.
_Avoid_: source collection

**Indexed Variant Subset**:
A named, optional, rebuildable Dense-store query index over a caller-supplied set of canonical ALIDs, built from a maintained variant-list Reference Resource as a derived artifact after the Store exists and before overview and validation (opengwas/opengwasdb ADR 0061; stores epic #214). It is not part of Store validity: deleting it leaves a valid release and no query answer changes.
_Avoid_: projection, membership index, HM3 store

**Store Layout**:
The storage organisation used for a Store Release, such as dense observed-only, dense reference-completed, ragged observed-only, ragged reference-completed, or hybrid.
_Avoid_: source format

**Store Composition Rule**:
A release-level admission rule deciding which Analyses may share one Store's variant axis, applied after every other membership decision. A shared Store admits only Analyses whose rows were MAF-filtered (ADR 0033); an otherwise admissible Analysis it refuses is excluded as `not_maf_filtered` and belongs in a separate Store Release.
_Avoid_: routing rule, panel policy

**Store Release**:
A versioned, immutable, validated OpenGWASDB analytical asset that Store Release Build produces from an accepted Release Bundle and material Build Recipe choices.
_Avoid_: database, live store

**Trial Store Release**:
A deliberately small Store Release used to exercise a distinct Source Collection, Source Format, or Store Layout while developing and testing the generic release pipeline; it is not a phase of a production Store Release.
_Avoid_: pilot store, pilot release

**Rehearsal Run**:
A representative subset run within Store Release Build for a Store Release, producing stage reports for one explicit human review gate before the full build proceeds. Not a Preflight.
_Avoid_: preflight run, pilot run, pilot phase, trial run

**Preflight**:
The checks that refuse a run before it reads or writes anything costly, with one meaning in each workflow. In Manifest Generation, `pixi run preflight` is the inventory-readiness gate: it proves a frozen Source Inventory and the release config before candidate selection, and reads no association rows. In Store Release Build, it is the release entry point's pre-write checks: its dry run and its refusals (Release Status, published releases, leftover snapshots, runtime and resources), before any snapshot or build work. A Preflight never builds anything.
_Avoid_: preflight run (see Rehearsal Run)

**Emit**:
Manifest Generation's act of writing a candidate Release Bundle into the Store Registry: staged, checked against the Release Bundle contract and Manifest Generation's own rules, then moved into `stores/<store-id>/` atomically. Only Manifest Generation emits, and it never emits over an accepted bundle.
_Avoid_: publish (a candidate), finalise (a candidate), register (a candidate)

**Release Artifact**:
A large material file or directory produced or consumed while building a Store Release, such as filtered source files, transient work files, build logs, or the built OpenGWASDB store. Release Artifacts live outside the Store Registry repository under the Artifact Root, one directory per Store Release, so a release's artifact path is the identifier-derived `<artifact-root>/<store-id>/` (ADRs 0022 and 0030).
_Avoid_: release bundle, manifest

**Publish**:
Store Release Build's act of moving a validated Store from its staged `.partial` path to its final path under the Artifact Root, as one recoverable transaction recorded in `publication.json`. To finalize is only to complete that transaction. Only Store Release Build publishes.
_Avoid_: emit (a Store), publish (a candidate)

**Artifact Root**:
The deployment-configured directory under which every Store Release's Release Artifacts live, one directory per Store Release id (`<artifact-root>/<store-id>/`). It is not a Release Bundle field: a bundle is immutable once accepted, so the root resolves from deployment configuration -- a workflow config override, the `OPENGWASDB_ARTIFACT_ROOT` environment variable, the repository `ogstores.yaml`, then the built-in default -- so one bundle can be built on CI, a laptop, or the production host.
_Avoid_: output directory, data root

**Work Root**:
Where Manifest Generation keeps its working files for one Store Release: for the shared candidate workflow, `<output.work_root>/<store-id>/` (its preflight report, resolver manifest, log, the run's resolver index and resolution receipt); for a legacy generator, `<artifact-root>/<store-id>/generate` (`paths.generation_dir`), where its working bundle lives and is emitted from. Resolution Records are not kept here but in the Resolution Area. Store Release Build never reads a Work Root.
_Avoid_: release directory, families/ release dir

**Release Lineage**:
The relationship between Store Releases, including observed-only releases, reference-completed releases, corrected releases, and other derived releases.
_Avoid_: source lineage

**Release Status**:
The registry lifecycle state for a Release Bundle: candidate, accepted, built, validated, superseded, or withdrawn.
_Avoid_: job state, scheduler state

**Association Coverage**:
Release-level metadata describing what associations are expected to be present, such as full GWAS, cis, trans, cis-plus-signals, top hits, or unknown.
_Avoid_: per-analysis coverage

**Validation Record**:
A release-level summary of validation status, checks, tool versions, timestamps, and pointers to detailed logs or benchmark reports.
_Avoid_: validation log, build report

**Trait Annotation**:
Descriptive metadata about traits in a Store Release that may be curated after release without changing the analytical asset itself.
_Avoid_: trait release, study manifest

**Source Trait Label**:
The trait label supplied by the upstream Source Collection and preserved in the Release Manifest as provenance.
_Avoid_: curated trait label, analysis label

**Analytical Metadata**:
Metadata that affects the interpretation of association statistics in a Store Release, such as sample size, ancestry, genome build, units, case-control counts, allele conventions, harmonisation assumptions, and Trait Ontology Mapping.
_Avoid_: trait annotation, display metadata

**Trait Ontology Mapping**:
The association between an Analysis's Trait and a controlled-vocabulary identifier appropriate to that Trait — an EFO, MONDO, OBA, or GO term — resolved before build and frozen into the Release Manifest as Analytical Metadata. Source-provided when the Source Collection already supplies one; otherwise resolved against a Canonical Trait Mapping Table, or left unmapped. The paired `trait_ontology_label` is the human-readable name of that ontology term (`carnitine measurement`, `multiple sclerosis`), never the name of the vocabulary or identifier authority that issued the identifier. Where no acceptable term exists it is left empty rather than backfilled with an identifier of another kind: a gene's Ensembl ID is Target annotation, not a Trait identity, and belongs in the target sidecar and the cis coordinates. A wrong mapping is corrected the same way any other Analytical Metadata error is: via a Release Erratum, not a silent edit.
_Avoid_: trait annotation, ontology term

**Analysis Label**:
The human-interpretable display name for an Analysis's subject, drawn from whichever identity concept is native to the Store Release — a trait name for phenotype-centric Analyses, a gene symbol for single-target gene-centric Analyses, and the assay identifier (a SomaScan SeqId) for an aggregate assay that measures several targets at once and therefore has no single honest gene symbol.
_Avoid_: trait label, display name

**Trait Ontology Mapping Method**:
The controlled value describing how a Trait Ontology Mapping was produced, such as source-provided, canonical-table lookup, or unmapped.
_Avoid_: mapping confidence, mapping status

**Canonical Trait Mapping Table**:
A curated, versioned Reference Resource that maps trait labels to ontology terms such as EFO or MONDO, produced by a candidate-generation-and-review process outside any single Manifest Generator, and consulted by generators as a deterministic, offline, build-time lookup rather than a live external call. It maps Trait labels only; gene, transcript, and UniProt identity is carried as Target annotation and the assay identifier, never as a Trait Ontology Mapping.
_Avoid_: ontology snapshot, mapping service

**Effect Scale**:
The OpenGWASDB controlled vocabulary value describing the scale of stored effects for an Analysis, such as `sd`, `log_or`, or `log_hazard`.
_Avoid_: source effect label

**Source Ancestry Label**:
The ancestry or population label supplied by the upstream Source Collection and preserved as provenance.
_Avoid_: assigned ancestry

**Assigned Ancestry**:
The registry-normalised ancestry label used for store inclusion checks, grouping, and ancestry-specific build choices. It never moves an Analysis into a different Store Release: a Store's Selection decides which Analyses it considers, and a disagreement between Assigned Ancestry and that Selection is a Quality Check. It is one controlled vocabulary on every Analysis — one of the ancestry mixture's super-population codes (AFR, AMR, EAS, EUR, MID, NAF, SAS) or empty when unassigned — so a free-text Source Ancestry Label such as European is never stored here.
_Avoid_: source ancestry label

**Ancestry Assignment Method**:
The controlled value describing how Assigned Ancestry was produced, such as AF-based assignment, source fallback, source trusted because allele frequencies were unavailable, or unassigned.
_Avoid_: ancestry confidence, ancestry status

**Sample Size Metadata**:
Analysis-level or variant-level metadata describing participant count, case-control counts, effective sample size, or unknown sample-size semantics.
_Avoid_: sample size

**Source License**:
The licence or usage terms attached to an upstream Analysis or source file, inherited from the Source Collection default unless overridden per Analysis.
_Avoid_: access posture, authorisation

**Publication Metadata**:
Compact bibliographic provenance for an Analysis or Source Collection, such as DOI, PMID, and consortium.
_Avoid_: trait annotation, analytical metadata

**Release Erratum**:
A recorded correction or warning for a published Store Release, used when an analytical interpretation issue is discovered after publication.
_Avoid_: metadata update, silent fix
