# AMR Reference-Completion LD panel: a calibration proposal

**Status:** proposal for owner decision — **not** an approved production policy.
**Author:** OpenGWASDB Stores agent (ticket #217 research follow-up)
**Date:** 2026-10-12
**Scope:** which GRCh38 LD Reference Resource, if any, may complete MVP AMR-assigned
Analyses (#229). This document proposes a **calibration** panel only. It
authorises no download, genotype construction, panel build, Store build or
association read. Production adoption requires the owner decision recorded in
[Open owner decisions](#open-owner-decisions) and independent review.

## 1. Why this is needed

Epic #232 requires a completed descendant for each MVP ancestry except META, and
ticket #229 states the operational prerequisite plainly: *a verified AMR,
assembly-compatible LD Reference Resource is required before Reference
Completion; a convenient EUR panel is not an acceptable silent substitute*.

The registry currently declares no AMR panel. Two facts make that a blocker
rather than an opt-out:

- [`resources/reference-resources/hgdp1kgp-hg38-ld/resource.yaml`](../resources/reference-resources/hgdp1kgp-hg38-ld/resource.yaml)
  declares `ancestry: [AFR, EAS, SAS]`; its description says Analyses assigned
  AMR/MID/NAF "remain observed-only" until a panel exists.
- [`resources/reference-resources/hgdp1kgp-hg38-ld/populations_to_superpop.tsv`](../resources/reference-resources/hgdp1kgp-hg38-ld/populations_to_superpop.tsv)
  maps `CLM`, `MXL`, `PEL`, `PUR` (1kGP Admixed American) and `Pima`, `Maya`,
  `Karitiana`, `Surui`, `Colombian` (HGDP American) to `excluded` / `no_AMR_panel`,
  and `ld_regions_hg38.tsv` (host:
  `/data/opengwasdb/reference/hgdp1kgp-hg38/ld_regions_hg38.tsv`)
  defines block boundaries for **AFR, EAS, EUR and SAS only**.

So the current behaviour is correct-by-design (ADR 0020), but it cannot satisfy
#229. `docs/epic-232-execution.md` (orchestrator-owned, not modified here) already
records: *"#229 needs an explicitly suitable LD resource; HGDP resource declares
AFR/EAS/SAS, not AMR. Missing reference is not observed-only success."*

This proposal answers the owner's question, *"1kg?"*, by evaluating the candidate
sample sources and recommending a calibration design.

## 2. Verified source and sample facts

### 2.1 1kGP Admixed American (Phase 3)

The authority for Phase 3 sample counts is the official IGSR sample panel
[`integrated_call_samples_v3.20130502.ALL.panel`](https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/integrated_call_samples_v3.20130502.ALL.panel)
(55,156 bytes; **SHA-256 `b4023dc6ee2d62ee89c8d4d347db4d348e65518d66d346574cdae7a4bbd76858`**),
not a search summary. Verified from that file: **2,504 samples total**, of which
**347 are AMR**. The four AMR populations and their Phase 3 counts:

| Population | Phase 3 samples | Region |
|---|---:|---|
| CLM (Colombians, Medellín) | 94 | AMR |
| MXL (Mexican Ancestry, Los Angeles) | 64 | AMR |
| PEL (Peruvians, Lima) | 85 | AMR |
| PUR (Puerto Ricans) | 104 | AMR |
| **Total** | **347** | |

The **347 total is verified from the panel file, not assumed.** Phase 3 is the
2,504-sample panel — the "original unrelated" baseline cited in §2.2 — and the
698 related samples / 602 trios belong to the separate 3,202 high-coverage
expansion, so they must not be attributed to these 347. The Phase 3 VCFs are
GRCh37 and would need liftover, so they are cited for counts only and are not
the proposed genotype source
([IGSR Phase 3](https://www.internationalgenome.org/data-portal/data-collection/phase-3),
[IGSR FAQ](https://www.internationalgenome.org/faq/)).

### 2.2 1kGP 30× high-coverage GRCh38

The primary description is Byrska-Bishop et al., *Cell* 185, 3426–3440 (2022):
**3,202 samples**, **602 complete parent–child trios**, **2,504 original
unrelated + 698 additional related samples**, re-sequenced to ~30× and aligned
to **GRCh38**
([PubMed 36055201](https://pubmed.ncbi.nlm.nih.gov/36055201/),
DOI [10.1016/j.cell.2022.08.004](https://doi.org/10.1016/j.cell.2022.08.004);
[IGSR 30× collection](https://www.internationalgenome.org/data-portal/data-collection/30x-high-coverage)).
Filtering the related subset is advised for analyses that assume independence;
the extra depth improves variant discovery, not sample independence. A panel
built from all 3,202 would include 698 non-independent individuals.

### 2.3 gnomAD v3.1.2 HGDP + 1kGP subset (cached candidate)

The registry already acquires this joint GRCh38 genome-wide callset
([`acquire_hgdp1kgp.py`](../resources/scripts/ld-panel/acquire_hgdp1kgp.py),
gnomAD v3.1.2). The primary description of the subset's sample QC and
relatedness handling is **Koenig et al. 2024**, *Genome Research* 34(5):796–809
(DOI 10.1101/gr.278378.123; PMID 38749656; PMC11216312). Their pipeline reduces
the raw source **4,150 → 4,117 after 33 QC failures → 4,094 after 23 ancestry
outliers**; the published 4,094 set comprises **3,400 unrelated + 698 related**
samples (at the 4,117 PCA stage it was 3,419 unrelated + 698 related), genome-wide
on GRCh38 (4,094 = 929 HGDP + 3,165 1kGP)
([gnomAD v3.1.2 release](https://gnomad.broadinstitute.org/news/2021-10-22-gnomad-v3-1-2-minor-release/),
[gnomAD downloads](https://gnomad.broadinstitute.org/downloads#v3-hgdp-1kg)).
The **kinship > 0.05 / maximal-independent-set** prune reused for the LD subset
here is a **proposed reuse of the paper's PCA-relatedness QC choice**, not a
claim that upstream calibrated that threshold for LD-panel construction, and the
choice is this pilot's pending owner approval.

Measured directly from the host's cached
`/data/opengwasdb/reference/hgdp1kgp-hg38/source/cohort_populations.tsv`
(sample metadata only — no genotype read):

- The cache has **4,151 rows = 3,202 1kGP + 948 HGDP + 1 control**;
  `high_quality == true` is **4,097 = 3,166 1kGP + 931 HGDP**. This named local
  `high_quality` set is **not the same selection as the published 4,094**: the
  paper's pipeline is raw 4,150 → 4,117 (33 QC) → 4,094 (23 ancestry outliers).
  The task is therefore to **reconcile exact QC and ancestry-outlier membership**
  against the paper's selection, not to resolve a 3-row count difference in
  isolation.
- **AMR rows:** 1,000 Genomes 490, HGDP 62 (all-project); `high_quality` gives
  487 1kGP AMR and 62 HGDP American.
- **The official Phase 3 AMR baseline is the 347 IDs from §2.1.** All 347 are in
  the metadata; after the gnomAD `high_quality` flag **345 remain** (CLM 92,
  MXL 64, PEL 85, PUR 104); `HG01137` and `HG01366` fail. The **baseline is
  therefore ≤ 345 after QC/kinship filtering**, and the final n is unknown until
  the joint callset's own kinship/pedigree is applied. The other 143 cached AMR
  samples are the expanded related additions and are **not** an unrelated
  baseline.
- **Native GRCh38, not a Phase 3 lift-over.** The joint callset is gnomAD v3.1.2
  GRCh38 genome-wide; no liftover is required, unlike the GRCh37 Phase 3 VCFs.
- This cached callset is a **candidate**, not a verified panel input: its sample
  identity and QC still need reconciliation, and a different callset could carry
  different QC and genotypes.

### 2.4 HGDP American populations are not a substitute for admixed 1kGP AMR

The HGDP American continent is represented by five indigenous populations —
Pima, Maya, Karitiana, Surui and Colombian — with small, relatedness-heavy
samples (the HGDP-CEPH panel has ~25 each before removing duplicate/relative
pairs; e.g. Karitiana/Surui are strongly bottlenecked and homozygous)
([HGDP resource](https://www.hagsc.org/hgdp/),
[Cann et al. 2002, PubMed 11954565](https://pubmed.ncbi.nlm.nih.gov/11954565/)).
1kGP AMR (CLM/MXL/PEL/PUR) are modern **admixed** populations with tripartite
Native-American/European/West-African ancestry. They are not interchangeable:
the HGDP groups are baseline indigenous references; the 1kGP groups are
contemporary admixed cohorts.

## 3. Why HGDP American populations must not be silently pooled

Pooling HGDP American `Pima/Maya/Karitiana/Surui/Colombian` with 1kGP
`CLM/MXL/PEL/PUR` into one "AMR" panel is a **scientific-suitability decision,
not a categorical invalidity.** A four-population 1kGP AMR panel is itself a
pool, and the registry's ADR 0028/0039 ancestry alias matching would accept a
declared pool just as it accepts that one. The concerns are instead:

1. **A different LD regime.** Bottlenecked, highly homozygous indigenous
   Amazonian groups and post-Columbian admixed urban cohorts have different
   long-range LD; a merged eigenbasis may not be the LD of any Analysis.
2. **Auditability is not suitability.** ADR alias matching can audit *that* the
   panel a release used matches its declaration; it cannot certify that the
   pooled LD is scientifically appropriate.
3. **It needs its own policy and calibration.** If HGDP American samples are
   ever pooled with 1kGP AMR, that must be an explicit, separately calibrated
   policy decision — not a default or an implicit side effect.

Recommendation: keep the HGDP American populations on a **separate calibration
axis** and out of the primary AMR panel for now, but treat this as a
policy/calibration question rather than a prohibition.

## 4. Recommendation

**Propose a calibration-only AMR panel built from the official 347 Phase 3 AMR
IDs (≤ 345 after gnomAD QC/kinship), sourced from the native-GRCh38 joint
callset, and estimated on the existing block layout.** Then decide production
from measured calibration evidence, not from this proposal.

Rationale:

- **Start from the cached joint callset as a candidate.** It contains the 347
  Phase 3 AMR IDs, but it is not yet a verified input: identity and QC need
  reconciliation (§2.3). A fresh 1kGP 30× callset may differ in QC, calls and
  source identity and would include 698 related samples; it is a genuine
  alternative, not automatically deeper or better, and the owner may prefer it.
- **n is small and the floor is not fixed.** The official baseline is 347 (≤ 345
  after QC), and the expanded 490/487 are **not** an unrelated baseline; the
  final n is unknown until kinship filtering is applied. The production
  AFR/EAS/SAS panels use 804/817/783 samples, so AMR is a calibration artifact
  whose quality must be measured (ADR 0020, ADR 0031). MAC ≥ 50 at n = 347
  implies MAF ≥ 7.20%, so the variant-inclusion floor must be chosen by the
  sensitivity pilot (§6), not inherited.
- **Separate resource declaration.** A new
  `hgdp1kgp-hg38-amr-calibration/` resource mirrors
  `hgdp1kgp-hg38-eur-calibration/` (status `available`, explicitly
  non-production, `role: calibration`). The production
  `hgdp1kgp-hg38-ld` policy stays unchanged (no AMR), so no silent policy
  expansion occurs.

## 5. Block-boundary strategy (calibration, not policy)

`ld_regions_hg38.tsv` has no AMR rows. Options assessed:

| Option | Basis | Assessment |
|---|---|---|
| **A. Reuse EUR boundaries (1,357 blocks)** | Matches the already-built HGDP+1kGP EUR calibration control's layout | **Recommended for calibration.** The rationale is a fixed, matched control layout — comparable boundaries and a like-for-like comparison — not any assumption about AMR admixture proportions. Avoids re-deriving boundaries from n ≈ 347. |
| B. Reuse AFR boundaries (1,580) | Captures the African-ancestry component | Use as a sensitivity check where A and B differ; not the primary layout. |
| C. Union/intersection of A and B | Hybrid | Adds complexity before the primary calibration is understood. Defer. |
| D. Derive AMR boundaries *de novo* | Recombination structure of the AMR sample | **Not chosen for the first calibration, not categorically rejected.** Boundary detection at n ≈ 347 on an admixed sample is noisier than the pinned production set (ADR 0020) and breaks comparability, but de-novo boundaries remain a legitimate future option. |

**Critical distinction:** reusing EUR **block boundaries** is not substituting the
EUR **panel**. The LD/eigenvectors are still estimated from AMR samples. The
boundary reuse must be declared explicitly in the resource; a reader must never
be able to mistake this for the EUR panel. Boundary choice is labelled
**calibration** until the owner approves a production policy.

**What the EUR-boundary control cannot see.** Reusing EUR boundaries may omit
LD that crosses a EUR boundary in the admixed AMR sample, so a future production
gate must include an explicit **cross-block test** (AMR-panel imputation across
boundary edges, plus a de-novo-boundary sensitivity), not only within-block
quality.

## 6. Variant QC, alignment, relatedness and sample inventory

| Concern | Proposal |
|---|---|
| Assembly / identifiers | Native GRCh38 joint callset; canonical ALID `chr:pos:A1:A2`, matching `construct_block.canonical_alid` (ADR 0020/0031) |
| Variant inclusion | **Do not blindly copy MAC ≥ 50.** At n = 347 diploid (694 chromosomes) MAC ≥ 50 implies MAF ≥ **7.20%**; the optional expanded n = 487 only lowers that to 5.13% and is not the baseline. Propose a **MAC/MAF sensitivity pilot** over candidate floors (e.g. MAC 20/30/50) and adopt no lower cutoff without calibration evidence |
| Missingness | mean-impute (existing `construct_block`), invariant/all-missing variants dropped |
| Orientation | canonical EA = min(allele) with the existing reverse-code rule |
| Relatedness | apply the joint callset's own pedigree/QC and the **Koenig et al. 2024** kinship > 0.05 / maximal-independent-set prune (proposed reuse of their PCA QC choice); record the retained count; the baseline is the official 347 (≤ 345 by `high_quality`) and the final n is unknown until this runs |
| Sample inventory | start from the official Phase 3 AMR IDs (CLM 94/MXL 64/PEL 85/PUR 104; 345 after `high_quality`); the 143 expanded related AMR IDs are optional pruned sensitivity only; HGDP American excluded from the primary panel by default; a `membership.json` per panel |
| Assembly check | reuse `verify_vcf` (GRCh38 header) and `verify_index`; no liftover |
| Provenance | every block's `.provenance.json` (n individuals, input/retained variants, MAC, cumvar, components, achieved variance) |

**MAC/MAF is the sharpest open methodological question.** The existing
AFR/EAS/SAS panels use MAC ≥ 50 because MAC tracks estimation precision at
n ≈ 800 while MAF would not (ADR 0020). At AMR's ~345 samples the same MAC floor
is a ≥ 7.20% MAF filter, and a smaller retained n raises it further, which could
exclude exactly the lower-frequency variants completion needs. The pilot must
compare floors rather than inherit one, and must state the chosen floor's
MAF-equivalent and retained variant counts per block. No lowered cutoff is
adopted by this proposal.

The sample-metadata TSV is CRLF-terminated; any consumer must normalise line
endings before comparing `high_quality` (found while measuring §2.3), and must
not infer relatedness from the `high_quality` flag alone.

## 7. Resource and eigendecomposition contract

The panel must satisfy the existing core public contract, not invent a new one:

- **Representation:** eigendecomposition, not an LD matrix
  ([core ADR 0031](https://github.com/opengwas/opengwasdb/blob/dev/docs/adr/0031-ld-panels-store-eigendecompositions.md)).
  `load_ld_eigenvectors` reads `.ldeig.npz`; the matrix is optional/dead weight.
- **Components:** variance-driven, cumulative target **0.99**. The existing
  `component_floor` of 250 is a *minimum request limited by available rank*, not
  a mathematical guarantee that every block has ≥ 250 components at n ≈ 347;
  record the achieved component count and variance per block, and require the
  consumer's 0.9 truncation to be satisfiable from stored data
  (`find_underresolved_blocks.py` zero under-resolved blocks at 0.9).
- **PSD:** the thin-SVD construction yields `eigenvalues = s² ≥ 0` by
  construction, so PSD is structural; the pilot still asserts non-negative
  eigenvalues and finite vectors.
- **`resource.yaml`:** `kind: ld_panel`, `ancestry: [AMR]`, `genome_build: GRCh38`,
  `variant_id_convention: "chr:pos:a1:a2"`, `ld.block_file`, `ld.representation`,
  `ld.components_retained: variance_driven`, `ld.components_cumvar_target: 0.99`,
  `source.n_individuals.AMR`, `source.variant_inclusion` (the calibrated
  MAC/MAF floor, with its MAF-equivalent recorded),
  `source.population_map` (calibration policy), `source.generator`, and an
  explicit description that the block boundaries are reused from the EUR
  production layout and the panel is **calibration, not production**.
- **Membership policy:** a **separate calibration mapping** (e.g.
  `populations_to_superpop.amr-calibration.tsv`) so the production
  `populations_to_superpop.tsv` keeps `no_AMR_panel` until the owner changes it.
  `make_membership.py`'s hard-coded `("AFR","EAS","SAS","EUR")` handle list would
  need an AMR entry in the calibration path only.

## 8. Pilot, integrity and scientific validation

1. **Pilot scope.** One to five blocks (mirror the EUR control's 5-block,
   2,514-analysis design), sufficient to measure wall time, peak RSS, block
   success rate and eigenvalue conditioning. No genome-wide build.
2. **Integrity.** Per-block provenance present and positive; `find_underresolved_blocks.py
   --thresh 0.9` reports zero; sample counts match `membership.json`; the
   cache-vs-release sample discrepancy (§2.3) is reconciled first.
3. **Scientific validation.** A paired held-out comparison on AMR-assigned MVP
   Analyses, mirroring `compare_eur_imputation.py`/`evaluate_calibration.py`:
   compare AMR-panel imputation quality against a pre-registered baseline with an
   explicit acceptable mean loss. The honest baseline is an **AMR-aware**
   comparison (e.g. held-out ALIDs within the AMR cohort), **not** the EUR panel
   silently standing in for AMR.
4. **Representation trade-offs to report.** 1kGP-only vs 1kGP+HGDP (separate vs
   pooled); EUR-boundary vs AFR-boundary sensitivity **including an explicit
   cross-block test** at boundary edges; per-population versus pooled AMR
   estimates; **MAC/MAF-floor sensitivity** (MAC 20/30/50 with the MAF-equivalent
   and retained-variant counts); and the effect of relatedness pruning on n and
   on block conditioning.
5. **Budget.** Measure in the pilot (wall time, peak RSS, scratch per block, job
   count) and record it with the evidence. **Do not infer the budget from core
   #254**, which is a validation-memory workstream, not an LD-panel measurement.

## 9. Explicit non-goals

- No European-panel substitution for AMR completion.
- No full production AMR panel or Store build before owner approval.
- No silent change to the production `no_AMR_panel` policy.
- No download, genotype construction or association read in this proposal.
- No change to the AFR/EAS/SAS/EUR block file or the existing panels.

## Open owner decisions

1. **Approve a calibration AMR panel** from the existing gnomAD HGDP+1kGP cache
   using 1kGP CLM/MXL/PEL/PUR (recommended), or require a fresh 1kGP 30×
   download, or defer AMR completion.
2. **HGDP American sample use:** exclude from the primary panel (recommended),
   or build a separate HGDP-American-only sensitivity panel; **never silently
   pool**.
3. **Block boundaries:** adopt reused EUR boundaries for calibration
   (recommended), with AFR-boundary sensitivity; or commission AMR boundary
   derivation.
4. **Production gate:** the minimum pilot evidence (n retained, blocks resolved,
   mean imputation-quality loss, peak RSS) required before AMR completion is
   declared production-suitable, and who signs it off.
5. **Acceptability:** the measured retained AMR n is the primary baseline of at
   most 345 (unknown after kinship pruning), **not 487**. If that is inadequate
   for a production panel, the Reference-Completed release remains **blocked** —
   a missing reference is not an observed-only epic success.
6. **Variant floor:** the MAC/MAF floor adopted after the sensitivity pilot (no
   lowered cutoff without calibration), and whether a floor that implies a high
   MAF is acceptable for AMR completion.

## Owner request and review

This proposal answers the owner's AMR reference question recorded on the epic:
<https://github.com/opengwas/opengwasdb-stores/issues/232#issuecomment-6133854434>.
It is submitted for independent review before any owner approval, and no panel
build is authorised by writing it.

## Citations

- **Primary Phase 3 sample panel (source of the 347 counts):**
  <https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/integrated_call_samples_v3.20130502.ALL.panel>
  (55,156 bytes; SHA-256 `b4023dc6ee2d62ee89c8d4d347db4d348e65518d66d346574cdae7a4bbd76858`).
- IGSR Phase 3 collection (2,504 unrelated, 26 populations):
  <https://www.internationalgenome.org/data-portal/data-collection/phase-3>
- **Byrska-Bishop et al. 2022**, *Cell* 185, 3426–3440 (3,202 = 2,504 unrelated +
  698 related; 602 trios; GRCh38); DOI 10.1016/j.cell.2022.08.004, PMID 36055201,
  PMC9439720: <https://pubmed.ncbi.nlm.nih.gov/36055201/>
- IGSR 30× high-coverage GRCh38 collection:
  <https://www.internationalgenome.org/data-portal/data-collection/30x-high-coverage>
- IGSR population pages (CLM/MXL/PEL/PUR):
  <https://www.internationalgenome.org/data-portal/population/CLM>,
  <https://www.internationalgenome.org/data-portal/population/MXL>,
  <https://www.internationalgenome.org/data-portal/population/PEL>,
  <https://www.internationalgenome.org/data-portal/population/PUR>
- IGSR FAQ (relatedness/pedigrees): <https://www.internationalgenome.org/faq/>
- gnomAD v3.1.2 release (HGDP+1kGP subset, 4,094 = 929 + 3,165, GRCh38):
  <https://gnomad.broadinstitute.org/news/2021-10-22-gnomad-v3-1-2-minor-release/>
  and downloads: <https://gnomad.broadinstitute.org/downloads#v3-hgdp-1kg>
- **Koenig et al. 2024**, *Genome Research* 34(5):796–809 (HGDP+1kGP sample QC
  and relatedness: raw 4,150 → 4,117 → 4,094; 3,400 unrelated + 698 related);
  DOI 10.1101/gr.278378.123, PMID 38749656, PMC11216312:
  <https://pmc.ncbi.nlm.nih.gov/articles/PMC11216312/>
- HGDP-CEPH resource: <https://www.hagsc.org/hgdp/>
- Cann et al. 2002, "A human genome diversity cell line panel", *Science*
  296(5566):261–262; DOI 10.1126/science.296.5566.261b, PMID 11954565:
  <https://pubmed.ncbi.nlm.nih.gov/11954565/>
- Owner AMR-reference request (epic comment):
  <https://github.com/opengwas/opengwasdb-stores/issues/232#issuecomment-6133854434>
- Core LD-panel contract: `docs/adr/0031-ld-panels-store-eigendecompositions.md`
  in opengwas/opengwasdb.
- Registry decisions: ADR 0020 (non-EUR panels), ADR 0028 (ancestry-matched
  completion), ADR 0039 (region identity and ancestry refusal), ADR 0019
  (Reference-AF/effect-scale validation).
