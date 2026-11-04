- **Every HGDP+1kGP LD panel block's `EAF` is the frequency of its `EA`, and a
  block holds one row per ALID (issue #348).** PLINK's `--export A` counts REF
  and names it in the column suffix; the builder read it as ALT dosage, so every
  shipped AFR/EAS/SAS/EUR/AMR block stored the *other* allele's frequency, and
  the old panels also kept both records of an ALID that names an insertion and a
  deletion. `tools/ld-panel/construct_block.py` now reads the counted allele from
  the suffix and orients the reported frequency and the eigenvectors to the
  effect allele, so new panels are right. `tools/ld-panel/verify_block.py` gains
  a non-circular EAF check against the recorded export vocabulary, a
  panel-level positive-orientation gate against the UKB ancestry mixture (`r >
  0.9`, refused below a minimum match count), and a repaired-block mode that
  holds a row-dropped block to its pre-repair backup because the exact
  `||X v_j||^2 = value_j` identity cannot survive a row drop. The new
  `tools/ld-panel/repair_panel.py` corrects a shipped panel in place without a
  rebuild: `EAF := 1 - EAF`, and the losing row of each duplicated ALID is
  dropped from the TSV and from `vectors` (`values` untouched), with backups, an
  atomic replace per file, a repair record in each block's `provenance.json` and
  a sha256 receipt. It refuses a panel whose EAF does not correlate *negatively*
  with the reference first, so a re-run cannot flip twice, and it resumes.
