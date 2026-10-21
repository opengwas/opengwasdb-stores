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
- Run it after a merge to `dev`, from a clean checkout. It prints each
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
| 5 | status `superseded`, `withdrawn` | `placeholder` | `Release Status is <status>: kept as placeholder rows, not listed` |
| 6 | `validated`, Access Posture neither `public` nor `controlled` | `refused` | `Access Posture is <value>; a listed Store is public or controlled` |
| 7 | `validated`, `observed.n_analyses` missing or not an integer | `refused` | `Validation Record has no integer observed.n_analyses` |
| 8 | `validated`, admitted Release Manifest count differs from it | `refused` | `Release Manifest admits <n> Analyses but the Validation Record's observed.n_analyses is <m>` |
| 9 | `validated`, a manifest value its column cannot hold | `refused` | `analyses.tsv row '<analysis_id>': <column> '<value>' is not an integer` (or `a finite number`) |
| 10 | `validated`, otherwise | `listed` | `Release Status is validated, Access Posture is <public or controlled>` |

- A refused Store does not stop the build; the other Stores are decided on
  their own.
- `check()` runs first because `Bundle.trial` raises on a malformed `trial`.
- Rule 5 writes no `analyses` rows yet: placeholder rows, and the
  `superseded_by` they carry, are #324.
- A value is never coerced: a non-numeric cell in a numeric column refuses its
  Store, naming the Analysis and the column.

## Tables

One SQLite file, `catalog.sqlite`, built in one transaction into a fresh file.
The columns and their types are `src/analysis_catalog/columns.py`.

- **`stores`**: one row per **listed** Store Release, with the columns of epic
  #313. A Store that is excluded, placeholder or refused has no row, so nothing
  excluded is discoverable here; `selection` is where it is accounted for.
  Facts come from `release.yaml` (`label`, `description`, `status`,
  `superseded_by`, `access_posture`, `source_snapshot_id`, `accepted_at`,
  `notes`), `build.yaml` (`layout`, `completion_state`) and `validation.yaml`
  (`validated_at`, `build_environment.opengwasdb_version`, and from `observed`:
  `n_analyses`, `n_variants`, `n_associations`, `store_bytes`, `format_version`,
  `reference_assembly`, `association_coverage`). `reference_assembly` and
  `association_coverage` are opengwasdb's values stored as reported, and `NULL`
  while a record lacks them (opengwasdb#294). A count that is not an integer
  refuses the Store.
- **`analyses`**: one row per admitted Release Manifest row of a listed Store
  (ADR 0025: rows not excluded from build), keyed by `id`
  `<store_id>:<analysis_id>`. `status` is the Store Release's Release Status.
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

Counts, the Analysis-type columns, indexes and full-text search are #324, and
the summary report is #325.

## Versions

```text
<artifact-root>/catalog/
  versions/<version>/
    catalog.sqlite
    stores.tsv          the stores table, tab-separated, NULL as an empty cell
    analyses.tsv.gz     the analyses table, the same
    report.md           every decision and reason, and the change since the previous version
  current -> versions/<version>
```

- **The version** is the first 16 hex digits of a SHA-256 over: the builder's
  version and every `*.py` source file of the package, which includes the
  serving policy; and per registered bundle its decision and reason, its
  `release.yaml`, `build.yaml` and `validation.yaml`, and, where the decision
  rests on the Release Manifest (listed, placeholder, refused), its
  `analyses.tsv`. It does not include the build time or the registry commit.
  Editing an excluded Store's manifest changes nothing; changing its decision
  does.
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
  metadata` as counts, then the ids (the first 200 of each, with the columns
  that changed), and each Store Release whose decision changed.
