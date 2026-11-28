# Increment Bundles add Analyses to and remove them from a published Hybrid under its Store id

## Status

Proposed, for review before implementation. This ADR sets the bundle model, `bundle-check` and the
amendment check, issue [#546](https://github.com/opengwas/opengwasdb-stores/issues/546). The
route that publishes an increment is [#560](https://github.com/opengwas/opengwasdb-stores/issues/560).
OGS-00011's first increment, [#561](https://github.com/opengwas/opengwasdb-stores/issues/561), is
the first to use it. This is the registry half of epic
[opengwas/opengwasdb#508](https://github.com/opengwas/opengwasdb/issues/508). The engine half is
opengwasdb ADR 0109, *Splice: Analyses enter and leave a published Hybrid Store Release by
building small and splicing* (opengwasdb#520).

Where each decision comes from:

- **The owner's rulings**, made by Gibran Hemani (`explodecomputer`) on 28 Nov 2026 and recorded
  on [opengwas/opengwasdb#508](https://github.com/opengwas/opengwasdb/issues/508) ("Owner's
  rulings, 28 Nov 2026"), marked *(owner)*:
  - **Ruling 2, identity.** Keep the Store id, so every kept `OGS-00011:<analysis_id>` resolves.
    Each increment is a new Store Release, with a new `release_id` and a lineage list in the
    manifest. The previous release moves to `replaced/<UTC>/`. A defect removal is also a
    Release Erratum (ADR 0005). ADR 0004 is amended, not superseded.
  - **Ruling 1.** The route is built against format 0.3.0.
  - **Ruling 6.** GCST90132314 is checked by name.
- **This ADR**, marked *(this ADR)*. Everything else here is this ADR's decision.

What this ADR changes in other documents:

- [ADR 0004](0004-store-release-identity.md) gains an amendment pointing here (§1).
- CONTEXT.md gains **Increment Bundle**. **Release Bundle**, **Store Release**, **Release
  Lineage** and **Release Erratum** change to match.

## Context

A published Store's membership is fixed. Today the registry can change it only by rebuilding the
Store, or by the one-off exception #478 made. Under that exception OGS-00011 was re-emitted over
its accepted bundle (3,169 → 3,245 Analyses), and its Release Status went from `validated` back
to `candidate`, a move `bundle.LEGAL_STATUS_TRANSITIONS` (`src/ogstores/bundle.py:52`) does not
allow. ADR 0004 recorded the exception as setting no precedent.

The engine now gives a third way. `opengwasdb splice-release` (ADR 0109) takes a published
Reference-Completed Hybrid (SOURCE) and an **Increment**: a small Hybrid holding the Analyses to
add, built with `build-hybrid --increment-of SOURCE` and completed against SOURCE's panel. It
writes a new release that adds those Analyses and drops any named ones. Each splice appends an
entry to the manifest's `provenance.lineage`. The new release stays at format 0.3.0 (ADR 0109 §9,
ruling F1). The splice never writes in place: moving the previous release aside, and deciding
whether the new one may publish, are the registry's jobs.

What the registry holds today, which this design rests on:

- **The Release Manifest is read directly as membership.** The Analysis Catalog lists a
  validated bundle's non-excluded `analyses.tsv` rows as its Analyses
  (`src/analysis_catalog/policy.py:142-143`, `partition_membership`; ADR 0039, "Inputs"). So
  does the index's `n_manifest`. Any admitted row in a validated bundle's `analyses.tsv` is
  served as a member, whether or not the published Store holds it.
- **Two sidecars are tied to `analyses.tsv` row for row.** `sidecars/quality_checks.tsv` must
  name every `analyses.tsv` Analysis once, in the same order, and agree on which are excluded.
  `sidecars/exclusions.tsv` must agree on why (`bundle.py:1796` `_check_quality_checks`,
  `:1784` `_exclusion_reasons`).
- **The published membership is a file.** `amendment._membership_changes`
  (`src/ogstores/amendment.py`) compares the derived build manifest the bundle makes now with
  `<root>/<id>/work/analyses.tsv` and its exclusion sidecar, byte for byte and by id. It is part
  of `published_store_changes`, the one walk of a published release that ADR 0037's own-id
  completion and ADR 0044's in-place additions both use.
- **The derived manifest decides Snakemake's view.** The Snakefile makes the bundle's
  `analyses.tsv` `ancient` only when it would materialise to the `work/` files already on disk
  (`workflow/Snakefile:354` `build_manifest_input`). An unforced run that schedules a published
  release's Store-writing step is refused (`run.refuse_rebuilding_published_releases`,
  `src/ogstores/run.py:1121`).
- **The emit replaces a whole bundle, and only a candidate.** `emit.py:242`
  `_refuse_replacing_more_than_a_candidate`. A re-emit re-resolves every row. At #478, 2,378
  kept Analyses changed under the newer resolver.
- **The registry sees a Store only through `opengwasdb info --format json`** (ADR 0023,
  `src/ogstores/store_info.py`). It prints `store_id`, `release_id`, `format_version`, layout,
  coverage, completion state, assembly and encoding (opengwasdb `cli/main.py` `info`). It does
  not print `provenance` or `n_analyses`.
- **An in-place addition keeps the `release_id`.** OGS-00011's rewrite and its post additions
  carry the same `release_id`, `1459d7e2-48de-486a-ad7a-e6dc8d26f935`
  (`stores/OGS-00011/validation.yaml`, `rewrite` and `post_additions`), although the additions
  changed the Store (Rho, Clumps, recount). A `release_id` names a release's lineage, not its
  bytes.

## Decision

### 1. Identity: the amendment to ADR 0004 *(owner: ruling 2; the conditions: this ADR)*

**A Store id now names a sequence of Store Releases.** Each is identified by
`(store_id, release_id)`. All of them are built from one Release Bundle, and each one after the
first is an increment of the one before. A kept Analysis keeps its public id,
`<store_id>:<analysis_id>`. Its `analysis_index` is release-scoped (ADR 0109 §2), as the
registry's derived manifest already makes it (ADR 0025).

**A membership change keeps the Store id** when all of the following hold:

1. The bundle is a Reference-Completed Hybrid that completes its own Store
   (`layout: hybrid`, `completion_state: reference_completed`, no `derived_from`). That is the
   engine's splice scope: OGS-00011, OGS-00032 and OGS-00033.
2. The change is declared in an accepted **Increment Bundle** (§2).
3. It changes no kept Analysis's row and no Build Recipe input. `build.yaml` is unchanged.
   Every added Analysis is resolved under the bundle's release config and resolver settings
   (§3), and built under the release's pins (ADR 0109 §3).
4. It is published through the increment route (§6), which splices.

Two kinds of change qualify:

- **An increment** adds Analyses, and may also drop some.
- **A defect removal** only drops. It is also a **Release Erratum** (ADR 0005). Its Increment
  Bundle carries an `erratum` block (§2). The accepting human copies that text into
  `release.yaml`'s `notes` as a `Release Erratum (increment <n>, <issue>)` paragraph, which is
  where every erratum already lives.

**Each still needs a new Store id**, as ADR 0004 said before:

- a change to the Build Recipe, or to anything ADR 0109 §3 pins: Variant Reference, LD panel,
  encoding, assemblies, rsID policy, EAF-orientation settings, `format_version`;
- a change to a kept Analysis's row, including re-resolving kept rows under a newer resolver
  (the re-emit);
- a change to the release config's selection policy: thresholds, Quality Checks, the Store
  Composition Rule;
- any membership change to a Dense, Ragged or Observed-Only Hybrid bundle. The engine refuses
  to splice those layouts.

`--config force=1` keeps its meaning. It rebuilds a release from its bundle as it stands, with
increments folded in, and the lineage chain starts again from that rebuild.

**How the registry shows the identity.** The `release_id` is the engine's, stamped fresh by each
splice. The registry never invents one. The registry's own ordinal is the Increment Bundle's
number (`0001`, `0002`, …). The registry shows the lineage in three places:

- the Increment Bundle's `published` block: `release_id`, `source_release_id`,
  `increment_release_id`, `published_at`, `archive`;
- the Validation Record's `increments` list, one entry per published increment. The whole chain
  can be read from the current record without opening an archive (§7);
- `observed.release_id`, and a `release_id` column in `stores.tsv` (§8).

`replaced/<UTC>/` holds each previous release, as it does for ADR 0043 and ADR 0044.

### 2. The Increment Bundle *(this ADR)*

An increment is declared in its own directory inside the Release Bundle. **It does not touch
`analyses.tsv` or any other bundle file until the route publishes it:**

```text
stores/<id>/increments/<nnnn>/
  increment.yaml      the declaration: status, reason, drops, erratum, pins, published block
  analyses.tsv        the rows it adds, in the bundle's exact columns (absent for a drop-only increment)
  sidecars/*.tsv      their per-Analysis evidence, each in the bundle's sidecar header
  …                   any other evidence the emit writes (e.g. an overlap assessment)
```

```yaml
increment: 1                      # equals the directory number; numbered from 1 with no gaps
store_id: OGS-00011
status: accepted                  # candidate -> accepted -> published; candidate|accepted -> withdrawn
reason: "stores#557: GWAS Catalog studies re-pooled into hybrid__European"
created_at: '2026-11-29T…Z'
accepted_at: '2026-11-29T…Z'      # required from `accepted` on; set by the accepting human
source_snapshot: {…}              # as release.yaml's, for an increment that adds
generator: {version: …, commands: […]}
require_added: [GCST90132314]     # ids the emit and the route refuse to go without (ruling 6)
drops:                            # [] for an add-only increment
  - analysis_id: GCST…
    reason: "z mis-scaled (stores#532)"
    defect: true
erratum:                          # required when any drop has defect: true
  issue: opengwas/opengwasdb-stores#532
  text: "…"
published:                        # written by the route at publication, never by hand
  release_id: …
  source_release_id: …
  increment_release_id: …
  published_at: …
  archive: <root>/<id>/replaced/<UTC>
```

**The rows it adds.** These are the increment's `analyses.tsv` rows, in the bundle's columns and
column order, with `analysis_index` running `0..k-1` within the increment.

- An admitted row is an added Analysis.
- An excluded row (`exclude_from_build=true`, with an `inclusion_reason`) is a candidate refused
  at resolution. It joins the bundle as an audit row, with its evidence. That is how ruling 6's
  "each passes or is refused at resolution, with evidence in `ancestry.tsv`" is recorded.

**Rows removed by a drop.** A dropped Analysis is never deleted (ADR 0025). At publication its
bundle row becomes `exclude_from_build=true`, with
`inclusion_reason: "excluded: removed_in_increment_<nnnn>: <reason>"`. Its `quality_checks.tsv`
row's exclusion columns are set to match, and `exclusions.tsv` gains its row.

**Status belongs to the increment; the bundle's Release Status stays `validated`.** An increment
moves `candidate → accepted → published`. A candidate or an accepted increment may become
`withdrawn`. A bundle has at most one open increment (`candidate` or `accepted`), so the parent
of a splice is never ambiguous. Increment `n+1` may be emitted only once `n` is published or
withdrawn. This removes #478's illegal `validated → candidate` move: the bundle never goes back.

**The fold, at publication.** Publishing increment `n` folds it into the bundle, and only the
route does this (§6). The fold changes exactly these things, and every other byte of the bundle
stays as it was:

- `analyses.tsv`: the increment's rows are appended, with `analysis_index` continuing the
  bundle's. Each declared drop's two exclusion columns are set.
- Each `sidecars/*.tsv` keyed by `analysis_id`: the increment's rows are appended. Each drop's
  exclusion columns are set in `quality_checks.tsv`, and its row is appended to
  `exclusions.tsv`. A sidecar whose header differs from the increment's is refused.
- `summary.yaml` is regenerated.
- `increment.yaml` becomes `published`, with its `published` block.

Evidence that is not a per-Analysis table stays in the increment's directory.

**The fold keeps the order the splice gives.** After the fold, the derived build manifest
(ADR 0025: excluded rows dropped, order kept, `analysis_index` re-densified) holds the kept
Analyses in their old order, then the added ones in the increment's order. That is exactly
ADR 0109 §2's `analysis_map`. So for add-only increments, a `--config force=1` rebuild from the
bundle orders its Analyses as the spliced release does, and ADR 0109's permitted difference 1
does not arise.

**A replace, and re-admitting an existing audit row, are refused for now.** Either would put one
`analysis_id` on two `analyses.tsv` rows: the retired one as an audit row, and the new one. Every
per-id reader assumes one row per id: `_exclusion_reasons`, the quality-checks pairing,
`amendment._manifest_rows`, and the resolver's per-Analysis filename (`_check_analysis_ids`).
OGS-00011's first increment needs neither (ADR 0109, Context). The engine supports a replace in
one splice, so the bundle model can admit it later, once a ticket needs one and those readers are
audited. Until then `bundle-check` refuses an increment row whose `analysis_id` is already in the
bundle.

### 3. Emitting an increment: rows are added, existing rows are never re-rendered *(this ADR)*

#478's path re-emitted the whole bundle. That replaced `analyses.tsv` and re-resolved every row.
An increment is emitted instead by Manifest Generation's increment mode, which writes only
`increments/<nnnn>/`:

- **Selection.** The release config is the bundle's own, and the frozen Source Inventory is a new
  snapshot. The increment's candidates are the selected Analyses whose `analysis_id` is in
  neither the bundle's `analyses.tsv` nor an earlier increment. Rows already in the bundle,
  admitted or excluded, are neither resolved nor rendered again.
- **Resolution.** Only those candidates are resolved, under the locked engine and the bundle's
  resolver settings. The emit refuses when the composed settings (`resolver_settings` of the
  config) differ from those of the receipt the bundle records. The resolver version is recorded
  in `increment.yaml`. It may differ from the bundle's 0.14.1, and the Validation Record's
  resolver-mismatch warning names both.
- **Render.** Quality Checks, the Assigned Ancestry check and the Store Composition Rule run per
  Analysis, as for any candidate. The checks that compare rows with each other run over
  bundle ∪ increment: a duplicate `analysis_id`, and byte-identical source files by `checksum`.
- **Emit.** The increment is staged under `stores/.staging/`, held to the increment contract
  (§4) together with its bundle, and moved into `increments/<nnnn>/` under the emit's per-id
  lock, as `candidate`. The emit refuses:
  - a bundle that is not `validated`, or not in §1's scope;
  - a bundle with an open increment;
  - any `require_added` id that is not an admitted row.
- **A drop-only increment** is written by the same mode from `--drop ID --reason TEXT`
  (`--defect`, `--erratum-issue`). Nothing is resolved.

The emit never writes over an increment past `candidate`. The bundle's own emit still refuses to
replace anything but a candidate.

### 4. `bundle-check`: what the contract accepts *(this ADR)*

`bundle.check` gains the increment contract, so `pixi run bundle-check` and the emit apply it.
For each `increments/<nnnn>/`:

- **The declaration.**
  - `increment.yaml` keys and types are valid, and `increment` equals the directory number.
    Numbers run from 1 with no gaps.
  - `status` is in the vocabulary. With a previous state given, the transition is legal.
  - `accepted_at` is set from `accepted` on. `published` is set exactly when `status: published`.
  - `erratum` is present when any drop is a defect.
  - At most one increment is open.
  - The bundle is in §1's scope.
- **The rows.**
  - The increment's `analyses.tsv` has the bundle's columns, in order.
  - It passes the same `_check_analyses` path as a bundle's: an admitted row must be buildable,
    and an excluded row needs an `inclusion_reason`.
  - Its `analysis_id` values are unique and absent from the bundle, until it is published (§2).
  - Each sidecar has the bundle's header and passes the bundle's sidecar checks against the
    increment's rows.
- **The drops.** For an unpublished increment, each drop names an admitted bundle row and has a
  reason. A drop of an Analysis the same increment adds is refused.
- **A published increment is folded.**
  - Each of its rows appears in the bundle's `analyses.tsv`, identical except `analysis_index`,
    in its order, after the rows of every earlier increment.
  - Each drop is excluded there, with the `removed_in_increment_<nnnn>` reason.

### 5. The amendment check: a declared increment passes, an undeclared change refuses *(this ADR)*

**`published_store_changes` does not change.** The published membership is still the file
`work/analyses.tsv`. The route rewrites that file at publication, from the folded bundle (§6),
and archives the previous one in `replaced/<UTC>/work/`. So the byte comparison and its message
still hold, for ADR 0037's own-id completion and for ADR 0044's additions alike.

- **A pending increment passes.** A candidate or accepted increment touches no file the check
  reads. `pixi run release <ID>`, an ADR 0044 addition and a forced rebuild behave as if it were
  not there. If one of them publishes while an increment is pending, the splice's parent changes,
  and the parent check (§6) refuses the stale splice.
- **An undeclared change refuses, as today.** A re-emit, a hand-added row, or a kept row's
  changed columns all make the derived manifest differ from `work/analyses.tsv`. The refusal
  names the rows and now points to an Increment Bundle (ADR 0045), as well as to
  `--config force=1`.
- **What tells an increment from a re-emit** is that the increment is outside `analyses.tsv`
  until it folds, and that the fold is checkable (§4). A re-emit changes rows in place, and no
  declaration explains it.

**`increment_changes(b, n)` is the route's check.** The route runs it at its preflight and again
before the marker. It refuses unless:

1. `published_store_changes(b)` is empty, so the bundle still plans the live Store;
2. increment `n` is the bundle's one open increment, and it is `accepted`;
3. folding `n` into the bundle gives a bundle that passes `bundle.check`;
4. the declared adds are absent from the live membership, and the declared drops are in it;
5. after the splice, the splice's reported `added` and `dropped` equal the declared sets exactly,
   and its `n_analyses_after` equals the folded derived manifest's row count.

The check is a pure function of the bundle, the increment and the published records. No Store is
opened (ADR 0023).

### 6. The route, in outline, for #560 *(this ADR)*

One entry point, in the mould of ADR 0043 and ADR 0044, on the shared lifecycle
(`staged_publication`):

```text
pixi run publish-increment <ID> [--increment N] [--dry-run]
```

1. **Preflight, before anything is written.**
   - The usual refusals: Release Status, leftovers (`refuse_leftovers`), and the locked runtime.
   - The published Store is `info`'s 0.3.0.
   - `increment_changes` holds.
   - **The route takes the release's run lock and holds it until the marker.** No registry route
     can publish this Store between the splice reading it and the publication.
2. **`--source-record`, written from the registry's records.** Each entry and where it comes
   from:
   - **rsID policy:** `records/build.json` argv `--on-rsid-conflict`, which must equal
     `validation.yaml` `observed.rsid_conflicts.policy` (OGS-00011: `drop` and `drop`);
   - **source assembly:** `--source-assembly`;
   - **Variant Reference:** the path from `--variant-reference`, and that file's sha256. The file
     is refused if it was modified after `build.json`'s `start_time`;
   - **LD panel:** the path from `records/complete.json` `--ld-panel`, and its digest;
   - **Indexed Variant Subsets:** each `completion-indexed-subset-<name>.json`'s
     `--variant-list` and `--overflow-only`;
   - **Clumping:** `build.yaml` `post.ld_stores` gives the `--ld-store` pairs.

   Values that disagree, or one that is missing, refuse. None is defaulted.
3. **The steps, each through `run.execute_step`.** Their records are
   `records/increment-<nnnn>-{build,complete,observed-view-check,splice}.json`. Each record is
   held to its planned argv, and the splice runs `--plan` first.
   - The Increment builds from `work/increments/<nnnn>/analyses.tsv`, which is the increment's
     materialised manifest.
   - The splice's destination is `store.opengwasdb.partial`, so the engine's own staging is this
     mode's `stage`.
4. **The verdict.**
   - `increment_changes` item 5 holds.
   - Each `require_added` id is in the splice's `added` and admitted in the fold (ruling 6:
     GCST90132314).
   - The validation is `validate`'s verdict. An error refuses. A warning carried unchanged from
     the replaced record is accepted, by exact message (ADR 0043, amended 25 Nov). A new warning
     that names only added Analyses is accepted and listed in the increment entry, as a union
     build would emit it. Any other new warning refuses: ADR 0109 §8 says kept Analyses' answers
     never change.
5. **The parent-release check, immediately before the marker.** Publication is refused unless:
   - the live Store's `release_id` (`info`) equals the splice's `source_release_id`;
   - the published Store directory and its register record are unchanged since the splice step's
     `start_time` (ADR 0043's "a path is not a release"). The `release_id` alone does not prove
     the parent: an ADR 0044 addition rewrites a release and keeps its `release_id` (Context).

   The operator re-runs the splice against the new live release.
6. **Publication.** The marker, then `register.complete_publication`.
   - The old Store, the records snapshot, the replaced Validation Record and the old
     `work/analyses.tsv` go under `replaced/<UTC>/`.
   - The new Validation Record and the register record are written.
   - **The folded bundle files and the new `work/analyses.tsv` are written in the same
     transaction.** The route stages the folded bundle beforehand and holds it to `bundle.check`,
     as the emit stages a candidate. The marker names it, with the sha256 of each bundle file it
     replaces, and recovery finishes the swap from the marker. A bundle that changed after it
     was staged is refused before the marker.
   - The emit's directory exchange is in `manifest_generation`, which Store Release Build may not
     import (ADR 0035). #560 moves the exchange into `ogstores`.
7. **Snakemake settling.** The new `work/analyses.tsv` is newer than `records/build.json`. By
   mtime, Snakemake would schedule the build, and the unforced run would be refused. So the
   Snakefile makes the manifest→build edge `ancient` for a release whose last publication is an
   increment, and whose `work/analyses.tsv` has the sha256 that increment's entry records. A
   forced run is never settled. This is ADR 0044's `settled`, extended to one more edge.
8. **The Increment's Stores** under `<root>/<id>/increments/<nnnn>/` are removed after
   publication, as a replaced Observed-Only Store is (ADR 0037). Their records stay in
   `records/`.

Refusal and interrupt tests go in the release-faults suites (#560's "done when").

### 7. What is recorded *(owner: ruling 2's lineage; the fields: this ADR)*

**The Validation Record** is a republication (`staged_publication.republication`). Every block
the replaced record had is carried (`acceptance`, `rewrite`, `post_additions`), and this run
changes the following:

- `status`, `checks`, `warnings` and `errors` come from this release's `validate` verdict (§6.4).
- `build_environment` names the splice's engine. Its `engines` list adds the four increment steps.
- `observed`:
  - `release_id` is the new one, from `info`;
  - `format_version` comes from `info`, and `store_bytes` from a stat;
  - `n_analyses` is the splice's `n_analyses_after`, checked against the folded manifest;
  - `n_variants` is its `n_variants_after`;
  - `n_associations`, `n_loci_5e8`, `n_unclumped_5e8` and `n_top_hits` are taken from the
    splice's report when it gives them, and are otherwise `null`. They are never derived (#322);
  - `has_hm3`, `has_rho` and `has_clumps` are carried. The splice rewrites every structure
    SOURCE has;
  - `validate_status` and `completion_state` are set as usual.
- **`build_elapsed_s` is the sum of this release's own steps:** the Increment's build, its
  completion, its Observed-View Check, the splice, and any `validate` the route runs. It is not
  cumulative (ADR 0109 §9). The previous release's cost is one archive away through the lineage,
  so a cumulative figure would count it twice over a chain. `stores.tsv`'s column therefore
  reads as "what producing this release from its inputs cost", which for a build is the whole
  build.
- **The `increments` list gains one entry per publication.** It holds:
  - `increment`, `bundle` (the directory), `reason`;
  - `release_id`, `source_release_id`, `increment_release_id`;
  - `added` (ids), `dropped` (id and reason), and `erratum`;
  - `n_analyses_before` and `n_analyses_after`, `n_variants_before` and `n_variants_after`;
  - the rsID and `source_alid` changes the splice reports (ADR 0109 §7), counted;
  - the warnings accepted as naming only added Analyses;
  - `steps`: each `{step, record, elapsed_s}`, and `elapsed_s`, their sum;
  - the splice's phase timings;
  - `manifest_sha256`, the new `work/analyses.tsv`'s;
  - `archive` and `published_at`.

**The register record** is the publishing phase's (`completion-register` for OGS-00011). Its
`inputs` keep the replaced release's and add the four increment records. It carries the same
`increments` list. The amendment walk ignores the increment records, because their stems are not
Store-writing steps (`amendment.STORE_WRITING_STEPS`). The `build` and `complete` records still
describe the Store's data lineage.

### 8. How the registry shows it *(this ADR)*

- **`stores.tsv` and `STORES.md`.** The Store keeps its row. `n_analyses` is the Validation
  Record's, and `n_manifest` and `n_excluded` come from the folded `analyses.tsv`, so the three
  agree once published. A pending increment changes none of them. A new `release_id` column is
  filled from `observed.release_id` and left empty where no route recorded one. `STORES.md` shows
  it.
- **The Analysis Catalog.** Its membership is the folded `analyses.tsv`'s admitted rows, so the
  catalog changes when the increment publishes, never before. Each kept `<store_id>:<analysis_id>`
  keeps its public id. An added one appears, and a dropped one disappears, with its audit row and
  reason left in the bundle. The catalog's version is a hash of its inputs (ADR 0039), so the
  publication gives a new version, and its report's "added / removed / changed" counts describe
  the increment. Nothing in ADR 0039 or ADR 0041 assumes a validated bundle's membership is
  frozen, so neither needs to change.
- **Queries.** ogquery keys an Analysis on `analysis_id`. A dropped id is refused with
  `UnknownAnalysisError`, never answered empty (ADR 0109 §8b).

### 9. What ADR 0109's interface does not yet give the registry *(this ADR)*

The registry needs four things ADR 0109 does not provide. The route works around each one until
the engine provides it:

- **`info --format json` does not print `provenance.lineage`.** ADR 0109 §12 lists the lineage
  among the "manifest fields the registry can read", but ADR 0023 lets the registry read a Store
  only through `info`. Until `info` prints the last lineage entry, the parent check reads
  `source_release_id` from the splice step's record (its printed JSON), and relies on §6's lock
  and mtime checks. ADR 0043 handles `converted_from` the same way.
- **`splice-release` prints no `validate` verdict.** ADR 0109 §9 says the splice's staged
  `validate` is the release's. The registry needs `validate --format json`'s status, checks,
  warnings and errors, run with the recipe's `--ld-store` pairs. Until the splice prints them,
  the route runs the phase's planned `validate` on the staged release, about 40 min on OGS-00011,
  as ADR 0043 does.
- **The splice's report lacks the post-step counts.** `n_associations`, `n_loci_5e8`,
  `n_unclumped_5e8` and `n_top_hits` are recorded `null` until the report carries them.
- **No command line prints an LD panel's digest.** The registry cannot compute #327's digest
  itself without importing the engine (ADR 0023). It needs an `opengwasdb` command that prints
  the digest, or `--source-record` must accept the panel's path and let the splice compute it.

## Considered options

- **A new Store id per increment.** Rejected by ruling 2: kept `<store_id>:<analysis_id>` ids
  would stop resolving. Each increment would also duplicate a whole bundle under a new id.
- **Re-emit the whole bundle** (#478's path). Rejected:
  - it re-resolves kept rows, and 2,378 changed at #478;
  - it needs the illegal `validated → candidate` move;
  - the emit refuses to replace an accepted bundle;
  - the amendment check cannot tell it from an undeclared rewrite.
- **Append the new rows to `analyses.tsv` at emit, and let the declaration say they are
  pending.** Rejected. The catalog and `n_manifest` read `analyses.tsv` directly (Context), so an
  accepted increment would be served for the hours or days before its publication. Every reader
  would have to learn the declaration, and one that did not would answer wrongly and silently.
- **Append them as excluded "pending" audit rows, and flip them at publication.** Rejected. It
  conflates pending with refused, counts pending rows in `n_excluded`, and still edits rows at
  publication.
- **A per-row `increment` column in `analyses.tsv`.** Rejected. It changes every row's bytes,
  which the amendment comparison would read as every row changed, and it adds a column to every
  bundle's schema.
- **Declare increments in `release.yaml`.** Rejected. `release.yaml` is the accepted identity
  file, a candidate's content does not belong inside it, and the added rows' sidecars need a home.
- **Keep `work/analyses.tsv` as the original build's manifest, and compare membership against a
  fold of it plus the published increments.** Rejected. Every reader of the baseline would need
  the fold: the amendment check, `build_manifest_input` and `register`'s `n_analyses` fallback.
  Rewriting the file at publication keeps one meaning: the live Store's membership.
- **A new Release Status for a bundle being incremented.** Rejected in favour of a status per
  increment, so the bundle never moves backwards.
- **A cumulative `build_elapsed_s`.** Rejected (§7).
- **Allow replace and re-admission now.** Deferred (§2). No increment needs them yet, and they
  break the one-row-per-id assumption of several readers.

## Consequences

- OGS-00011's first increment (#561) is one emit, one owner acceptance, and one
  `publish-increment` run. ADR 0109 §10 estimates the run at about 7–10 h, where a rebuild costs
  40.7 h. The run still needs the Store build gates.
- A defect removal at OGS-00032/33's size is a drop-only increment with an erratum, rather than
  a rebuild.
- An accepted bundle may now change in a fourth way: a published increment folds in. CONTEXT.md
  says so.
- The bundle's row order stays the published release's Analysis order, through every increment.
- Until the engine provides §9's items, the route validates twice, and records the post-step
  counts as `null`.
- A forced rebuild of an incremented release builds the union from scratch and starts a new
  lineage. Its earlier increments stay in the bundle as history.
- Phase 2 of #546 implements:
  - the `Bundle.increments` loader;
  - the increment contract in `bundle.check` (§4);
  - the fold, as a pure function;
  - `increment_changes`;
  - the amendment refusal's new pointer.

  #560 implements the route, the marker's bundle swap, the settling and the `stores.tsv`
  column. The emit's increment mode (§3) is Manifest Generation's, and #561 needs it before
  its bundle can exist. Which ticket owns it is open.
