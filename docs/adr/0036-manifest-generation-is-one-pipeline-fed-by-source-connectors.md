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
- **Connectors are files, frozen with the inventory.** Generation reads no live
  source. A metadata change is a reviewed re-freeze, never a silent difference
  between two runs.
- **One Selection gives one Release Bundle.** A collection split into several
  Store Releases is several Selections. Assigned Ancestry never moves an
  Analysis into another Store Release; a disagreement with the Selection is a
  Quality Check.
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
