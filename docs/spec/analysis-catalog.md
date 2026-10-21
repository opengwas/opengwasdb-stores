# Analysis Catalog

The specification of `pixi run catalog` (#323, epic #313). The decisions behind
it are [ADR 0039](../adr/0039-analysis-catalog.md); the words are
[CONTEXT.md](../../CONTEXT.md)'s. This file is the contract: the command, the
serving decisions, the tables, and the files a rebuild writes.

The catalog is the package `src/analysis_catalog/`. It imports only
`ogstores.bundle` and `ogstores.paths` from `ogstores`, and nothing in
`ogstores` or `manifest_generation` imports it
(`tests/packaging/test_boundary.py`). It reads Release Bundles and never opens a
Store (ADR 0023).

## The command

```text
pixi run catalog [--registry stores] [--artifact-root PATH]
```

- `--registry` is the directory of Release Bundles; it defaults to `stores/` of
  the checkout.
- The Artifact Root resolves as `ogstores.paths.artifact_root` does:
  `--artifact-root`, then `OPENGWASDB_ARTIFACT_ROOT`, then `ogstores.yaml`.
- It refuses to build, and writes nothing, when the registry is a git
  repository with uncommitted or untracked changes, because the commit it
  records would then be false. A registry that is not a git repository (a test
  fixture) records a `NULL` commit.
- Run it on the host after a merge to `dev`, from a dedicated clean checkout of
  `origin/dev`, not a shared working checkout (ADR 0039). It prints each
  registered Store Release's decision, the version, and whether it published,
  moved `current`, or was a no-op.

## Decisions

Every registered Store Release (each `OGS-xxxxx` directory of the registry) gets
one decision and a reason, in `selection`. The first rule that applies decides:

| # | Fact | Decision | Reason |
|---|---|---|---|
| 1 | `bundle.check()` returns errors | `refused` | `bundle.check() failed: ` and the errors |
| 2 | `trial: true` | `excluded` | `Trial Store Release` |
| 3 | Access Posture `embargoed` | `excluded` | `Access Posture is embargoed` |
| 4 | status `candidate`, `accepted`, `built` | `excluded` | `Release Status is <status>, not validated` |
| 5 | status `superseded`, `withdrawn` | `placeholder`, after rules 7 to 9 | `Release Status is <status>: kept as placeholder rows, not listed` |
| 6 | `validated`, Access Posture neither `public` nor `controlled` | `refused` | `Access Posture is <value>; a listed Store is public or controlled` |
| 7 | `validated` or a placeholder, `observed.n_analyses` missing or not an integer | `refused` | `Validation Record has no integer observed.n_analyses` |
| 8 | `validated` or a placeholder, admitted Release Manifest count differs from it | `refused` | `Release Manifest admits <n> Analyses but the Validation Record's observed.n_analyses is <m>` |
| 9 | `validated` or a placeholder, a manifest value its column cannot hold | `refused` | `analyses.tsv row '<analysis_id>': <column> '<value>' is not an integer` (or `a finite number`) |
| 10 | `validated`, otherwise | `listed` | `Release Status is validated, Access Posture is <public or controlled>` |

- A refused Store does not stop the build; the other Stores are decided on
  their own.
- `check()` runs first because `Bundle.trial` raises on a malformed `trial`.
- A placeholder Store passes the same count and type checks as a listed one
  (rules 7 to 9), and a failure refuses it, so a refused retired Store is not
  resolvable. A retired Trial Store Release or `embargoed` Store is excluded by
  rules 2 and 3 first, and never becomes a placeholder.
- The one exception is the schema's own absence marker: a literal `NaN` in
  `info_score_threshold` ("unavailable") or `maf_threshold` ("no MAF filter")
  is `NULL` (`docs/release-metadata-schema.md`), distinct from `0`, which stays
  `0.0`. Reading the marker is not a coercion; in any other numeric column
  `NaN` refuses the Store.
- A value is never coerced or rounded: an integer outside SQLite's signed 64
  bits, a `NaN` or infinity, and a finite nonzero real that would become `0.0`
  or `inf` each refuse the Store, naming the Analysis, column and value. A bad
  cell never stops the build.
- A value is never coerced: a non-numeric cell in a numeric column refuses its
  Store, naming the Analysis and the column.

## Tables

One SQLite file, `catalog.sqlite`, built in one transaction into a fresh file.
The columns and their types are `src/analysis_catalog/columns.py`.

- **`stores`**: one row per **listed or placeholder** Store Release, with the
  columns of epic #313. `superseded_by` is the successor's Store id for a
  superseded Store and `NULL` for a withdrawn one (whatever its `release.yaml`
  says). A Store that is excluded or refused has no row, so nothing excluded is
  discoverable here; `selection` is where it is accounted for.
  Facts come from `release.yaml` (`label`, `description`, `status`,
  `superseded_by`, `access_posture`, `source_snapshot_id`, `accepted_at`,
  `notes`), `build.yaml` (`layout`, `completion_state`) and `validation.yaml`
  (`validated_at`, `build_environment.opengwasdb_version`, and from `observed`:
  `n_analyses`, `n_variants`, `n_associations`, `store_bytes`, `format_version`,
  `reference_assembly`, `association_coverage`). `reference_assembly` and
  `association_coverage` are opengwasdb's values stored as reported, and `NULL`
  while a record lacks them (opengwasdb#294). A count that is not an integer
  refuses the Store.
- **`analyses`**: one row per admitted Release Manifest row of a listed or
  placeholder Store (ADR 0025: rows not excluded from build), keyed by `id`
  `<store_id>:<analysis_id>`. `status` is the Store Release's Release Status,
  and `listed` is `1` for an Analysis of a listed Store and `0` for a
  placeholder row. A client resolves any identifier by `id`; the successor is
  `stores.superseded_by`.
  A column a bundle lacks, and an empty cell, are `NULL`, never `0` or `''`.
  `sample_size`, `n_cases`, `n_controls` and `trait_bp` are `INTEGER`;
  `original_sd`, `maf_threshold` and `info_score_threshold` are `REAL`.
  `analysis_label` falls back to `source_label`. Build internals
  (`source_file`, checksums, `inclusion_reason`, `exclude_from_build`,
  `analysis_index`) are not carried.
- **`selection`**: `store_id`, `decision`, `reason`, for every registered Store
  Release.
- **`catalog_meta`**: `key`, `value`: `version`, `built_at`, `registry_commit`
  (`NULL` outside git), `builder_version`, and `serving_policy` (the rules above,
  as a JSON list).

### Summary report (#325)

Each new version carries `summary.html`, rendered from
`docs/reports/analysis-catalog-summary.qmd` after the version's other files are
written and before `current` moves. A render failure fails the rebuild: the
`.partial` version is removed and `current` stays. A no-op rebuild renders
nothing. The report opens `catalog.sqlite` read-only and reads the version's
`changes.tsv`; it recomputes nothing from the bundles. A total over Stores is
`unknown (k of n Stores report it)` whenever any listed Store has a `NULL`, and
is never a partial sum. An empty catalog renders and says so.

Rendering needs Quarto and R beside the builder, so the operator runs
`pixi run -e dev catalog` (the default environment has no reporting feature).
`pixi run -e dev docs-smoke` renders the report against a fixture catalog
(`tests/catalog/smoke_summary.py`).

### Listing, types, counts, search and indexes (#324)

**One way to list.** `listed_analyses` is the view
`SELECT * FROM analyses WHERE listed = 1`. A listing, a keyset page, a region
lookup all read it, and every index below is a partial index
`WHERE listed = 1`, which SQLite uses through the view. A search joins
`analyses` by rowid, because the full-text index holds listed rows only. A
placeholder row is reached only by `id` (or `store_id`) on `analyses`.

**Analysis type and ancestry category** are three columns of `analyses`, derived
once at build (`analysis_catalog.policy.outcome_type`, `trait_class` and
`ancestry_category`):

- `outcome_type`: `binary` when `sample_size_kind` is `case_control` or
  `stored_effect_scale` is `log_or` (the owner's ruling on #324), so
  (`sd`, `case_control`) and (`log_hazard`, `case_control`) are binary;
  `continuous` for (`sd`, `total`); `NULL` otherwise, including a blank and
  `effective` or `variant_level` without a binary scale. Never a guess.
- `trait_class`: `molecular` when any of `context`, `tissue` or `trait_chr` is
  set, `phenotype` otherwise. A Store whose bundle lacks the three columns is
  `phenotype`.
- `ancestry_category`: `assigned_ancestry` when set; `META` when it is empty and
  `source_ancestry_label` is exactly `META`; `NULL` (unassigned) otherwise. The
  Assigned Ancestry vocabulary is unchanged: `META` is the catalog's category,
  never a stored Assigned Ancestry.

**Counts** are the table `counts (dimension, value, n_analyses)`, computed at
build over listed Analyses only. The dimensions are `store`, `ancestry`
(`ancestry_category`), `tissue`, `effect_scale` (`stored_effect_scale`),
`outcome_type`, `trait_class` and `coverage` (the Store's
`association_coverage` as reported, never relabelled). A `NULL` `value` is a
category of its own, and so is `META`; any other value is counted as it
stands. Each dimension sums to the number of listed Analyses.

**Search** is the FTS5 table `analysis_search` over `analysis_label`,
`trait_ontology_label`, `consortium` and `first_author`, external-content on
the view `listed_search_content` (listed rows, exposing the `analyses` rowid),
so `rebuild` and `integrity-check` agree with what is indexed and a placeholder
row is never in it. It joins by rowid:

```sql
SELECT a.* FROM analysis_search s JOIN analyses a ON a.rowid = s.rowid
WHERE analysis_search MATCH ? ORDER BY rank LIMIT ?
```

**Indexes** (all partial, `WHERE listed = 1`): `(id)`; `(col, id)` for each
sortable column `store_id`, `analysis_label`, `trait_ontology_label`,
`assigned_ancestry`, `tissue`, `stored_effect_scale` and `sample_size` (the
columns of the website table and its filters, user stories 2 and 3 of epic
[#313](https://github.com/opengwas/opengwasdb-stores/issues/313)); and
`(trait_chr, trait_bp, id)` for region lookups. `ORDER BY col, id LIMIT n` and
`WHERE (col, id) > (?, ?) ORDER BY col, id LIMIT n` use them. A row-value
comparison is never true for a `NULL` `col`, so a cursor over a nullable
column pages its `NULL` rows first with `col IS NULL AND id > ?`, then the rest
with the row-value form.

## Versions

```text
<artifact-root>/catalog/
  versions/<version>/
    catalog.sqlite
    stores.tsv          the stores table, tab-separated, NULL as an empty cell
    analyses.tsv.gz     the analyses table, the same
    changes.tsv         every added, removed or changed Analysis id (not on a first build)
    report.md           every decision and reason, and the change since the previous version
  current -> versions/<version>
```

- **The version** is the first 16 hex digits of a SHA-256 over: the builder's
  version and every `*.py` source file of the package, which includes the
  serving policy; and every file of every registered Release Bundle directory,
  with each Store's decision and reason. That is every file of every registered
  Release Bundle, plus the builder's source. It does not include the build time
  or the registry commit.
- **A rebuild whose version exists writes nothing**, and moves `current` only
  if it names another version.
- **Publication**: the build is written to `versions/<version>.partial/`,
  renamed to `versions/<version>/`, and `current` is then swapped atomically (a
  temporary symlink, then `os.replace`). A failed build removes its partial
  directory and leaves `current` alone. A `.partial` directory left by a crash
  is replaced by the next build.
- **Retention**: the 5 most recent versions (by `built_at`) are kept; the
  version `current` names is never deleted.
- **`report.md`**: the registered, listed, placeholder, excluded and refused
  counts; a sentence beginning `No Store Release is listed:` when none is; a
  table of every Store Release's decision and reason; and, against the previous
  `current`, `Analyses added`, `Analyses removed` and `Analyses with changed
  metadata` as counts, a sample of 20 ids of each (with the columns that
  changed), and each Store Release whose decision changed. `changes.tsv` holds
  every id with its `change` (`added`, `removed` or `changed`).
