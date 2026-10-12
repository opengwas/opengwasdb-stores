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

IGSR's Phase 3 collection is **2,504 unrelated samples from 26 populations**.
The four AMR populations and their official Phase 3 counts

| Population | Phase 3 samples | Region |
|---|---:|---|
| CLM (Colombians, Medellín) | 94 | AMR |
| MXL (Mexican Ancestry, Los Angeles) | 64 | AMR |
| PEL (Peruvians, Lima) | 85 | AMR |
| PUR (Puerto Ricans) | 104 | AMR |
| **Total** | **347** | |

The **347 total is verified, not assumed**, and it **includes related
individuals**: Phase 3 trios were retained for phasing even though the panel was
intended for allele-frequency work ([IGSR Phase 3](https://www.internationalgenome.org/data-portal/data-collection/phase-3),
[IGSR FAQ](https://www.internationalgenome.org/faq/)).

### 2.2 1kGP 30× high-coverage GRCh38

The NYGC high-coverage re-sequencing aligned to **GRCh38** contains **3,202
samples = 2,504 unrelated + 698 related, completing 602 parent–child trios**
([IGSR 30× collection](https://www.internationalgenome.org/data-portal/data-collection/30x-high-coverage)).
IGSR advises filtering the related subset for analyses that assume independence,
and provides pedigree files to do so. The extra depth improves variant discovery,
not the independence of the sample set; a panel built from all 3,202 would
include ~698 non-independent individuals.

### 2.3 gnomAD v3.1.2 HGDP + 1kGP subset (already cached)

The registry already acquires this joint GRCh38 genome-wide callset
([`acquire_hgdp1kgp.py`](../resources/scripts/ld-panel/acquire_hgdp1kgp.py),
gnomAD v3.1.2). gnomAD describes the subset as **~4,094 samples (929 HGDP +
3,165 1kGP)** genome-wide on GRCh38, with a recommended relatedness filter of
pairwise kinship **> 0.05** pruned by a maximal independent set
([gnomAD downloads](https://gnomad.broadinstitute.org/downloads)).

Measured directly from the host's cached
`/data/opengwasdb/reference/hgdp1kgp-hg38/source/cohort_populations.tsv`
(sample metadata only — no genotype read):

- AMR rows by project: **1,000 Genomes 490, HGDP 62**; `high_quality == true`
  (gnomAD QC flag) gives **487** 1kGP AMR and **62** HGDP American.
- 1kGP AMR high-quality: CLM 130, MXL 97, PEL 122, PUR 138.
- HGDP American high-quality: Maya 21, Pima 14, Karitiana 12, Surui 8,
  Colombian 7.
- The cache holds 3,202 1kGP rows and 948 HGDP rows, whereas the gnomAD release
  note says 3,165 + 929. **This discrepancy must be reconciled before any build**
  (a v3.1.2 vs later-subset mismatch is the likely cause); it is flagged rather
  than smoothed over.

### 2.4 HGDP American populations are not a substitute for admixed 1kGP AMR

The HGDP American continent is represented by five indigenous populations —
Pima, Maya, Karitiana, Surui and Colombian — with small, relatedness-heavy
samples (the HGDP-CEPH panel has ~25 each before removing duplicate/relative
pairs; e.g. Karitiana/Surui are strongly bottlenecked and homozygous)
([HGDP resource](https://www.hagsc.org/hgdp/), [Cann et al. 2002](https://www.nature.com/articles/nature02029)).
1kGP AMR (CLM/MXL/PEL/PUR) are modern **admixed** populations with tripartite
Native-American/European/West-African ancestry. They are not interchangeable:
the HGDP groups are baseline indigenous references; the 1kGP groups are
contemporary admixed cohorts.

## 3. Why HGDP American populations must not be silently pooled

Pooling HGDP American `Pima/Maya/Karitiana/Surui/Colombian` with 1kGP
`CLM/MXL/PEL/PUR` into one "AMR" panel would:

1. **Break ancestry identity.** The registry's ADR 0028 filters completion by
   `assigned_ancestry`; ADR 0039 requires exact alias matching and refuses a
   panel that matches nothing. A pool is a synthetic mixture that matches no
   declared ancestry and cannot be audited against either source.
2. **Mix incompatible LD regimes.** Bottlenecked, highly homozygous indigenous
   Amazonian groups and post-Columbian admixed urban cohorts have different
   long-range LD. The resulting eigenvectors are not the LD of any Analysis.
3. **Reproduce the exact failure the policy exists to prevent.** ADR 0020 already
   excludes 1kGP ASW/ACB from AFR because "recent admixture creates long-range LD
   absent from continental African populations". The same argument excludes
   pooling here — with the additional problem that it would look like an AMR
   panel while being neither admixed nor indigenous.

If HGDP American samples are used at all, they belong on a **separate
calibration axis** (an HGDP-American-only sensitivity panel), never silently
merged. This document recommends deferring even that until the 1kGP-AMR
calibration is measured.

## 4. Recommendation

**Build a calibration-only AMR panel from the already-cached gnomAD v3.1.2
HGDP+1kGP GRCh38 callset, using 1kGP CLM/MXL/PEL/PUR as the sample set, and
estimate it on the existing block layout.** Then decide production from measured
calibration evidence, not from this proposal.

Rationale:

- **Reuse, not new download.** The joint callset is already verified and cached
  (§2.3); it contains 487 high-quality 1kGP AMR samples. A new 1kGP 30× download
  would add depth but also 698 related samples and several hundred GB of
  redundant data — no new information about AMR LD that the existing callset does
  not already carry for the same individuals.
- **n ≈ 487 is within the existing non-EUR regime.** The production AFR/EAS/SAS
  panels use 804/817/783 samples; 487 is smaller but not a different order of
  magnitude, so an AMR panel is a legitimate calibration artifact whose quality
  must be measured against the EUR control (ADR 0020, ADR 0031).
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
| **A. Reuse EUR boundaries (1,357 blocks)** | EUR is the largest ancestry component of admixed AMR; layout identical to the EUR calibration control | **Recommended for calibration.** Keeps the AMR calibration directly comparable to the EUR control and avoids re-deriving boundaries from n ≈ 487. |
| B. Reuse AFR boundaries (1,580) | Captures the African-ancestry component | Use as a sensitivity check where A and B differ; not the primary layout. |
| C. Union/intersection of A and B | Hybrid | Adds complexity before the primary calibration is understood. Defer. |
| D. Derive AMR boundaries *de novo* | Recombination structure of the AMR sample | **Not recommended as policy.** Boundary detection at n ≈ 487 on an admixed sample yields noisier boundaries than the pinned production set (ADR 0020), and would break comparability. May be computed offline as evidence that the reused layout is adequate. |

**Critical distinction:** reusing EUR **block boundaries** is not substituting the
EUR **panel**. The LD/eigenvectors are still estimated from AMR samples. The
boundary reuse must be declared explicitly in the resource; a reader must never
be able to mistake this for the EUR panel. Boundary choice is labelled
**calibration** until the owner approves a production policy.

## 6. Variant QC, alignment, relatedness and sample inventory

| Concern | Proposal |
|---|---|
| Assembly / identifiers | GRCh38; canonical ALID `chr:pos:A1:A2`, matching `construct_block.canonical_alid` (ADR 0020/0031) |
| Variant inclusion | MAC ≥ 50, unchanged; a frequency floor would be unestimable at n ≈ 487 |
| Missingness | mean-impute (existing `construct_block`), invariant/all-missing variants dropped |
| Orientation | canonical EA = min(allele) with the existing reverse-code rule |
| Relatedness | use gnomAD v3.1.2 kinship > 0.05 / maximal-independent-set pruning, and record the retained count; 1kGP high-coverage's 698 related samples are **not** added |
| Sample inventory | 1kGP only, per-population counts recorded (CLM/MXL/PEL/PUR); HGDP American excluded from the primary panel by default; a `membership.json` per panel |
| Assembly check | reuse `verify_vcf` (GRCh38 header) and `verify_index`; no liftover |
| Provenance | every block's `.provenance.json` (n individuals, input/retained variants, MAC, cumvar, components, achieved variance) |

The sample-metadata TSV is CRLF-terminated; any consumer must normalise line
endings before comparing `high_quality` (found while measuring §2.3).

## 7. Resource and eigendecomposition contract

The panel must satisfy the existing core public contract, not invent a new one:

- **Representation:** eigendecomposition, not an LD matrix
  ([core ADR 0031](https://github.com/opengwas/opengwasdb/blob/dev/docs/adr/0031-ld-panels-store-eigendecompositions.md)).
  `load_ld_eigenvectors` reads `.ldeig.npz`; the matrix is optional/dead weight.
- **Components:** variance-driven, cumulative target **0.99**, `component_floor`
  ≥ 250 (existing `construct_block` defaults), with the consumer's 0.9 truncation
  provable from stored data (`find_underresolved_blocks.py` must report zero
  under-resolved blocks at 0.9).
- **PSD:** the thin-SVD construction yields `eigenvalues = s² ≥ 0` by
  construction, so PSD is structural; the pilot still asserts non-negative
  eigenvalues and finite vectors.
- **`resource.yaml`:** `kind: ld_panel`, `ancestry: [AMR]`, `genome_build: GRCh38`,
  `variant_id_convention: "chr:pos:a1:a2"`, `ld.block_file`, `ld.representation`,
  `ld.components_retained: variance_driven`, `ld.components_cumvar_target: 0.99`,
  `source.n_individuals.AMR`, `source.variant_inclusion: MAC >= 50`,
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
   pooled); EUR-boundary vs AFR-boundary sensitivity; per-population versus
   pooled AMR estimates; and the effect of relatedness pruning on n and on
   block conditioning.
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
5. **Acceptability:** whether `n ≈ 487` AMR samples is sufficient for a
   production panel at all, or AMR completion remains calibration/observed-only
   pending a larger cohort.

## Citations

- IGSR Phase 3 collection (2,504 unrelated, 26 populations):
  <https://www.internationalgenome.org/data-portal/data-collection/phase-3>
- IGSR 30× high-coverage GRCh38 collection (3,202 = 2,504 + 698 related; 602 trios):
  <https://www.internationalgenome.org/data-portal/data-collection/30x-high-coverage>
- IGSR population pages (CLM/MXL/PEL/PUR):
  <https://www.internationalgenome.org/data-portal/population/CLM>,
  <https://www.internationalgenome.org/data-portal/population/MXL>,
  <https://www.internationalgenome.org/data-portal/population/PEL>,
  <https://www.internationalgenome.org/data-portal/population/PUR>
- IGSR FAQ (relatedness/pedigrees): <https://www.internationalgenome.org/faq/>
- gnomAD downloads (HGDP+1kGP v3.1.2, GRCh38, relatedness filtering):
  <https://gnomad.broadinstitute.org/downloads>
- HGDP-CEPH resource: <https://www.hagsc.org/hgdp/>
- Cann et al. 2002, HGDP cell-line panel: <https://www.nature.com/articles/nature02029>
- Core LD-panel contract: `docs/adr/0031-ld-panels-store-eigendecompositions.md`
  in opengwas/opengwasdb.
- Registry decisions: ADR 0020 (non-EUR panels), ADR 0028 (ancestry-matched
  completion), ADR 0039 (region identity and ancestry refusal), ADR 0019
  (Reference-AF/effect-scale validation).
