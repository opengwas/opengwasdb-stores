# LD Store Registry

The specification of the LD Store Registry's records, their writer and the
index generated from them (#536, epic #496). The decision behind it is
[ADR 0042](../adr/0042-ld-store-registry.md); the words are
[CONTEXT.md](../../CONTEXT.md)'s. This file is the contract.

The registry is the package `src/ld_registry/`. It imports from `ogstores`
(`store_info.run_engine_json` and `store_size.store_size_on_disk`); nothing in
`ogstores` imports it (`tests/packaging/test_boundary.py`). It looks inside an
LD Store only through the engine's command line, `opengwasdb ld info`
(ADR 0023), and the index never looks at one.

## Where things are

```text
stores/ld/OGL-00001/record.yaml   one LD Store record per LD Store
ld-resources.tsv                  generated: one row per record
STORES.md                         generated: its "LD resources" section
```

- **An id** is `OGL-` and five digits, assigned once, in order, and never
  reused (ADR 0022's rule for `OGS-`). It is the record's directory name.
  OGL-00001 to OGL-00018 are taken by #496's draft, which later tickets keep.
  OGL-00013 to OGL-00018 are the LD panels' (below).
- **`stores/ld/` holds only `OGL-NNNNN/` directories**, each holding its
  `record.yaml`. Anything else there, a file, a symbolic link or a directory
  not named by an id, is an error that names it.
- **A record** is a YAML mapping, `stores/ld/<ld_id>/record.yaml`. A record that
  is a symbolic link is an error.
- **`ld-resources.tsv`** sits at the repository root beside `stores.tsv`.

## The record

A record is written only from an existing Store's provenance. Each field has
one writer. A **person** writes what the Store cannot know; the **writer**
(`python3 -m ld_registry write`) writes what it reads from the Store. The
writer never changes a field a person wrote, and a person never edits a field
the writer owns.

| field | written by | required | value |
|---|---|---|---|
| `ld_id` | person | yes | the record's id, equal to its directory's name |
| `kind` | writer | yes | `ld_store` or `genotype_store`, inferred from the provenance; below |
| `label` | person | yes | a display name; nothing resolves a record by it |
| `status` | person | yes | one of the statuses below |
| `default` | person | no | `true` on the one `served` record that is its ancestry's default; absent otherwise. Only `true` is valid, `served` is the only status that may carry it, and at most one `served` record per `provenance.ancestry` may (#537; it feeds ogdb-api#195) |
| `licence` | person | yes | the data's licence, never empty. `to confirm` is valid, and is shown as written, while a licence is undecided. An imported matrix's must be its description's (below) |
| `used_for` | person | yes | what the Store is used for |
| `issue` | person | no | the issue that built it, such as `ogld#7` |
| `notes` | person | no | anything else a reader needs |
| `unrecorded_targets` | person | no | targets of the Store's links that no record describes, each a `path` (absolute) and its `reason`; see the index's links check |
| `rebuild` | the build (through the writer's `--rebuild`), or a person for a Store built before the registry | yes | how the Store is rebuilt; below |
| `location` | writer | yes | the Store's absolute path, as the writer was given it |
| `provenance` | writer | yes | the Store's own provenance; below |
| `provenance_sha256` | writer | yes | the sha256, in lower-case hex, of the provenance as `opengwasdb ld info` prints it, in canonical form: `json.dumps(info, sort_keys=True, separators=(",", ":"))`, encoded as UTF-8 |
| `measured.store_bytes` | writer | yes | the bytes of every regular file under `location`, from an `lstat` of each (`ogstores.store_size.store_size_on_disk`). No link is followed, to a file or a directory, so an overlay's bytes are its own files alone; an unreadable directory is an error, never skipped. A Genotype Store's is its sidecar alone |
| `measured.links` | writer | no | every symbolic link under `location`, by its path in the Store (such as `near` or `variants/x`), and its target with every link resolved (an absolute path). The walk follows no link (`ogstores.store_size.walk_no_follow`, the walk `store_bytes` takes); a dangling link is refused, and an unreadable directory is an error, never skipped. Written when the Store has any; such a Store is an overlay |
| `measured.genotype_bytes` | writer | a Genotype Store | the bytes of the pgen, psam and pvar files its provenance pins, from a stat of each; below. An LD Store has none |

Any other key is an error, so a misspelt field is refused rather than ignored.

**Checksums.** The record carries the digest of the Store's provenance, not of
its files (owner, 25 November 2026, in the review of #536). The integrity of
the Store's files is `opengwasdb ld validate`'s job.

### `kind` and a Genotype Store

`opengwasdb ld info` prints no kind, so the writer infers it from the
provenance (genotype-store.md, "Container and shared data"): a Genotype
Store's provenance carries `filesets`, `inputs` and `arrays`, and no LD Store's
carries any of the three. A provenance with all three is a `genotype_store`,
one with none is an `ld_store`, and one with any but not all is neither kind,
and the writer refuses it.

A Genotype Store's `location` is its sidecar; the genotypes live in the pgen,
psam and pvar files that `filesets` pins, each with a path relative to the
sidecar and a recorded size. The writer stats each one, resolved against
`location`, and `measured.genotype_bytes` is their sum. A pin that is
malformed (a missing pgen, psam or pvar, or a pin without `path` or `size`), is
absolute, does not resolve, or whose size is not the pin's, is refused, naming
the fileset and pin.

The index requires `measured.genotype_bytes` of a `genotype_store` record, and
refuses it on any other kind, naming the record and the field.

### `rebuild`

What rebuilds the Store, so it can be rebuilt from the record (#541). It is
written by the build that made the Store, through the writer's `--rebuild`
option, or, for a Store built before the registry, by a person from what
actually built it. It is not a Release Bundle's Build Recipe.

| field | value |
|---|---|
| `repository` | the repository the commands run in, such as `opengwas/ogld` |
| `version` | the version of that repository they run at. One the recipe writes names a commit, as `git describe` does: it refuses a checkout with uncommitted changes or untracked files. A person writing it for a Store built before the registry names every release that ran, and what each did, when more than one did, as OGL-00012's does |
| `script` | the script the commands run, relative to the repository; for a build run through the engine's command line, the subcommand, such as `opengwasdb ld build --genotypes` |
| `commands` | the commands, in order, as run from the repository's root |
| `inputs` | optional: each file the commands read, by the engine's name for it (the option `--<name>` the commands give it), as its absolute `path` and its `sha256` in lower-case hex. An import records its four (`metadata`, `variants`, `chain`, `fasta`). The writer pins each `sha256` from the Store's own `source.sha256`, and checks each `path` against the commands (below; #542) |

The first four are required. `commands` holds only commands that can be run; a step
known only as prose, such as one reconstructed for a Store built before the
registry, goes in the record's `notes`, naming its source.

**A runnable `rebuild`** is one whose every command is the engine's:
`opengwasdb ld build`, `opengwasdb ld import` or `opengwasdb ld validate`,
after an optional `pixi run -e <environment>`, with exactly one build or
import, of the record's `location`. `ogld <subcommand>`, the engine's former name, is read as
`opengwasdb ld <subcommand>` and runs at the engine the lock pins. A command
holds no shell syntax (`&&`, `;`, `|`, a redirection): each runs as one argv.
OGL-00006's `rebuild` runs `ogld derive-partners`, so it is a record of what
built the Store, and not runnable.
OGL-00001 to OGL-00005's runs `scripts/build_hgdp1kgp.py`, and is not runnable
either; but the obstacle to rebuilding them is the engine. They were built at
ogld 0.0.1.dev292 without Partner Rows (their provenance has no `t_partner`),
and the engine the lock pins requires `--t-partner`, so no build today makes
an LD Store equal to them; their records' `notes` say so. OGL-00007 to
OGL-00011's `rebuild` is runnable. A Genotype Store's rebuild copies its staged
inputs from the original's directory, so it needs that directory to exist;
their `notes` say so too.

OGL-00012's `rebuild` is runnable too (#542): #10's own `ogld import` and
`ogld validate`, which run as `opengwasdb ld import` and `validate` by the
rule above, with its four inputs and their digests. Its `repository`,
`opengwas/ogld`, and `version` are what made it: the import ran at 0.8.0
until chr15 was refused (ogld#102), and the same command, resumed at 0.8.1,
re-planned chr15, closed chr15 to chr22, joined and validated, so `version`
names both releases and what each did. One thing differs from what ran: #10
wrote the Store in scratch and moved it to its `location` on the same
filesystem, so the commands name the `location` and a work directory beside it
(`<dir>/work`, which never existed), which a rebuild moves with the Store. Its
`notes` say so, and give the run's logs and where the variant table came from.

### `provenance`

Copied from `opengwasdb ld info <location>`'s JSON. A record indexes the
provenance and carries its digest; it does not copy all of it, so the source
checksums, the variant filters and the PC checks stay in the Store.

| field | from `ld info` |
|---|---|
| `source_kind` | `source.kind` |
| `source_description` | `source.description`; for an imported matrix, the object below |
| `source_panel` | `panel.id` |
| `ancestry` | `panel.ancestry` |
| `n` | `panel.n` |
| `k` | `adjustment.k` |
| `reference_assembly` | `reference_assembly` |
| `format_version` | `format_version` |
| `r_scale` | `encoding.r_scale` |
| `near_bp`, `far_bp`, `t_far` | the same names |
| `t_partner` | `t_partner`, when the Store records one |
| `n_variants` | `n_variants` |
| `counts` | `counts`, as the engine records it (optional) |
| `variants_dropped` | `variants_dropped`: the variants dropped, per loss class, as the engine records it (optional) |
| `software` | `software`, as the engine records it; `software.ogld` is required |
| `created_at` | `build.created_at`, an ISO 8601 date and time in UTC, such as `2026-11-13T04:27:31Z` |
| `wall_seconds` | `build.wall_seconds` |

Every field but `t_partner`, `counts` and `variants_dropped` is required. A
required one `ld info` does not print is refused by the writer, never written
as empty.

**An imported matrix** (`source_kind: ld_matrix`, `ogld import`) prints its
`source.description` as a JSON object in text (ogld's build-inputs.md §12,
"Published LD import inputs"). The writer records it as that object; one that
is not a JSON object is refused. The kind fixes the shape: an `ld_matrix`
record's `source_description` must be this object, and any other kind's must
be text; a record of the wrong shape is refused, naming the field. Its fields:

| field | required | value |
|---|---|---|
| `url` | yes | the published matrix |
| `licence` | yes | the source's licence |
| `producer_commit` | yes | the producer's commit |
| `transformation` | yes | how covariance became r |
| `liftover_chain` | yes | the chain that lifted it to `reference_assembly` |
| `qc` | yes | the published QC, which the import does not reapply |
| `far_bp_by_chromosome` | yes | each chromosome's radius after liftover; `far_bp` is their minimum |
| `far_target_bp`, `displaced_cap` | no | the import's radius target and displaced-segment cap |
| `displaced_by_chromosome` | no | the variants each chromosome's displaced segments lost |

Any other field is refused, naming it, so the spec grows with the engine's
description rather than letting a field pass unread.

**An import's licence is its source's.** For `source_kind: ld_matrix`, the
record's `licence` must equal `provenance.source_description.licence`;
otherwise the record is refused, naming both values.

### Status

| status | meaning |
|---|---|
| `served` | the Data API serves it |
| `built` | built and checked, not yet served |
| `retiring` | still served; its retirement is decided and waits on what the record's notes name |
| `reference` | not served; kept to check other Stores' answers against |
| `retired` | neither served nor kept |

A Store still building has no record: its provenance does not exist yet.

## The writer

```text
pixi run -e dev python3 -m ld_registry write <ld_id> <location> [--ld-root stores/ld]
```

- It runs `opengwasdb ld info <location>` and writes the writer's fields into
  `stores/ld/<ld_id>/record.yaml`, creating it if it does not exist. Every other
  field is kept as written. Comments are not kept: the writer writes its own
  header.
- **A published Store never changes, so a rewrite must find the same Store.**
  Once a record holds a Store's provenance, the writer refuses a Store whose
  `kind`, `location`, any `provenance` field or `provenance_sha256` differs
  from the record's, naming the record and each field. Such a Store is another
  Store, and it needs a record of its own. A refused write changes nothing.
- **A writer value that breaks the spec is refused the same way**, naming the
  record and the field, before anything is written: a malformed value from
  `ld info`, such as a `created_at` that is not a UTC time, leaves no record,
  or leaves the existing one byte for byte as it was. The Store's provenance
  never changes, so no rewrite could mend it.
- **A Store's links never change either.** A rewrite whose `measured.links`
  differ from the record's, a link added, removed or retargeted, is refused,
  naming each `measured.links.<path>`.
- `measured.store_bytes` may change on a rewrite; the writer prints the old and
  new values when they differ.
- **A field the spec has added since a record was written** is filled in on a
  rewrite when the record's `provenance_sha256` is the Store's: the same
  digest is the same provenance, so the new field is no contradiction. Under
  another digest it is one, and the rewrite is refused as above. The same
  holds for `measured.links`: a record written before links were measured,
  one whose `measured` has no `links`, gains them on a refresh under the same
  digest. Because a record without links holds no `links` either, a link that
  appears in such a Store is recorded on a refresh, not refused; once a
  record holds `links`, a changed link is refused.
- **`--rebuild <file>`** gives the record its `rebuild`, a YAML mapping, as
  the build that made the Store writes it. It is validated with the writer's
  fields, and refused the same way, naming each `rebuild.*` field, when one is
  missing or malformed. A record that already holds a `rebuild` keeps it: a
  different one is refused, naming each field that differs, so a rewrite
  never changes it silently. Without the option, a `rebuild` a person wrote is
  kept.
- **The writer pins `rebuild.inputs` from the Store (#542).** It reads the
  Store's `source.sha256` through `opengwasdb ld info` and
  `provenance.source_sha256`, the one reader of that field, which the recipe's
  check of a Genotype Store's staged copies also uses. For each
  input of the `rebuild`, given or kept, it writes `sha256` as the digest the
  Store holds of `<name>`; a digest already written must be that one. An input
  the Store holds no digest of, or whose digest differs, is refused, naming
  `rebuild.inputs.<name>.sha256`. Each `path` must be the one value the
  commands give `--<name>` (`--<name> <path>` or `--<name>=<path>`); a path
  they give another value, or never read, is refused, naming
  `rebuild.inputs.<name>.path`. Each refusal comes before anything is written.
- It then checks the record and exits 1, naming each field, while the record
  breaks the spec: a new record does so until a person has written its fields.

## The recipe

`tools/ld-stores/ld_stores.py` (`pixi run ld-stores`) builds the HGDP+1kGP
LD Stores and Genotype Stores, through the engine's command line alone
(ADR 0023), and writes their records. It moved here from opengwasdb's
`scripts/ld/build_hgdp1kgp.py build` and ogld#92's `build92.sh` (#541). The
plink2 conversion that makes its inputs stays in opengwasdb
(`build_hgdp1kgp.py convert`, build-inputs §3).

```text
pixi run ld-stores build --kind ld_store|genotype_store --panel AMR --out <dir> --ld-id OGL-NNNNN
```

- **Its inputs** are a panel's pgen fileset per chromosome
  (`$OPENGWASDB_LD_PGEN_V2/<PANEL>/chr<N>.{pgen,psam,pvar}`), its keep file
  (`$OPENGWASDB_LD_KEEP/<PANEL>.keep`) and its PC File
  (`$OPENGWASDB_LD_PCS/<PANEL>.eigenvec`), each overridable by a flag.
- **Its flags are the Genotype Stores'** (OGL-00007 to OGL-00011), from their
  provenance and `build92.sh`: `--k 10 --k-min 5 --r-scale 511 --t-partner
  0.01 --mac-min 20 --info-min none`, the panel id `hgdp1kgp-v2-<PANEL>`, and
  the source and PC source texts their provenance records. The LD Stores
  OGL-00001 to OGL-00005 share every flag but `--t-partner`: they were built
  without Partner Rows, which the engine now requires, so an LD Store this
  recipe builds carries Partner Rows at `t_partner` 0.01, unlike them.
  Threads are 16 for an LD Store and 8 for a Genotype Store, as their
  provenance's `build.threads` records.
- **A Genotype Store's inputs are staged** first, as `build92.sh` did:
  read-only copies, modification times kept, in `<dir>/pgen/` and
  `<dir>/inputs/`, because its sidecar pins them by a path relative to itself.
  Each copy must have the sha256 the panel's published LD Store
  (`$OPENGWASDB_LD_STORES/hgdp1kgp-v2-<PANEL>-k10.store`, or
  `--ld-stores-root`) records of it, read through `opengwasdb ld info`: the
  pgen, psam and pvar files and the keep file in `source.sha256`, as
  `build92.sh` checked them, and the PC File in `adjustment.pc_file.sha256`,
  because an LD Store's `source.sha256` does not hold it. A copy that differs,
  one the LD Store did not read, or a provenance without either field, refuses
  the build before the engine runs. A refusal, or a failed copy, removes
  everything staging made, a partial copy and its directories included, and
  nothing else.
- It runs `opengwasdb ld build <dir>/hgdp1kgp-v2-<PANEL>-k10.store`, then
  `opengwasdb ld validate`, each in `build92.sh`'s environment
  (`OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS` and `MKL_NUM_THREADS` 1,
  `PYTHONNOUSERSITE` 1, and `TMPDIR` `<dir>/.<store>.tmp`, removed once both
  succeed), then the writer, with `rebuild` set to this repository, the
  commit `git describe` names, and those two commands. A person then writes
  the record's label, status, licence and use.
- It refuses, before running anything, an `--ld-id` that is not `OGL-` and
  five digits, a Store or staged input that exists, a missing input, an id
  whose record already holds a Store, and a checkout with uncommitted changes
  or untracked files. It reads its own command line as the engine does, so an
  input given as `--flag=value` is checked as `--flag value` is.

## The import recipe

`tools/ld-stores/ld_stores.py import` (#542) imports a Pan-UKB LD Store
through `opengwasdb ld import` (opengwasdb build-inputs §12), as ogld#10 did by
hand, and writes its record.

```text
pixi run ld-stores import --ancestry EUR --metadata <bm-metadata.json> --variants <variants.tsv.bgz> \
    --chain <hg19ToHg38.over.chain.gz> --fasta <hg38.fa.gz> --work <work> --out <store> \
    [--parts <dir>] [--delete-parts] [--download-workers N] [--threads N] (--ld-id OGL-NNNNN | --plan-only)
```

- **`--ancestry`** is EUR alone. The engine's importer reads Pan-UKB EUR (its
  source URL, panel and n are fixed), so every other ancestry is refused,
  naming opengwas/ogld#104, until the importer takes one.
- **Its inputs** are the BlockMatrix metadata, the variant table, the chain
  and the FASTA. The variant table is exported from Pan-UKB's
  `UKBB.<ancestry>.ldadj.variant.ht` with Hail by opengwasdb's
  `scripts/ld/export_panukb_variants.py`; that export stays in opengwasdb,
  input preparation as the plink2 conversion is (owner, 25 November 2026).
- **Its command line** is the engine's, in #10's order: `opengwasdb ld import
  <store> --work <work> --metadata … --variants … --chain … --fasta …`, then
  `--parts`, `--delete-parts`, `--download-workers` and `--threads` when given;
  an option not given is the engine's default.
- **A full import** runs it, then `opengwasdb ld validate` once (at
  `--threads` when given), each in the recipe's environment, then the writer,
  with `rebuild` set to this repository, the commit `git describe` names,
  `script` `opengwasdb ld import`, those two commands, and the four inputs by
  path, the section every recipe writes (`recipe.rebuild_section`); the writer
  pins their digests from the Store, and refuses a Store that holds no digest
  of one before its record is written. It refuses, before running anything,
  what every recipe that writes a record does
  (`recipe.refuse_before_recording`): an `--ld-id` that is not `OGL-` and five
  digits, a Store that exists, a missing input, an id whose record already
  holds a Store, and a dirty checkout. A person then writes the record's label, status, licence and use.
- **`--plan-only`** runs `opengwasdb ld import … --plan-only` alone: the
  engine writes `<work>/plan.json` and each chromosome's lifted table, with no
  network access, and creates no Store. It writes no record. The same command
  without `--plan-only` resumes from that plan. It refuses a Store that exists
  and a missing input first. The engine is probed with
  `opengwasdb ld import --plan-only --help` (`recipe.require_plan_only`): it
  exits 0 when the option exists, and an engine without it answers with its
  CLI's own "No such option" error naming `--plan-only`, which is refused,
  naming the change that adds it; any other failure of the probe is refused
  with its own error, and nothing else is tried. To run against an engine
  other than the lock's, put its `opengwasdb` first on `PATH`.

### Comparing plans

```text
pixi run ld-stores compare-plan <plan.json> <reference.json>
```

It compares an import's plan with a reference plan, both `plan.json` as the
engine writes them, field by field (`ld_registry.compare.plan_differences`):
each chromosome's `parts`, `losses`, `far_bp` and `source_variants`, and any
other key its entry holds; a chromosome one plan lacks; the Store's `far_bp`;
and the identity, its `inputs` (the four digests) included. Every difference is
printed, reference then plan, and every one fails, exiting 1, except
`identity.out`, the output path the plan pins, which differs between runs by
design and is reported as the run's own. A long list, such as a chromosome's
parts, is shown by its length.

**OGL-00012's acceptance** (#542) is a plan-only re-plan from its record,
`pixi run ld-stores rebuild OGL-00012 --out <dir> --plan-only`, compared with
#10's plan (`/data/opengwasdb/work/orchestrate/ogld-88/results/10/plan-final.json`,
chr15 re-planned at 0.8.1), with no download: the same comparison ogld#102
made. A real re-plan reads about 1.4 GB of inputs and writes about 35 GB of
lifted tables, and needs the owner's approval of its command, host and budget.

## Rebuilding from a record

```text
pixi run ld-stores rebuild OGL-NNNNN --out <dir>
```

It runs the record's runnable `rebuild` with only the output changed: every
argument under the Store's directory (the parent of its `location`) moves to
`<dir>`. The inputs the commands read from inside that directory, a Genotype
Store's staged copies, are copied first, modification times kept, so the new
sidecar pins them as the original does; a `--pgen` brings its psam and pvar.
A record whose commands validate nothing, such as OGL-00007 to OGL-00011's, is
validated once after its build, at the build's threads; one whose commands
validate is not validated again. The commands run in the recipe's environment.
An import's `--work` is an output: it moves with the Store and is never copied.
A recorded `--work` that is not under the Store's directory cannot move with
it, so the record is refused, naming that work directory, rather than
re-planned or rebuilt into the original's. An option is read the same way
whether written `--flag value` or `--flag=value` (`ld_registry.argv`), here
and by the writer's check of `rebuild.inputs`.

With `--plan-only`, a record whose build is an import (OGL-00012's) runs that
import alone, rewritten the same way, with `--plan-only`: it writes the plan
and the lifted tables under `<dir>`, and no Store, and nothing is validated or
compared. The engine is probed for the option first, as for `import`.
A record whose build is `opengwasdb ld build` is refused with `--plan-only`.

It refuses an output that exists, an output inside any recorded Store's
`location` or inside a Genotype Store's directory, each compared once every
symbolic link is followed, and a record without a runnable `rebuild`. Every
record under `stores/ld/` that can be read counts, valid or not
(`record.load_records`); while one cannot be read at all, or `stores/ld/` holds
an entry that is no record's directory, the rebuild is refused, naming it,
because that record could name the Store it would write into. It writes
no record: the rebuilt Store is a new Store, and the record and the original
Store are only read.

### The acceptance run

After the rebuild, whose own commands validated it, it compares
`opengwasdb ld info` of the original with that of the rebuilt, field by field
(`ld_registry.compare`). It looks at nothing the command line does not show
(ADR 0023). It fails, exiting 1:

- on a difference in any field but the build's own, a closed list:
  `build.created_at`, `build.wall_seconds`, `build.peak_memory_bytes` (it
  depends on the machine; owner, 25 November 2026), and each
  `software.<key>` version, each only when both sides hold it. A field one
  side lacks fails, whichever it is;
- on a difference beyond a relative tolerance of 1e-9 in the PC Check's
  diagnostic statistics, a closed list (`TOLERATED_SCAN_STATISTICS`): each
  `adjustment.pc_check.scan[*]`'s `ratio`, `rest_null_q99`, `rest_share`,
  `window_null_q99` and `window_share`. They are diagnostics, and their last
  digits depend on the numerical library: OGL-00009, built by ogld 0.5.0, and
  its rebuilds by opengwasdb 0.20.0 differ in 77 of them at about 1e-15
  relative (owner, 25 November 2026, after #541's acceptance run). A
  difference within the tolerance is reported as `(within tolerance)`, never
  dropped. Every other field is compared exactly: the scan's `pc` and
  `window`, `pc_check.dropped`, the PCs kept, and every value the Store
  holds;
- when the original's provenance is no longer the one its record's
  `provenance_sha256` names;
- when the rebuilt Store's provenance lacks what an HGDP+1kGP Store records
  (`ld_registry.compare.provenance_problems`, the checks of `ld info` that
  moved here from opengwasdb's `test_hgdp1kgp_stores.py`): `panel.id`
  `hgdp1kgp-v2-<ancestry>`, `panel.n` equal to its keep file's samples,
  `encoding.r_scale` 511, `n_variants` and `t_far` in range, the build's
  `created_at` (a UTC time), wall time, peak memory and `software.ogld`,
  `adjustment.k_target` 10, `k_min` 5, a `k` between them with as many
  covariates, and the PC Check's `dropped` list; an LD Store also its
  `counts.near_pairs` and its PC Diagnostic. The checks that need the engine's
  internals (every validation rule by name, the autosomes held, the direct
  decoder against zarr) stay in opengwasdb's `ld-acceptance-stores`, which
  takes any LD Store path. An imported matrix (`source.kind` `ld_matrix`,
  such as OGL-00012) is not an HGDP+1kGP Store, and its acceptance is the
  comparison alone (#542).

Every difference, the build's own included, is printed. `pixi run ld-stores
compare <original> <rebuilt> [--keep <file>]` is the same acceptance of two
Stores already built, without a record: the comparison and the provenance
checks, with `panel.n` held to the keep file when one is given.
`tests/ld-stores-acceptance/` validates a Store already rebuilt, apart from its
rebuild, and runs the same checks, skipped wherever `OPENGWASDB_LD_REBUILT_ID`
and `OPENGWASDB_LD_REBUILT_STORE` are not set. A rebuild of a real Store needs
the owner's approval of its command, host and budget.

## The index

`pixi run index` (the Snakefile's `index` rule) runs
`ld_registry.index.generate_ld_index(ld_root, repo_root, resources_root)`, which writes `ld-resources.tsv` and
renders STORES.md's section, then `ogstores.index.generate_index`, which writes
STORES.md with that section after the Store Releases. Each file has one writer.

- It reads the records alone, never a Store.
- A second `default: true` for one ancestry among `served` records is an error naming the later record and the one it duplicates; `default` on a record that is not `served` is an error too.
- A record that breaks the spec, or anything else under `stores/ld/`, stops it
  before anything is written, and the error names the record and the field.
- **Links.** Each of a record's `measured.links` must point under another
  record's `location`, as a whole path, and that record must not be
  `retired`, unless the target lies under a path in the record's
  `unrecorded_targets`. Otherwise the index stops, naming the record, the link
  (`measured.links.<path>`) and the target. So retiring or dropping a Store an
  overlay depends on fails the index instead of breaking the overlay silently.
  Link errors are reported beside the records' own errors, checked against
  the records that are valid.
- CI's `index-ci` runs the same rule and fails on a dirty tree, so a hand edit
  to either file, or a record committed without regenerating them, fails CI.

### The values

`ld_registry.rows.ld_values` gives each record's values in the columns below,
typed (whole numbers, numbers, text), with `None` for a value the record does
not hold. The TSV and STORES.md render them; the Analysis Catalog's table
(#537) reads them as they are, so a missing value stays `NULL` there.

### `ld-resources.tsv`

One row per record and one per LD panel (below), in id order, under this header:

```tsv
ld_id	label	kind	provenance	status	used_for	ancestry	source_panel	n	k	reference_assembly	format_version	representation	n_variants	n_blocks	store_bytes	genotype_bytes	r_scale	near_bp	far_bp	t_far	t_partner	created_at	build_wall_s	built_with	source	licence	location	record	issue	notes	default
```

**A value the record does not hold is an empty cell, never `0` and never `-`.**
Each column is the record field of the same name, except:

| column | value |
|---|---|
| `provenance` | `provenance.source_kind` |
| `ancestry`, `source_panel`, `n`, `k`, `reference_assembly`, `format_version`, `n_variants`, `r_scale`, `near_bp`, `far_bp`, `t_far`, `t_partner`, `created_at` | `provenance.<column>` |
| `representation` | by `kind` first. For an `ld_store`, `pairwise r (Near Band, Far Pairs)`, with `, Partner Rows` when the record has `provenance.t_partner`. For a `genotype_store`, `pgen + sidecar, r computed per query` |
| `n_blocks` | empty for a record: an LD panel's (below) |
| `store_bytes` | `measured.store_bytes` |
| `genotype_bytes` | `measured.genotype_bytes`; empty for an LD Store |
| `build_wall_s` | `provenance.wall_seconds`, rounded to a whole second |
| `built_with` | `ogld <provenance.software.ogld> (<rebuild.script>)` |
| `source` | `provenance.source_description`. For `source_kind` `ld_matrix`, rendered from its fields, never as JSON: `<url> (<transformation>), lifted over by <file name of liftover_chain>; <qc>` |
| `record` | `stores/ld/<ld_id>/record.yaml` |
| `default` | `1` for a record that declares `default: true`, empty otherwise. The one column after the draft's 31 (#537) |

A number is written as Python writes it: `t_far` keeps every digit the Store
recorded.

### LD panels

An LD panel (a `kind: ld_panel` Reference Resource, ADR 0011) is listed beside
the records but is not one: no Store holds its provenance, and its row is read
from `resources/reference-resources/<resource_id>/resource.yaml` by
`ld_registry.panels.panel_rows(resources_root)`, which calls
`ogstores.resources.load_resource_declaration`. The declaration is the
panel's one writer, so its row cannot drift from it. `ld_registry.rows.ld_values`
is not involved.

One declaration gives one row **per ancestry** in its `ancestry`, with:

| declaration field | column |
|---|---|
| `ld_ids.<ancestry>` | `ld_id`. Required for every declared ancestry, and for no other: a missing entry is refused, naming the file and the ancestry. An id is assigned once, so a repeated one is refused too |
| `ancestry`, `resource_id`, `used_for`, `licence` | required: a declaration without one is refused, naming the file and the field. `ancestry` has at least one entry |
| `resource_id`, with ` (<ancestry>)` when the declaration has several | `label` |
| `kind` | `ld_panel`; `provenance` is `genotypes` |
| `status` | `status`, as declared. A panel's role is not its status: the calibration control is `available`, and its `used_for` says it is not used for completion |
| `used_for`, `licence`, `index_notes` | `used_for`, `licence`, `notes` |
| `ancestry` | `ancestry` |
| `source.cohort` | `source_panel` and `source` |
| `source.n_individuals` | `n` (one value, or one per ancestry) |
| `genome_build` | `reference_assembly` |
| `ld.representation`, `ld.components_cumvar_target` | `representation`: `per-block eigendecomposition (.ldeig.npz)`, then `, <target> cumulative variance` when the declaration holds a target |
| `ld.n_variants`, `ld.n_blocks`, `ld.bytes` | `n_variants`, `n_blocks`, `store_bytes`. Per ancestry for a multi-ancestry panel (a scalar there is refused); a single-ancestry panel may declare a scalar. An absent count is an empty cell; a per-ancestry map (and `source.n_individuals`) that lacks a declared ancestry, or names another, is refused, naming the file, the field and the ancestry. A value kept as declared stays as declared |
| `location`, `ancestry_dir`, `physical_directory` | `location`: `<location>/<ancestry>` for a multi-ancestry panel; `<location>/<ancestry_dir> (-> <physical_directory>)` for an alias; else `location` |
| the file's path | `record` |

Every other column is empty. **A count the declaration does not hold is `None`,
an empty cell, never `0`:** the UKB EUR panel's variants and bytes stay empty,
because no session reads its directory.

The counts are measured on the host from each panel directory, by
`tools/ld-panel/measure_panel.py`: `n_blocks` is the `.ldeig.npz` files,
`n_variants` the rows of every block's `.tsv`, `bytes` the size of every file
under the directory (`ogstores.store_size.store_size_on_disk`, a stat of each, so no directory entries).

**An LD panel's id must not be a record's.** The index refuses a collision,
naming the id and the panel, before it writes anything
(`ld_registry.index.IdCollision`).

### STORES.md

An `## LD resources` section after the Store Releases, with a subset of the
columns: id, label, kind, status, ancestry, n, k, format, variants, bytes and
licence. An empty cell is `-` there, as in the rest of STORES.md.
