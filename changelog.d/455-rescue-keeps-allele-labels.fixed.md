- **A `swap_alleles` rescue keeps the raw file's allele columns and rewrites the
  harmonised statistics, not the labels (issue #455).** `create_rescued_gwas_files.py`
  used to exchange `effect_allele` and `other_allele`, which made the effect allele
  the reference of every indel in the rescued copies, so a build at opengwasdb
  0.2.1 (#381) spelt each code-10/11 indel inverted. It now negates `beta`, inverts
  `odds_ratio` and its interval (bounds exchanged) and complements
  `effect_allele_frequency` exactly; `hm_code`, the alleles and the unharmonised
  study columns (`MinFreq`, `MaxFreq`, `Direction`) stay byte-identical, a missing
  value stays missing, and an unreadable value, a column it has not classified or a
  wrong field count stops the rescue. GCST90275159 and GCST90275160 (dummy
  effects) are excluded from the rescue for good (owner, 10 Nov 2026) and marked
  `excluded` in the rescue manifest. `tools/verify_rescued_gwas_files.py`
  checks every row of a regenerated copy against its raw file;
  `tools/replace_rescued_copies.sh` installs them without a mixed state;
  `--derived-base` needs `--manifest-out`. `flip_frequency` is unchanged.
