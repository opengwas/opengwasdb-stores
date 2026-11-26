- **The reference-completion LD panels are in `ld-resources.tsv` (issue #540, epic #496).**
  Six rows, `OGL-00013` to `OGL-00018`, are read from the four `kind: ld_panel`
  `resource.yaml` files by `ld_registry.panels`: one row per ancestry, with the
  id its new `ld_ids` map gives it, and the `n_variants`, `n_blocks` and `bytes`
  measured on the host by `tools/ld-panel/measure_panel.py` (AFR 14,136,388
  variants in 1,580 blocks; AMR 12,669,255 in 1,357). The UKB EUR panel's
  variants and bytes stay empty. A missing `ld_ids` entry is refused, naming the
  file and the ancestry, and the index refuses a panel id that is a record's.
