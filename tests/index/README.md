# Master list index test suite

Test suite for `ogstores.index` (Issue #118), governed by [ADR 0022](../../docs/adr/0022-flat-opaque-store-ids.md), [ADR 0023](../../docs/adr/0023-the-registry-store-seam-is-a-command-line.md), and [ADR 0024](../../docs/adr/0024-one-family-record-no-source-collection-tier.md).

## Contracts and invariants covered

1. **19 Canonical Columns in `stores.tsv`**:
   - **Derived (12)**: `store_id`, `label`, `family`, `layout`, `completion_state`, `status`, `derived_from`, `store_uri`, `created_at`, `opengwasdb_rev`, `generator_command`, `build_command`.
   - **Observed (7)**: `format_version`, `n_analyses`, `n_variants`, `n_associations`, `store_bytes`, `build_elapsed_s`, `validate_status`.

2. **Derived `build_command`**:
   - `build_command` is rendered dynamically from `ogstores.plan.plan(bundle)[0].argv`, never stored or hand-maintained.

3. **Observed measurements from git metadata**:
   - Observed columns are extracted exclusively from `validation.yaml` in git, never scraped from disk or artifact roots. Unbuilt or candidate releases have empty/null observed fields.
   - `validate_status` is the Validation Record's own top-level `status`, never a per-check `checks.store` entry and never `observed.validate_status`; a record that failed overall reports `failed` even when an individual check passed (issue #124). A missing Validation Record reports an empty verdict.

4. **Human-readable `STORES.md`**:
   - Generates a clean Markdown summary table representation of the master list.

5. **`by-label/` Symlink trees**:
   - `stores/by-label/<label>` $\rightarrow$ `../<store_id>` relative symlinks in git.
   - `<artifact_root>/by-label/<label>` $\rightarrow$ `../<store_id>` filesystem symlinks when `artifact_root` is present.

6. **Subsumption of legacy documentation**:
   - `docs/store-catalog.md` is deleted and subsumed by the generated views.

7. **Strict seam compliance (ADR 0023)**:
   - Index reads git only and never opens, inspects, or reads files inside artifact roots or Store directories (proven via tripwires).

8. **CI Clean Tree Verification**:
   - Re-running index on the current repository matches committed `stores.tsv` and `STORES.md` with no drift.

## Running the suite

```sh
pixi run -e dev python3 tests/index/test_index.py
# or through the repo orchestrator
pixi run test-python
```
