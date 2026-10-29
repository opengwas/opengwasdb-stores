# Preregistration: QC Panel Concordance Study & Reference-AF Fallback Policy

**Document status:** Locked / Preregistered (Final report and decision recorded in [`docs/qc-panel-concordance-report.md`](../qc-panel-concordance-report.md)).  
**Issue:** [opengwasdb-stores#152](https://github.com/opengwas/opengwasdb-stores/issues/152)  
**Parent:** [opengwasdb-stores#150](https://github.com/opengwas/opengwasdb-stores/issues/150)  
**Next Step:** [opengwasdb-stores#153](https://github.com/opengwas/opengwasdb-stores/issues/153) (Phase B candidate generation)  
**Seam dependencies:** opengwasdb#207 (`opengwasdb.build.resolve.resolve_analysis`), opengwasdb-stores#151 (Frozen Source Inventory `gwas-catalog-ssf-eur-hybrid-2026-09-10`).

---

## 1. Context & Objectives

Generating the full EBI GWAS Catalog European Hybrid Release Candidate requires resolving two Reference Resource questions before executing the production candidate run:

1. **Ancestry extraction panel decision:** Whether the fixed 10,000-variant `qc-panel-hg38` (`resources/reference-resources/qc-panel-hg38/qc_panel.tsv`) provides scientifically interchangeable ancestry assignment and effect-scale evidence compared to scanning against the full 5.8-million-variant ancestry reference (`/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz`).
2. **Reference-AF fallback policy decision:** What explicit outcome is produced when a quantitative Analysis lacks usable source allele frequencies, given that the previously declared `/data/opengwasdb/reference/ukb-hg38` panel is absent on this host.

Neither decision may be made implicitly at runtime or adjusted post-hoc. This document locks the sample frame, comparison metrics, acceptance criteria, decision rules, and fallback policy prior to running the real-data concordance study.

---

## 2. Resource Fingerprints & Versions

Every input resource is recorded with its canonical path, size, and cryptographic checksum:

| Resource ID | Role | Location | Size (bytes) | SHA-256 Checksum |
|---|---|---|---|---|
| `ukb-ancestry-mixture-hg38` (frequencies) | Full Ancestry Reference | `/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz` | 494,105,972 | `067914e0c21fe2f0d464f8489e7d91bd4eeafdbf71bc0b93e895a2117ddc0925` |
| `ukb-ancestry-mixture-hg38` (groups) | Fine-to-Superpop Map | `/data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv` | 385 | `005093f9a8f74cc792e6ee4e828f7f4fa1ca7508e92c2d1dd463f320bac3834d` |
| `qc-panel-hg38` | 10k Extraction Panel (v1) | `resources/reference-resources/qc-panel-hg38/qc_panel.tsv` | 420,582 | `0bfe49c9f8083b7f8a3c9bb79b40b7540e8eb47f3e5e9524dbd0d591dc5bca88` |
| `gwas-catalog-ssf-eur-hybrid-2026-09-10` | Frozen Source Inventory | `resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv` | 3,507,041 | `d22e857c5c2880438cf3659c865c375755dcccb95e86d141f0b5df31b9a252f7` |
| `config-full.yaml` | Phase B Full Config | `resources/generators/gwas-catalog-eur-hybrid/config-full.yaml` | — | Tracked in git |

---

## 3. Preregistered Stratified Sample Frame

The concordance sample is derived deterministically from the 4,570 ready rows of the frozen Source Inventory (`resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv`).

### 3.1 Stratification Architecture

The sample consists of **106 Analyses** partitioned into:

1. **Quantitative trait size deciles (50 Analyses):**
   - 3,513 ready quantitative rows partitioned into 10 deciles by recorded `data_bytes` (from 221 KB to 4.04 GB).
   - 5 Analyses selected deterministically per decile (at 0%, 25%, 50%, 75%, 100% relative rank within each decile).
2. **Case-control trait size deciles (50 Analyses):**
   - 1,057 ready case-control rows partitioned into 10 deciles by recorded `data_bytes` (from 248 KB to 2.94 GB).
   - 5 Analyses selected deterministically per decile (including small accession `GCST90271757` in decile 1).
3. **Explicit edge cases & anomaly fixtures (6 Analyses):**
   - `GCST90446781`: Known inverted-EAF source ($r \approx -0.98$ against reference consensus; verifies orientation gate sensitivity).
   - `GCST000553`: Old GWAS-SSF format with `hm_` prefix headers and unpopulated allele frequency rows.
   - `GCST90565871` & `GCST90565872`: First byte-identical duplicate accession pair (329,444,995 bytes).
   - `GCST90624704` & `GCST90624705`: Second byte-identical duplicate accession pair (388,828,307 bytes).

The sample manifest is frozen at `resources/inventories/gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.tsv` with provenance sidecar `.meta.yaml`.

---

## 4. Comparison Metrics & Mathematical Seam

Both evaluations use the one-pass bounded Analysis resolver `opengwasdb.build.resolve.resolve_analysis` (ADR 0044) with default gates $\tau=0.50$, $\delta=0.20$, $n_{\min}=5000$, $\text{residual}_{\max}=0.06$, $\text{flip}_r=-0.50$:

- **Method A (Full Reference Scan):** `extraction_panel=None` (scans all 5,808,902 reference variants).
- **Method B (QC Panel Extraction):** `extraction_panel=qc_panel_alids` (extracts only the 10,000 panel sites).

### 4.1 Tracked Seam Outputs

For every Analysis $i$, the study extracts and compares:

| Metric | Full Reference ($F_i$) | QC Panel ($P_i$) | Delta / Concordance Criterion |
|---|---|---|---|
| `assigned_ancestry` | $A_F \in \{\text{EUR}, \text{AFR}, \dots, \text{None}\}$ | $A_P \in \{\text{EUR}, \text{AFR}, \dots, \text{None}\}$ | Categorical identity: $A_F == A_P$ |
| `dominant_superpop` | $D_F \in \{\text{EUR}, \text{AFR}, \dots\}$ | $D_P \in \{\text{EUR}, \text{AFR}, \dots\}$ | Categorical identity: $D_F == D_P$ |
| `dominant_proportion` | $p_F \in [0, 1]$ | $p_P \in [0, 1]$ | Absolute delta: $|p_P - p_F| \le 0.05$ |
| `runner_up_margin` | $m_F \ge 0$ | $m_P \ge 0$ | Absolute delta: $|m_P - m_F| \le 0.05$ |
| `residual` | $r_F \ge 0$ | $r_P \ge 0$ | Absolute delta: $|r_P - r_F| \le 0.02$ |
| `af_overlap` | $N_F \in \mathbb{N}$ | $N_P \in \mathbb{N}$ | For full-GWAS: $N_P \ge 5,000$ ($n_{\min}$) |
| `gate_reason` | $G_F \in \{\text{ok}, \text{overlap}, \dots\}$ | $G_P \in \{\text{ok}, \text{overlap}, \dots\}$ | Categorical identity: $G_F == G_P$ |
| `eaf_orientation_outcome` | $O_F \in \{\text{passed}, \text{flipped}, \dots\}$ | $O_P \in \{\text{passed}, \text{flipped}, \dots\}$ | Categorical identity: $O_F == O_P$ |
| `eaf_orientation_r` | $r_F \in [-1, 1]$ | $r_P \in [-1, 1]$ | Same sign and category |
| `sd_status` | $S_F \in \{\text{estimated}, \text{skipped}, \dots\}$ | $S_P \in \{\text{estimated}, \text{skipped}, \dots\}$ | Categorical identity: $S_F == S_P$ |
| `phenotype_sd` | $\text{SD}_F \in \mathbb{R}^+$ | $\text{SD}_P \in \mathbb{R}^+$ | Relative delta: $|\text{SD}_P - \text{SD}_F| / \text{SD}_F \le 0.02$ |
| `seconds` | $t_F$ | $t_P$ | Wall-clock execution speedup |

---

## 5. Disagreement Classification Taxonomy

Any divergence between Method A and Method B is classified into an explicit category:

1. **`assigned_ancestry_flip` (Critical):** One method assigns an ancestry (e.g. `EUR`) while the other assigns a different ancestry or leaves the Analysis `Unassigned`.
2. **`dominant_superpop_flip` (Critical):** The fitted dominant ancestry group differs between methods.
3. **`gate_reason_divergence` (Major):** Both methods reject/unassign, but cite different reasons (e.g. `margin` vs `residual`).
4. **`overlap_drop` (Diagnostic):** Panel overlap $N_P < 5000$ on a source with limited genomic coverage where full reference had $N_F \ge 5000$.
5. **`orientation_flip_missed` (Critical):** A mis-oriented or flipped frequency column is caught by Full Reference but missed by Panel.
6. **`residual_divergence` (Minor):** Proportion/margin concordant, but RMS residual shifts by $> 0.02$.
7. **`sd_divergence` (Minor):** Phenotype SD estimate shifts by $> 2\%$.

---

## 6. Preregistered Decision Rule

The decision to adopt `qc-panel-hg38` versus retaining the full reference `ref_freqs.hg38.tsv.gz` is governed strictly by the following rule:

### 6.1 Panel Adoption Criteria (ALL must pass)

1. **Zero False Positives:** Zero cases where Full Reference gates out / unassigns an Analysis but QC Panel admits it as `EUR`.
2. **100% Orientation Sensitivity:** Every known orientation anomaly (including `GCST90446781` and synthetic flipped fixtures) is detected by QC Panel ($G_P = \text{eaf\_orientation}$).
3. **$\ge 98\%$ Full-GWAS Concordance:** For genome-wide files with $\ge 500,000$ source variants, `assigned_ancestry` concordance must be $\ge 98\%$.
4. **No Gate Tuning:** Acceptance thresholds ($\tau=0.50, \delta=0.20, n_{\min}=5000, \text{residual}_{\max}=0.06$) remain strictly fixed; no relaxation of gates is permitted to force concordance.
5. **Documented Disposition for Targeted Sources:** If targeted/sparse sources (<50,000 variants, e.g. `GCST90271757`) fail the $n_{\min}=5000$ overlap gate under the 10k panel, this must be documented as an expected characteristic of sparse panels rather than masked.

### 6.2 Action on Pass vs Fail

- **PASS:** Update `resources/generators/gwas-catalog-eur-hybrid/config-full.yaml` to declare `qc-panel-hg38` as the extraction panel in `ancestry_assignment.extraction_panel`.
- **FAIL:** Retain `extraction_panel: null` in `config-full.yaml` (full reference scan for all Analyses).

---

## 7. Reference-AF Fallback Policy Decision

### 7.1 Evaluated Alternatives

1. **Option 1: Materialise `/data/opengwasdb/reference/ukb-hg38`:**
   - Requires generating or acquiring ~140 GB of block-partitioned UK Biobank allele frequencies.
   - Status: Absent on current host; unverified provenance.
2. **Option 2: Register a replacement Reference Resource:**
   - Requires validating a replacement panel (e.g. 1kGP/HGDP) against the registry schema and verifying coverage across all 4,570 sources.
   - Status: No replacement is currently approved or verified for this Source Collection.
3. **Option 3: Source-AF-Only Policy (Recommended):**
   - In accordance with ADR 0019, quantitative Analyses with usable source AF use `estimated_from_source_maf`.
   - Quantitative Analyses lacking usable source AF receive an explicit `skipped` resolution with reason `no_reference_resource_for_ancestry` and are excluded from the Store Release Candidate.
   - Case-control Analyses receive an explicit `skipped` resolution with reason `non_quantitative_effect_scale` (`binary_trait`).

### 7.2 Standing Policy Selection

**Option 3 (Source-AF-Only Policy) is selected for the Full GWAS Catalog EUR Hybrid Release.**

- `config-full.yaml` explicitly sets `effect_scale_validation.reference_resources: []`.
- Preflight asserts that no missing reference path is treated as usable evidence.
- Quantitative Analyses with missing/unusable source AF are classified as explicit exclusions (`no_reference_resource_for_ancestry`) rather than fabricated estimates against a missing panel.
- Adding a fallback panel in a future release requires registering a validated Reference Resource and updating config, not changing code.

---

## 8. Operator Herdr Execution Plan

The concordance study is executed via Herdr pane using up to 64 physical cores:

```bash
# In Herdr pane:
pixi run --environment dev python3 resources/generators/gwas-catalog-eur-hybrid/run_concordance_study.py \
  --manifest resources/inventories/gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.tsv \
  --config resources/generators/gwas-catalog-eur-hybrid/config-full.yaml \
  --cores 64 \
  --out-dir /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance
```

Expected outputs:
- `concordance_results.json`: Full machine-readable results per Analysis.
- `concordance_comparison.tsv`: Tabular delta summary.
- `concordance_report.md`: Markdown summary report with pass/fail gate evaluation.
