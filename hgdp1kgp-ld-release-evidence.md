# HGDP+1kGP LD panel release evidence

Status: **SHIP** AFR/EAS/SAS panels for ancestry-matched reference completion.

Generation completed on 2026-08-06 under
`/data/opengwasdb/reference/hgdp1kgp-hg38/panel` using MAC ≥ 50 and a 0.99
cumulative-variance target with a 250-component floor. Corrected populated block
counts are AFR 1,580, EAS 1,120, SAS 1,267, and EUR calibration 1,357. The sole
unpopulated declared interval, EAS chr14:16038675-16100330, contains no retained
variant and is explicitly empty.

The five-region paired masking experiment used 2,514 UKB-B analyses and held out
20% of variants shared by the observed store and both EUR panels. HGDP+1kGP EUR
accepted 12,430/12,570 eligible analysis-region fits (98.9%) with median held-out
beta correlation 0.963. UKB EUR accepted 12,559/12,570 (99.9%) with median 0.978.
Among 12,421 paired successful fits, UKB's median advantage was 0.014 correlation
points and it was higher in 91.2% of pairs.

The exploratory release gate was acceptance ≥98%, median correlation ≥0.95, and
median paired loss versus UKB EUR ≤0.02. The calibration passes all three. This
supports shipping the ancestry-matched panels but not replacing UKB EUR. Full
method, region-level results, plots, limitations, and the allele-orientation defect
found and corrected during validation are in
`docs/eur-panel-imputation-comparison.html`. The compact tracked results are in
`docs/data/eur-panel-imputation-summary.csv`; the 5 MB analysis-level table remains
external at `/data/opengwasdb/wip/eur-panel-imputation-comparison.csv`.

Issue #40 remains separately blocked: as checked on 2026-08-06,
`explodecomputer/opengwasdb#10` is still open, so the required real consumer smoke
test cannot yet be claimed.

## UKB EUR issue #34 completion

The 601 historically under-resolved UKB EUR eigendecompositions were regenerated
at cumulative variance 0.99 and swapped into
`/data/opengwasdb/reference/ukb-hg38/EUR`. A production scan on 2026-08-06 found
zero of the 1,345 then-present eigendecompositions under-resolved at the consumer's
0.9 threshold. The same audit identified 12 legacy matrix-backed blocks without
an eigendecomposition; these were backfilled in the same session (250–715
components, achieved variance 0.990002–0.992257). The directory now contains
1,357/1,357 `.ldeig.npz` artifacts.

The rebuilt UKB-B completion is recorded externally at
`/data/opengwasdb/wip/ukb-b-c128-completed.PROVENANCE.md`: completion-quality mean
Pearson correlation improved from 0.9719 to 0.9770 and missing cells fell from
22,016,187 to 18,648,444 relative to the pre-issue-#34 completed store.
