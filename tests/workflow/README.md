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
   `top_hits` and `rho` steps are conditional on `build.yaml`'s `post` configuration. A two-id Reference-Completed child substitutes `complete` for `build` and follows the same tail. A release that completes its own Store (ADR 0037) follows its `register` with a completion phase:
   ```text
   register ──> complete ──> completion-rho ──> completion-overview ──> completion-validate ──> completion-register
   ```

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

8. **A whole-stack crash at each kind of boundary (#195; `test_release_faults.py`, suite `release-faults/boundaries`)**:
   - At every entry-point boundary (`release.ENTRY_POINT_BOUNDARIES`, `marker-removed` included), and at one register-job boundary of each kind Snakemake can tell apart (`publication-started`: no marker; `store-published`: marker, no `records/register.json`; `register-record-written`: both), a forced entry-point run is SIGKILLed with its whole process group. Each case is a whole release, so the register job's other boundaries are left to `tests/register`, which kills the publication transaction at every one of them.
   - From the marker on, the next run completes the publication, clears Snakemake's incomplete mark and finds nothing to do. Before it, the next run refuses. `--resolve-snapshot delete` (before any job) or `restore` (after), plus Snakemake's `--unlock`, then let a forced run replace the release.
   - Every case ends with the old Store, records and record archived byte-for-byte, nothing pending, and a further run with nothing to do. An entry-point boundary added to the code without a case here fails the suite.

9. **Live and interrupted runs (#195, review round 3; `test_release_faults.py`, suite `release-faults/interrupts`)**:
   - A forced run is paused once its build starts. A second run, a forced one, `--resolve-snapshot restore` and `delete` are each refused with "a run of OGS-00099 is in progress (pid <the paused run>)", never the leftover-snapshot message. A dry run reports the holder, the live snapshot is untouched, and the paused run then completes and archives the old release correctly.
   - Recovery from another run never reads or completes a marker under a live run's lock.
   - A dry run during a live run's publication says the run is in progress, not that it will complete an interrupted publication.
   - Ctrl-C to the foreground group, SIGTERM to the entry point, and SIGTERM to every process of the run, each mid-build, exit 130 or 143. The records are restored byte for byte (bytes and mtimes), with no snapshot, marker or archive left. The next unforced run meets only the ordinary "already published" refusal, with no `IncompleteFilesException` and no leftover message. After SIGTERM to every process, the delivery that reaches Snakemake itself, a forced run then replaces the release cleanly.

8. **Operator config reaches Snakemake (#195)**:
   - `pixi run release <ID> --config key=value` and `release-dry` pass the override to Snakemake's config rather than as a target. Each task's `pixi.toml` cmd is run with the operator's words appended, as Pixi does, and the dry run must plan under the configured artifact root.

10. **Only buildable bundles are built (#247)**:
   - Every fixture bundle is `accepted` unless a test says otherwise.
   - Running Snakemake directly on a candidate is refused at `onstart`, naming its Release Status and how to accept it, with nothing written under the artifact root.
   - Snakemake's `all` target expands to buildable bundles only.
   - Through the entry point, a named candidate is refused before any lock is taken or anything is written, and the candidate parent an accepted child would schedule is refused before any build work. An unregistered id still gets the "Unknown Store Release ID" guidance.
   - `pixi run release-dry all` lists each left-out bundle with its refusal, and a dry run of a named candidate says the real run would refuse.

11. **Reference completion end to end, under two ids (#303)**:
   - One entry-point run naming only the Reference-Completed child builds and publishes its Observed-Only parent, then completes, validates and publishes the child under its own id. Each id's register record reports success, each Store sits at its id's published path with no `.partial` left behind, and `opengwasdb validate` exits 0 on both.
   - The child is a real completion, not an empty one: its manifest reports `completion_state: reference_completed` with its own `release_id`, the position the fixture VCF holds out but the fixture LD panel covers comes back `imputed`, and `observed_only=True` returns the parent's observed associations with identical z, se and eaf by ALID — compared exactly, not within a tolerance.
   - Re-running the entry point over the published pair executes 0 jobs, prints its "nothing to be done" line and leaves every record's mtime unchanged. That holds from a copy of the artifact root too, which stands in for another checkout with no shared Snakemake provenance: finishing a deferred publication rewrites neither `validation.yaml` nor `records/register.json` when they already hold the marker's bytes, so no consumer's record goes stale.
   - The pair is built at most once per process (`two_id_completion_template`), and the fixture is the signal profile later completion tickets reuse: `create_signal_fixture_vcf`, `create_signal_ld_panel` and `create_completed_child_bundle` in `test_workflow.py`.

12. **Reference completion end to end, under one id (#304, ADR 0037)**:
   - One Release Bundle declares the build and the completion of a Dense Store, with no `derived_from`. One fresh entry-point run builds and publishes the Observed-Only Store, completes it from that published Store into the id's staged path, validates it there, and publishes the completed Store at the same path, all without `--config force=1`. Both phases' records report success: `build`, `validate`, `register`, then `complete`, `completion-validate`, `completion-register`.
   - `complete` read the published `store.opengwasdb` and wrote `store.opengwasdb.partial`. The completed manifest keeps `store_id` = the id and takes `release_id` = `<id>-completed`. That the fixture imputes its held-out position and decodes to the VCF's values is contract 11's to prove, on the same commands.
   - The replaced Observed-Only Store is archived in the one `replaced/<stamp>/`, beside a copy of its `records/register.json` and its Validation Record, and it holds exactly the VCF's observed positions. `observed_only=True` on the completed Store returns its associations with identical z, se and eaf by ALID. The build phase's records stay in `records/`, and `validation.yaml` describes the completed Store and names the archive.
   - A rerun executes 0 jobs and rewrites no record, both in place, where Snakemake reads the build's own metadata, and from a copy of the artifact root, where it decides by mtime.
   - The release is built at most once per process (`single_id_completion_template`), from #303's signal fixture and `amend_to_complete_own_store`, the shape later completion tickets reuse.

## Running the suite

```sh
pixi run -e dev python3 tests/workflow/test_workflow.py
# or through the repo orchestrator
pixi run test-python
pixi run test --suite workflow --suite release-faults/boundaries --suite release-faults/interrupts
```

These three suites each build fixture Stores through the whole release stack,
so they run one at a time, with the machine to themselves. CI runs each on a
runner of its own.
