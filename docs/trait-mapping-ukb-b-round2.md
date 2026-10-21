# UKB-B canonical trait mapping: round 2

Round `ukb-b-2026q4` reran the 2,093 UKB-B labels left unmapped after
`ukb-b-2026q3`. It uses the issue #185 pipeline: UK Biobank Showcase field
resolution, coded-field value text, the ICD-10 cross-reference channel, the
phenotype-only candidate space, BioLORD-2023 retrieval, 100-candidate
shortlists, and Showcase question context for non-coded fields.

## Result

| Outcome | Labels |
| --- | ---: |
| Auto-accepted into `mapping.tsv` | 141 |
| New review queue: uncertain pick | 465 |
| New review queue: uncertain abstention | 380 |
| Confident `none_suitable` | 971 |
| No candidate retrieved | 136 |
| **Total** | **2,093** |

The round added 141 canonical mappings, resolving 142 Analyses. Together with
round 1, `mapping.tsv` now contains 550 UKB-B mappings and resolves 553 of the
2,514 UKB-B Analyses (22.0%); 1,961 remain unmapped. The Jev choice stage used
9,144,273 input tokens and cost $0.3841. There were no chooser errors or pending
results.

The new review list is
[`review-queue-ukb-b-2026q4.tsv`](../resources/reference-resources/canonical-trait-mapping-efo/review-queue-ukb-b-2026q4.tsv).
It supersedes the Q3 queue for active review; the Q3 files remain as historical
round evidence. The 136 labels for which filtering left no candidate cannot be
represented by the proposal-backed review-queue schema, so they remain visible
in the complete per-label audit table
[`trait-mapping-ukb-b-round2.tsv`](data/trait-mapping-ukb-b-round2.tsv).

## Movement from round 1

| Round 1 outcome | Auto-accepted | Review: pick | Review: abstention | No suitable | No candidate | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Review: pick | 103 | 342 | 39 | 63 | 19 | 566 |
| Review: abstention | 26 | 82 | 166 | 159 | 57 | 490 |
| No suitable | 12 | 41 | 175 | 749 | 60 | 1,037 |
| **Total** | **141** | **465** | **380** | **971** | **136** | **2,093** |

The improved retrieval recovered 12 confident mappings from round 1's
confident `none_suitable` list and another 129 from its two review buckets.
The three motivating retrieval misses now behave as intended: all prostate
cancer variants are auto-accepted; the main-diagnosis constipation and
chalazion rows retrieve the correct term but remain in review at confidence
0.58 and 0.82 respectively.

## Retrieval and regression checks

- **Showcase resolution:** 2,489 of 2,502 unique UKB-B labels matched a
  Showcase field (99.48%); 13 were unmatched, below the 1% target.
- **ICD-10 benchmark:** the source-xref term appeared in the top 10 for
  120/125 labels (96.0%), up from the 76/125 raw-label baseline.
- **OGS-00011 regression set:** on the 2,220 labels shared with the original
  validation, recall@10 increased from 74.41% to 75.50%; recall@100 was 85.59%.
  At the 0.85 confidence / 0.20 margin gates, exact agreement with the GWAS
  Catalog term was 87.75% (695/792), effectively unchanged from 87.83%
  (657/748), while the number of exact matches increased by 38.
- **Off-target space:** the configured non-phenotype prefixes and cell-line /
  cell-type lineages are excluded before ranking. PR #188 separately verified
  that none of the 934 OGS-00011 truth terms falls in the excluded space.
- **Hospital administration:** all 302 remaining administrative labels entered
  the round; none was auto-accepted (15 are in review, 259 are confident
  abstentions, and 28 had no candidate). No label was filtered out upstream.

## Auto-acceptance spot check

A deterministic random sample of 25 of the 141 new auto-accepted rows (Python
seed `185`) was checked against the source label and selected term. All 25 were
acceptable: 21 exact or near-exact mappings and four broader but compatible
terms. The reviewed rows and notes are published in
[`trait-mapping-ukb-b-round2-spot-check.tsv`](data/trait-mapping-ukb-b-round2-spot-check.tsv).
This is an AI-assisted spot check, not independent human curation.

## Pins and artifacts

- Ontology: EFO v3.94.0, index format 3 (87,709 live and obsolete CURIE terms).
- Embeddings: `FremyCompany/BioLORD-2023`, ontology build
  `blake2b:5328e64d77fdaf5847e565c01bb94c2d`.
- Chooser: Jev `jev-1.13.0`.
- Shortlist size: 100.
- Promotion gates: confidence ≥ 0.85 and runner-up margin ≥ 0.20.
- Showcase snapshot downloaded 2026-09-29. Per-file SHA-256 digests are in
  [`trait-mapping-ukb-b-round2-summary.json`](data/trait-mapping-ukb-b-round2-summary.json).
- Per-label outcomes:
  [`trait-mapping-ukb-b-round2.tsv`](data/trait-mapping-ukb-b-round2.tsv).
- Confident abstentions:
  [`no-suitable-term-ukb-b-2026q4.tsv`](../resources/reference-resources/canonical-trait-mapping-efo/no-suitable-term-ukb-b-2026q4.tsv).

The untracked resumable round is under
`.cache/curation/rounds/ukb-b-2026q4/` on the build host. Re-running any stage
uses the pinned `round.yaml` and skips completed work.
