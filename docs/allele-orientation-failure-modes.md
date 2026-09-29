# Allele Orientation & Ancestry Gate Failure Modes

This note summarizes the failure modes identified during ancestry assignment and effect-scale validation in the GWAS Catalog EUR Hybrid candidate release (**`OGS-00011`**), the empirical investigation of allele-orientation failures, and the classification of rescuable vs. non-rescuable studies.

---

## 1. Overall Ancestry Gate Distribution

Across the **4,783** selected ready Analyses in `OGS-00011`, the multi-gate admission rule (`opengwasdb.ancestry.mixture.apply_gates`, ADR 0028) evaluated the source allele frequencies against the 5.8M-variant Ancestry Reference Panel (`ref_freqs.hg38.tsv.gz`).

```text
Gate Reason              Count    Description
----------------------------------------------------------------------------------------------------
ok                       3,286    Passed all 5 gates (3,283 EUR, 3 non-EUR: 2 EAS, 1 AFR)
overlap                  1,119    < 5,000 overlapping reference variants (1,066 have 0 overlap / no AF)
residual                   226    NNLS RMS residual > 0.06 (median 0.246; noisy, admixed, or poor fit)
eaf_orientation            145    Strong negative frequency correlation (r <= -0.50, median -0.977)
proportion                   4    Dominant super-population proportion < 0.50 (heavily admixed)
margin                       2    Dominant margin over runner-up < 0.20 (near-tie between ancestries)
resolution_failed            1    GCST006329: duplicate 'beta' column in upstream header
----------------------------------------------------------------------------------------------------
Total                    4,783
```

---

## 2. Investigation of Allele Orientation Failures

In addition to the 145 studies flagged with `gate_reason == 'eaf_orientation'`, 17 studies flagged with `gate_reason == 'residual'` also exhibited negative frequency correlation ($r < 0$). In total, **162 studies** showed inverted or discordant frequency orientation.

### The Two Underlying Root Causes

An inverted frequency correlation ($r_{\text{EAF}} \approx -1.0$) against the reference consensus arises from two fundamentally different upstream mechanisms:

1. **Allele Column Swap (`swap_alleles`)**:
   - The authors or pipeline accidentally inverted the column headers `effect_allele` and `other_allele` in the summary statistics table.
   - Because the reported effect allele is actually the non-effect allele, the reported $\beta$ has the **opposite biological direction**.
   - **Remedy**: Swapping `effect_allele` and `other_allele` values fixes the alleles, the frequency orientation, and restores the true biological direction of $\beta$.

2. **Frequency Column Mislabelling (`flip_frequency`)**:
   - The authors correctly assigned `effect_allele`, `other_allele`, and $\beta$, but the frequency column represents the non-effect allele frequency (or unoriented minor allele frequency, $1 - \text{EAF}$).
   - **Remedy**: Transforming $\text{EAF} \leftarrow 1.0 - \text{EAF}$ without modifying the alleles or $\beta$.
   - **Danger**: Swapping the alleles in this group would corrupt $\beta$, reversing the biological effect direction for all variants across the genome.

---

## 3. Correlation Verification Methodology

To objectively distinguish between `swap_alleles` and `flip_frequency`, each of the 162 target studies was matched against independent comparison studies from either **OGS-00010** (UK Biobank 2,024 traits) or the **3,286 `ok` analyses** in OGS-00011:

1. **Top Variants**: Extracted the top 100 variants (by p-value) from the target study.
2. **Canonical ALID Alignment**:
   - Both target and comparison betas were harmonised to the canonical variant ALID (`chr:pos:a1:a2` where `a1 < a2`):
     $$\beta_{\text{ALID}} = \begin{cases} \beta & \text{if effect\_allele} = a_1 \\ -\beta & \text{if effect\_allele} = a_2 \end{cases}$$
3. **Correlation**: Calculated Pearson and Spearman correlation between aligned target and comparison effects:
   - $r \ge 0.20 \implies$ **`flip_frequency`** (canonical betas agree; alleles are correct, only frequency was inverted)
   - $r \le -0.20 \implies$ **`swap_alleles`** (canonical betas are inverted; alleles were swapped in the source file)
   - $|r| < 0.20$ or low overlap $\implies$ **`inconclusive`**

---

## 4. Verification Results & Findings

```text
Recommendation            145 eaf_orientation    All 162 Evaluated Studies
--------------------------------------------------------------------------
swap_alleles                       58                         61
flip_frequency                     33                         37
inconclusive                       15                         18
no_matching_trait                  39                         46
--------------------------------------------------------------------------
Total                             145                        162
```

### Breakdown by Major Publication Cohorts

The 162 studies belong to only 29 distinct publications (PMIDs). The top cohorts show striking internal consistency:

| PMID | Total | flip_frequency | swap_alleles | inconclusive | no_match | Traits |
|---|---|---|---|---|---|---|
| **39505872** | 34 | 9 | 23 | 2 | 0 | Retinal vascular morphology & density |
| **37689771** | 32 | 11 | 2 | 3 | 16 | ADHD, Depression, Autism, Schizophrenia |
| **41760662** | 24 | 2 | 21 | 1 | 0 | Cardiac ventricular volumes (MTAG) |
| **39543113** | 11 | 0 | 0 | 0 | 11 | Biventricular shape principal components |
| **24699409** | 9 | 1 | 0 | 1 | 7 | Insulin response / sensitivity indices |
| **38872030** | 6 | 1 | 1 | 4 | 0 | Body composition total mass |
| **38182742** | 4 | 0 | 4 | 0 | 0 | Type 2 Diabetes |
| **37700353** | 4 | 3 | 1 | 0 | 0 | Psychiatric / behavioral phenotypes |
| **40272846** | 4 | 1 | 1 | 0 | 2 | Body fat distribution axes |
| **41610418** | 4 | 0 | 0 | 0 | 4 | Acute myeloid leukemia cytogenetics |
| **27386562** | 1 | 1 | 0 | 0 | 0 | Multiple Sclerosis (`GCST003566`, $r = +0.998$) |

### Notable Positive Controls

- **Multiple Sclerosis (`GCST003566`, PMID 27386562)**:
  - Matched against 3 independent MS studies (`GCST90014448`, `GCST90014449`, `GCST90705066`) over 99 variants.
  - **Pearson $r = +0.998$, Spearman $r = +0.902$** $\implies$ **`flip_frequency`**.
  - Confirms the author's effect allele and negative HLA risk allele betas are correct; only the frequency column was inverted.
- **Type 2 Diabetes (`GCST90296697`, PMID 38182742)**:
  - Matched against `GCST006867` over 89 variants.
  - **Pearson $r = -0.993$, Spearman $r = -0.939$** $\implies$ **`swap_alleles`**.
  - Confirms the author's table labelled the protective allele as the other allele and inverted beta.

---

## 5. Artifacts Created

1. **`stores/OGS-00011/sidecars/allele-check/`** (symlinked from `sidecars/allele-check`):
   - **162 individual YAML files** (e.g. `GCST003566.yaml`, `GCST90296697.yaml`) detailing each study's match, variant overlap, correlation statistics, and verdict.
   - **`summary.tsv`**: Full tabulated matrix of all 162 analyses.
2. **`resources/scripts/check_allele_orientation.py`**:
   - The reproducible inspection script.
3. **`resources/scripts/create_rescued_gwas_files.py`**:
   - Script generating the 91 verified derived files into `/data/opengwasdb/derived/ebi-gwas-catalog/` and creating the overlay manifest `eur-hybrid-rescue-orientation-manifest.tsv`.
