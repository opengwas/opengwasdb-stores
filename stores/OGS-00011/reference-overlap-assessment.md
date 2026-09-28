# OGS-00011 variant-reference overlap assessment (issue #174)

**Verdict: heterogeneous, with identifiable high-overflow Analyses; not a uniform
39% panel deficit.** These figures are from the first failed Hybrid build's
per-Analysis `.npz` spill array headers, not from a new bounded resolver scan.
They are full-build association-cell counts and therefore provide stronger
coverage evidence for this particular attempt; they do not replace the new
pre-build overlap sidecar on a regenerated candidate. The second attempt's
15,078,327,210-cell overflow is the authoritative failure size.

Evidence: `/data/opengwasdb/stores/OGS-00011/.store.opengwasdb.partial.hybridspill.x8zjhchg/`
(`{analysis_index}.npz` and `{analysis_index}.ovf.npz`, `z.npy` array shapes),
paired by `analysis_index` with
`/data/opengwasdb/stores/OGS-00011/work/analyses.tsv`. All 3,262 indices
had both arrays. Dense: 24,049,899,876 cells; overflow: 15,325,956,112 cells;
weighted off-reference share **38.92%**. Median Analysis overlap is **94.06%**;
5th-percentile overlap **16.93%**. **133** Analyses have less than 5% overlap;
**1,579** have at least 95%. The largest 100 off-reference contributors account
for **29.02%** of overflow cells; the largest 500 account for **73.52%**.
Examples (on-reference / off-reference cells):

| Analysis | On-reference | Off-reference |
| --- | ---: | ---: |
| GCST90455659 | 9,456,146 | 89,571,546 |
| GCST90455660 | 9,456,095 | 89,086,083 |
| GCST90502911 | 9,498,487 | 75,727,755 |
| GCST90503108 | 10,851,831 | 63,775,997 |
| GCST90319320 | 9,982,981 | 59,334,428 |

This is not attributable to only a handful of defective sources: even after
removing the top 500 off-reference contributors, over a quarter of the overflow
remains. Investigate these high-overflow source groups and assembly/allele
normalisation first; separately review whether the common-EUR axis is adequate
for the substantial long tail. Do not accept the current release as-is.

## Candidate-generation gate

`sidecars/reference_overlap.tsv` records both ancestry-panel and declared Hybrid
axis matches per Analysis, with separate ancestry and physical-scan denominators.
`validation.yaml` reports median, 5th and 10th percentile axis overlap, Analyses
below **5%**, and the scanned-row-weighted projected off-reference share. A
share above **25%**, or missing/invalid evidence for an included Analysis, blocks
candidate publication and names the worst contributors. This is a *bounded scan
projection*, not a whole-file cell count; in particular a case-control source
can stop at the ancestry-site limit, while quantitative SD estimation normally
continues to EOF. Excluded Analyses remain in the sidecar but do not contribute
to the build projection. Missing measurements on failed excluded scans are blank,
not zero-overlap estimates.

The existing tracked OGS-00011 bundle predates this change; it must be regenerated
against an `opengwasdb` resolver carrying the new `--variant-reference` diagnostics
before its own `validation.yaml` and sidecar can be updated. The measured 38.92%
from the first build would exceed the new 25% gate if the bounded projection is
representative.
