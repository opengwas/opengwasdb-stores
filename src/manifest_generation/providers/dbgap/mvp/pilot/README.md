# Bounded MVP metric-resolution pilot (#221)

Fixture-first code for the reviewed metric-resolution plan
(`docs/mvp-metric-resolution-plan.md`). It builds the 15-Analysis pilot
selection and manifest from metadata only, then can run the **public**
`opengwasdb resolve-analyses` CLI once under enforced resource bounds.

This code is **not** a live-pilot instruction. `run` refuses to execute without
`--execute`, the approved immutable metadata SHA and a parent start receipt.

## Interfaces

```sh
# DRY PLAN (default): metadata only, no resolver, no body open.
python3 -m manifest_generation.providers.dbgap.mvp.pilot.cli plan \
  --metadata-root /data/opengwasdb/work/epic232/220/metadata-refresh-parent-baf141c-20261013T123913Z \
  --mapping /data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z/mapping.tsv \
  --effect-provenance <owner/source effect-scale provenance.tsv> \
  --out-root /data/opengwasdb/work/epic232/221/metric-pilot-staging
```

`run` additionally needs `--execute`, `--approved-metadata-sha`, `--start-receipt`
and the pinned `--core-commit 4e922566...`; the parent start receipt binds owner,
scope, code head, core commit, executable, the ids, reference paths, caps and
start identity. A `resolution-receipt.json` records the outcome, records
verification, guard counts and resource actuals.

`--scope` selects the row set: `pilot` (the reviewed 15, default),
`bucket:<EUR|AFR|AMR|EAS|META>` (every admitted mapping row in that bucket) or
`all` (6,022). The resolver manifest contains only rows with resolved effect
provenance; pending/unknown rows are excluded and counted in the receipt. The
guard read allowlist is built from exactly the selected, resolved source files.
Resource caps are unchanged (2 workers, ≤4 CPU, 200000 rows, 50000 ancestry
sites, 20000 evidence).

## What each module owns

- `selection.py` — metadata-only build of the reviewed 15 rows and the resolver
  manifest. Effect scale comes from an explicit effect-provenance row
  (`--effect-provenance`: `stored_effect_scale`, `original_effect_scale`,
  `original_sd_method`, `effect_scale_provenance`) or, absent that, the dbGaP
  member's own authoritative `dbgap_trait_type` plus the exact
  `inv-norm transformed` phrase: `binary` -> `log_or`/`binary_trait` (no SD),
  `quantitative` + `inv-norm transformed` -> `sd`/`declared_standardised`
  (SD = 1). A resolved row always emits a non-empty `original_sd_method`
  (the core would otherwise default it to `estimated_from_source_maf`);
  anything unknown stays pending and is excluded from the resolver manifest.
  META is canonicalised to the empty super-population; the core's inferred
  ancestry is not rewritten.
- `wrapper.py` — fresh-root preflight (refuses reuse, symlink ancestors,
  `stores/` components and protected roots before any `mkdir`); limit validation
  (finite, positive, within 7200 s / 65536 MB / 2 workers) before any write; the
  wrapper becomes a **child subreaper** and holds the resolver at a **startup
  barrier** until the monitor is running, so nothing loads before accounting;
  stdio goes to files (never a pipe, so a detached grandchild cannot suspend the
  wait); on wall-timeout/RSS breach the whole session group and any reparented
  descendants are SIGKILLed and reaped, and an outcome is `ok` only when nothing
  escaped. CPU affinity uses the process's **actual allowed** set and a failure
  is fatal; BLAS/OpenMP=1 is pinned after any caller env. Accounting takes a
  single host-wide `/proc` snapshot per poll tick (`ProcSnapshot`), reused for
  group, transitive/reparented-descendant, starttime and RSS membership, so a
  tick is linear in the host process count rather than `host_pids * members`.
- `guard/sitecustomize.py` — a read/write audit hook injected via `PYTHONPATH`
  (no env install): library/interpreter reads, the allowlisted reference panels
  and 15 source bodies, and writes only inside the fresh root are allowed; any
  other source/data read or out-of-root mutation raises and is counted.
- `cli.py` — DRY PLAN default; `run` requires `--execute`, the approved
  immutable metadata SHA, a fully-bound parent start receipt (owner, scope, code
  head, core commit, executable, ids, references, caps, start identity) and the
  pinned core commit `4e922566`. It verifies the frozen receipt/content sha,
  runs the resolver once, then requires every selected record + `index.json`
  with a known status, an unchanged metadata content-hash and zero guard
  denials before writing `COMMITTED`.

## Resource accounting

- Limits: 2 workers, ≤4 CPU, BLAS/OpenMP 1, 7200 s whole-pool wall, 65536 MB
  aggregate process-tree RSS.
- RSS is the sum of `VmRSS` over the process group tree; shared pages are
  double-counted, so it is a documented **ceiling**, not an exact resident
  figure. It is recorded in the receipt alongside wall time and any escaped
  pids.

## Read scope

The pilot is authorised to load the ancestry reference (494,105,972 B), the
`ancestry_groups.tsv` map (385 B), the tracked `qc_panel.tsv` (420,582 B) and the
15 named association bodies, one bounded pass each. There is no source-body
hashing (the resolver inherits the recorded SHA/stat) and no separate SD
re-scan. The wrapper never opens a source body itself; only the public resolver
does.

## Outputs and promotion

A fresh output root holds `selection/selection-evidence.tsv`,
`selection/pilot-analyses.tsv`, the core's `records/` + `index.json`, and
`receipt/resolution-receipt.json`. A `COMMITTED` marker is written **only** when
the outcome is fully ok; a failure leaves the partial output in place for
inspection and never promotes it. Capped runs are recorded as
`capped_pilot_diagnostic`, not whole-source qualification.

All registry writes use `manifest_generation.safe_write` (containment +
exclusive temp). The core owns its own JSON writes; no claim is made that they
use this helper.

## Resolution records → observed emission

A completed resolution run leaves `records/<id>.json` + `index.json` under its
run root. `pilot/merge_records.py` turns those records plus the metadata-only
selection evidence into the emitter's canonical resolution table and refuses
when any admitted Analysis lacks a complete (`status: success`) record:

```
python3 -m manifest_generation.providers.dbgap.mvp.pilot.merge_records \
  --scope bucket:EAS \
  --records-dir /data/opengwasdb/work/epic232/221/resolve-eas-<ts>/records \
  --metadata-root <metadata-refresh-root> \
  --mapping <mapping.tsv> \
  --out-dir <staging>
```

That writes `<staging>/merge/resolution.tsv` and
`<staging>/merge/merge-receipt.json` and never opens an association body. Add
`--emit` (with `--config resources/generators/dbgap-mvp/config-candidate.yaml`)
to drive the observed Store through the audited emitter:

```
python3 -m manifest_generation.providers.dbgap.mvp.generate_candidate emit \
  --config resources/generators/dbgap-mvp/config-candidate.yaml \
  --mapping <mapping.tsv> \
  --resolution <staging>/merge/resolution.tsv \
  --out-dir <staging>/bundles \
  --buckets EAS
```

`--buckets` (repeatable/space-separated) restricts emission to the named
ancestry buckets so a per-bucket resolution can be emitted on its own; the
default is every configured Store. The emitter refuses a resolution table that
is missing any intended member, has an extra row, or fails any per-row gate. An
unassigned record (empty `assigned_ancestry`) is preserved rather than forced to
the bucket label; a non-empty cross-ancestry mismatch still fails.

`assigned_ancestry` is the canonical value and `ancestry_assignment_method`
describes how *that* value was produced (`af_assigned` for a published value,
`unassigned` for a canonically-empty one), never the raw core inference: META
publishes an empty `assigned_ancestry` with `ancestry_assignment_method:
unassigned`, while the core's raw AF inference stays untouched in the resolver
record/receipt. The `source_ancestry_label` standard column carries the exact
acquisition-mapping ancestry (the source-declared label), never guessed from the
core assignment. The output
directory must not be a path component named `stores`, so stage under a
`bundles/` directory and let the parent move the accepted bundle into the
registry.

**Known gap (not retro-fixed).** `source_ancestry_label` was added to the merge
after the first EAS observed bundle was emitted, so the accepted OGS-00015
input omits the column. That omission is a known provenance gap bound to the old
resolver manifest/records snapshot recorded in that bundle's `source_snapshot`;
it is documented here and deliberately not rewritten in the live accepted input.
EAS data is otherwise unchanged.

The observed Store is emitted `status: accepted` with an `accepted_at` solely so
the dev release workflow may build it (#247: only `BUILDABLE_STATUSES` are
built); `run_emit` refuses to finish unless every observed bundle passes
`bundle.partition_buildable`. That is workflow buildability, **not** scientific
release acceptance: `validation.yaml:acceptance.human_acceptance` stays
`pending`. A completion child whose reference is pending/blocked is emitted
`status: candidate` and is deliberately refused by the workflow.

## Tests

`tests/dbgap-mvp/test_pilot.py` runs hermetically with a fake resolver and
temp-directory fixtures: selection/provenance rules, META empty ancestry, DRY
PLAN gating, start-receipt/approved-SHA gates, wall-timeout killing the whole
group, RSS breach, no-commit-on-failure, and alias/protected-root refusals. No
real source or reference file is opened. `tests/dbgap-mvp/test_proc_accounting.py`
uses a synthetic `/proc` to count host enumerations and `/proc/<pid>/stat` reads,
proving one snapshot per tick (linear, not `host_pids * members`) while pinning
the transitive/reparented RSS and zombie/foreign exclusion invariants.
