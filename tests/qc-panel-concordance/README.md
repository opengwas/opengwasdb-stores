# QC Panel Concordance Test Suite (#152)

Hermetic unit and synthetic fixture tests for the QC Panel Concordance Study.

## Test Coverage

1. **`TestSampleManifestIntegrity`**:
   - Asserts existence and validity of `resources/inventories/gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.tsv` and `.meta.yaml`.
   - Checks that exactly 106 Analyses are present.
   - Verifies 50 quantitative analyses across 10 deciles (5 per decile).
   - Verifies 50 case-control analyses across 10 deciles (5 per decile).
   - Verifies explicit edge cases: `GCST90446781`, `GCST000553`, `GCST90271757`, duplicate pairs `GCST90565871`/`GCST90565872` and `GCST90624704`/`GCST90624705`.
   - Validates SHA-256 digest consistency between TSV and sidecar.

2. **`TestStratifiedSamplingAlgorithm`**:
   - Asserts deterministic decile slicing, sorting, and edge-case inclusion.

3. **`TestDisagreementCategorization`**:
   - Verifies classification taxonomy (`false_positive_panel_assignment`, `overlap_drop`, `orientation_flip_missed`, `residual_divergence`, `sd_divergence`).

4. **`TestSyntheticConcordanceFixtures`**:
   - Builds synthetic 40-variant reference panel and runs `compare_single_analysis` on synthetic GWAS-SSF files.
   - Verifies concordant EUR assignment on clean European data.
   - Verifies orientation flip ($r < -0.5$) detection by both Method A and Method B.

5. **`TestReferenceAfFallbackPolicy`**:
   - Verifies explicit skip codes for source-AF-only policy and case-control traits.

## Invocation

```bash
pixi run --environment dev python3 tests/qc-panel-concordance/test_qc_panel_concordance.py
```
