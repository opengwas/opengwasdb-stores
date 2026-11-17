# Rescued `swap_alleles` copies, regenerated (opengwasdb-stores#455)

Evidence from regenerating the 56 `swap_alleles` copies into scratch and the
owner-approved replacement completed on **10 November 2026** in
`/data/opengwasdb/derived/ebi-gwas-catalog/`. Data are not committed.

Only the harmonised statistics are rewritten (`beta`, `odds_ratio` with its interval,
`effect_allele_frequency`). The unharmonised study columns (`MinFreq`, `MaxFreq`,
`Direction`, `FreqSE`) are left exactly as raw: the EBI harmoniser never flipped them, so
they follow the study's own effect allele whatever the row's `hm_code`. A column the
rescue has not classified refuses the file.

**#455 checklist: 56 regenerated, 2 excluded (owner, 10 Nov 2026).**

Excluded for good: **GCST90275159** and **GCST90275160** (`EXCLUDED_SWAP_STUDIES`). Their
`beta` (-9/9 by `hm_code`) and standard error (1000) are dummies by their own yaml, so their
`swap_alleles` verdict (r = -0.445 over 62 variants) rests on no real effects. Neither is
regenerated or replaced. In the rescue manifest their rows have `status` = `excluded`, the
reason in `error`, and no `data_file`, `yaml_file`, `sha256` or sizes, so no reader takes
them as rescued. Their current derived copies stay on disk, unused. (Their `ref_allele: EA`
flag is stale and harmless: neither has indels.)

- `verification-receipt.tsv`: `tools/verify_rescued_gwas_files.py`, every row of every
  regenerated copy against its raw file: 0 mismatching rows in 56 files, 577,093,421 rows;
  `sha256` is the regenerated copy's. The replace script reads its file list from here.
- `manifest-checksums.tsv`: the 56 rows whose checksum the rescue manifest changes (the 33
  `flip_frequency` rows are unchanged; the two excluded rows change to `excluded`, listed in
  `excluded-rows.tsv`).
- `indel-alid-spelling-GCST90296697.json`: `tools/check_indel_alid_spelling.py` on a Ragged
  GWAS-SSF Analysis built by the engine at 259516a from the regenerated copy (`regen`), the
  historical swapped copy (`swapped`) and the raw file (`raw`, the control).
- `tools/replace_rescued_copies.sh` installed all 56 copies and the manifest after
  the [owner's approval on 10 November](https://github.com/opengwas/opengwasdb-stores/issues/455#issuecomment-6768078473).
  The operation ran from 13:31:23 to 13:32:31 GMT, exit 0, in 67.87 seconds;
  `scratch/stores-455/replace.out` under the #307 run directory records the checks,
  staging, replacement and manifest installation. The
  [completion report](https://github.com/opengwas/opengwasdb-stores/issues/455#issuecomment-6768709621)
  confirms all 56 live SHA-256 checksums match the receipt and both exclusions are
  recorded. The old manifest is saved at
  `/data/opengwasdb/work/orchestrate/opengwasdb-307/scratch/stores-455/manifest.before.tsv`
  (SHA-256 `4dba12ca204b34f102df4690ce50a8069be85d937ede1f8d94fac68ab2de97ee`).
