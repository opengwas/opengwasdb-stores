- **The Store Release readiness view names its real generator and its stale
  criteria are corrected (issue #415).** The header now gives the one command
  that reproduces the committed render,
  `python3 -m ogstores.release_readiness --info --built --output
  docs/store-release-readiness.md`, and the render is regenerated: it lists all
  24 registered Stores, including OGS-00013, -00015 and -00024 to -00027,
  where it listed 12. The 0.1.0 migration channel no longer calls that
  migration pending the adopted runtime (it needs a rebuild, #218/#197), and
  the AMR reference declaration reports the `hgdp1kgp-hg38-amr-ld` panel
  (#241) that OGS-00014 completed against (#229, #409).
