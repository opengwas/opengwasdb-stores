- **OGS-00007, OGS-00024, OGS-00025 and OGS-00026 now record the regions their bundles retain
  (issue #224, opengwas/opengwasdb#430).** On the owner's approval, a forced `release … --config
  force=1` at `8979754e`, opengwasdb 0.9.0 (`ea81c4a`), rebuilt each one; all exited 0. Each Store now
  carries its bundle's `sidecars/sparse_regions.tsv` as `regions.tsv`, declared in its manifest, and is
  stamped 0.2.1: 4,252, 439 and 507 completion regions for OGS-00024/25/26, and 10 cis plus 10
  significant_trans for OGS-00007. The unchanged bundles built the same counts as before and `validate`
  reports no errors. The replaced Stores and records are archived under `replaced/<UTC>/`.
