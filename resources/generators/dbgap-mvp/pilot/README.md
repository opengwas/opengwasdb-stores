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
  --effect-provenance <owner/source effect-scale provenance.tsv> \
  --out-root /data/opengwasdb/work/epic232/221/metric-pilot-staging
```

`run` additionally needs `--execute`, `--approved-metadata-sha`, `--start-receipt`
and the pinned `--core-commit 4e922566...`; the parent start receipt binds owner,
scope, code head, core commit, executable, the 15 ids, reference paths, caps and
start identity. A `resolution-receipt.json` records the outcome, records
verification, guard counts and resource actuals.

## What each module owns

- `selection.py` — metadata-only build of the reviewed 15 rows and the resolver
  manifest. Effect scale comes **only** from explicit owner/source provenance
  (`--effect-provenance`, columns `stored_effect_scale`, `original_effect_scale`,
  `effect_scale_provenance`); study design and label regexes are deliberately
  ignored, and a row without provenance is `unknown`/pending and is **excluded
  from the resolver manifest** because the public reader defaults a blank
  `stored_effect_scale` to `sd`. META is canonicalised to the empty
  super-population; the core's inferred ancestry is not rewritten.
- `wrapper.py` — fresh-root preflight (refuses reuse, symlink ancestors,
  `stores/` components and protected roots before any `mkdir`); limit validation
  (finite, positive, within 1800 s / 4096 MB / 2 workers) before any write; the
  wrapper becomes a **child subreaper** and holds the resolver at a **startup
  barrier** until the monitor is running, so nothing loads before accounting;
  stdio goes to files (never a pipe, so a detached grandchild cannot suspend the
  wait); on wall-timeout/RSS breach the whole session group and any reparented
  descendants are SIGKILLed and reaped, and an outcome is `ok` only when nothing
  escaped. CPU affinity uses the process's **actual allowed** set and a failure
  is fatal; BLAS/OpenMP=1 is pinned after any caller env.
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
