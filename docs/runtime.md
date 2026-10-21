# The release runtime: pinned, current-format, and gated before writes

This document records the one `opengwasdb` runtime the supported release
workflow uses (stores#218, adopting the runtime portion of stores#197), what it
provides, and what upgrading it does **not** do.

## The pin

| Fact | Value |
|---|---|
| Repository | `https://github.com/opengwas/opengwasdb` |
| Revision | `023774a7881940c497e3bdb6710637819b023755` (head of core PR #291) |
| Core merge | opengwas/opengwasdb PR #291 (`check-observed-view`, core #289), on top of `dev` with #277, #278, #281 and #282; earlier pins: PR #276 (`4e922566`, bounded Dense validation, core #254) and PR #275 (`1be2a15`) |
| Package version | `0.3.0` |
| Writers' Store `format_version` | `0.2.0` |
| Python floor | 3.12 |
| Zarr / numcodecs | `zarr>=3.4,<4`, `numcodecs>=0.17` (Zarr v3 with the sharding codec) |

The pin is an immutable git revision, not a branch or a floating tag. The
executable form of it is the `opengwasdb` entry in `pixi.toml`'s
`[feature.store-build.pypi-dependencies]` and the resolved package in
`pixi.lock`. The Python 3.12 / zarr 3.4 / numcodecs 0.17 floors are not
preferences: the pinned runtime requires them, and a zarr 2.x environment cannot
open a format-0.2.0 Store.

`ogstores.runtime.PINNED_OPENGWASDB_REVISION` is the single source of truth the
tests assert `pixi.toml` and `pixi.lock` against. Change all three together, and
re-run `pixi lock` in the same commit.

## What the pin provides

- **Format-0.2.0 builders** (opengwas/opengwasdb#247): Zarr v3 with the
  sharding codec; Dense inner chunk `[1000, 64]` and shard `[100000, 1024]`.
  `build-dense-vcf`, `build-hybrid`, `build-ragged-ssf` and `build-ragged-besd`
  all write it, and the converter writes the same layout from a 0.1.0 source.
- **The required Ragged/Overflow `by_variant/` index**
  (opengwas/opengwasdb#252/#268). Every Ragged and Hybrid builder writes it
  inline; `opengwasdb validate` fails without it. It is part of the format
  contract, **not** an optional enhancement, and the registry plans no separate
  step for it.
- **Indexed Variant Subset support** (opengwas/opengwasdb#264/#269/#271),
  including Dense, completed-Dense and Hybrid Dense components:
  `build-indexed-subset`, audited by `validate` and reported by `overview`.
- The shared HM3 benchmark seam (opengwas/opengwasdb#272).

## The pre-write gate

Before the entry point snapshots anything or a build writes a Store,
`ogstores.runtime.verify_runtime_for_releases` (called from
`workflow/release.py`, and from the Snakefile `onstart` for a direct
`snakemake` run) refuses a run when:

1. no `opengwasdb` executable resolves on `PATH`;
2. the revision cannot be proven from the executed install's own metadata, or
   is a known revision other than the tracked pin -- an unprovable identity is
   refused, not assumed, so an ad-hoc executable substituted onto `PATH` cannot
   silently become the factory runtime;
3. the runtime does not provide every subcommand the *scheduled* jobs will run
   (probed with `--help`); or
4. a **scheduled** `complete` job would consume an existing parent Store whose
   declared `format_version` is not `0.2.0`.

The gate is scheduled-step-aware, because an id appearing in the dry run is not
proof that the Store is rebuilt. `workflow/release.py` and the Snakefile
`onstart` pass `(artifact_root, store_id, step)` for every dry-run job, so:

* the parent format is gated only when the child's own `complete` job will run;
  an enhancement-only rerun of an existing completed Store (build its HM3
  subset, validate, overview) never consults its unused completion recipe;
* a same-run `build` or `complete` job for the parent produces a fresh
  format-0.2.0 Store, so the stale on-disk artifact is not consumed and is not
  gated;
* a parent that is neither on disk nor scheduled to be produced is refused
  loudly before Snakemake starts.

The parent's declared format is read through `opengwasdb info --format json` --
the core's supported reporting interface -- not by opening the Store in this
repository. The run prints the resolved identity it will use, and every Step's
durable record already carries the exact `opengwasdb` revision and executable
that ran.

The format-0.2.0 and required-`by_variant` guarantees rest on the **verified
pin identity**, not on the `--help` probes: the subcommand probe only proves the
CLI surface a scheduled step calls exists. That is why an unprovable revision is
refused even when the commands answer.

The ordinary invocation therefore uses the pinned runtime with no `PATH`
overlay:

```text
pixi run release OGS-00016
```

The HM3 pipeline proof's temporary explicit `--opengwasdb-checkout` remains a
proof tool, not the supported release entry point.

## Converting or rebuilding a format-0.1.0 parent

A `0.1.0` parent is readable but not completable by a format-0.2.0 builder
(opengwas/opengwasdb ADR 0038 §4); completion writes into the source's arrays
and never re-encodes them. When a build ticket needs a completed descendant of
an existing `0.1.0` Store, it has two supported orders, both **core-owned
procedures** rather than registry API:

1. **Convert, then complete.** In the pinned `opengwasdb` checkout, convert the
   parent to format 0.2.0 with the core's conversion tool
   (`scripts/convert_store_to_0_2_0.py`; see the core's own documentation and
   CHANGELOG), register the converted parent as its own release, then run the
   completion against the converted Store. The core tool is the authority on
   which layouts it supports and on its verification gate.
2. **Rebuild the parent, then complete.** A fresh `pixi run release <parent>` at
   the pinned runtime writes a new format-0.2.0 observed Store; the completion
   can then consume it. This is the only order that applies the current builder
   fixes to the parent's bytes.

The runtime gate refuses the wrong order before any write and directs the
operator here; it does not itself invoke a converter. Remember that converting
or rebuilding a *registered* release is a catalogue action with its own
gates, not a side effect of a completion run.

## Upgrading does not convert or requalify anything

Bumping the pin changes what a **new** build is. It does not rebuild, convert,
revalidate or requalify any published Store. Every registered Store built before
the pin is still format 0.1.0, remains readable, and stays exactly as it was.

Consequences a build ticket must handle explicitly:

- A Reference Completion of a 0.1.0 parent is refused at the gate above and by
  the core builder itself. Convert the parent to format 0.2.0 with the core's
  conversion procedure, or rebuild the parent at the pinned runtime, then
  complete the converted/rebuild release. A completion never re-encodes its
  source. See "Converting or rebuilding a format-0.1.0 parent" above.
- Each build ticket must therefore supply a compatible observed parent before
  it can register a completed descendant. The existence of a published 0.1.0
  parent is not that parent.
- The required `by_variant/` index is part of a 0.2.0 build; a converted or
  older working copy that predates its required status still needs current
  validation.

## Coordination with core #254

Core opengwas/opengwasdb#254 (bounded Dense-validation memory) was not on `dev`
when this pin was first taken (`1be2a15`). It has since merged (PR #276) and this
pin was re-coordinated to `4e922566`, which carries the same format and
capability set plus the reduced validation memory bound (full-run peak RSS on
OGS-00009 33.7 → 2.35 GiB, OGS-00010 36.5 → 3.01 GiB, validate clean). The
earlier prohibition stands: do not install an uncommitted core checkout as the
chosen runtime, and treat any future repin as a deliberate coordinated change.

## Coordination with core #289

Core opengwas/opengwasdb#289 adds `opengwasdb check-observed-view`, the
Observed-View Check that gates a single-id completion's swap (stores #306,
ADR 0037). The pin was raised to `023774a7`, the head of its PR #291, so the
tracked runtime has the command; it is re-pinned to the merge commit once that
PR is on `dev`.
