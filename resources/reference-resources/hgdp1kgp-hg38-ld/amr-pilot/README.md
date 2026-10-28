# AMR LD calibration pilot blocks (#241)

`blocks.tsv` lists the five frozen EUR-layout blocks of the AMR calibration
pilot (`tools/ld-panel/run_amr_pilot.py`). They are the regions of the EUR
calibration control's comparison. `ancestry` is `AMR`, so `run_panels.py`
writes the blocks under `AMR/`; `boundary_layout` records that the boundaries
are EUR's. The genome-wide build re-runs its held-out calibration on these same
blocks.

The pilot's membership inputs, the AMR mapping and the 347 Phase 3 AMR IDs, were
here when the pilot ran. They have moved, unchanged, to
[`../../hgdp1kgp-hg38-amr-ld/`](../../hgdp1kgp-hg38-amr-ld/). The production
`../populations_to_superpop.tsv` still excludes AMR (`no_AMR_panel`).

Results: [`docs/amr-ld-pilot-evidence.md`](../../../../docs/amr-ld-pilot-evidence.md).
