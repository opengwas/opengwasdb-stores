# MVP (PMID 39024449) acquisition reconciliation — owner decision record

Snapshot: evidence captured 2026-10-11; owner decision recorded 2026-10-12.
Records the owner decision on the dbGaP `phs002453` GIA acquisition against the
PMID 39024449 candidate table (epic #232, ticket #220) and the resulting
metadata-only reconciliation. No association rows were read, no conversion or
download was run, and no ancestry grouping was settled (ticket #221).

## Owner decision

> "for mvp use the dbgap information for all three queries."

Recorded 2026-10-12 (owner comment created 21:24:22 UTC; the primary evidence
itself was captured 2026-10-11), authority **dbGaP**, durable citation
<https://github.com/opengwas/opengwasdb-stores/issues/220#issuecomment-6132878250>.
dbGaP `phs002453` is the canonical source; the GWAS Catalog counterparts are
retained as provenance. The decision is committed in
[`resources/scripts/dbgap-phs002453-key-discrepancies.yaml`](../../resources/scripts/dbgap-phs002453-key-discrepancies.yaml)
(`owner_decisions`), and each resolved record carries its `owner_decision`.

## Resolved — the two missing accessions

| Accession | Canonical (dbGaP) | Catalog provenance retained |
|---|---|---|
| `GCST90476298` | AFR, `platelet count (mean, inv-norm transformed)`, N=114731 | Catalog `sample_size` 235 (EBI `meta.yaml` md5 `30d14f53…`) |
| `GCST90481129` | META, `Inflammation of eyelids`, N=571620, cases=49766, controls=521854 | 4-ancestry Catalog aggregate N=577873 (AFR 110183 + AMR 54744 + EAS 6253 + EUR 406693); EBI `meta.yaml` md5 `56cb4b91…` |

Both bind with `match_basis=discrepancy-ledger`. The converted sidecar's
canonical `trait_description`/`samples.sample_size` follow dbGaP; the Catalog
values are kept under `catalog_metadata` only where they differ.

## Trait-label policy (57 mismatches) — resolved, dbGaP authoritative

dbGaP `Analyzed variable` is the canonical label for all 57; the GWAS Catalog
counterpart is preserved in `mapping.tsv` (`candidate_trait`) and in the
converted sidecar's `catalog_metadata`.  All 57 are **resolved as
dbGaP-authoritative** by the owner decision; none is an outstanding gate.  This
does not assert the two labels are lexically equivalent.  `mapping-review.tsv`
records the raw classification alongside the resolved disposition:

* **30 punctuation-only** (case/quoting/possessives) — raw classification.
* **27 lexical/morphological** — raw classification:
  `lipoid`/`lipid` (5), `H. pylori`/`Helicobacter pylori` (4),
  `Dermatomycoses`/`Dermatomycosis` (4), `examinations`/`examination` (2),
  `Prostrate`/`prostate` (8), `Ostearthritis`/`Osteoarthritis` (4).

Every row's `disposition` is `resolved_dbgap_authoritative`; `trait_category`
is the raw classification.  No fuzzy or typo-based identity binding is
performed.

## Reconciliation result

Metadata-only `map` over the real mirror (reads `*.dbGaP.metadata.txt` and TOC
files only; never opens a `.txt.gz`), written to the ticket work directory
`/data/opengwasdb/work/epic232/220/resolved-sim/` through a `GIA` symlink. The
historical mirror mapping is **not** overwritten.

| Measure | Value |
|---|---|
| Intended Analyses | 6,022 |
| Bound (exact unique / trait tie-break / ledger) | 5,795 / 225 / 2 |
| Total bound | **6,022** |
| `unmatched_analysis` / `unused_candidate` / `numeric_key_discrepancy` | 0 / 0 / 0 |
| `trait_check` exact / prefix / mismatch | 5,962 / 3 / 57 |
| `mapping-review.tsv` rows | 57 (30 punctuation-only, 27 lexical; all `resolved_dbgap_authoritative`) |

Simulation output hashes (under `/data/opengwasdb/work/epic232/220/resolved-sim/`):

| File | md5 |
|---|---|
| `mapping.tsv` | `7a650879e70cdad773a912a168f0d856` |
| `mapping-report.tsv` | `5a1ab1262266b47f6de258b6114b9e79` |
| `mapping-review.tsv` | `c6064939acd52a4481b8c82ff5c2e0f1` |

The historical mirror `mapping.tsv`/`mapping-report.tsv` were regenerated
byte-for-byte from an empty ledger and confirmed unchanged
(`a9b83d6c57874d00f71e2760c4a9edde` / `f0b00a0b3b2bc1422b10bba0b1b27f9b`); no
`mapping-review.tsv` is written to the historical mirror.

### Operational note (mirror footprint)

An initial simulation ran directly over the mirror before the isolated-output
instruction, then was relocated: the resolved outputs were moved to
`/data/opengwasdb/work/epic232/220/resolved-mirror-overwrite/`, the historical
mapping regenerated under `/data/opengwasdb/work/epic232/220/historical/`, and
the mirror restored to the historical content (verified by the md5s above).  No
mirror write has occurred since 2026-10-12 22:31:03 +0100.

### Interpreter

Unit tests were rerun with the adopted environment
`/home/gh13047/repo/opengwasdb-stores-wt/218-current-runtime/.pixi/envs/dev/bin/python`
(core `opengwasdb` at `4e92256`).  An earlier full-suite run used the
`opengwasdb-stores/.pixi/envs/dev` interpreter whose installed core is older
(zarr codec rename, no `build-indexed-subset`), which is why unrelated suites
failed; those failures are environmental, not this ticket's.

## Sidecar schema

No core or stores consumer validates the dbGaP `-meta.yaml` keys: the sidecar is
only read generically (`yaml.safe_load`) by the acquisition tooling/tests, and
it already carried the extra `source` and `dbgap_analysis_description` fields
before this change. The `catalog_metadata` block is therefore additive
provenance; the EBI field set is unchanged and any strict consumer can ignore
the extra keys. If a future strict schema forbids extras, the same values
remain in the ledger and `mapping.tsv`.

## Deliberately excluded

* No raw-content scan, conversion, download or re-extraction.
* No acquisition-manifest refresh and no `status=ok` promotion yet: the
  manifest/`sha256` for the two accessions still requires an actual conversion,
  which is a separate authorised step.
* No full Store build, release acceptance/promotion or identity change.
* No edit to the global source-candidate TSV; the Catalog values are preserved
  as provenance rather than rewritten.
* No ancestry grouping or META-store split (ticket #221).

## State is not acquisition completion

This record proves a metadata-only **mapping** reconciliation, not acquisition
completion. Two converted files are still absent, and the pre-existing
converted canonical outputs for the two accessions (if any) are stale and need a
bounded retry once conversion is authorised. Do not claim "6,022 converted" or
acquisition complete from this document.

## Next steps (separately authorised)

1. Targeted conversion of `GCST90476298` and `GCST90481129` and manifest refresh
   with genuine `sha256`; re-verify the resulting sidecar carries the dbGaP
   canonical metadata and the preserved Catalog provenance.
2. Freeze the TSV Source Inventory for candidate generation once the
   ancestry/META split (#221) is settled.
