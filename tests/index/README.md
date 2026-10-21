# Master list index test suite

Test suite for `ogstores.index` (Issue #118), governed by [ADR 0022](../../docs/adr/0022-flat-opaque-store-ids.md), [ADR 0023](../../docs/adr/0023-the-registry-store-seam-is-a-command-line.md), and [ADR 0028](../../docs/adr/0028-store-family-tier-retired.md).

## Contracts and invariants covered

1. **Canonical Columns in `stores.tsv`**:
   - Release and command fields remain derived from bundle YAML and `plan()`.
   - `n_analyses` plus the descriptive summary fields are derived from `analyses.tsv`.
   - `n_analyses` is admitted membership; `n_manifest` and `n_excluded` keep the excluded manifest rows visible rather than counting them as built Analyses (issue #217).
   - Store measurements remain observed values from `validation.yaml`.
   - `completion_state` is the state of the Store that was published: `observed.completion_state` from the record when it carries it, else the bundle's declared value. Records written before that field keep today's rows byte for byte, so two-id and observed-only releases are unchanged; a release that completes its own Store reports `observed_only` instead, because its earlier publication can only have been that Store (issue #307, ADR 0037).
   - A record whose state contradicts its bundle otherwise — an Observed-Only Store under a two-id child's id — is refused rather than published, and `bundle.is_completion_pending` is the one predicate the pending marker and readiness's interim rule share (issue #307).

2. **Derived `build_command`**:
   - `build_command` is rendered dynamically from the build phase's first `plan()` step, never stored or hand-maintained. It is `steps[0]` for every bundle whose plan is one phase; a single-id release therefore names the command that builds its Store, not the one that completes it (issue #307).
   - The artifact root those paths hang from is resolved from configuration (`paths.artifact_root()`), never from a Build Recipe (issue #126).

3. **Observed measurements from git metadata**:
   - Observed Store-measurement columns are extracted exclusively from the register-written `observed` block in `validation.yaml`, never scraped from disk or artifact roots, and never from a legacy top-level key (issue #135). Unbuilt or candidate releases have empty/null observed fields.
   - `n_analyses` is instead derived from the membership table, so a stale observed count cannot override the bundle.
   - `validate_status` is the Validation Record's own top-level `status`, never a per-check `checks.store` entry and never `observed.validate_status`; a record that failed overall reports `failed` even when an individual check passed (issue #124). A missing Validation Record reports an empty verdict.

4. **Human-readable `STORES.md`**:
   - Generates a clean Markdown summary table representation of the master list.
   - A row whose published state is `observed_only` while its bundle declares `reference_completed` reads `observed_only (completion pending)`, so a completion on its way is visible. `stores.tsv` keeps the bare state, machine-readable (issue #307).

5. **`by-label/` Symlink trees**:
   - `stores/by-label/<label>` $\rightarrow$ `../<store_id>` relative symlinks in git.
   - `<artifact_root>/by-label/<label>` $\rightarrow$ `../<store_id>` filesystem symlinks when `artifact_root` is present.

6. **Subsumption of legacy documentation**:
   - `docs/store-catalog.md` is deleted and subsumed by the generated views.

7. **Strict seam compliance (ADR 0023)**:
   - Index reads git only and never opens, inspects, or reads files inside artifact roots or Store directories (proven via tripwires).

8. **CI Clean Tree Verification**:
   - Re-running index on the current repository matches committed `stores.tsv`, `STORES.md`, and every bundle's `summary.yaml` with no drift.

## Running the suite

```sh
pixi run -e dev python3 tests/index/test_index.py
# or through the repo orchestrator
pixi run test-python
```
