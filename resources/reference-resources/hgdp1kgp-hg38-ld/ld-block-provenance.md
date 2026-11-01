# LD block source audit

The three recoverable GRCh38 block definitions disagree and none matches the
1,357 EUR blocks declared by the production `gpm-hg38-eur-ld` resource:

| Source | EUR rows | SHA-256 |
|---|---:|---|
| `MRCIEU/ukb-rap-utils` blob `727a55a` (`ld_regions_hg38.tsv`) | 1,336 | `7741b85dce047d50cde0065562653444b5937d02622ea77f4df885c9c3a662fd` |
| `MRCIEU/genotype-phenotype-map` blob `be73d8d` (`ld_regions_hg38_updated.tsv`) | 1,353 | `1655decc38cbaa56b14872fdc1582b7726662e803ca9569f3847133005626754` |
| `MRCIEU/genotype-phenotype-map` blob `98950de` (`ld_regions_hg38_fulllength.tsv`) | 1,397 | `539b8c4f2ac14404259cacf053fe956e4e84e441dd32a185a9a50059fe37bdb8` |

The production declaration is the calibration contract. The canonical
`ld_regions_hg38.tsv` therefore retains the recoverable ancestry-specific
AFR/EAS/SAS rows but replaces EUR with boundaries reconstructed from the 1,357
real block basenames under `/data/opengwasdb/reference/ukb-hg38/EUR`. Its
SHA-256 is `0763afc708cd665d65aa6bae7e01e2cf68f34e5aa84d4a69897639b010727f56`.
No synthetic edge blocks were manufactured.

## Variant-table invariants a block's rows carry

These hold for every block of every panel built from these definitions, and are
what a rebuild or an in-place repair must reproduce (stores#348):

- **`EAF` is the frequency of the row's `EA`**, never of `OA`. PLINK's additive
  export counts one allele per column and names it in the column suffix; plink2
  counts REF. The generator reads that allele rather than assuming ALT, and a
  block whose `EAF` is the other allele's frequency is a defect, not a variant
  of the contract.
- **One row per ALID within a block.** gnomAD splits multi-allelic sites, so a
  deletion and an insertion at one position can canonicalise to one ALID; of the
  records that pass every filter, the one with the higher MAF is kept, a tie
  going to the earlier record. Rows sharing an ALID across two adjacent blocks
  are by design (both blocks include their boundary positions).
- **Row order is the only tie between the files.** TSV row *i* is eigenvector
  row *i*, so dropping a row is a row selection from `vectors`; `values` is the
  full spectrum and is never rewritten, so the consumer's cumulative-variance cut
  keeps its meaning.
- **Provenance records what was done to the block.** `provenance.json` is the
  completion marker, so it is written last, and an in-place repair appends its
  record to `repairs`: the collision rule, the rows dropped from the TSV and
  `vectors`, the rows whose `EAF` was inverted, and the sha256 of each original
  file. The originals also go to a backup directory, and one receipt names every
  repaired block's before and after hashes.
- **A repaired block verifies against its backup**, not against the exact
  `||X v_j||^2 = value_j` binding identity, which needs every column and a row
  drop cannot preserve, and not against genotypes either: a shipped table keeps
  an ALID that still names two source records and carries no `REF`/`ALT` to tell
  them apart. An unrepaired block is still held to that identity.
- **Orientation is gated, not assumed.** `run_panels` gates every ancestry it
  builds or finishes before a run may succeed, `build_amr_panel` gates the
  finished panel as its last step, `verify_block --panel` gates a panel on
  demand, and `repair_panel` gates before and after it writes. All four measure
  the panel's `EAF` against the UKB ancestry mixture, one group per ancestry,
  and require `r > 0.9` on matched variants; all four refuse, rather than pass,
  a sample too few variants match to decide on.
