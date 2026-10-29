# OpenGWAS Roadmap

Status: draft  
Last updated: 2026-07-06

## Overview

OpenGWAS should move from being a very large online archive of GWAS summary statistics into a modular, reproducible, high-performance summary-statistics compute substrate.

The current model is economically and operationally fragile: around 20 TB of GWAS VCF files plus multiple derived query formats, all maintained centrally, with cloud costs growing faster than plausible revenue. OpenGWASDB changes the shape of the problem. It lets us publish compact, immutable Store Releases that can be queried locally, on HPC, or behind a thin API without rebuilding many specialised copies of the same data.

The strategic aim is to create a pipeline from published GWAS to performant storage to whole-phenome enabled analyses.

## OpenGWASDB storage format

OpenGWASDB gives OpenGWAS a different architecture:

- Store data once in compact, self-contained releases rather than keeping many query-specific representations.
- Use Dense stores for full GWAS batches that share a large variant axis.
- Use Ragged stores for sparse molecular QTL, cis-plus-signals, and other uneven datasets.
- Complete stores against declared reference panels at build time so common query paths do not require online LD proxy computation.
- Keep stores modular by data batch, ancestry, assay, or release cycle.
- Query stores through the same layout-independent API whether they are local files, mounted HPC storage, or routed through a remote service.
- Treat published Store Releases as immutable; editing means building a new release from a changed manifest, not mutating a live object.

This makes OpenGWAS less like a single cloud-hosted database and more like a family of versioned analytical assets.

Performance:

- A full UK Biobank dense observed-only benchmark built 2,514 GWAS VCFs into a 58.05 GB store from 425.4 GB of source VCFs, a 7.33x reduction.
- The same benchmark retained fast stateless query patterns over a genome-wide store, including PheWAS, regional, top-hit, random lookup, and full-analysis extraction.
- A ragged eQTLGen cis benchmark shows the separate sparse path for molecular-style datasets where dense storage is the wrong default.

Main finding: GWAS summary statistics can be stored in a compact way with highly performant querying.

## Principles of the opengwas framework

1. Store releases are the unit of trust.
   A Store Release must be self-identifying, immutable, validated, and interpretable without a running catalogue service.

2. Local-first, service-compatible.
   The same store should support laptop, HPC, and cloud/API usage. The service layer should route and authorise queries, not become the only place the data can be used. The interface to the store should be user friendly and easy to install.

3. Build once, query many.
   Expensive normalisation, liftover, reference completion, and indexing should happen during build or release generation, not inside interactive user queries.

4. Dense and Ragged are peers.
   Full-coverage GWASs and sparse molecular datasets need different storage layouts. The roadmap should not force sparse biology into dense arrays just for uniformity.

5. Reference completion is a release enhancement.
   Imputed associations must remain distinguishable from observed associations, and quality metrics must be recorded. Completion produces a new release.

6. The catalogue is outside the store.
   Store discovery, remote routing, default release selection, billing, and access control belong in a catalogue/service layer above the file format.

7. Reference-panel assignment should be data-derived where possible.
   Source ancestry labels are useful but incomplete. Allele-frequency similarity between each analysis and a pool of LD reference panels should help decide which reference panel and therefore which store an analysis belongs to.

## Ancestry-aware store assignment

Reference completion depends on having the right LD reference panel. The existing UKB EUR LD eigenvector scripts should become the first implementation of a reusable reference-panel build pipeline, then be repeated for each major ancestry.

A possible EBI assignment workflow:

1. Build a pool of ancestry-specific LD reference panels, each with eigenvectors, LD blocks, variant lists, allele frequencies, checksums, and provenance.
2. For each incoming analysis, compute an allele-frequency fingerprint over a shared, well-behaved set of variants.
3. Compare the analysis fingerprint against each reference panel using metrics such as allele-frequency correlation, weighted RMSE, principal-component projection, and missingness.
4. Assign the analysis to the best matching reference panel when the distance is clearly below a threshold.
5. Cluster analyses with the same reference-panel assignment into the same store when their coverage, source format, access class, and release cadence also align.
6. Mark mixed, uncertain, or low-information analyses explicitly rather than forcing them into a confident ancestry bucket.
7. Record the selected panel, competing panel scores, variants used, thresholds, and assignment confidence in the build manifest and catalogue.

This gives the store factory a practical partitioning rule: stores can be organised around reference panels, not only around source collections. It also makes reference completion, query semantics, and downstream causal inference more transparent.

Caveats:

- Allele-frequency similarity is a proxy for LD compatibility, not proof of it.
- Multi-ancestry meta-analyses may need a mixed/unassigned class, multiple completed releases, or a policy of observed-only storage.
- EAF availability and scope vary by source; the assignment algorithm must tolerate missing, variant-scoped, and association-scoped frequencies.
- Palindromic alleles, strand issues, low INFO variants, and allele-orientation errors should be excluded or down-weighted during assignment.
- Panel choice should remain auditable and overrideable when curator knowledge is stronger than the automatic score.

## Roadmap

### Horizon 1: Stabilise The Core Store Engine

Goal: make OpenGWASDB reliable enough to replace the expensive internal query formats for the main OpenGWAS data classes.

Deliverables:

- Freeze a v0.1/v0.2 Store Release contract for Dense Observed-Only full-GWAS stores.
- Complete the Ragged Observed-Only path for cis-plus-signals and molecular QTL stores.
- Maintain one public sparse array query contract across Dense and Ragged layouts.
- Harden build pipelines from GWAS VCF, GWAS-SSF-like tabular sources, BESD-like sparse inputs, and batch manifests.
- Expand validation so corrupted manifests, indexes, arrays, association status, and variant axes fail before query time.
- Maintain benchmark reports for storage footprint, build time, and query latency by store type.
- Define acceptance targets for production replacement, such as storage ratio, build throughput, query latency, and validation runtime.

Key decision:

- OpenGWASDB should become the canonical analytical representation for summary statistics; legacy VCF and source formats should become provenance and rebuild inputs, not the primary query substrate.

### Horizon 2: Reference Completion

Goal: remove query-time imputation and make stores comparable on declared reference panels.

Deliverables:

- Define an LD reference panel registry with panel identity, assembly, ancestry, variant set, LD blocks, checksums, allele convention, and provenance.
- Generalise the current UKB EUR LD eigenvector scripts into a repeatable panel-build workflow for each major ancestry.
- Store allele-frequency fingerprints for LD reference panels and, when available, for analyses.
- Prototype automated reference-panel assignment for EBI analyses using allele-frequency similarity.
- Implement Dense Reference-Completed releases for full GWAS batches.
- Implement Ragged Reference-Completed regions for cis and significant trans regions without expanding suggestive singleton signals.
- Store imputed `z` and `se`, association status, imputation masks, and block-by-analysis quality summaries.
- Support observed-only query mode over completed stores.
- Benchmark completion cost and query-time savings across ancestry-specific panels.

Key decision:

- Reference completion should be treated as a reproducible release-generation step, not as an online service feature. The API can expose completed results, but should not be responsible for computing them interactively.
- Store partitioning should be allowed to follow reference-panel assignment: if two analyses match different LD panels, they should usually not be forced into the same completed store.

### Horizon 3: Store Factory And Release Operations

Goal: make it feasible to build and maintain many stores, including dozens of small sparse stores and several very large multi-ancestry stores.

Deliverables:

- Create a store-build factory that consumes source manifests and emits validated Store Releases plus build reports.
- Support batch-level build manifests for ancestry, assay, consortium, source format, access class, and release cadence.
- Add analysis-to-panel assignment reports so curators can inspect why an analysis entered a particular ancestry-specific store.
- Add resumable and parallel build execution for large dense stores.
- Add incremental build planning for unpublished working sets, while preserving immutable published releases.
- Represent add/remove/edit operations as manifest diffs that produce new releases.
- Track release lineage: source release, observed-only store, completed store, derived products, and superseded releases.
- Build a registry of expected stores, likely including full GWAS stores, ancestry-specific stores, molecular sparse stores, and derived-analysis stores.
- Produce operational dashboards for storage footprint, build time, validation failures, and query benchmark regressions.

Key decision:

- "Editing stores" should mean editing build manifests and producing new releases. Mutable published stores would undercut reproducibility and make downstream analyses hard to audit.

### Horizon 4: Thin Service Layer

Goal: provide remote access to many stores while keeping the stores usable without the service.

Deliverables:

- Design a RESTful API for exact variant, range, PheWAS, top-hit, sparse lookup, analysis extraction, and metadata queries.
- Add a catalogue that maps store IDs, releases, ancestries, assays, traits, and access policies to physical store locations.
- Route one request across multiple Store Releases and return a unified sparse array result plus metadata joins.
- Support stateless workers that can mount stores from local disk, object storage, or HPC filesystems.
- Add authentication, authorisation, quotas, and audit logging outside the Store Release format.
- Add client libraries that can use either local paths or remote API endpoints with similar query semantics.
- Keep expensive derived computations out of synchronous API paths unless they operate on precomputed store products.

Key decision:

- The API should be a router and access layer over Store Releases, not a replacement for the Store Release abstraction.

### Horizon 5: Derived Genetic Knowledge Products

Goal: leverage the assembled, performant store system to create analyses that are difficult or impossible with the current archive.

Deliverables:

- Build MR of everything versus everything from precomputed instruments and store-native sparse queries.
- Store sample-overlap matrices, using the pleiodb approach as prior art, so downstream causal and genetic-correlation analyses can account for overlap.
- Store fine-mapping credible sets and very sparse Bayes factors as first-class derived products linked back to source Store Releases.
- Extend the format strategy for WGS-scale summary statistics where variant counts and allele complexity exceed current GWAS assumptions.
- Produce cross-store trait, variant, instrument, locus, and ancestry indexes for discovery and batch analysis.
- Feed completed and harmonised stores into GPMap analyses as a stable summary-statistics layer.

Key decision:

- OpenGWASDB becomes most valuable when it is not only a fast lookup engine, but the substrate for repeatedly generated derived datasets: instruments, overlap matrices, credible sets, fine-mapping summaries, and all-by-all causal scans.

## Strategic Value

This approach gives us two things:

1. A cost effective way to continue hosting OpenGWAS sustainably
2. An analytical substrate for causal inference.

The exact value proposition needs to be refined.

## Near-Term Backlog

Highest priority:

- Productionise dense full-GWAS builds and validation.
- Finish ragged observed-only build/query/validation for molecular datasets.
- Turn the UKB EUR LD eigenvector scripts into a reusable reference-panel build pipeline.
- Prototype EBI analysis-to-reference-panel assignment using allele-frequency similarity.
- Define production acceptance metrics and run them on representative stores.
- Make build manifests explicit enough to reproduce store releases.
- Decide the first official store partitioning strategy: by reference panel, ancestry, data source, assay, release, or a combination.

Next priority:

- Implement reference completion against one GPMap/LD panel path.
- Design the external catalogue schema.
- Specify REST endpoints and response shapes around the existing sparse array query contract.
- Define derived-product store types for sample overlap, credible sets, Bayes factors, and MR results.

Later priority:

- Multi-panel and multi-ancestry completion at scale.
- WGS-scale format extensions.
- All-by-all MR production pipeline.
- Cost-aware deployment across object storage, HPC filesystems, and API workers.

## Open Questions

- What is the first production store family that proves the replacement: UK Biobank full GWAS, all current OpenGWAS full GWAS, or a mixed dense-plus-ragged pilot?
- What are the storage and latency thresholds required before OpenGWASDB can retire a current query format?
- Should official public stores be partitioned primarily by source collection, ancestry, access policy, assay type, or release cadence?
- Should official completed stores be partitioned primarily by assigned LD reference panel?
- Which reference panels should define the first completed releases, and how will ancestry labels be governed?
- What thresholds should decide confident, uncertain, mixed, or unassigned allele-frequency matches?
- How should multi-ancestry meta-analyses be represented: observed-only, assigned to the closest panel, or completed separately against multiple panels?
- What derived product should be the flagship demonstration of the new platform: all-by-all MR, fine-mapped credible sets, sample-overlap-aware scans, or GPMap integration?
- How much of the service layer should be OpenGWAS-specific, and how much should remain generic over Store Releases?
