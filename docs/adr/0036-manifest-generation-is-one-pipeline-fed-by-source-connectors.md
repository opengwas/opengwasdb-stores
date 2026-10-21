# Manifest Generation is one pipeline fed by source connectors

Every Source Collection has its own end-to-end Manifest Generator today. They
share only the end of the work: the Release Bundle contract, Manifest
Generation's rules and the emitter (#248, #250). Everything before that is
written again per source:
- Source Inventory reconciliation: GWAS Catalog uses the shared module, ukb-b
  has its own 1,100-line copy, and FinnGen does it in R;
- driving `opengwasdb resolve-analyses`: GWAS Catalog and dbGaP each wrap it
  their own way, ukb-b uses a recorded resolution, and the R generators skip it;
- ancestry and effect-scale estimation, done in R (`ancestry.R`,
  `effect_scale_validation.R`) against ADR 0017;
- rendering the bundle files.

So a new data source costs a new generator. Many more sources are coming: more
GWAS Catalog collections like Chen 2023 and Sun 2018, later FinnGen rounds, and
dbGaP datasets that each look different. Decided in a design session for #249
on 15 Oct 2026.

## Decision

**Manifest Generation is one shared pipeline. A source plugs in through files,
never through code the pipeline calls.**

A source supplies four things:

| Connector | Written per | What it produces |
|---|---|---|
| Acquisition | Source Provider (per Source Collection when the provider is inconsistent, as dbGaP is) | the files, and an **Acquisition Manifest**: location, size and checksum per file |
| Metadata adapter, in any language | the same | a **Source Metadata Table**: canonical per-Analysis source metadata, whose rows are the intended membership |
| **Column Map** | Source Collection | how OpenGWASDB's generic tabular reader reads the collection's files: columns and conventions |
| Release config | Store Release | identity, **Selection**, Build Recipe shape, Quality Check overrides |

The pipeline does the rest, the same way for every source:
1. **Freeze.** Reconcile the selected intent against the Acquisition Manifest
   into a frozen Source Inventory. An intended Analysis with no file, a file
   nobody claims, or a duplicate fails the run.

   *Clarified by #283:* "no file" means no Acquisition Manifest row at all.
   A row whose readiness status records why acquisition could not produce a
   usable file is an explained absence: the Analysis stays in the Source
   Inventory as a non-ready row and never becomes a release member. Freeze
   writes the selected rows of both connector files beside the inventory and
   checksums them into its provenance
   ([spec](../spec/source-connectors.md)).
2. Preflight.
3. **Analysis Resolution** of every Analysis, by OpenGWASDB.
4. Verify the records.
5. Trait Ontology Mapping, filling gaps from the Canonical Trait Mapping Table.
6. **Release policy.** Quality Checks, the Assigned Ancestry check, and the
   Store Composition Rule.
7. Render, check and emit.

The rules that make this hold:

- **Every Analysis is resolved; there is no declared-only path.** A Source
  Format OpenGWASDB cannot resolve is a gap in OpenGWASDB. Its collections wait
  for OpenGWASDB rather than entering a bundle on the source's claims.
  eQTLGen's BESD waits for BESD resolution support.
- **OpenGWASDB measures and Manifest Generation decides.** A Quality Check
  tests one Resolution Record measurement against a threshold. The shared
  default can be overridden per release, and the override is recorded. Each
  check is declared as an exclusion (an `exclude_from_build` audit row with
  its reason) or a warning. Every Analysis's measurements are recorded whether
  it passes or not.

  *Clarified by #285:* a check's measurement comes from the Resolution
  Record, except where OpenGWASDB does not yet measure it. Today the only
  exception is effect-placeholder rows, read from a reviewed listing until
  core counts them. The Assigned Ancestry check of step 6 is itself a Quality
  Check (`assigned_ancestry`), as the Selection rule below requires.
- **Connectors are files, frozen with the inventory.** Generation reads no live
  source. A metadata change is a reviewed re-freeze, never a silent difference
  between two runs.
- **One Selection gives one Release Bundle.** A collection split into several
  Store Releases is several Selections. Assigned Ancestry never moves an
  Analysis into another Store Release; a disagreement with the Selection is a
  Quality Check.

  *Clarified by #287:* a source with no allele frequencies, such as Sun 2018,
  is still resolved. When its Resolution Record shows ancestry unmeasurable
  for that reason -- under a MAF request, every row read counted without a
  frequency (`maf_rows_missing` equal to `canonical_rows_observed`), and no
  ancestry site -- a release config may opt in (`release_policy.trust_source_ancestry_without_af`)
  to take the Source Metadata Table's `source_superpopulation` as Assigned
  Ancestry, with Ancestry Assignment Method `source_trusted_no_af`. Each such
  Analysis is a warning-level `assigned_ancestry` Quality Check result, in
  `validation.yaml` and the Quality Check sidecar. Without the opt-in it is
  excluded as `ancestry_unassigned`. A measured ancestry never takes the
  source label, and a measured disagreement remains an exclusion; so does a
  source whose frequencies the fit could not read (palindromic sites only,
  unusable standard errors), which has frequencies rather than none.

  *Clarified by #290:* a Selection may name no single super-population
  (`release_policy.selection_superpopulation: multi-ancestry`, dbGaP MVP's
  META bucket, OGS-00016). Such a Selection never assigns Assigned Ancestry:
  each Analysis keeps its measured fit, recorded in the Quality Check sidecar
  and the ancestry sidecar, while the emitted Assigned Ancestry stays empty
  with Ancestry Assignment Method `unassigned`, exactly as the accepted MVP
  bundles carry it. The `assigned_ancestry` check is `not_applicable` there,
  so nothing is excluded for ancestry -- not even a measured super-population,
  because the Selection claims none. And a release that names one may opt in
  (`release_policy.keep_unassigned_ancestry_as_warning`) to keep an Analysis
  the gates left unassigned, still unassigned, as a warning-level
  `assigned_ancestry` result in `validation.yaml` and the sidecar; a measured
  *different* super-population remains `ancestry_not_eur`, and without the
  opt-in nothing changes.
- **A Resolution Record belongs to the Analysis.** It is kept once per Source
  Collection, keyed by the resolver's fingerprint, and reused by every
  Selection. Each bundle's resolution receipt names the records it used.
- **Tabular formats are data, not readers.** A Column Map declares, for one
  Source Collection, the provider columns for each required and optional field
  and the collection's conventions:
  - missing-value tokens;
  - effect type;
  - p-value form;
  - which allele the effect and frequency refer to;
  - chromosome spelling;
  - genome build.

  It travels in the Release Bundle, checksummed, so resolution and Store
  Release Build read every file the same way. Today's GWAS-SSF and FinnGen
  readers become built-in Column Maps. A dedicated reader remains only for a
  format a map cannot describe, such as GWAS-VCF or BESD.
- **Reformatting is acquisition's job, done only when a map cannot cope.** A
  structural change, such as splitting one file into one per Analysis, records
  in the Acquisition Manifest the original's location and checksum and the tool
  that changed it. Nothing is built for this until a source needs it.

  *Clarified by #300:* filtering to a release's region policy at acquisition
  is a second, deliberate case, made for scale: download, filter, delete the
  original. It keeps the same lineage rule, and the lineage also records the
  region policy's parameters. The QC-panel regions are always kept, so that
  Analysis Resolution can still measure ancestry and SD. The original is
  deleted only after the filtered file's checksum is recorded. The filter is
  the registry's (`manifest_generation.region_filter`) and may move upstream
  later; the policy is per release config
  ([spec](../spec/source-connectors.md#region-filtered-acquisition)).

  One historical exception is grandfathered: files whose originals were
  deleted before acquisition recorded lineage. Chen 2023's 1,400 files
  (OGS-00006) were filtered by the retired R generator, which deleted each
  original and recorded its size but no checksum. Their lineage records the
  original's location and size (`original_bytes`), with `original_sha256`
  blank, and they carry the evidence R committed with the bundle
  ([spec](../spec/source-connectors.md#files-the-retired-r-generator-filtered)).
  No checksum is invented for them, and the exception covers no other file:
  every new acquisition records the original's checksum.

## Considered options

- **Adapters as code the pipeline calls** (`fetch(analysis_ids)`). This is a
  tighter interface, but it makes generation depend on live provider APIs.
  Two runs could then silently disagree, and every adapter would have to be
  written in the pipeline's language.
- **One run per collection, routing each Analysis to a Store by Assigned
  Ancestry.** This saves runs. But it re-homes a cohort whose frequencies look
  admixed, a scientific judgement that would be made silently by routing
  rather than shown as a check result.
- **A declared-only path for formats the resolver cannot read.** It would let
  eQTLGen move now. But it creates a category with no Quality Checks, and it
  invites a release to declare values "just this once".
- **A dedicated reader, or a conversion to GWAS-SSF, per new format.** This is
  how today's sources work, and it scales with the number of formats. The
  shared tabular core in OpenGWASDB already reduces the GWAS-SSF and FinnGen
  readers to column declarations; the Column Map makes that declaration data.
- **Resolution Records per Store Release.** They would be re-measured for every
  Trial Store Release, every full release and every composition split, and two
  Stores could carry different measurements of one Analysis.

## Consequences

- **The package (#249) is step 0.** The shared code moves into its final home
  before being reshaped.
- **Sources move onto the pipeline in this order:**
  1. GWAS Catalog first. It generalises `candidate_workflow`, and Chen, Sun and
     later GWAS Catalog collections then need only a release config.
  2. FinnGen.
  3. ukb-b, after the 2.5k OGS-00009 lands from its current generator.
  4. dbGaP MVP.
  5. eQTLGen, once OpenGWASDB resolves BESD.

  Each move regenerates that source's latest bundle and explains every
  difference from the committed one. Built and accepted bundles are never
  touched, and their recorded commands remain as history.
- **The R generators and their libraries retire** as their sources move. This
  ends the duplicate ancestry and effect-scale code that contradicts ADR 0017.
- **OpenGWASDB owns two prerequisites:** the Column Map reader and BESD
  resolution. The Column Map reader blocks no current source, because each one
  has a reader today. When it lands, MVP's acquisition can stop rewriting
  dbGaP files into EBI's GWAS-SSF.
- **The Release Bundle contract grows** a Column Map for tabular collections,
  and per-Analysis Quality Check results.
- **ADR 0032's decisions all stand.** The thin workflow it describes becomes
  the pipeline every source uses. ADR 0009's reader capability gains the Column
  Map.
