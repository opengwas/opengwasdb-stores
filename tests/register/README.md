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
   - `validate_verdict` accepts exactly the pinned CLI's object: no other output or keys, a boolean `ok`, string lists, `ok == (not errors)`, an int `exit_code` and bool `success` (`false`/`true`/`0.0`/`1.0` exit codes are refused), and an exit code and success that agree with `ok`. The full matrix is covered: passing, warning, failing, 14 malformed outputs, 10 wrongly typed record fields and 6 contradictions. Any other record raises `ValidateVerdictError`, leaving `validation.yaml` untouched; text output is never searched for the word "warning".

8. **Phase B acceptance evidence is kept apart (#195)**:
   - A candidate record's `checks`, `warnings`, `reports` and `reference_overlap` appear verbatim under a dated `acceptance` block with the candidate's commit, and never in the run's `status`, `checks`, `warnings` or `errors`.
   - Any kind of generator evidence survives, without `register` naming it (#250): a made-up block and a dbGaP candidate's own `acceptance` block are carried, and the generator-run fields (`status`, `validated_at`, `validator`, `build_environment`, `observed`, `errors`) are not. A candidate block named `recorded_at` or `commit` is refused.
   - A re-registration passes the block on verbatim; a migrated register-shape record without one gives none.
   - `committed_revision` returns a file's commit only when the file on disk is that commit's.

9. **Forced replacement (#195)**:
   - `register_release(force=True)` over a published Store archives the old Store, the run's records snapshot (as `records/`) and a copy of the old `validation.yaml` into `replaced/<stamp>/`, and records the archive in `validation.yaml` (`replaced.archive`) and `register.json` (`replaced_archive`). Without `force` it raises `StoreExistsError` and changes nothing.

10. **Publication is a recoverable transaction (#195)**:
    - The run is killed (a subprocess exiting as SIGKILL would) at every boundary after `publication.json` is written. `complete_publication` then finishes it: the new Store is published, the old Store, records and Validation Record are archived, `validation.yaml` and `register.json` are written, and nothing is left pending. An exception at each boundary is finished the same way.
    - Two kinds of replacement are killed, each at every boundary it crosses: a forced run's, which archives the old Store (`old-store-archived`), and a completion phase's, which deletes it (`old-store-deleting`, part-way through, and `old-store-deleted`) (#309). A completion phase killed part-way is shown to have left a half-deleted tree, and the run after the one that finishes finds nothing to do. A boundary that no kind is killed at fails the suite. This suite is where every boundary is covered: `release-faults/boundaries` crashes the whole stack at one boundary of each kind Snakemake sees, and at `old-store-deleting` once, because each of its cases costs a release.
    - With `finalize=False` (the entry point's register job), the marker is kept after the last step, for the entry point to remove.
    - A kill before the marker leaves the old Store and record in place, and its snapshot refuses the next run. A killed first publication is completed too. An impossible state raises `PublicationError` and keeps the marker, and `register` refuses while a publication is pending.

11. **A release that completes its own Store (ADR 0037, #304, #309)**:
    - The completion phase replaces the Store its build phase published without `force` and deletes it. `replaced/<stamp>/` keeps copies of the replaced `records/register.json` and of the passing `records/completion-observed-view-check.json`, and the replaced Validation Record, and no Store.
    - It registers only over a passing check: a verdict that says `passed: false`, an unreadable one (empty, text, truncated, a list, `passed` a string, null or absent), a doubled stdout whose first verdict failed, and a missing record are each refused before the marker, with every file of the release unchanged.
    - A forced run's completion phase archives the Store instead. `forced_completion` reads it from the build phase's register record, and refuses one whose `replaced_archive` is neither a path nor null, or that exists but is malformed JSON or not an object, deleting nothing. A record with no `replaced_archive`, written before #195 as every published Store's is, means unforced: the completion phase registers over build-phase records with exactly OGS-00009's key sets (a `validate` without `--format json` that printed `valid`), deletes the replaced Store and archives its records. A marker written before #309, with no `replaced_store`, still archives.

12. **Documented record shape**:
   - The written `validation.yaml` carries exactly the canonical `validator`, `build_environment` and `observed` keys that `tests/validation-record/` asserts against the committed records (issue #135).

## Running the suite

```sh
pixi run -e dev python3 tests/register/test_register.py
# or through the repo orchestrator
pixi run test-python
```
