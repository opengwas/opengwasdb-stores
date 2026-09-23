# Allele Orientation Check (Issue #155)

This directory contains the per-study YAML audits and summary TSV determining whether each orientation-failing study in **`OGS-00011`** represents:
- An **allele swap** (`swap_alleles`: 58 studies among `gate_reason == eaf_orientation`, 61 overall), or
- A **frequency-only inversion** (`flip_frequency`: 33 studies among `gate_reason == eaf_orientation`, 37 overall).

See full documentation and methodology in [docs/allele-orientation-failure-modes.md](../../../docs/allele-orientation-failure-modes.md).

## Files

- `summary.tsv`: Tabulated matrix of all 162 evaluated studies, matched traits, variant counts, Pearson/Spearman $r$, and verdicts.
- `<analysis_id>.yaml`: Detailed evaluation record for each study, listing comparison traits, variant counts, correlation values, and recommendation.
