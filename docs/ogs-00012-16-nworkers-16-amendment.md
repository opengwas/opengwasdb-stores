# Execution-only amendment: `n-workers: 16` for MVP observed Stores 12/13/14/16

Owner-authorized execution amendment, recorded 2026-10-14 (owner instruction
17:43 UTC: "use 16 workers per build please"). It changes **only** the runtime
worker count in the Build Recipe of the four MVP observed Stores
(`OGS-00012`, `OGS-00013`, `OGS-00014`, `OGS-00016`) so the supported dev batch
can run three concurrent builds × 16 workers.

## What changed

`stores/OGS-000{12,13,14,16}/build.yaml` gain exactly one Build Recipe option:

```yaml
build:
  options:
    n-workers: 16
```

Nothing else in those bundles changes. `analyses.tsv` (membership and Analytical
Metadata), `release.yaml` (identity, status, `source_snapshot`, `accepted_at`),
`validation.yaml`, the pinned runtime commit `4e922566`, the source inputs and
the reference axis are byte-unchanged. `OGS-00015` (EAS) and `OGS-00017`
(FinnGen) are untouched.

## Why it is execution-only, not a new Store Release

[ADR 0004](adr/0004-store-release-identity.md) requires a new Store Release when
the accepted Release Manifest changes **or when a material Build Recipe choice
changes the produced analytical asset**. This amendment changes neither:

- The Release Manifest (`analyses.tsv`) is identical, so the accepted input
  membership is not re-accepted and no new Store Release ID is minted.
- The worker count is a **pure runtime knob**: opengwasdb ADR 0024
  (`0024-parallel-dense-vcf-build-disk-spill`) parallelises Pass 2 by
  disk-spill with no large IPC, and states "`n_workers` is a pure runtime knob;
  results are independent of it", verified by `test_two_workers_matches_serial`
  (serial and 2-worker builds bit-identical). The produced analytical asset is
  therefore independent of the value.

So the accepted input membership, Release IDs, scientific-acceptance posture
and format are unchanged and remain immutable; this is a deployment scheduling
choice, not an interpretation change.

## Relation to accepted-bundle immutability

ADR 0014, ADR 0031 and the workflow spec state that an *accepted* bundle is
immutable. This amendment does edit the Build Recipe of accepted bundles, so it
is recorded explicitly rather than applied silently, and it is deliberately
limited to the execution-only `n-workers` option:

- the owner authorized this execution change (2026-10-14);
- ADR 0004's material-choice test is not met (the produced asset is independent
  of `n_workers`; core ADR 0024), so no new Store Release or lineage is minted;
- the Release Manifest and every identity/source field are byte-unchanged, so
  the accepted input membership is not re-accepted.

If the project decides that **any** edit to an accepted bundle must mint a new
erratum Release rather than a recorded non-material amendment, this change is the
trigger to revisit that rule; it is flagged here before any broader recipe patch.

## Scope and limits

- Applies to the four MVP observed Stores above only.
- No new general runtime override, scheduler or thread API is introduced; the
  option is the existing `build-dense-vcf --n-workers` flag already used by
  `OGS-00017` (`n-workers: 32`).
- The pinned runtime `4e922566d7fa4c13a11be3637069da63e48be28c` remains
  read-only; no environment, source, reference or artifact write is part of this
  amendment. `pixi run index` regenerates `stores.tsv`, `STORES.md`, the bundle
  `summary.yaml` and `stores/by-label/` from these tracked files with the
  artifact root unset, so the artifact root is not written.
