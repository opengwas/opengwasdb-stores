# Workflow behavior and orchestration test suite

Test suite for Phase A `workflow/Snakefile` orchestration (Issues #115, #116), governed by [ADR 0022](../../docs/adr/0022-flat-opaque-store-ids.md), [ADR 0023](../../docs/adr/0023-the-registry-store-seam-is-a-command-line.md), and [ADR 0028](../../docs/adr/0028-store-family-tier-retired.md).

## Contracts and invariants covered

1. **Dependency wiring only (ADR 0023)**:
   - `workflow/Snakefile` contains **no** Store Family name literals, **no** source column name literals, **no** manifest translations, and **no** layout branches.
   - Each rule asks `ogstores.plan.plan` for a `Step` and hands it to `ogstores.run.run_step`.

2. **The rule chain**:
   ```text
   build (or complete) ──> top_hits ──> rho ──> overview ──> validate ──> register
   ```
   `top_hits` and `rho` steps are conditional on `build.yaml`'s `post` configuration. A Reference-Completed release substitutes `complete` for `build` and follows the same tail.

3. **Tracked outputs are record files**:
   - Each rule's tracked output is its execution record file (`records/<step>.json` via `ogstores.paths.record_path`), **never** the Store directory.

4. **Terminal register step**:
   - The terminal `register` rule completes the DAG for the release and writes `records/register.json`.
   - Safely finalizes the staged transaction by atomically publishing `store.opengwasdb.partial` to `store.opengwasdb`.

5. **Multi-release DAG expansion (Issue #116)**:
   - Snakemake wildcard expansion across `stores/` is the sole multi-release orchestrator; no external batch/loop runner script exists.
   - Several Store Release IDs requested in a single invocation build in correct dependency and lineage order.
   - Requesting only a Reference-Completed child release automatically builds its parent first via the lineage input edge (`child complete` depends on `parent register` record).
   - The index target depends only on Release Bundle files (`release.yaml`, `build.yaml`), never on Store artifacts or record files, guaranteeing that refreshing the master list proposes 0 build jobs.

6. **End-to-end execution, idempotency, and resumption**:
   - A fixture-scale Dense store builds and registers end to end with a single snakemake command.
   - Re-running snakemake after success executes 0 jobs (idempotent no-op).
   - Re-running after an interrupted step resumes from the missing step rather than starting from scratch.
   - Deleting a single record file re-runs exactly that step and its downstream dependents.

7. **A published release is not rebuilt by accident (#195)**:
   - A run that would rebuild a release whose final Store exists fails before any job starts. It names the release and its Store, and leaves every file of that release, and its `validation.yaml`, byte-for-byte and mtime-for-mtime unchanged, with no `.partial` staged.
   - `--config force=1` replaces a named published release. The old Store, its records and its `validation.yaml` land unchanged in `replaced/<UTC>/`, the new Store is published, and the new record names the archive.
   - A forced run whose build fails restores `records/` exactly, leaving the Store and `validation.yaml` untouched and no snapshot or archive behind.
   - A leftover `records.before-force-<UTC>/` makes both an unforced and a forced run refuse. The error names the snapshot and how to restore or delete it.
   - Through the entry point (`workflow/release.py`): an unforced rebuild of a published release is refused in preflight, and `--no-hooks`, `--touch`, `--forceall`, `--nolock`, `--ignore-incomplete` and `--snakefile` are refused before any write, forced or not. An operator cannot set `release_run`.
   - `--resolve-snapshot` misuse (no snapshot, a bad action, `all`, `--dry-run`) is refused without writing.
   - Only the register job killed mid-publication: the same run finishes it.

8. **A whole-stack crash at every boundary (#195; `test_release_faults.py`, its own suite `release-faults`)**:
   - For each of the 13 boundaries (`release.ENTRY_POINT_BOUNDARIES` plus `register.PUBLICATION_BOUNDARIES`, `marker-removed` included), a forced entry-point run is SIGKILLed with its whole process group.
   - From the marker on, the next run completes the publication, clears Snakemake's incomplete mark and finds nothing to do. Before it, the next run refuses. `--resolve-snapshot delete` (before any job) or `restore` (after), plus Snakemake's `--unlock`, then let a forced run replace the release.
   - Every case ends with the old Store, records and record archived byte-for-byte, nothing pending, and a further run with nothing to do. A boundary added to the code without a case here fails the suite.

9. **Live and interrupted runs (#195, review round 3; also in `release-faults`)**:
   - A forced run is paused once its build starts. A second run, a forced one, `--resolve-snapshot restore` and `delete` are each refused with "a run of OGS-00099 is in progress (pid <the paused run>)", never the leftover-snapshot message. A dry run reports the holder, the live snapshot is untouched, and the paused run then completes and archives the old release correctly.
   - Recovery from another run never reads or completes a marker under a live run's lock.
   - A dry run during a live run's publication says the run is in progress, not that it will complete an interrupted publication.
   - Ctrl-C to the foreground group, SIGTERM to the entry point, and SIGTERM to every process of the run, each mid-build, exit 130 or 143. The records are restored byte for byte (bytes and mtimes), with no snapshot, marker or archive left. The next unforced run meets only the ordinary "already published" refusal, with no `IncompleteFilesException` and no leftover message, and a forced run then replaces the release cleanly.

8. **Operator config reaches Snakemake (#195)**:
   - `pixi run release <ID> --config key=value` and `release-dry` pass the override to Snakemake's config rather than as a target. Each task's `pixi.toml` cmd is run with the operator's words appended, as Pixi does, and the dry run must plan under the configured artifact root.

## Running the suite

```sh
pixi run -e dev python3 tests/workflow/test_workflow.py
# or through the repo orchestrator
pixi run test-python
```
