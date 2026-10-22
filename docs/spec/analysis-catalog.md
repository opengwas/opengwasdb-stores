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
| 1 | bundle.check() returns errors | `refused` | `bundle.check() failed: ` and the errors |
| 2 | Trial Store Release | `excluded` | `Trial Store Release` |
| 3 | Access Posture is embargoed | `excluded` | `Access Posture is embargoed` |
| 4 | Release Status is candidate, accepted or built | `excluded` | `Release Status is <status>, not validated` |
| 5 | Release Status is superseded or withdrawn, and the Validation Record is not passed or passed_with_warnings | `excluded` | `retired before validation` |
| 6 | Release Status is validated, and Access Posture is neither public nor controlled | `refused` | `Access Posture is <value>; a listed Store is public or controlled` |
| 7 | Release Status is validated, superseded or withdrawn, and observed.n_analyses is missing or not an integer | `refused` | `Validation Record has no integer observed.n_analyses` |
| 8 | the admitted Release Manifest count differs from observed.n_analyses | `refused` | `Release Manifest admits <n> Analyses but the Validation Record's observed.n_analyses is <m>` |
| 9 | a manifest or Validation Record value cannot be stored exactly, or check() tolerates ':' in an analysis_id under #319 | `refused` | `analyses.tsv row '<analysis_id>': <column> '<value>' is not an integer` (or `a finite number`, and so on), or `the Release Manifest carries analysis_id values containing ':' (<n>), tolerated by bundle.check() under #319` |
| 10 | Release Status is superseded or withdrawn | `placeholder` | `Release Status is <status>: kept as placeholder rows, not listed` |
| 11 | Release Status is validated | `listed` | `Release Status is validated, Access Posture is <public or controlled>` |

The Fact and Decision columns are `analysis_catalog.policy.RULES` verbatim and in
order, which `decide` implements,
`catalog_meta.serving_policy` records, `report.md` quotes when nothing is listed,
and `tests/catalog/test_catalog.py` checks this table and ADR 0039's list against.

- A refused Store does not stop the build; the other Stores are decided on
  their own.
- `check()` runs first because `Bundle.trial` raises on a malformed `trial`.
- A placeholder Store passes the same count and type checks as a listed one
  (rules 7 to 9), and a failure refuses it, so a refused retired Store is not
  resolvable. A retired Trial Store Release or `embargoed` Store is excluded by
  rules 2 and 3 first, and never becomes a placeholder.
- A retired release that never passed validation was never served, so it is
  excluded (rule 5), not refused, and has no rows. That is every retired release
  whose Validation Record is missing or not `passed` or `passed_with_warnings`:
  `check()` does not require a record of a retired release, and a candidate or
  accepted one that was retired has null counts. A retired release that did pass
  but has no `observed.n_analyses` is refused by rule 7, as a `validated` one is.
- `check()` reads `superseded_by` only on a `superseded` release (it refuses it
  on any other status), so the catalog reads it through `Bundle.superseded_by` and
  has no rule of its own for a withdrawn one.
- A `n_associations` below `0` refuses the Store, as `register` refuses it.
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

One SQLite file, `catalog.sqlite`, built in one transaction into a fresh file,
one Store at a time: a Store's rows are inserted as its decision is made, and a
Store refused on a value in its rows is rolled back whole. Memory is set by the
largest single Store's manifest.
The columns and their types are `src/analysis_catalog/columns.py`.

- **`stores`**: one row per **listed or placeholder** Store Release, with the
  columns of epic #313. `superseded_by` is the successor's Store id for a
  superseded Store and `NULL` for a withdrawn one (`check()` refuses a
  `superseded_by` on any other status). A Store that is excluded or refused has no row, so nothing excluded is
  discoverable here; `selection` is where it is accounted for.
  Facts come from `release.yaml` (`label`, `description`, `status`,
  `superseded_by`, `access_posture`, `source_snapshot_id`, `accepted_at`,
  `notes`, and `association_coverage`), `build.yaml` (`layout`,
  `completion_state`) and `validation.yaml` (`validated_at`,
  `build_environment.opengwasdb_version`, and from `observed`: `n_analyses`,
  `n_variants`, `n_associations`, `store_bytes`, `format_version`,
  `reference_assembly`). `reference_assembly` is opengwasdb's value stored as
  reported, and `NULL` while a record lacks it (opengwasdb#294).
  `association_coverage` is the release's own declaration, in the one vocabulary
  of ADR 0040 (`gwas`, `cis`, `cis_and_signals`, `signals_only`), never the
  Validation Record's `observed` value: opengwasdb#298 makes coverage a build
  input, so the bundle states it. It is never `NULL` for a registered bundle,
  because `bundle.check()` requires it. A count that is not an integer refuses
  the Store.
- **`analyses`**: one row per admitted Release Manifest row of a listed or
  placeholder Store (ADR 0025: rows not excluded from build), keyed by `id`
  `<store_id>:<analysis_id>`. `status` is the Store Release's Release Status,
  and `listed` is `1` for an Analysis of a listed Store and `0` for a
  placeholder row. A client resolves any identifier by `id`; the successor is
  `stores.superseded_by`.
  A column a bundle lacks, and an empty cell, are `NULL`, never `0` or `''`.
  `sample_size`, `n_cases`, `n_controls` and `trait_bp` are `INTEGER`;
  `original_sd`, `maf_threshold` and `info_score_threshold` are `REAL`.
  `analysis_label` falls back to `source_label`, and `label_from_source` is `1`
  on a row where it did (the manifest had no `analysis_label`), `0` otherwise.
  Build internals
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
`pixi run catalog`, which runs in the `dev` environment (the task's
`default-environment`) and uses that environment's own Quarto, refusing before it
builds anything when none is active. `pixi run -e dev catalog-summary-smoke` renders the report against a fixture catalog
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
  `stored_effect_scale` is `log_or` (#324), so
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
`outcome_type`, `trait_class` and `coverage` (the Store Release's declared
`association_coverage`, never relabelled). A `NULL` `value` is a
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
`assigned_ancestry`, `tissue`, `stored_effect_scale`, `sample_size` and the
derived `ancestry_category`, `outcome_type` and `trait_class` (the columns of the
website table and its filters, user stories 2 and 3 of epic
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

- **The version** is the first 16 hex digits of a SHA-256 over exactly these
  inputs, which are everything a decision depends on:
  - the builder's version and the serving policy;
  - the sorted list of registered store ids, so an empty directory changes it;
  - every `*.py` source file of `analysis_catalog` and of `ogstores` (`check()`,
    `Bundle`, `partition_membership`, `LEGACY_ANALYSIS_ID_RELEASES`);
  - the installed opengwasdb, whose reader parses the manifest: its distribution
    version and, when the install records one (`direct_url.json`), its commit;
  - the summary template (`docs/reports/analysis-catalog-summary.qmd`);
  - every file of every registered Release Bundle directory, read in fixed-size
    chunks.

  Decisions are not hashed: they are a function of those inputs, so the version
  is known before any Release Manifest row is read. It does not include the build
  time or the registry commit.
- **A rebuild whose version exists writes nothing**, reads no Release Manifest
  row, and moves `current` only if it names another version. Its decisions are
  those in that version's `selection`.
- **Publication**: the build is written to `versions/<version>.partial/`,
  renamed to `versions/<version>/`, and `current` is then swapped atomically (a
  temporary symlink, then `os.replace`). A failed build removes its partial
  directory and leaves `current` alone. A `.partial` directory left by a crash
  is replaced by the next build. The database is written with `synchronous =
  OFF`, so before the rename every file of the `.partial` directory and the
  directory itself are fsynced; after it `versions/` is, and after the `current`
  swap `catalog/` is. A host crash cannot leave a torn version, which is
  immutable and so would never be rebuilt.
- **Retention**: the 5 most recent versions (by `built_at`) are kept; the
  version `current` names is never deleted.
- **`report.md`**: the registered, listed, placeholder, excluded and refused
  counts; a sentence beginning `No Store Release is listed:` when none is; a
  table of every Store Release's decision and reason; and, against the previous
  `current`, `Analyses added`, `Analyses removed` and `Analyses with changed
  metadata` as counts, a sample of 20 ids of each (with the columns that
  changed), and each Store Release whose decision changed. `changes.tsv` holds
  every id with its `change` (`added`, `removed` or `changed`).
