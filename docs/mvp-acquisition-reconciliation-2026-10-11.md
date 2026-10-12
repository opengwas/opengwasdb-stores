# MVP (PMID 39024449) acquisition reconciliation — decision queue

Snapshot 2026-10-11. Read-only reconciliation of the dbGaP `phs002453` GIA
acquisition against the PMID 39024449 candidate table (epic #232, ticket #220).
No association rows were read, no conversion or download was run, and no
ancestry grouping was settled (that is ticket #221).

The intended source set is **6,022** Analyses. Acquisition extracted all 6,022
members from 33 MD5-verified tars. The exact numeric-key pass binds **6,020**;
the remaining 2 are genuine first-party numeric/identity discrepancies, not
spelling noise, and are held in
[`resources/scripts/dbgap-phs002453-key-discrepancies.yaml`](../../resources/scripts/dbgap-phs002453-key-discrepancies.yaml)
with `disposition: owner_decision_required`.

## State

| Measure | Value |
|---|---|
| Intended Analyses | 6,022 |
| Extracted, metadata-parsed | 6,022 |
| Bound by exact numeric key | 6,020 |
| Numeric-key discrepancies awaiting owner decision | 2 |
| Trait-label mismatches (binding unaffected) | 57 |
| Trait-label mismatches requiring owner scientific review | 27 |
| Trait-label mismatches formatting-only (policy acceptance) | 30 |

`map` does not guess a binding. An `owner_decision_required` record is reported
as `numeric_key_discrepancy` and keeps the mapping gate red; a `resolved` record
binds the named pair only after a recorded human decision. Every record is
re-validated on each run, and a stale or contradictory ledger fails loudly.

## Owner decisions required — missing accessions

### D1. GCST90476298 (African, quantitative)

* dbGaP member: `MVP_R4.1000G_AGR.Platelet_Mean_INT.AFR.GIA.dbGaP.txt.gz`
  * `Analyzed variable`: platelet count (mean, inv-norm transformed)
  * `Sample size`: `Total Sample Size=114731`; data header `num_samples=114731`
* GWAS Catalog (raw `meta.yaml`, md5 `30d14f53e4956e046b9a53f25def2968`):
  * trait `platelet count (mean, inv-norm transformed)`, ancestry African
    American or Afro-Caribbean, `sample_size: 235`
* Trait text and ancestry agree exactly; only the sample size differs
  (235 vs 114731). No dbGaP member declares `Total Sample Size=235`, so no
  alternative pairing exists.

**Question.** Is the Catalog `sample_size=235` a curation error this acquisition
may correct (set the ledger record `resolved`), or is GCST90476298 genuinely a
different Analysis whose deposited file is missing (leave
`owner_decision_required`)?

### D2. GCST90481129 (META, case-control)

* GWAS Catalog: AFR 110,183 + AMR 54,744 + **EAS 6,253** + EUR 406,693 =
  **577,873**; cases 50,122; controls 527,751.
* dbGaP member `Phe_371_3.META.GIA.dbGaP.txt.gz`: a meta-analysis of
  "AFR, AMR, and EUR" = **571,620**; cases 49,766; controls 521,854.
* The difference is exactly the EAS component (356 cases + 5,897 controls =
  6,253). No EAS `Phe_371_3` member is deposited.

**Question.** Is GCST90481129's Analysis genuinely absent from this deposit
(leave `owner_decision_required`), or is the deposit's 3-ancestry META the
intended Analysis and the Catalog EAS contribution an error (set `resolved`)?
The META mixture provenance is preserved for ticket #221 either way.

## Trait-label mismatches — disposition policy

All 57 bind by an exact unique numeric key; only the trait-text consistency
check differs. `map` writes a per-record queue, `mapping-review.tsv`, recording
the original dbGaP and Catalog labels; `trait_disposition` splits them into:

* **30 formatting-only** — the labels agree after Unicode normalisation,
  case-folding and removal of quoting/possessives (e.g. `Raynauds` /
  `Raynaud's`, `Ostearthritis` is *not* here). These need a recorded policy
  acceptance (`formatting_only_pending_policy`) but are never used to bind.
* **27 lexical/morphological** — `owner_scientific_review`. No spelling,
  plural, abbreviation or synonym heuristic accepts them:
  * `lipoid` vs `lipid` (5)
  * `H. pylori` vs `Helicobacter pylori infection` (4)
  * `Dermatomycoses` vs `Dermatomycosis` (4)
  * `examinations` vs `examination` (2)
  * `Prostrate` vs `prostate` (8)
  * `Ostearthritis` vs `Osteoarthritis` (4)

**Question.** Accept the deterministic punctuation-only policy, and disposition
each of the 27 lexical rows as same-concept or genuinely different.

## Deliberately excluded

* No conversion, download, re-extraction or full multi-TB hash/read scan.
* No `status=ok` for the two unbound accessions; their manifest rows remain
  non-ready until a decision, a genuine conversion and a checksum pass.
* No ancestry grouping or META-store split (ticket #221).
* No Store build, release acceptance, artifact mutation or identity change.

## Next steps after a decision

1. Set the ledger record disposition and record the decision in the ledger.
2. Run targeted conversion of only the resolved accession(s) and refresh the
   affected acquisition manifest with a genuine `sha256`.
3. Re-freeze the TSV Source Inventory for candidate generation.
