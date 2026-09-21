# QC Panel Concordance Study & Reference Policy Report

**Status:** Final Decision / Ratified  
**Issue:** [opengwasdb-stores#152](https://github.com/opengwas/opengwasdb-stores/issues/152)  
**Parent:** [opengwasdb-stores#150](https://github.com/opengwas/opengwasdb-stores/issues/150)  
**Next Step:** [opengwasdb-stores#153](https://github.com/opengwas/opengwasdb-stores/issues/153) (Phase B candidate generation)  
**Preregistration:** [`docs/spec/qc-panel-concordance-preregistration.md`](spec/qc-panel-concordance-preregistration.md)  
**Date:** 2026-09-21  

---

## 1. Executive Summary & Decisions

Issue [#152](https://github.com/opengwas/opengwasdb-stores/issues/152) addresses two blocking Reference Resource decisions before Phase B candidate generation for the full EBI GWAS Catalog European Hybrid Release Candidate (`gwas-catalog-ssf-eur-hybrid-2026-09-10`, 4,570 ready Analyses):

1. **Ancestry Extraction Method Decision:**  
   **REJECT `qc-panel-hg38` / RETAIN Full Reference Scan (`extraction_panel: null`).**  
   The preregistered 106-Analysis concordance evaluation comparing Method A (scanning all 5.8M reference variants) vs Method B (extracting the 10k-variant `qc-panel-hg38`) showed a 1.6× speedup but failed the preregistered genome-wide concordance acceptance gate ($95.1\% < 98.0\%$). Under the strict decision rule without post-hoc gate tuning, `qc-panel-hg38` is rejected for this release and the full reference scan (`ref_freqs.hg38.tsv.gz`) is retained in `config-full.yaml`.

2. **Reference-AF Fallback Policy Decision:**  
   **RETAIN Standing Source-AF-Only Policy (Option 3).**  
   Because `/data/opengwasdb/reference/ukb-hg38` is absent on this host, `effect_scale_validation.reference_resources` remains `[]`. Quantitative Analyses lacking usable source allele frequencies receive an explicit `skipped` resolution (`no_reference_resource_for_ancestry`) and are excluded from the Store Release Candidate rather than estimated against an absent panel.

---

## 2. Resource Fingerprints & Artifact Provenance

### 2.1 Pinned Input Resources

| Resource ID / Role | Location | Size (bytes) | SHA-256 Checksum |
|---|---|---|---|
| Full Ancestry Reference (`ukb-ancestry-mixture-hg38`) | `/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz` | 494,105,972 | `067914e0c21fe2f0d464f8489e7d91bd4eeafdbf71bc0b93e895a2117ddc0925` |
| Fine-to-Superpop Group Map | `/data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv` | 385 | `005093f9a8f74cc792e6ee4e828f7f4fa1ca7508e92c2d1dd463f320bac3834d` |
| 10k Extraction Panel (`qc-panel-hg38` v1) | `resources/reference-resources/qc-panel-hg38/qc_panel.tsv` | 420,582 | `0bfe49c9f8083b7f8a3c9bb79b40b7540e8eb47f3e5e9524dbd0d591dc5bca88` |
| Frozen Source Inventory | `resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv` | 3,507,041 | `d22e857c5c2880438cf3659c865c375755dcccb95e86d141f0b5df31b9a252f7` |
| Stratified Sample Manifest | `resources/inventories/gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.tsv` | 75,314 | `962618a5f72e0fa6027173e7b0b102cba472c9b4c9ff460f7bada04f17c2d557` |

### 2.2 Generated Study Outputs

The concordance study ran across 64 physical CPU cores on host `app-dc3-ogws-p0`. All three authoritative output files are persisted under `/data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance`:

| Artifact | Location | Size (bytes) | SHA-256 Checksum |
|---|---|---|---|
| Complete JSON Results | `/data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance/concordance_results.json` | 181,873 | `609fff779ad148a37af4aa397af801505ea54578aa295b11f9c434a48dcafb23` |
| Tabular Comparison TSV | `/data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance/concordance_comparison.tsv` | 19,890 | `7fef76ed842a45ef789ede313a339a8ea12eb64ed2f823fe92a8ac898d103ce9` |
| Summary Markdown Report | `/data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance/concordance_report.md` | 4,037 | `af3527d87f7d9c1c580290e69b59c6e6ee6be322e087f730c4f6b7f6714cb3da` |

---

## 3. Preregistered Decision Rule Evaluation

The study evaluated all 106 stratified Analyses (54 quantitative, 52 case-control across 10 deciles and 6 edge fixtures) using standard bounded resolver gates ($\tau=0.50, \delta=0.20, n_{\min}=5000, \text{residual}_{\max}=0.06$).

### 3.1 Decision Matrix

| Preregistered Criterion | Required Gate | Observed Value | Verdict |
|---|---|---|---|
| **Criterion 1: Zero False Positive EUR** | $== 0$ | 0 | **PASS** |
| **Criterion 2: Orientation Flip Sensitivity** | $100.0\%$ | 100.0% (4/4 inverted files caught) | **PASS** |
| **Criterion 3: Genome-Wide Concordance** | $\ge 98.0\%$ | **95.10%** (97/102 concordant) | **FAIL** |
| **Criterion 4: Zero Execution Errors** | $== 0$ errors | 0 unhandled errors | **PASS** |

### 3.2 Performance & Speedup

- **Full Reference (Method A):** Mean scan time 157.80 s/Analysis (total wall time ~16,727 core-seconds).
- **QC Panel (Method B):** Mean scan time 98.49 s/Analysis (total wall time ~10,440 core-seconds).
- **Observed Speedup:** 1.60×.

While Method B provided modest execution acceleration, it failed Criterion 3 because 5 of the 102 genome-wide files (>10 MB) lost enough variant overlap against the 10k panel to drop below $n_{\min}=5,000$, causing valid European GWAS to be rejected under Method B when they were correctly assigned under Method A.

---

## 4. Disagreement Classification & Dispositions

Across all 106 Analyses, exactly **14 disagreements** were observed. Every divergence has been classified and given an explicit disposition:

| Analysis ID | Stratum | Study Design | Full Assigned | Panel Assigned | Full Gate | Panel Gate | Full Overlap | Panel Overlap | Category | Severity | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `GCST004293` | `quant_decile_1` | quantitative | `None` | `None` | `overlap` | `overlap` | 1,335 | 0 | `dominant_superpop_flip` | Critical | **Concordant unassigned:** Ultra-sparse targeted array file (221 KB). Both methods gate out as `overlap` (<5k). Full fits `EUR` dominant superpop on 1,335 variants, Panel finds 0 overlapping variants (`dominant_superpop: None`). Expected behavior for sparse inputs. |
| `GCST90025959` | `quant_decile_3` | quantitative | `EUR` | `None` | `ok` | `overlap` | 363,900 | 719 | `overlap_drop` | Diagnostic | **False negative under Panel:** Source has 363.9k variants in full reference but only 719 in 10k panel. Drops below $n_{\min}=5,000$ and gets unassigned under panel. Full reference correctly admits `EUR`. |
| `GCST90271757` | `case_control_decile_1` | case-control | `EUR` | `None` | `ok` | `overlap` | 5,242 | 8 | `overlap_drop` | Diagnostic | **Sparse array edge case:** Source has 5,242 variants (barely cleared 5k on full ref). Only 8 variants on 10k panel. Full admits `EUR`; Panel unassigns. |
| `GCST90281050` | `case_control_decile_2` | case-control | `EUR` | `None` | `ok` | `overlap` | 2,754,444 | 3,713 | `overlap_drop` | Diagnostic | **False negative under Panel:** Non-standard GWAS array with 2.75M variants on full reference has only 3,713 on 10k panel. Full admits `EUR`; Panel unassigns. |
| `GCST90446768` | `quant_decile_9` | quantitative | `None` | `None` | `eaf_orientation` | `eaf_orientation` | 4,549,073 | 7,672 | `residual_divergence` | Minor | **Concordant gate rejection:** Inverted EAF ($r = -0.985$) caught by both methods. Residual delta = 0.124 on unassigned inverted fit. |
| `GCST90446774` | `quant_decile_10` | quantitative | `None` | `None` | `eaf_orientation` | `eaf_orientation` | 4,549,073 | 7,672 | `residual_divergence` | Minor | **Concordant gate rejection:** Inverted EAF ($r = -0.985$) caught by both methods. |
| `GCST90446781` | `explicit_edge_case` | quantitative | `None` | `None` | `eaf_orientation` | `eaf_orientation` | 4,491,287 | 7,577 | `residual_divergence` | Minor | **Concordant gate rejection:** Preregistered inverted fixture ($r = -0.985$) caught by both methods. |
| `GCST90502916` | `quant_decile_1` | quantitative | `EUR` | `None` | `ok` | `overlap` | 758,792 | 1,754 | `overlap_drop` | Diagnostic | **False negative under Panel:** 758k variant file has only 1,754 on 10k panel. Full admits `EUR`; Panel unassigns. |
| `GCST90559190` | `quant_decile_1` | quantitative | `EUR` | `None` | `ok` | `overlap` | 11,492 | 25 | `overlap_drop` | Diagnostic | **Sparse array edge case:** 11.5k variants on full ref admits `EUR`; 25 on panel unassigns. |
| `GCST90565871` | `explicit_edge_case` | case-control | `EUR` | `None` | `ok` | `overlap` | 231,928 | 287 | `overlap_drop` | Diagnostic | **False negative under Panel:** Duplicate pair 1 (329 MB) has 231.9k variants in full ref but only 287 on panel. Full admits `EUR`; Panel unassigns. |
| `GCST90565872` | `explicit_edge_case` | case-control | `EUR` | `None` | `ok` | `overlap` | 231,928 | 287 | `overlap_drop` | Diagnostic | **False negative under Panel:** Byte-identical duplicate of `GCST90565871` behaves identically. |
| `GCST90651074` | `case_control_decile_2` | case-control | `None` | `None` | `residual` | `overlap` | 71,867 | 101 | `gate_reason_divergence` | Minor | **Concordant unassigned:** Non-European / poor fit GWAS. Full reference unassigns on RMS residual ($0.301 > 0.06$); Panel unassigns on overlap ($101 < 5000$). |
| `GCST90832110` | `case_control_decile_2` | case-control | `None` | `None` | `eaf_orientation` | `eaf_orientation` | 3,318,028 | 5,835 | `residual_divergence` | Minor | **Concordant gate rejection:** Inverted EAF ($r = -0.925$) caught by both methods. |
| `GCST90841074` | `case_control_decile_2` | case-control | `None` | `None` | `eaf_orientation` | `eaf_orientation` | 4,408,052 | 7,574 | `dominant_superpop_flip` | Critical | **Concordant gate rejection:** Inverted EAF ($r = -0.957$) caught by both methods. Dominant superpop classification on inverted frequencies shifted between AFR and EAS; both unassign. |

---

## 5. Final Policies & Configuration Updates

### 5.1 Ancestry Assignment Method
- `config-full.yaml` explicitly retains `extraction_panel: null` under `ancestry_assignment`.
- All 4,570 ready Analyses will be resolved against the full 5.8M reference `/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz`.
- This ensures zero overlap drops on non-standard GWAS arrays and maximizes release completeness.

### 5.2 Reference-AF Fallback Policy
- `config-full.yaml` explicitly maintains `effect_scale_validation.reference_resources: []`.
- Preflight asserts that no missing reference panel is declared or queried.
- Quantitative Analyses with usable source AF will use `estimated_from_source_maf`.
- Quantitative Analyses lacking usable source AF will receive an explicit `skipped` resolution (`no_reference_resource_for_ancestry`) and be omitted from candidate release membership.
- Case-control Analyses will receive an explicit `skipped` resolution (`binary_trait`).

---

## 6. Readiness for Issue #153 (Candidate Generation)

All reference-resource decisions, fallback policies, and preflight assertions for the EBI GWAS Catalog EUR Hybrid Release Candidate are fully settled and evidenced:

1. **Frozen Inventory:** `resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv` (4,570 ready Analyses).
2. **Preflight Health:** Passed with 0 missing files, 0 size mismatches, 38.4 TB free scratch space.
3. **Execution Estimate:** Scanning 4,570 Analyses against the full 5.8M reference on 64 cores is estimated at $\sim 4{,}570 \times 158\,\text{s} \div 64 \approx 3.1\,\text{hours}$ wall-clock time.
