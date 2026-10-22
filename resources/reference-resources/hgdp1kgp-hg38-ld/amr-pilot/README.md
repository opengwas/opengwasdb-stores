# AMR LD calibration pilot inputs (#241)

These are the inputs of the AMR calibration pilot, `tools/ld-panel/run_amr_pilot.py`. They are not
a production policy: the production `../populations_to_superpop.tsv` still
excludes AMR (`no_AMR_panel`).

- `phase3-amr-samples.tsv`: the 347 AMR rows of the IGSR Phase 3 sample panel
  [`integrated_call_samples_v3.20130502.ALL.panel`](https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/integrated_call_samples_v3.20130502.ALL.panel).
  The full file is 55,156 bytes with SHA-256
  `b4023dc6ee2d62ee89c8d4d347db4d348e65518d66d346574cdae7a4bbd76858`.
  `make_membership.py --samples` uses it as the allowlist. Against the cached
  gnomAD cohort it yields 345 high-quality members.
- `populations_to_superpop.tsv`: labels the same 79 populations as the production
  mapping. Only 1kGP CLM, MXL, PEL and PUR are included, as `AMR`. The HGDP
  American populations are excluded by name (`hgdp_excluded_amr_pilot`), per the
  owner decision on #241.
- `blocks.tsv`: the five frozen EUR-layout blocks, the regions of the EUR
  calibration control's comparison. `ancestry` is `AMR` so that `run_panels.py`
  writes them under `AMR/`; `boundary_layout` records that the boundaries are EUR's.

Results: [`docs/amr-ld-pilot-evidence.md`](../../../../docs/amr-ld-pilot-evidence.md).
