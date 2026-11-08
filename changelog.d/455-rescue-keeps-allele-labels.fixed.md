- **A `swap_alleles` rescue keeps the raw file's allele columns and rewrites the
  statistics, not the labels (issue #455).** `create_rescued_gwas_files.py` used
  to exchange `effect_allele` and `other_allele`, which made the effect allele
  the reference of every indel in the 58 rescued copies, so a build at
  opengwasdb 0.2.1 (#381) spelt each code-10/11 indel inverted. It now negates
  `beta` (and `z`, `Stat`), inverts `odds_ratio` and its interval (bounds
  exchanged), complements `effect_allele_frequency` (and `MinFreq`/`MaxFreq`),
  exchanges the signs in `Direction`, and leaves `hm_code` and every other
  column byte-identical; a missing value stays missing and an unreadable one
  stops the rescue. `tools/verify_rescued_gwas_files.py` checks every row of a
  regenerated copy against its raw file, and `--derived-base`/`--only` let the
  copies be regenerated into scratch first. `flip_frequency` is unchanged.
