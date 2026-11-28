# Increment Bundles add Analyses to and remove them from a published Hybrid under its Store id

## Status

Accepted. Issue [#546](https://github.com/opengwas/opengwasdb-stores/issues/546) implements the
Increment Bundle, its contract in `bundle-check`, the emit's increment mode, the source-record
attestation and the amendment check. [#560](https://github.com/opengwas/opengwasdb-stores/issues/560)
implements the route that publishes an increment (§6). OGS-00011's first increment,
[#561](https://github.com/opengwas/opengwasdb-stores/issues/561), is the first to use both. This
is the registry half of epic [opengwas/opengwasdb#508](https://github.com/opengwas/opengwasdb/issues/508).
The engine half is opengwasdb ADR 0109, *Splice: Analyses enter and leave a published Hybrid
Store Release by building small and splicing* (opengwasdb#520).

Where each decision comes from:

- **The owner's rulings**, made by Gibran Hemani (`explodecomputer`) on 28 Nov 2026 and recorded
  on [opengwas/opengwasdb#508](https://github.com/opengwas/opengwasdb/issues/508) ("Owner's
  rulings, 28 Nov 2026"), marked *(owner)*:
  - **Ruling 2, identity.** Keep the Store id, so every kept `OGS-00011:<analysis_id>` resolves.
    Each increment is a new Store Release, with a new `release_id` and a lineage list in the
    manifest. The previous release moves to `replaced/<UTC>/`. A defect removal is also a
    Release Erratum (ADR 0005). ADR 0004 is amended, not superseded.
  - **Ruling 1.** The route is built against format 0.3.0.
  - **Ruling 6.** The first increment adds the studies #557 moves into `hybrid__European`, each
    admitted or refused at resolution with ancestry evidence. GCST90132314 is checked by name.
- **The orchestrator's rulings on this ADR's first review** (28 Nov 2026, in the orchestration
  of #508), marked *(ruling, review 1)*:
  - a warning the replaced Validation Record did not carry refuses, unless the owner accepted
    it by its text;
  - owner acceptance is bound to the reviewed ids and to the bytes of the Increment Bundle;
  - an attested `--source-record`, accepted by the owner, is the only anchor for a fact SOURCE
    did not record;
  - the parent is checked by ADR 0109's content fingerprint, under a lock held only from that
    check to the end of publication;
  - `validate` runs once, inside the splice;
  - the engine supplies `info` provenance, the splice's verdict and counts, and the LD-panel
    digest command (#521, #522).
- **This ADR**, marked *(this ADR)*. Everything else here is this ADR's decision.

What this ADR changes in other documents:

- [ADR 0004](0004-store-release-identity.md), [ADR 0039](0039-analysis-catalog.md) and
  [ADR 0044](0044-missing-post-steps-are-added-to-a-published-store-in-place.md) each gain an
  amendment that points here.
- CONTEXT.md gains **Increment Bundle** and **Splice**. **Release Bundle**, **Release
  Manifest**, **Store Release**, **Release Lineage** and **Release Erratum** change to match.
- `docs/release-metadata-schema.md` documents `increment.yaml`, `source-record.json` and the
  Validation Record's `increments` list.

## Context

A published Store's membership is fixed. Before this ADR the registry could change it only by
rebuilding the Store, or by #478's one-off exception. Under that exception OGS-00011 was
re-emitted over its accepted bundle (3,169 → 3,245 Analyses), and its Release Status went from
`validated` back to `candidate`, a move `bundle.LEGAL_STATUS_TRANSITIONS`
(`src/ogstores/bundle.py:52`) does not allow.

`opengwasdb splice-release` (ADR 0109) is the third way. It takes a published
Reference-Completed Hybrid, SOURCE, and writes a new release that drops the named Analyses and
adds those of an **Increment**. An Increment is a small Hybrid holding the Analyses to add,
built with `build-hybrid --increment-of SOURCE` and completed against SOURCE's panel. A
drop-only splice has no Increment. Each splice appends an entry to the manifest's
`provenance.lineage`. That entry names SOURCE by `source_release_id` and by
`source_fingerprint`, a content digest. The new release stays at format 0.3.0 (ADR 0109 §9).
The splice never writes in place: moving the previous release aside, and deciding whether the
new one may publish, are the registry's jobs.

What the registry holds today, which this design rests on:

- **The Release Manifest is the catalog's membership.** The Analysis Catalog takes a validated
  bundle's admitted `analyses.tsv` rows as its Analyses (`src/analysis_catalog/policy.py:142`).
  It refuses the whole Store when their count differs from `observed.n_analyses`
  (`policy.py:145`). An edit to the manifest that keeps the count, such as swapping one row
  for another, goes unnoticed. `stores.tsv`'s `n_analyses` is the same count, taken from the
  same rows (`src/ogstores/index.py:223`).
- **The catalog's version covers every bundle file.** `publish.compute_version` hashes every
  file in every bundle directory (`src/analysis_catalog/publish.py:95`, `:132`). A file that
  changes no membership still gives a new catalog version.
- **Two sidecars are tied to `analyses.tsv` row for row.** `sidecars/quality_checks.tsv` must
  name each `analyses.tsv` Analysis once, in the same order, and agree on which are excluded.
  `sidecars/exclusions.tsv` must give the same reasons (`bundle.py:1796`, `:1784`).
- **The published membership is two files.** `amendment._membership_changes`
  (`src/ogstores/amendment.py`) checks the derived build manifest the bundle makes now against
  `<root>/<id>/work/analyses.tsv`, byte for byte. It checks the excluded ids against
  `work/analyses.exclusions.json`. The Snakefile treats the bundle's `analyses.tsv` as unchanged
  only when it would materialise to both files as they are on disk
  (`workflow/Snakefile:354`, `manifest.build_manifest_is_current`).
- **An unforced run never rebuilds a published Store.** `run.refuse_rebuilding_published_releases`
  (`src/ogstores/run.py:1121`) refuses one.
- **The emit replaces a whole bundle, and only a candidate** (`emit.py:242`). A re-emit
  re-resolves every row: at #478, 2,378 kept Analyses changed under the newer resolver.
  Without `renameat2`'s exchange, the emit falls back to two renames, and a crash between
  them leaves no bundle at the path (`emit.py:_replace`).
- **The registry sees a Store only through `opengwasdb info --format json`** (ADR 0023).
  #521 makes `info` print `provenance` (and so the lineage) and `n_analyses`. With
  `--fingerprint`, it also prints the release's fingerprint (ADR 0109 §12).
- **An in-place addition keeps the `release_id`.** OGS-00011's post additions kept
  `1459d7e2-48de-486a-ad7a-e6dc8d26f935` although they rewrote Rho, Clumps and the hit counts
  (`stores/OGS-00011/validation.yaml`). The fingerprint is what tells two such states apart.

## Decision

### 1. Identity: the amendment to ADR 0004 *(owner: ruling 2; the conditions: this ADR)*

**A Store id names a sequence of Store Releases.** Each is identified by `(store_id,
release_id)`, and all of them are built from the Store's one Release Bundle. Each release after
the first is spliced from the one before. A kept Analysis keeps its public id,
`<store_id>:<analysis_id>`. Its `analysis_index` is release-scoped (ADR 0109 §2), as the
registry's derived manifest already makes it (ADR 0025).

**A membership change keeps the Store id** when all of the following hold:

1. The bundle is a Reference-Completed Hybrid that completes its own Store (`layout: hybrid`,
   `completion_state: reference_completed`, no `derived_from`). That is the splice's scope:
   OGS-00011, OGS-00032 and OGS-00033.
2. The change is declared in an Increment Bundle that the owner has accepted (§2).
3. It changes no kept Analysis's row and no Build Recipe input. `build.yaml` is unchanged. An
   added Analysis is resolved under the bundle's own release config and resolver settings
   (§3), and built under the release's pins (ADR 0109 §3).
4. It is published through the increment route (§6), which splices.

Two kinds of change qualify:

- **An increment** adds Analyses, and may also drop some.
- **A defect removal** only drops. It is also a **Release Erratum** (ADR 0005). Its Increment
  Bundle carries an `erratum` block, which is part of what the owner accepts.

**Each of the following still needs a new Store id**, as ADR 0004 said before:

- a change to the Build Recipe, or to anything ADR 0109 §3 pins: Variant Reference, reference
  panel, LD panel, encoding, assemblies, rsID policy, EAF-orientation settings,
  `format_version`;
- a change to a kept Analysis's row, including re-resolving kept rows under a newer resolver
  (the re-emit);
- a change to the release config's selection policy: thresholds, Quality Checks, the Store
  Composition Rule;
- any membership change to a Dense, Ragged or Observed-Only Hybrid bundle, which the engine
  does not splice.

`--config force=1` keeps its meaning. It rebuilds a release from its bundle as the bundle
stands, folded increments included, and the lineage starts again from that rebuild.

**How the registry shows the identity.** The `release_id` is the engine's, stamped fresh by each
splice; the registry never invents one. The registry's own ordinal is the Increment Bundle's
number. The lineage shows in three places:

- the Increment Bundle's `published` block;
- the Validation Record's `increments` list, one entry per published increment. The whole chain
  can be read from the current record alone (§7);
- `observed.release_id`.

### 2. The Increment Bundle *(this ADR; acceptance: ruling, review 1)*

An increment is declared in its own directory inside the Release Bundle. **It touches no other
bundle file until the route publishes it.**

```text
stores/<id>/increments/<nnnn>/
  increment.yaml        the declaration, its acceptance, its publication
  analyses.tsv          the rows it adds, in the bundle's exact columns (absent for a drop-only increment)
  sidecars/*.tsv        their per-Analysis evidence, each with the bundle's sidecar header
  source-record.json    the owner-attested facts SOURCE did not record (§6)
```

```yaml
increment: 1                    # the directory's number; numbered from 1 with no gaps
store_id: OGS-00011
status: accepted                # candidate -> accepted -> published; candidate|accepted -> withdrawn
reason: "stores#557: GWAS Catalog studies re-pooled into hybrid__European"
created_at: '2026-11-29T…Z'
generator: {version: …, commands: […]}   # an increment that adds
source_snapshot: {…}                     # an increment that adds, as in release.yaml
require_added: [GCST90132314]   # ids the emit and the route refuse to go without (ruling 6)
drops: []                       # each {analysis_id, reason, defect}
erratum: null                   # {issue, text}; required when a drop is a defect
acceptance:                     # the owner's, bound to these bytes
  accepted_by: Gibran Hemani
  accepted_at: '2026-11-29T…Z'
  reviewed_analysis_ids: [GCST004131, …]   # every id it adds, refuses or drops
  content_sha256: …             # of the Increment Bundle, as defined below
accepted_warnings: []           # each {message, accepted_by, accepted_at}: §6 step 4
published: null                 # written only by the route (§6, §7)
```

**The rows it adds.** These are the increment's `analyses.tsv` rows, in the bundle's columns and
column order. `analysis_index` runs `0..k-1` within the increment.

- An admitted row is an added Analysis.
- An excluded row (`exclude_from_build=true`, with an `inclusion_reason`) is a candidate refused
  at resolution. It joins the bundle as an audit row, with its evidence, which is how ruling 6's
  "each admitted or refused at resolution" is recorded.

**Drops.** A dropped Analysis is never deleted (ADR 0025). At publication its row's
`exclude_from_build` becomes `true`, and its `inclusion_reason` becomes
`excluded: removed_in_increment_<nnnn>: <reason>`. A later increment may drop an Analysis an
earlier increment added.

**Acceptance is bound to bytes** *(ruling, review 1)*. An increment becomes `accepted` only with
an `acceptance` block naming:

- who accepted it, and when;
- `reviewed_analysis_ids`: the ids the owner reviewed;
- `content_sha256`.

The reviewed ids must be exactly the increment's rows' ids plus its drops' ids. For #561 that
is the list in #557's joiners table, each id admitted or refused at resolution.

`content_sha256` is the sha256 of a listing, sorted by path, of `<sha256>  <relative path>` for
every file in the increment directory except `increment.yaml`. The listing has one more line,
`<sha256>  increment.yaml#declaration`, holding the sha256 of `increment.yaml` itself in
canonical JSON, without `status`, `acceptance`, `accepted_warnings` and `published`. A symbolic
link refuses. Any change to the bytes after acceptance, such as an edited row, a sidecar, the
attestation or a drop, breaks the digest, and `bundle-check` refuses the increment until it is
accepted again.

**Status belongs to the increment; the bundle's Release Status stays `validated`.** A bundle has
at most one open increment (`candidate` or `accepted`), so the parent of a splice is never
ambiguous. Increment `n+1` is emitted only once `n` is `published` or `withdrawn`. The bundle
never goes back to `candidate`, unlike #478.

**The fold, at publication.** Publishing increment `n` folds it into the bundle. Only the route
does this (§6). Folding changes these files and nothing else:

- `analyses.tsv`: the increment's rows are appended, with `analysis_index` continuing the
  bundle's. The two exclusion columns of each declared drop are set.
- each `sidecars/*.tsv` keyed by `analysis_id`: the increment's rows are appended. Each drop's
  exclusion columns are set in `quality_checks.tsv`, and its row is appended to
  `exclusions.tsv`. A sidecar whose header differs from the bundle's is refused;
- `summary.yaml`, which is regenerated;
- `increment.yaml`, which becomes `published` with its `published` block.

The same publication writes `validation.yaml` (§7). It also writes the derived
`work/analyses.tsv` and `work/analyses.exclusions.json` under the Artifact Root, both
materialised from the folded bundle. Those two files are not bundle files. Release notes are
not part of the fold: an erratum is in the accepted `increment.yaml`, and the publication copies
it into the Validation Record's `increments` entry.

**The fold keeps the order the splice gives.** After the fold, the derived build manifest holds
the kept Analyses in their old order, then the added ones in the increment's order. That is ADR
0109 §2's `analysis_map`. So a `--config force=1` rebuild of an add-only lineage orders its
Analyses as the spliced release does.

**A replace, and re-admitting an existing audit row, are refused for now.** Either would put
one `analysis_id` on two `analyses.tsv` rows. Every per-id reader assumes one row per id:
`_exclusion_reasons`, the quality-checks pairing, `amendment._manifest_rows`, and the
resolver's per-Analysis filename. #561 needs neither: GCST008970 was never an OGS-00011 member
(ADR 0109, Context). The engine already supports a replace in one splice, so the bundle can
admit one once those readers are audited.

### 3. Emitting an increment: rows are added, existing rows are never re-rendered *(this ADR)*

Manifest Generation's increment mode writes only `increments/<nnnn>/`.

- **Selection.** `generate-candidate <id> --increment` reads the bundle's own release config
  and a new frozen Source Inventory. Its candidates are the selected Analyses whose
  `analysis_id` is in neither the bundle's `analyses.tsv` nor an earlier increment. Rows already
  in the bundle, admitted or excluded, are neither resolved nor rendered again.
- **Resolution.** Only those candidates are resolved, under the locked engine. The increment
  records its resolver version and settings digest. Where the resolver differs from the
  bundle's, the Validation Record's resolver-mismatch warning names both.
- **Render.** Quality Checks, the Assigned Ancestry check and the Store Composition Rule run per
  Analysis, as for any candidate.
- **Emit.** The increment is staged under `stores/.staging/`, held to the increment contract
  (§4) together with its bundle, and moved into `increments/<nnnn>/` as `candidate`, under the
  emit's per-id lock. The emit refuses:
  - a bundle that is not `validated`, or not in §1's scope;
  - a bundle with an open increment;
  - a `require_added` id that is not an admitted row;
  - a row whose `checksum` equals a bundle row's, which would be the same file under another id.
- **A drop-only increment** is written by `python3 -m manifest_generation.emit <id> --increment
  --drop ID=REASON [--defect --erratum-issue … --erratum-text …]`. Nothing is resolved.

The emit never writes over an increment past `candidate`. The bundle's own emit still refuses to
replace anything but a candidate. Acceptance is a human edit of `increment.yaml`: the owner sets
`status: accepted` and writes the `acceptance` block. `bundle-check` verifies it.

### 4. `bundle-check`: what the contract accepts *(this ADR)*

`bundle.check` applies the increment contract, so `pixi run bundle-check` and the emit both
enforce it. For each `increments/<nnnn>/`:

- **The declaration.**
  - Keys and types are valid, and `increment` equals the directory number. Numbers run from 1
    with no gaps.
  - `status` is in the vocabulary. With a previous state given, the transition is legal.
  - `acceptance` is complete, and its `content_sha256` is the increment's, from `accepted` on.
  - `reviewed_analysis_ids` equals the increment's row ids plus its drop ids.
  - `published` is set exactly when `status: published`.
  - `erratum` is present when a drop is a defect.
  - At most one increment is open.
  - The bundle is in §1's scope.
- **The rows.**
  - The increment's `analyses.tsv` has the bundle's columns, in order.
  - It passes the bundle's Analysis checks: an admitted row must be buildable, and an excluded
    row needs an `inclusion_reason`.
  - Its ids are unique.
  - Until the increment is published, its ids are absent from the bundle and from earlier
    increments.
  - Each `require_added` id is an admitted row.
  - Each of its sidecars has the bundle's header and one row per increment row, in order.
- **The drops.** For an open increment, each drop names an Analysis admitted in the bundle,
  with a reason. A drop of an Analysis the same increment adds refuses.
- **The attestation.** `source-record.json` is present from `accepted` on, with every key §6
  names and a non-empty `attested_by`.
- **A published increment is folded.**
  - Each of its rows appears in the bundle's `analyses.tsv`, in its order, after the rows of
    every earlier increment. Each is identical except for `analysis_index`, and except for the
    two exclusion columns of a row that a later published increment dropped.
  - Each of its drops is excluded with its `removed_in_increment_<nnnn>` reason.

### 5. The amendment check: a declared increment passes, an undeclared change refuses *(this ADR; ruling, review 1)*

**The published membership is still the pair of `work/` files.** Each publication rewrites
both, from the folded bundle, and records their sha256 in the increment's `published` block. So
the existing comparison holds for every route: ADR 0037's own-id completion, ADR 0044's
additions and a forced run. The comparison is the derived manifest's bytes and the excluded ids.
With published increments, `_membership_changes` adds one check: the `work/` files must have
the sha256 the last published increment recorded. A file rewritten outside the route is
refused, so the comparison never trusts a baseline that no publication wrote.

- **An open increment passes.** It touches no file the check reads. Another route may publish
  while it is open; the parent check (§6) then refuses the stale splice.
- **The exclusion changes a published increment declared pass.** Its appended rows, its refused
  candidates and its drops all match the regenerated `work/` files. That includes a later
  increment's drop of an earlier increment's addition.
- **An undeclared change refuses.** A re-emit, a hand-added row, a hand-set exclusion, or a kept
  row's changed columns all make the bundle's derived files differ from `work/`. The refusal
  names the rows. It says that a membership change under this id is an Increment Bundle
  (ADR 0045), and that `--config force=1` rebuilds.

**`increment_changes(b, n, root)` is the route's check.** The route runs it after recovery, and
again before the marker. It refuses unless:

1. `published_store_changes(b)` is empty, so the bundle still plans the published Store;
2. increment `n` is the bundle's one open increment, and it is `accepted` with a valid digest;
3. folding `n` gives a bundle that passes `bundle.check`;
4. the folded bundle's derived files differ from the published `work/` files by exactly the
   increment's declaration. That means its admitted rows appended, its refused rows and its
   drops newly excluded, and nothing else;
5. given the splice's report: its `added` and `dropped` equal the declared sets exactly, and its
   `n_analyses_after` equals the folded manifest's row count.

The check is a pure function of the bundle, the increment, the published `work/` files and the
splice's report. It opens no Store (ADR 0023).

### 6. The route, for #560 *(this ADR; ruling, review 1)*

One entry point, on the shared lifecycle `staged_publication` (ADR 0043, ADR 0044):

```text
pixi run publish-increment <ID> [--increment N] [--dry-run]
```

1. **Recovery first.** Under the release's run lock, a pending marker for the Store is finished
   before anything else is read. The amendment check, and `increment_changes`, come after it.
   `release._run` today checks amendments before recovery, so the route orders them itself.
   Then the lock is released.
2. **Preflight**, before anything is written:
   - the usual refusals: Release Status, leftovers (`refuse_leftovers`), the locked runtime;
   - the published Store is `info`'s 0.3.0;
   - `increment_changes` holds;
   - `source-record.json` is accepted with the increment.
3. **The steps run without the lock**, each through `run.execute_step`, with records
   `records/increment-<nnnn>-{build,complete,observed-view-check,splice}.json`. Each record is
   held to its planned argv, and the splice runs `--plan` first.
   - The Increment builds from `work/increments/<nnnn>/analyses.tsv`, the increment's
     materialised manifest.
   - The Increment, its completion and its check are skipped for a drop-only increment.
   - The splice's destination is `store.opengwasdb.partial`. Its `--source-record` is the
     increment's `source-record.json`, and its `--ld-store` pairs are `build.yaml`'s
     `post.ld_stores`.

   The run takes 7–11 h (ADR 0109 §10), and the lock is not held for it. The fingerprint is
   what makes that safe.
4. **The verdict.**
   - `increment_changes` item 5 holds on the splice's report.
   - Every `require_added` id is in its `added` and admitted in the fold. That is ruling 6's
     GCST90132314.
   - **The validation is the splice's verdict.** The splice prints `validate --format json`'s
     verdict, run with the `--ld-store` pairs, and the route runs no second `validate`
     (ADR 0109 §9, §12).
   - An error refuses. A warning carried unchanged from the replaced Validation Record is
     accepted, by exact message (ADR 0043, amended 25 Nov).
   - **Any other warning refuses**, unless `increment.yaml`'s `accepted_warnings` records the
     owner's acceptance of that exact message. The verdict carries strings, not ids, so no
     warning is attributed to added Analyses by parsing. Adding an accepted warning is a
     human edit outside the digest, made after a refused run.
5. **The parent-release check, under the lock from here to the end of publication.** The route
   takes the release's run lock again and finishes any pending marker. It refuses unless the
   published Store's `release_id` and fingerprint (`info --fingerprint`) equal the staged
   release's `lineage[-1].source_release_id` and `source_fingerprint` (`info` on the staged
   release, ADR 0109 §9). An ADR 0044 addition published meanwhile keeps the `release_id` but
   changes the fingerprint, so it is caught. The operator then re-runs the splice against the
   new published release.
6. **Publication, crash-safe** *(ruling, review 1)*. The marker is written, then
   `register.complete_publication` carries it out. The marker gains:
   - the staged bundle directory, `stores/.staging/<id>.increment-<nnnn>/`: the whole folded
     bundle, `validation.yaml` included. It is held to `bundle.check` before the marker;
   - **for every bundle file and both `work/` files, the sha256 before and after.** These are
     durable, so a replay can tell for each file whether it is old (write it), new (done) or
     neither (stuck: refuse and keep the marker);
   - the old `work/` files' archive path, under `replaced/<UTC>/work/`.

   The bundle is replaced as a whole directory, by `renameat2(RENAME_EXCHANGE)` only. A
   filesystem that cannot exchange is refused before the marker, never sent through the emit's
   two-rename fallback. The swap code moves from `manifest_generation.emit` into `ogstores`,
   because Store Release Build imports nothing from Manifest Generation (ADR 0035). The order
   is:
   1. the Store swap and archive, as now;
   2. the bundle exchange;
   3. the two `work/` files;
   4. the register record;
   5. the marker removed, last.

   Recovery replays from the marker, under the lock, before anything else. A bundle or `work/`
   file whose sha256 is neither the old nor the new one refuses. **Each boundary has a fault
   test**: after the marker, after the Store swap, after the bundle exchange, after each
   `work/` file, after the register record. Each goes in the release-faults suites, with the
   refusals.
7. **Snakemake settling.** The new `work/` files are newer than `records/build.json`. By mtime,
   Snakemake would schedule the build, and the unforced run would be refused. So the Snakefile
   makes the manifest→build edge `ancient` for a release whose last publication is an
   increment, and whose `work/` files have the sha256 that increment recorded. A forced run is
   never settled.
8. **The Increment's Stores**, under `<root>/<id>/increments/<nnnn>/`, are removed after
   publication, as a replaced Observed-Only Store is (ADR 0037). Their records stay in
   `records/`.

**The attestation, `source-record.json`** *(ruling, review 1)*. `python3 -m ogstores.source_record
<id> --increment N --attested-by NAME --variant-reference-sha256 HEX --ld-panel-digest DIGEST`
writes it into an open increment. It reads the published release's records:

- `rsid_conflict_policy`: `records/build.json` argv `--on-rsid-conflict`. It must equal
  `validation.yaml`'s `observed.rsid_conflicts.policy` (OGS-00011: `drop` and `drop`);
- `source_assembly`: the **effective** assembly of every admitted row of `work/analyses.tsv`.
  That is the row's `source_assembly` where the column exists, and otherwise the build's
  `--source-assembly`. One assembly for every row is required, consistent with each row's
  `source_genome_build`. A default flag alone is never taken as a per-row record;
- `reference_panel_sha256`: explicitly `null` when the build took no `--reference-panel`, as
  for OGS-00011. A build that took one is refused until a digest is attested;
- `indexed_subsets`: each `completion-indexed-subset-<name>.json`'s `--variant-list` and
  `--overflow-only`;
- `variant_reference_sha256` and `ld_panel_digest`: **the owner's attested values**, given on
  the command line. The LD panel's comes from `opengwasdb ld-panel-digest PATH --ancestry …`
  (#522). A path, an mtime or a digest taken now is not proof of what the build read, so the
  writer never computes these itself. It records the recorded paths beside them;
- `attested_by`, and `evidence`: for each key, the record and field it was read from, or "owner
  attestation".

It refuses without `--attested-by`, when a record is missing, when the policies disagree, and
when rows' assemblies differ. The file is inside the increment, so the owner's acceptance digest
binds it.

### 7. What is recorded *(owner: ruling 2's lineage; the fields: this ADR)*

**The Validation Record** is a republication (`staged_publication.republication`). Every block
the replaced record had is carried (`acceptance`, `rewrite`, `post_additions`), and this run
changes the following:

- `status`, `checks`, `warnings` and `errors` are the splice's verdict (§6 step 4).
- `build_environment` names the splice's engine. Its `engines` list adds the increment steps.
- `observed`:
  - `release_id` comes from `info`, `format_version` from `info`, and `store_bytes` from a stat;
  - `n_analyses` is `info`'s, which must equal the splice's `n_analyses_after` and the folded
    manifest's admitted count;
  - `n_variants` is the splice's `n_variants_after`;
  - `n_associations`, `n_top_hits`, `n_loci_5e8` and `n_unclumped_5e8` are the splice's
    post-step counts (ADR 0109 §12);
  - `has_hm3`, `has_rho` and `has_clumps` are carried, because the splice rewrites every
    structure SOURCE has;
  - `validate_status` and `completion_state` are set as usual.
- **`build_elapsed_s` is the sum of this release's own steps:** the Increment's build, its
  completion, its Observed-View Check and the splice. It is not cumulative (ADR 0109 §9). The
  previous release's cost is one archive away through the lineage, so a cumulative figure
  would count it again at every increment.
- **The `increments` list gains one entry per publication**, with:
  - `increment`, `bundle`, `reason`;
  - `release_id`, `source_release_id`, `source_fingerprint`, `increment_release_id`;
  - `added`, `dropped` (each id with its reason), and `erratum`;
  - `n_analyses_before` and `n_analyses_after`, `n_variants_before` and `n_variants_after`;
  - the count of rsID and `source_alid` changes (ADR 0109 §7);
  - `accepted_warnings`;
  - `steps` (each `{step, record, elapsed_s}`), and `elapsed_s`;
  - the splice's phase timings;
  - `manifest_sha256` and `exclusions_sha256`;
  - `archive` and `published_at`.

**The register record** is the publishing phase's (`completion-register` for OGS-00011). Its
`inputs` keep the replaced release's and add the increment records. It carries the same
`increments` list. The amendment walk ignores the increment records, because their stems are
not Store-writing steps (`amendment.STORE_WRITING_STEPS`). The `build` and `complete` records
stay as the Store's data lineage.

### 8. How the registry shows it *(this ADR)*

- **`stores.tsv` and `STORES.md`.** The Store keeps its row, and **the column definitions do not
  change.** `n_analyses`, `n_manifest` and `n_excluded` stay counts of the bundle's
  `analyses.tsv`. A pending increment is outside that file, so it changes none of them, and the
  fold changes them at publication. The route refuses before the marker unless the folded
  admitted count equals `observed.n_analyses`. A mismatch that arises any other way is refused
  where it is read: the catalog refuses the Store (`policy.py:145`), as today.
- **The Analysis Catalog.** Catalog *artifact* publication and catalog *membership* are
  separate. Every bundle file feeds the version hash, so emitting or accepting an increment
  gives a new catalog version with unchanged membership. Membership changes only when the
  fold lands. After the fold:
  - every kept `<store_id>:<analysis_id>` resolves to the same row;
  - an added Analysis appears;
  - a dropped Analysis's identifier no longer resolves in the catalog. Its audit row and reason
    stay in the bundle, and ogquery refuses the id with `UnknownAnalysisError`. ADR 0039's
    "names the same row forever" promise is amended for drops (its amendment).

  The report's "added / removed / changed" counts describe the increment.
- **Queries.** ogquery keys an Analysis on `analysis_id`. An index taken from an earlier
  release is refused by its `release_id` guard (ADR 0109 §2).

### 9. ADR 0044 after a splice *(this ADR)*

ADR 0044's additions work on an incremented release, since the splice keeps it at 0.3.0. Two of
its rules are qualified:

- **Settling.** A spliced release's post-step records (`completion-rho`, `completion-clump`,
  `completion-overview`, the Indexed Variant Subset) were written for its parent. The splice
  rebuilt each of those structures, and the increment entry records that it did. So ADR 0044
  still counts those records as current evidence for its plan. ADR 0044's settled edges apply
  to the spliced release as they did to its parent, and §6 step 7 settles one more edge.
- **The rewrite-warning gate** (`post_additions._judge_rewrite_warnings`) holds a rewritten
  Store's warnings to its `rewrite` block. After a splice, the `rewrite` block is carried as
  history. The gate also accepts a warning that appears in a published increment's
  `accepted_warnings`, by exact message. It accepts no other new warning.

## Considered options

- **A new Store id per increment.** Rejected by ruling 2: kept `<store_id>:<analysis_id>` ids
  would stop resolving, and every increment would copy a whole bundle under a new id.
- **Re-emit the whole bundle** (#478's path). Rejected. It re-resolves kept rows (2,378 changed
  at #478), needs the illegal `validated → candidate` move, and the emit refuses to replace an
  accepted bundle. The amendment check also cannot tell it from an undeclared rewrite.
- **Append the new rows to `analyses.tsv` at emit, and let the declaration say they are
  pending.** Rejected. The catalog would refuse the Store while the counts disagree. If a
  pending add and a hand drop kept the count equal, it would serve the pending row. Every
  reader would need to learn the declaration.
- **Append them as excluded "pending" audit rows, and flip them at publication.** Rejected. It
  conflates pending with refused, counts pending rows in `n_excluded`, and still edits rows at
  publication.
- **A per-row `increment` column in `analyses.tsv`.** Rejected. It changes every row's bytes,
  and it adds a column to every bundle's schema.
- **Declare increments in `release.yaml`.** Rejected. `release.yaml` is the accepted identity
  file, and the added rows' sidecars need a home.
- **Keep the original build's `work/` files, and fold the published increments over them in the
  check.** Rejected. Every reader of the baseline would need the fold: the amendment check,
  `build_manifest_input` and `register`'s `n_analyses` fallback. Rewriting the files at
  publication, with their sha256 recorded, keeps one meaning: the published Store's membership.
- **Hold the run lock for the whole 7–11 h run.** Rejected. It would block every other route on
  the Store for that long. The fingerprint checked under a short lock gives the same guarantee.
- **Re-validate in the registry.** Rejected. ADR 0109 has the splice print its verdict, and
  `validate` runs once.
- **Attribute new warnings to added Analyses by their text.** Rejected. The verdict's warnings
  are strings, so attribution would be parsing. The owner accepts each one by its text.
- **Accept fresh digests, or paths and mtimes, as pins.** Rejected. They show what is there now,
  not what the build read. The owner attests instead.
- **A new Release Status for a bundle being incremented.** Rejected in favour of a status per
  increment, so the bundle never moves backwards.
- **A cumulative `build_elapsed_s`.** Rejected (§7).
- **Allow replace and re-admission now.** Deferred (§2).

## Consequences

- OGS-00011's first increment (#561) is one emit, one attestation, one owner acceptance bound to
  its 89 reviewed ids and its bytes, and one `publish-increment` run under the Store build
  gates.
- A defect removal at OGS-00032/33's size is a drop-only increment with an erratum, rather than
  a rebuild.
- An accepted bundle may change in a fourth way: a published increment folds in.
- The bundle's row order stays the published release's Analysis order through every
  increment.
- The route depends on #521 (`info` provenance, `n_analyses` and `--fingerprint`; the splice's
  verdict and counts) and #522 (`ld-panel-digest`). It refuses rather than proceed without
  them.
- A forced rebuild of an incremented release builds the union from scratch and starts a new
  lineage. The earlier increments stay in the bundle as history.
