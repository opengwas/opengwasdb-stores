# HGDP+1kGP hg38 AMR LD reference panel: inputs (#241)

These are the inputs of `tools/ld-panel/build_amr_panel.py`, which builds the
genome-wide AMR panel that the owner approved on 2026-10-22 after the
calibration pilot (`docs/amr-ld-pilot-evidence.md`). The panel's
`resource.yaml` is added once the build has been verified.

- `phase3-amr-samples.tsv`: the 347 AMR rows of the IGSR Phase 3 sample panel
  [`integrated_call_samples_v3.20130502.ALL.panel`](https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/integrated_call_samples_v3.20130502.ALL.panel).
  The full file is 55,156 bytes with SHA-256
  `b4023dc6ee2d62ee89c8d4d347db4d348e65518d66d346574cdae7a4bbd76858`.
  `make_membership.py --samples` uses it as the allowlist. Against the cached
  gnomAD cohort it yields 345 high-quality members.
- `populations_to_superpop.tsv`: labels the same 79 populations as
  `hgdp1kgp-hg38-ld`'s production mapping. Only 1kGP CLM, MXL, PEL and PUR are
  included, as `AMR`. The HGDP American populations are excluded by name
  (`hgdp_excluded_amr_pilot`), per the owner decision on #241. The production
  mapping is unchanged.
- `ld_regions_hg38.amr.tsv`: the 1,357 EUR blocks of `hgdp1kgp-hg38-ld`'s
  `ld_regions_hg38.tsv`, relabelled `AMR`. `boundary_layout` records that the
  boundaries are EUR's. LD is estimated from AMR genotypes only.

Variant QC (owner decision, 2026-10-22):
- PASS records only, filtered at conversion;
- at least 80% of members called;
- MAF ≥ 0.01 over called individuals, with no MAC floor;
- of the records sharing an ALID, the one with the higher MAF is kept.
