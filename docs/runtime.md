# The release runtime: pinned, current-format, and gated before writes

This document records the one `opengwasdb` runtime the supported release
workflow uses (stores#218, adopting the runtime portion of stores#197), what it
provides, and what upgrading it does **not** do.

## The pin

| Fact | Value |
|---|---|
| Repository | `https://github.com/opengwas/opengwasdb` |
| Revision | `1be2a156649e4d444f31f9b4107c3f47a046f855` (core `dev`) |
| Core merge | opengwas/opengwasdb PR #275 |
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
2. the resolved executable's known revision is not the tracked pin, so an
   ad-hoc executable substituted onto `PATH` cannot silently become the
   factory runtime;
3. the runtime does not provide every subcommand the scheduled bundles plan to
   run (probed with `--help`); or
4. a scheduled Reference-Completed release's **existing** parent Store reports
   a `format_version` other than `0.2.0`.

The fourth gate reads the parent's declared format through `opengwasdb info
--format json` -- the core's supported reporting interface -- not by opening
the Store in this repository. A parent scheduled in the same run does not exist
yet and is a fresh format-0.2.0 build, so it is not gated. The run prints the
resolved identity it will use, and every Step's durable record already carries
the exact `opengwasdb` revision and executable that ran.

The ordinary invocation therefore uses the pinned runtime with no `PATH`
overlay:

```text
pixi run release OGS-00016
```

The HM3 pipeline proof's temporary explicit `--opengwasdb-checkout` remains a
proof tool, not the supported release entry point.

## Upgrading does not convert or requalify anything

Bumping the pin changes what a **new** build is. It does not rebuild, convert,
revalidate or requalify any published Store. Every registered Store built before
the pin is still format 0.1.0, remains readable, and stays exactly as it was.

Consequences a build ticket must handle explicitly:

- A Reference Completion of a 0.1.0 parent is refused at the gate above and by
  the core builder itself. The one order that works is to convert the parent to
  format 0.2.0 with `scripts/convert_store_to_0_2_0.py` (in the `opengwasdb`
  checkout), then complete the converted release; or rebuild the parent at the
  pinned runtime. A completion never re-encodes its source.
- Each build ticket must therefore supply a compatible observed parent before
  it can register a completed descendant. The existence of a published 0.1.0
  parent is not that parent.
- The required `by_variant/` index is part of a 0.2.0 build; a converted or
  older working copy that predates its required status still needs current
  validation.

## Coordination with core #254

Core opengwas/opengwasdb#254 (bounded Dense-validation memory) was not on `dev`
when this pin was taken. The pinned runtime is capability-complete; its Dense
validation memory bound is the one #254 is fixing. Once #254 lands, this pin
needs a coordinated repin **before** full production validators are scheduled.
Do not install an uncommitted #254 checkout as the chosen runtime.
