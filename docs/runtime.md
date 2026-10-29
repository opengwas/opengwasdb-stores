# The release runtime: pinned, current-format, and gated before writes

This document records the one `opengwasdb` runtime the supported release
workflow uses (stores#218, adopting the runtime portion of stores#197), what it
provides, and what upgrading it does **not** do.

## The pin

| Fact | Value |
|---|---|
| Repository | `https://github.com/opengwas/opengwasdb` |
| Tag | `v0.3.0`, the first release opengwasdb's release workflow published (stores #387); the earlier `v0.2.0` at `3e77ecf` was tagged by hand |
| Revision | `bc22e8476c4378af582477f09b56af01e3f5f065`, what `v0.3.0` resolves to: the `main` merge of opengwas/opengwasdb PR #335, whose range above `0d59f151` is release tooling (the version from tags, a changelog) only |
| Pin history | the previous pin `0d59f151` was the core `dev` merge of PR #329 (core #302: a BESD Ragged build stores each pair's published sample size, and the `.esi` allele frequency per association on request; ADR 0067); before it, PR #330's branch head `5fc0766` (stores #326), the `dev` merge of PR #298 `75f0e77` (stores #341), the `dev` merge of PR #305 `d8896e4` (stores #314), the `dev` merge of PR #291 `d405de0` (stores #312), PR #291's branch head `023774a7` (stores #306, which excluded #288), PR #276 (`4e922566`, bounded Dense validation, core #254) and PR #275 (`1be2a15`) |
| Package version | `0.3.0` |
| Writers' Store `format_version` | `0.2.0` |
| Python floor | 3.12 |
| Zarr / numcodecs | `zarr>=3.4,<4`, `numcodecs>=0.17` (Zarr v3 with the sharding codec) |

The pin names a release **tag**, not a branch or a raw commit: opengwasdb has
tagged every merge to `main` since `v0.3.0` (epic #385) and, since PR #335, takes
its version from that tag, so the installed version names the release it was
built from -- and a checkout past a tag reports a development version -- instead
of repeating a string hardcoded in `pyproject.toml`. The revision is what the tag resolves to,
and it is that immutable revision the guard proves from the executed install's
own metadata. The executable form of the pin is the `opengwasdb` entry in
`pixi.toml`'s `[feature.store-build.pypi-dependencies]` and the resolved package
in `pixi.lock`. The Python 3.12 / zarr 3.4 / numcodecs 0.17 floors are not
preferences: the pinned runtime requires them, and a zarr 2.x environment cannot
open a format-0.2.0 Store.

`ogstores.runtime.PINNED_OPENGWASDB_TAG` and
`ogstores.runtime.PINNED_OPENGWASDB_REVISION` are the single source of truth the
tests assert `pixi.toml`'s tag and `pixi.lock`'s resolved commit against. Change
all four together, and re-run `pixi update opengwasdb` in the same commit.

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
- **One strand-aware hg19 lift** (opengwas/opengwasdb#288, core #284; moved here
  for stores #312's OGS-00009 rebuild). The resolver and the Dense, Hybrid and
  BESD builders share it, so signed statistics and EAF follow the projected
  allele pair on a reverse-strand variant. A single-pass Hybrid build whose
  off-reference rows lift onto the reverse strand is refused.
- **A policy for the Hybrid build's rsid harvest** (opengwas/opengwasdb#306,
  moved here for stores #225's OGS-00011 rebuild). `build-hybrid
  --on-rsid-conflict {refuse,drop}` decides what a routing key its sources name
  differently stores, and the build always reports what it compared as
  `rsid_conflicts`. See "Coordination with core #306".
- **A policy for a Hybrid subset's Overflow-only ALIDs**
  (opengwas/opengwasdb#324, moved here for the same rebuild).
  `build-indexed-subset --overflow-only {refuse,skip}` decides a requested ALID
  only the Hybrid's shared axis carries, and a `skip` build records how many.
  See "Coordination with core #324".
- **One policy for a post-lift variant collision** (opengwas/opengwasdb#304;
  moved here for stores #314's OGS-00027). A chain set need not be injective: two
  hg19 loci can map into one hg38 position, and when their projected alleles
  agree they become one canonical variant. `build-ragged-besd` now drops every
  ESI entry in such a colliding group, with its associations, exactly as a
  liftover failure is, and records the counts in `provenance.lift`; a Ragged CSR
  cannot hold one `variant_index` twice in one Analysis's segment. `validate`
  requires each Analysis's `variant_index` slice to be **strictly increasing**,
  on every release, and reports a repeat separately from a decrease.
- **Per-association sample size, and the `.esi` allele frequency on request**
  (opengwas/opengwasdb#302, ADR 0067; moved here for the OGS-00027
  replacement, stores #314 in epic #282). `build-ragged-besd
  --sample-size-table` stores each stored pair's published sample size in an
  opt-in `uint32` `n` plane and its by-variant copy, and never estimates one the
  table omits; `--store-esi-eaf` stores the `.esi` frequency per association,
  oriented to the effect allele and checked once against `--eaf-reference`. A
  Store holding an `n` plane keeps `se` in float16, the loader and `validate`
  refuse an `n` plane beside residual-coded `se`, every Ragged query result then
  carries `n`, and `info` reports `n=uint32`.

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
tracked runtime has the command. That PR merged into core `dev` as `d405de0`
(second parent `023774a7`), and stores #312 then moved the pin to `d405de0`
itself, because the OGS-00009 rebuild needs core #288's strand-aware hg19 lift,
its first parent's merge. A release that completes its own Store
(ADR 0037) on a runtime without the command is refused before anything is
written, by
`resources.verify_observed_view_check_for_releases`, which the entry point calls
in its dry run and its run, and the Snakefile in `onstart`, beside the Indexed
Variant Subset guard.

## Coordination with core #295

Core opengwas/opengwasdb#295 extends `check-observed-view` from Dense pairs to
Hybrid pairs: the Dense Components, every crossover (a source Overflow association
whose ALID becomes a Dense row on completion), and a seeded sample of
whole Overflow Analyses with exhaustive per-Analysis counts. Stores #333 moved
the pin to `eb4c749b`, #295's merge into the core integration branch
`orch/232-observed-view` (core PR #303), so that a Hybrid release can complete
its own Store (ADR 0037). That revision is not yet on core `dev`. The pin
has since moved to the merge of core `dev` into that branch, which carries
`dev`'s #304 and #298 as well (the pin table above); the branch itself has not
merged into `dev` yet.

`--help` shows the same `check-observed-view` command on revisions that compare
only Dense pairs, so the capability probe above cannot tell them apart. The
exact-pin check is what refuses an older runtime for a Hybrid or Ragged
single-id release: any revision other than the pin is refused before anything
is written.

## Coordination with core #296

Core opengwas/opengwasdb#296 extends `check-observed-view` to Ragged pairs. A
Ragged Store has no chunk-rows, so the check samples whole Analyses
(`--n-random` of them, from the same seed) and, for each, rebuilds the regions
completion retained from the LD panel: every observed association must come
back identical and not imputed, and every imputed association must lie inside
a retained region. That rebuild needs the panel completion used, so the
check refuses a Ragged pair without `--ld-panel` (exit 2); `--ancestry`
defaults to the one the completed Store records and, when given, must match
it. The planner passes both from the bundle's `complete` options. Stores #336
moved the pin to `32f47b3b`, #296's merge into the same integration branch, so
that a Ragged release can complete its own Store (ADR 0037). Like `eb4c749b`,
it is not yet on core `dev`.

The capability probe cannot tell `32f47b3b` from `eb4c749b` either: it asks
only whether `check-observed-view --help` succeeds, and on both it does. Here
too the exact-pin check, not the probe, is what refuses an older runtime for a
Ragged single-id release.

The check compares observed SE exactly. `complete-ragged` refits and
re-quantises SE stored as `int8_residual`, which on real data moves every
observed SE slightly (opengwas/opengwasdb#301), so a Ragged Store whose builder
chose that encoding, as OGS-00006's did, fails the check until #301 is
resolved.

## Coordination with core #304

Core opengwas/opengwasdb#304 is the fix for the collision class the full eQTLGen
cis Store (OGS-00027, stores #314) hit: 13 of its hg19 SNPs land on one hg38
variant after the lift, so `d405de0`'s `validate` passed a Store whose CSR held a
`variant_index` twice in one Analysis's segment. The pin moved to `d8896e4`, the
`dev` merge of PR #305, whose range from `d405de0` is #304's three commits and
its merge plus a docs-only merge (#293). The tightened ordering rule is a
validation rule, so it applies to every release, not only to new builds: it
reads each registered Ragged and Hybrid Store's full `variant_index` and finds
no repeat, so no published Store becomes invalid at this pin. A Store built
before it that does carry a repeat must be rebuilt, not revalidated.
## Coordination with core #306

A single-pass Hybrid build whose variant reference names no rsids harvests a
name for each source site, and two sites that case-fold to one routing key can
disagree: the EBI GWAS-SSF Sources spell some alleles in lower case and carry a
compound `variant_id` as their name. Refusing that was what stopped the
OGS-00011 rebuild of 2026-10-22, at `1:905:C:T`, and no option resolved it.

Core opengwas/opengwasdb#306 adds `build-hybrid --on-rsid-conflict {refuse,drop}`,
default `refuse`, which is the behaviour it had. Under `drop` the disputed
routing key gives no rsid to the variants it reaches: the table cell is `.` and
the key is absent from the rsid index, which is how a Store already records a
variant it can name no other way. The build always reports what its harvest
compared as `rsid_conflicts: {policy, n_keys, n_variants, sample}` in its stdout
JSON, with both counts `0` when nothing was disputed and `null` when no names
were compared across sources (`--reference-panel`, or a reference that names its
own rsids); a `--reference-panel` build refuses `drop` rather than ignore it.
The same change normalises a compound `rsN:pos:a:b` id and the first item of a
`;` list in the readers, so a name that is really one rsid is compared as that
rsid.

The policy is per Build Recipe, so it is the recipe that declares it; stores
#225 adds `on-rsid-conflict: drop` to OGS-00011's, as a Release Erratum, and
`register` copies the counts into the Validation Record's
`observed.rsid_conflicts`. A release that completes its own Store carries them
from its build phase, because `complete-hybrid` reports none: the counts
describe the completed Store too. A checkpoint records the policy, so a build
checkpointed before the option existed reads as `refuse`: resuming it under
`drop` is refused, as resuming under any other changed parameter is.

## Coordination with core #324

A subset build on a Hybrid root indexes the release's nested Dense Component,
not the whole release: an ALID the release carries only on the shared/Overflow
axis has a Variant Index there that this index cannot address. Resolving it
against the Dense Component alone would count it absent and leave it out
silently, so the builder refused the whole request and named the ALIDs. That is
what stopped the OGS-00011 rebuild of 2026-10-23: 17,151 of the 1,206,903 HM3
ALIDs it requested were Overflow-only, and 149 were absent from the release
altogether.

Core opengwas/opengwasdb#324 adds `build-indexed-subset --overflow-only
{refuse,skip}`, default `refuse`, which is the behaviour it had and names the
`skip` remedy when it refuses. Under `skip` the Overflow-only ALIDs stay out of
the index, and the group records `overflow_only_count` -- always written, `0`
when there were none -- beside the first 20 skipped ALIDs as
`overflow_only_sample`, in variant-list order. The count is never folded into
`absent_count`, which keeps its meaning: the release does not carry the ALID at
all. The recorded counts must sum `requested_count = resolved_count +
absent_count + overflow_only_count`, and both the read seam and `validate`
enforce it. A group written before the option records neither attribute and is
read as `0`, because the builder that wrote it could only refuse such a
request; a Dense root has no Overflow axis, so it accepts the flag with no
effect.

The policy is per declared subset, so it is the declaration that carries it:
stores #225 gives OGS-00011's `hm3` declaration `options: {overflow-only:
skip}`, and the planner passes the whole `options` mapping through to both
phases' `build-indexed-subset` argv. The build prints the human counts and a
JSON summary whose `overflow_only` block holds `policy`, `n_overflow_only` and
the sample. `register` harvests no subset output -- it reads the Store's build
or completion step -- so the counts of a subset built here live in that step's
record, `records/<phase>indexed-subset-<name>.json`, and nowhere in the
Validation Record.
