# Dense PMID 39024449 acquisition from dbGaP (phs002453)

`acquire_dbgap_phs002453.py` materialises the 6,022 GWAS Catalog Analyses of
PMID 39024449 (Verma et al. 2024, VA Million Veteran Program) from the authors'
own dbGaP deposit (`phs002453`, open access) instead of from the EBI mirror.

The two sources carry the same association values: EBI's *raw* `GCST*.tsv.gz`
files for this publication are the dbGaP GIA members with the columns renamed
and reordered and `NA` written `#NA`. Acquisition from dbGaP is ~115x faster per
stream (37 MB/s vs ~0.3 MB/s), and the tars are complete, where the EBI mirror
served a large fraction of accessions only as raw files that the harmonised
pass could not use.

The output is a GWAS-SSF mirror with EBI's layout, so downstream consumers (the
inventory freeze, preflight, the `opengwasdb.gwas-ssf` reader) see the same
bytes they would have seen from EBI, except for six round positions per
Analysis that EBI blanked and the mirror keeps (see below).

## Inputs

```
/data/opengwasdb/raw/dbgap-phs002453/
  GIA/tars/<tar>                     the 33 MD5-verified dbGaP tars (4.4 TiB)
  GIA/tars/<tar>.verified            exists when the tar's MD5 has been checked
  GIA/meta/*.table_of_contents.txt   one `tar -tv` listing per tar
  GIA/meta/tars.txt                  the tar list
resources/data/derived/store-candidates-analyses.tsv   candidate table (gitignored)
```

## Usage

```
# 1. materialise every member of every verified tar (33 tars, one sequential
#    pass each; members land in GIA/extracted/<tar directory>/<member>)
pixi run python3 -m manifest_generation.providers.dbgap.mvp.acquire_dbgap_phs002453 \
  --out /data/opengwasdb/raw/dbgap-phs002453 extract --jobs 4

# 2. bind each Analysis to its candidate-table row; writes mapping.tsv,
#    mapping-report.tsv and the mapping-review.tsv trait queue, and exits
#    non-zero unless the bijection is complete (the default
#    --discrepancies ledger is resources/scripts/dbgap-phs002453-key-discrepancies.yaml)
pixi run python3 -m manifest_generation.providers.dbgap.mvp.acquire_dbgap_phs002453 map \
  --candidates resources/data/derived/store-candidates-analyses.tsv

# 3. convert into gwas-ssf/<bucket>/<GCST>/{<GCST>.tsv.gz,<GCST>.tsv.gz-meta.yaml}
pixi run python3 -m manifest_generation.providers.dbgap.mvp.acquire_dbgap_phs002453 convert --jobs 32

# 4. one acquisition manifest per store_key, readable by
#    manifest_generation.source_inventory
pixi run python3 -m manifest_generation.providers.dbgap.mvp.acquire_dbgap_phs002453 manifest --jobs 8
```

Every stage is idempotent, writes through `.partial` files and renames, and can
be re-run after an interruption. For trials, `map`/`convert`/`manifest` accept
`--analysis` (repeatable, comma-separated) and `--limit`; `map --allow-partial`
relaxes the "all 6,022" requirement but never the bijection itself.

## What each stage guarantees

**extract** streams each tar exactly once. A tar has no index, so a member is
only reachable by reading everything before it: re-scanning a 140 GB tar per
member is not an option, and the pass therefore verifies each member's size
against the table of contents while writing it. A tar without a `.verified`
marker is skipped with a message rather than read. A member whose extracted size
disagrees with the table of contents is re-extracted.

**map** binds each Analysis to one candidate row of PMID 39024449 by
`(ancestry, sample_size, n_cases, n_controls)`:

- the ancestry comes from the member name (AFR/AMR/EAS/EUR/META);
- a META Analysis is identified as `ancestry_fraction < 1`, *not* by
  `ancestry_group`, because the candidate table still labels those rows
  `European` until the companion multi-ancestry store split relabels them;
- the metadata's *Analyzed variable* text is the consistency check. It is also
  the tie-break where the numeric key is not unique: 89 groups (225 rows) share
  `(ancestry, N, 0, 0)` because a trait's maximum/mean/minimum analyses have
  equal N, and the deposit's own text distinguishes them (case-folded, with the
  catalogue's `(PheCode N)` suffix stripped for binary traits). If no single
  candidate row matches, the Analysis is reported `ambiguous_analysis`.

`mapping.tsv` records every binding with its `match_basis` (`unique-key` or
`trait-tiebreak`) and `trait_check` (`exact`, `prefix`, `mismatch`).
`mapping-report.tsv` lists `unmatched_analysis`, `ambiguous_analysis`,
`duplicate_analysis_match`, `trait_check_mismatch` and `unused_candidate` rows.
The command exits non-zero unless all 6,022 candidate rows are bound to exactly
one Analysis each.

A numeric-key disagreement is never relaxed or guessed. The committed
`dbgap-phs002453-key-discrepancies.yaml` ledger records each known disagreement
with its first-party evidence. A `resolved` record binds the named pair with
`match_basis=discrepancy-ledger` after a recorded human decision; an
`owner_decision_required` record is reported as `numeric_key_discrepancy` and
keeps the bijection gate red. Every record is re-validated against the
extracted metadata and the candidate table, so a stale record (keys now agree,
member/candidate absent, or already bound) fails the run loudly. Pass
`--discrepancies` to point at a different ledger.

A `resolved` record must carry an `owner_decision` (authority, quote, date); a
resolved record without one is rejected. The ledger is the only place a numeric
disagreement may be resolved, and the owner decision is committed with it.

Canonical metadata follows the owner decision for ticket #220 (2026-10-11):
dbGaP is authoritative. The converted sidecar's `trait_description` and
`samples.sample_size` come from the deposit Analysis, and the acquisition
manifest's `trait`/`sample_size` do too. Where the GWAS Catalog counterpart
differs, it is retained as provenance in the sidecar's `catalog_metadata` block
(and in `mapping.tsv`'s `candidate_trait`), never discarded.

`mapping-review.tsv` is the per-record trait-label queue. Every
`trait_check=mismatch` row keeps its original dbGaP and Catalog labels, its raw
`trait_category` (`punctuation_only` or `lexical_variant`) and its
`disposition`. `disposition` is derived from the validated, cited
`owner_decisions` trait policy in the ledger: a complete policy yields
`resolved_<authority>_authoritative` (for ticket #220's 2026-10-12 decision,
`resolved_dbgap_authoritative`), while a missing or incomplete policy keeps the
category's original pending default (`formatting_only_pending_policy` or
`owner_scientific_review`). A `null`, blank or non-text provenance field counts
as missing rather than being coerced into evidence, and a `resolved`
discrepancy record likewise needs a non-empty authority/quote/recorded/citation.
The canonical label is then the dbGaP
`Analyzed variable` and the Catalog counterpart is preserved. The classification
does not assert the labels are lexically equivalent, and no spelling or synonym
heuristic binds an Analysis.

**convert** runs `gzip -dc member | awk | gzip` per Analysis. The awk program is
header-driven and matches columns by name; the output column set is EBI's, so a
source shape keeps its shape:

| source colour | output columns |
| --- | --- |
| `beta sebeta` | `beta standard_error` |
| `or ci` (binary) | `odds_ratio standard_error(#NA) ci_upper ci_lower` |
| missing `r2` (2 META members) | `r2` omitted, as in EBI's file for the same Analysis |

`effect_allele = ea`, `other_allele` is whichever of `ref`/`alt` is not `ea`,
values are copied verbatim, `NA` and empty become `#NA`, `ci` is split on the
comma into `ci_lower`/`ci_upper`, and a binary Analysis has no `standard_error`.
An unrecognised source column, a missing required column, a row with more fields
than the header, or an effect allele that is neither `ref` nor `alt` fails the
file loudly (non-zero exit, no output file) instead of guessing.

Two exceptions to "copied verbatim" come from comparing complete EBI raw files
against the converted dbGaP member (see "Verification"):

* **p_value goes through a float, as in EBI's files.** EBI's file has `1.0` where
  the deposit has `1`, and `0.0006` where the deposit has `6e-04` — the shortest
  decimal that round-trips, plus `.0` for an integer-valued float (518 + 5 rows
  per Analysis). This is reproduced, except for p-values below the normal
  float range (`1e-321`): awk reads those as 0, so they are copied through as
  written rather than becoming `0.0`. No other column is reformatted: `r2`,
  `odds_ratio`, `ci_upper`/`ci_lower`, `n`, `num_cases`/`num_controls` and
  `chromosome` are copied through unchanged even where they hold integer-like
  values.
* **Round positions written in scientific notation are written as integers.**
  Six rows per Analysis give the position as `2.4e+07`, `8e+06`, ... (R prints
  a round integer that way). EBI's converter leaves `base_pair_location` empty
  for those rows, so the reader drops those variants; the mirror writes
  `24000000`, `8000000`, ... instead. This is the only place the mirror
  deliberately differs from EBI. A position that is not an integer at all still
  gets an empty cell (the row is kept), as in EBI's file.

EBI's raw files also use **CRLF line endings** (its converter is a CSV writer
with the default terminator); the conversion writes CRLF for the same reason.

The sidecar mirrors EBI's `*-meta.yaml` fields (`gwas_id`, `trait_description`,
`genome_assembly: GRCh38`, `coordinate_system: 1-based`,
`genotyping_technology`, `samples`, `data_file_name`, `file_type: GWAS-SSF v1.0`,
`data_file_md5sum`, `is_harmonised: false`, `is_sorted: false`) and adds
provenance: `source.{repository,study,tar,member,tar_md5,url}` and
`dbgap_analysis_description`. Per the ticket #220 owner decision, the canonical
`trait_description` and `samples.sample_size` are the dbGaP deposit values; when
the GWAS Catalog counterpart differs it is preserved under `catalog_metadata`.

**manifest** writes `<out>/manifests/<store-key-slug>-download-manifest.tsv`
using exactly the columns of the EBI raw-pass manifest
(`providers/gwas_catalog/download_ebi_gwas_catalog_raw.py`), so a freeze reads it unchanged: the
fourteen `source_inventory.ACQUISITION_MANIFEST_COLUMNS` plus
`harmonised_status`, `genome_assembly`, `file_type`, `is_harmonised` and
`seconds`. Converted rows are `status=ok` with `genome_assembly GRCh38`,
`file_type GWAS-SSF v1.0`, `is_harmonised false` and the sha256 of the data file
(computed from the materialised bytes). `harmonised_status` is empty: this pass
has no preceding harmonised pass on this mirror, and the EBI raw manifest's
column carries that other pass's fact. A candidate with no converted file gets
`status=error` and an explanatory `error`, which the freeze classifies as
non-ready rather than as a usable member.

## Verification

`tests/dbgap-phs002453/test_acquire_dbgap.py` (wired into
`pixi run test`) builds synthetic real-format tars and
covers extraction, mapping (including the ambiguity and unmatched cases),
golden row equality against EBI's published rows, the META shape without `r2`,
unrecognised headers, idempotence/atomicity and the inventory freeze. It also
covers the key-discrepancy ledger (a pending record blocks and is named, a
resolved record binds with provenance, a stale record fails) and the
trait-disposition classification.

The end-to-end check on real data is a line-by-line comparison with EBI's raw
file (on the decompressed streams; our gzip settings differ from EBI's):

```
diff <(gzip -dc <ebi>/GCST90476552.tsv.gz) \
     <(gzip -dc <out>/gwas-ssf/GCST90476001-GCST90477000/GCST90476552/GCST90476552.tsv.gz)
```

The only differences allowed are the recovered positions: rows where EBI's
`base_pair_location` is empty and the mirror has the integer. For GCST90476552
(EAS quantitative, 9,337,549 rows) and GCST90477848 (EAS binary, 9,337,844 rows),
the two smallest EAS Analyses, exactly 6 lines differ in each file, all of them
this kind (chr9:24000000, chr10:28000000, chr10:120000000, chr10:124000000,
chr20:8000000, chr20:62000000); every other byte is identical. EBI is slow
(~0.1 MB/s per stream, ~2.5-3.5 MB/s with 24 range streams), so pick the
smallest Analyses of a store for this check.
