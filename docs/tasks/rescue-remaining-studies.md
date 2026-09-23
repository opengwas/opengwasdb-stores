# Task: Rescue remaining unassigned and orientation-failing studies for GWAS Catalog EUR Hybrid

## Parent

#150

## Background

In the full GWAS Catalog EUR Hybrid candidate release (`OGS-00011`), 3,262 Analyses are included in the build, while 1,521 remain excluded with explicit audit reasons in `sidecars/exclusions.tsv`. Following the allele-orientation verification study (`docs/allele-orientation-failure-modes.md`), 91 orientation-failing studies were validated and rescued (58 via `swap_alleles` and 33 via `flip_frequency`).

This task defines the work to rescue the remaining **71 orientation-failing studies** that could not be resolved from UK Biobank and the current in-batch comparison traits, as well as the **19 case-control studies with missing counts**.

---

## What to build

### 1. External Positive-Control Matcher for Unique Traits (46 studies)
46 orientation-failing studies had no direct trait match in UK Biobank (OGS-00010) or the 3,286 OK analyses in OGS-00011:
- **PMID 39543113** (11 biventricular shape principal components)
- **PMID 37689771** (16 autism and schizophrenia specialized models)
- **PMID 24699409** (7 insulin response/sensitivity curves)
- **PMID 41610418** (4 acute myeloid leukemia cytogenetic subtypes)
- **PMID 40021682** (3 direct bilirubin / metabolic phenotypes)
- Other single-study specialized traits

Build an external matcher using the OpenGWAS API (`https://api.opengwas.io`) and FinnGen (R12/R13) to query known benchmark associations for these specific phenotypes. For each study, compare the aligned effect direction against the external reference to classify as `swap_alleles` or `flip_frequency`.

### 2. Expanded Locus Clumping for Inconclusive Studies (18 studies)
18 studies had low variant overlap ($N < 20$) or weak correlation ($|r| < 0.2$) when evaluating only the top 100 variants.
Expand the variant sampling to genome-wide significant clumps ($p < 5 \times 10^{-8}$) or the top 500 variants to establish a clear correlation signal.

### 3. Curate Missing Sample Size Counts for Case-Control Studies (19 studies)
19 case-control studies cleared all ancestry gates but are excluded because `n_cases` or `n_controls` is blank in the source candidate table. Extract the case/control counts directly from the published paper text or the EBI GWAS Catalog study metadata table to admit them to `StoredEffectScale.LOG_OR`.

---

## Acceptance criteria

- [ ] External matching against OpenGWAS / FinnGen resolves the 46 `no_matching_trait` studies to an explicit recommendation (`swap_alleles`, `flip_frequency`, or verified unrescuable).
- [ ] Expanded clumping on the 18 `inconclusive` studies resolves at least 10 into clear positive or negative correlation.
- [ ] For each newly rescued study, a derived `.h.tsv.gz` is generated in `/data/opengwasdb/derived/ebi-gwas-catalog/` and documented in `stores/OGS-00011/sidecars/allele-check/<analysis_id>.yaml`.
- [ ] Case and control counts are curated for the 19 excluded case-control studies, and their exclusion reason `missing_case_control_counts` is cleared.
- [ ] The orientation-rescue overlay manifest `eur-hybrid-rescue-orientation-manifest.tsv` is updated with all newly rescued accessions.
- [ ] `pixi run inventory-freeze` and `pixi run generate-candidate OGS-00011 --resume` run cleanly and record the expanded included set.
- [ ] No regression: all currently included 3,262 analyses remain included with unchanged checksums and directions.

## Blocked by

- #155 (Full GWAS Catalog candidate release candidate OGS-00011)
