# Workflow behavior and orchestration test suite

Test suite for Phase A `workflow/Snakefile` orchestration (Issues #115, #116), governed by [ADR 0022](../../docs/adr/0022-flat-opaque-store-ids.md), [ADR 0023](../../docs/adr/0023-the-registry-store-seam-is-a-command-line.md), and [ADR 0024](../../docs/adr/0024-one-family-record-no-source-collection-tier.md).

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

5. **`complete-dense` checkpoint resumption**:
   - The `complete` rule detects existing checkpoint directories (`.<store>.checkpoint`) and automatically selects `complete-dense-resume` in a few visible lines.

6. **Multi-release DAG expansion (Issue #116)**:
   - Snakemake wildcard expansion across `stores/` is the sole multi-release orchestrator; no external batch/loop runner script exists.
   - Several Store Release IDs requested in a single invocation build in correct dependency and lineage order.
   - Requesting only a Reference-Completed child release automatically builds its parent first via the lineage input edge (`child complete` depends on `parent register` record).
   - Requesting a Store Family (e.g. `pixi run release-family <family>` or targeting `<family>`) resolves and builds every release in that family.
   - The `index` target depends only on Release Bundle files (`release.yaml`, `build.yaml`), never on Store artifacts or record files, guaranteeing that refreshing the master list proposes 0 build jobs.

7. **End-to-end execution, idempotency, and resumption**:
   - A fixture-scale Dense store builds and registers end to end with a single snakemake command.
   - Re-running snakemake after success executes 0 jobs (idempotent no-op).
   - Re-running after an interrupted step resumes from the missing step rather than starting from scratch.
   - Deleting a single record file re-runs exactly that step and its downstream dependents.

## Running the suite

```sh
pixi run -e dev python3 tests/workflow/test_workflow.py
# or through the repo orchestrator
pixi run test-python
```
