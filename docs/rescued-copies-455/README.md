# Rescued `swap_alleles` copies, regenerated (opengwasdb-stores#455)

Evidence from regenerating the 58 `swap_alleles` copies into scratch (nothing under
`/data/opengwasdb/derived/` was written). Data are not committed.

- `verification-receipt.tsv`: `tools/verify_rescued_gwas_files.py`, every row of every
  regenerated copy against its raw file: 0 mismatching rows in 58 files, 594,614,005 rows;
  `sha256` is the regenerated copy's.
- `manifest-checksums.tsv`: the 58 rows whose checksum the rescue manifest changes (the 33
  `flip_frequency` rows are byte-identical to the current manifest).
- `indel-alid-spelling-GCST90296697.json`: `tools/check_indel_alid_spelling.py` on a Ragged
  GWAS-SSF Analysis built by the engine at 259516a from the regenerated copy (`regen`), the
  current swapped copy (`swapped`) and the raw file (`raw`, the control).
