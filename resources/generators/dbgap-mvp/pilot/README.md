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
python3 resources/generators/dbgap-mvp/pilot/cli.py plan \
  --metadata-root /data/opengwasdb/work/epic232/220/metadata-refresh-parent-baf141c-20261013T123913Z \
  --mapping /data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z/mapping.tsv \
  --out-root /data/opengwasdb/work/epic232/221/metric-pilot-staging

# Bounded execution (parent-authorised only).
python3 resources/generators/dbgap-mvp/pilot/cli.py run \
  --metadata-root ... --mapping ... \
  --out-root /data/opengwasdb/work/epic232/221/metric-pilot-<runtime-utc> \
  --approved-metadata-sha 1f14b88c2c383d8bac97215a8807503ef600fed2d2a019067e9962777438310f \
  --start-receipt /path/to/parent-start-receipt.json \
  --execute
```

## What each module owns

- `selection.py` — metadata-only build of the reviewed 15 rows and the resolver
  manifest. Effect scale comes from real provenance (`study_design` case-control
  → `log_or`/`binary_trait`; inverse-normal-transformed quantitative →
  `sd`/`declared_standardised`); anything else is `unknown`/pending and is
  **excluded from the resolver manifest** because the public reader defaults a
  blank `stored_effect_scale` to `sd`. META is canonicalised to the empty
  super-population; the core's inferred ancestry is not rewritten.
- `wrapper.py` — fresh-root preflight (refuses reuse, symlink ancestors,
  `stores/` components and protected roots before any `mkdir`), the resource
  monitor (started before the resolver can load the reference), and the bounded
  runner. CPU affinity and BLAS/OpenMP=1 are set on the child; the whole process
  group is killed on wall-timeout or RSS breach, then re-checked so no
  grandchild escapes.
- `cli.py` — the DRY PLAN default and the fail-closed `run` gates. Every
  resolver argument is assembled here; arbitrary commands cannot be run.

## Resource accounting

- Limits: 2 workers, ≤4 CPU, BLAS/OpenMP 1, 1800 s whole-pool wall, 4096 MB
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

All registry writes use `resources/generators/lib/safe_write.py` (containment +
exclusive temp). The core owns its own JSON writes; no claim is made that they
use this helper.

## Tests

`tests/dbgap-mvp/test_pilot.py` runs hermetically with a fake resolver and
temp-directory fixtures: selection/provenance rules, META empty ancestry, DRY
PLAN gating, start-receipt/approved-SHA gates, wall-timeout killing the whole
group, RSS breach, no-commit-on-failure, and alias/protected-root refusals. No
real source or reference file is opened.
