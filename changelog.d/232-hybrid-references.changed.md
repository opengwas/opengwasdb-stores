- **The Hybrid bundles point at the regenerated, reference-first gnomAD v4.1
  axes (epic #232).** On the owner's decision of 2026-11-17 the maintained
  producer wrote a new generation to
  `/data/opengwasdb/reference/gnomad-v4-common-hg38-v2` (3,196 s at 12 jobs,
  revision `5760e78b`). Every list starts with the engine's Variant Reference
  marker v1, and a deletion is no longer folded into the insertion at the same
  site, so each ancestry's count rises by exactly its indel rows: EUR
  13,415,936 → 13,718,605, EAS 10,718,294 → 10,912,248, AFR 20,149,213 →
  20,600,947.
  - **Why.** opengwasdb 0.10.0 refuses an unmarked Variant Reference that
    contains an indel (the owner's ruling of 2026-11-16 on
    opengwas/opengwasdb#381), which is how the forced rebuilds of `OGS-00004`
    and `OGS-00005` were refused.
  - **The bundles.** `OGS-00004` and `OGS-00005` take the new EUR list,
    amending each accepted trial bundle's Build Recipe by owner decision for
    the forced rebuild that follows; each `release.yaml` notes records the
    amendment and what moved. `OGS-00032` takes its EAS list and `OGS-00033`
    its AFR list. `OGS-00011` is deliberately unchanged: its published Store
    was built from the old file, which the resource declaration keeps as the
    prior generation for #478 to repoint when it rebuilds.
  - **Generators and resource metadata.** The EUR pilot configs,
    their `generation-*.yaml` provenance, and the EAS and AFR full release
    configs name the new directory, so a re-emit agrees with its bundle. The
    resource declaration's `location` is the regenerated directory, with its
    marker and counts, and `/data/opengwasdb/reference/alid-panel/EUR-variants.tsv.gz`
    and `/data/opengwasdb/wip/hybrid-ieua/panel_alids.txt` are recorded as
    legacy, unmarked, and named by no bundle.
