The Analysis Catalog gains an `ld_resources` table (#537, epic #496): exactly the rows of `ld-resources.tsv`, one per LD Store Registry record and one per ancestry of each LD panel declared under `resources/reference-resources/`, taken from the one list the index writes (`ld_registry.index.resource_rows`). Columns are the registry's, with whole numbers as INTEGER, `t_far` and `t_partner` as REAL, and NULL for a value a row does not hold, such as the UKB panel's unknown counts. A record error, a bad panel declaration, or an id both claim refuses the build, naming the file.

The catalog version now hashes `stores/ld/`, the `ld_panel` declarations the panel rows come from, and `src/ld_registry/*.py`, so a change to any of them gives a new version. `BUILDER_VERSION` is unchanged, because every change is visible in the hashed source.

A record may declare `default: true` (person-written, only on a `served` record, at most one per ancestry; the index refuses two). `ld-resources.tsv` and the table gain a `default` column, 1 for the declared default and NULL otherwise, which SQL must quote as `"default"`. No record declares one yet.

A panel declaration's counts (`n_variants`, `n_blocks`, `bytes`, `n_individuals`) must now be whole numbers; text such as `about 1580` is refused, naming the file, field and ancestry.
