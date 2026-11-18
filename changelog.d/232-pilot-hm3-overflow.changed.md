- **The EUR Hybrid pilots' `hm3` subsets skip overflow-only variants (epic #232).**
  OGS-00004 and OGS-00005 now build their Dense Component on the gnomAD v4.1
  common axis, as OGS-00011 does, so they take OGS-00011's `overflow-only: skip`
  (owner decision of 2026-10-23): HapMap3 variants that reach only the overflow
  axis stay out of the subset and are counted, rather than refusing the build.
