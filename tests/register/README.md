# Registration and validation.yaml test suite

Test suite for `ogstores.register` (Issue #117), governed by [ADR 0022](../../docs/adr/0022-flat-opaque-store-ids.md) and [ADR 0023](../../docs/adr/0023-the-registry-store-seam-is-a-command-line.md).

## Contracts and invariants covered

1. **`validation.yaml` assembly and atomic safety**:
   - `validation.yaml` is written atomically only upon successful registration.
   - Any step failure or preflight error aborts registration and leaves pre-existing `validation.yaml` completely untouched.

2. **Observed measurements**:
   - Harvests observed facts (`format_version`, `n_variants`, `n_analyses`, `n_associations`, `store_bytes`, `build_elapsed_s`, and `validate_status`) directly from step records.

3. **Planned vs executed argv drift check**:
   - Compares executed argv in `records/<step>.json` against planned argv derived from `ogstores.plan.plan(bundle)`.
   - Staging normalization applies (`store.opengwasdb.partial` in executed matches `store.opengwasdb` in planned).
   - Any divergence raises `ArgvDriftError` naming the step, planned argv, and executed argv.

4. **Legitimate complete-dense divergence**:
   - `complete-dense-resume` substituted for planned `complete-dense` is accepted as a valid resumption divergence and recorded as `resumed: true` in `validation.yaml`.

5. **Strict seam compliance (ADR 0023)**:
   - `ogstores.register` opens **no** Store internal files (zarr arrays, store-internal manifests) and spawns **no** subprocesses / re-runs no validation (verified via tripwires).

6. **Atomic publication**:
   - Invokes `ogstores.run.publish_store()` upon successful registration to atomically rename `.partial` to the final Store artifact.

7. **Only this run's findings (#195)**:
   - The verdict, `checks` (`{store: <verdict>}`), `warnings` and `errors` come from this run's `opengwasdb validate --format json` record, exactly. A bundle whose previous `validation.yaml` carries stale checks, a reports pointer and 30 warnings registers with none of them.
   - A validate record without that JSON verdict is refused (`ValidateVerdictError`), leaving `validation.yaml` untouched; text output is never searched for the word "warning".

8. **Phase B acceptance evidence is kept apart (#195)**:
   - A candidate record's `checks`, `warnings` and `reports` appear verbatim under a dated `acceptance` block with the candidate's commit, and never in the run's `status`, `checks`, `warnings` or `errors`.
   - A re-registration passes the block on verbatim; a migrated register-shape record without one gives none.
   - `committed_revision` returns a file's commit only when the file on disk is that commit's.

9. **Forced replacement (#195)**:
   - `register_release(force=True)` over a published Store archives the old Store, the run's records snapshot (as `records/`) and a copy of the old `validation.yaml` into `replaced/<stamp>/`, and records the archive in `validation.yaml` (`replaced.archive`) and `register.json` (`replaced_archive`). Without `force` it raises `StoreExistsError` and changes nothing.

10. **Documented record shape**:
   - The written `validation.yaml` carries exactly the canonical `validator`, `build_environment` and `observed` keys that `tests/validation-record/` asserts against the committed records (issue #135).

## Running the suite

```sh
pixi run -e dev python3 tests/register/test_register.py
# or through the repo orchestrator
pixi run test-python
```
