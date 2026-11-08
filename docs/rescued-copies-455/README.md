# Rescued `swap_alleles` copies, regenerated (opengwasdb-stores#455)

Evidence from regenerating the 56 `swap_alleles` copies into scratch (nothing under
`/data/opengwasdb/derived/` was written). Data are not committed.

Only the harmonised statistics are rewritten (`beta`, `odds_ratio` with its interval,
`effect_allele_frequency`). The unharmonised study columns (`MinFreq`, `MaxFreq`,
`Direction`, `FreqSE`) are left exactly as raw: the EBI harmoniser never flipped them, so
they follow the study's own effect allele whatever the row's `hm_code`. A column the
rescue has not classified refuses the file.

Left out pending the owner: **GCST90275159** and **GCST90275160**, whose `beta` (-9/9 by
`hm_code`) and standard error (1000) are dummies by their own yaml, so their `swap_alleles`
verdict (r = -0.445 over 62 variants) rests on no real effects. Neither is regenerated or
replaced, and their manifest rows stay as they are.

- `verification-receipt.tsv`: `tools/verify_rescued_gwas_files.py`, every row of every
  regenerated copy against its raw file: 0 mismatching rows in 56 files, 577,093,421 rows;
  `sha256` is the regenerated copy's. The replace script reads its file list from here.
- `manifest-checksums.tsv`: the 56 rows whose checksum the rescue manifest changes (the 33
  `flip_frequency` rows and the two left-out rows are unchanged).
- `indel-alid-spelling-GCST90296697.json`: `tools/check_indel_alid_spelling.py` on a Ragged
  GWAS-SSF Analysis built by the engine at 259516a from the regenerated copy (`regen`), the
  current swapped copy (`swapped`) and the raw file (`raw`, the control).
- `tools/replace_rescued_copies.sh` installs the copies once the owner approves.
