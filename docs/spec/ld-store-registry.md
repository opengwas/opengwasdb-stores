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
| `licence` | person | yes | the data's licence, never empty. `to confirm` is valid, and is shown as written, while a licence is undecided. An imported matrix's must be its description's (below) |
| `used_for` | person | yes | what the Store is used for |
| `issue` | person | no | the issue that built it, such as `ogld#7` |
| `notes` | person | no | anything else a reader needs |
| `unrecorded_targets` | person | no | targets of the Store's links that no record describes, each a `path` (absolute) and its `reason`; see the index's links check |
| `rebuild` | person | yes | how the Store is rebuilt; below |
| `location` | writer | yes | the Store's absolute path, as the writer was given it |
| `provenance` | writer | yes | the Store's own provenance; below |
| `provenance_sha256` | writer | yes | the sha256, in lower-case hex, of the provenance as `opengwasdb ld info` prints it, in canonical form: `json.dumps(info, sort_keys=True, separators=(",", ":"))`, encoded as UTF-8 |
| `measured.store_bytes` | writer | yes | the bytes of every regular file under `location`, from an `lstat` of each (`ogstores.store_size.store_size_on_disk`). No link is followed, to a file or a directory, so an overlay's bytes are its own files alone; an unreadable directory is an error, never skipped. A Genotype Store's is its sidecar alone |
| `measured.links` | writer | no | every symbolic link under `location`, by its path in the Store (such as `near` or `variants/x`), and its target with every link resolved (an absolute path). The walk follows no link; a dangling link is refused, and an unreadable directory is an error, never skipped. Written when the Store has any; such a Store is an overlay |
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

What rebuilds the Store, so a later ticket (#541) can rebuild from the record.
It is written by the build that made the Store, or, for a Store built before
the registry, by a person from what actually built it. #541 adds the writer's
option for it; until then a person writes it. It is not a Release Bundle's
Build Recipe.

| field | value |
|---|---|
| `repository` | the repository the commands run in, such as `opengwas/ogld` |
| `version` | the version of that repository they run at |
| `script` | the script the commands run, relative to the repository; for a build run through the engine's command line, the subcommand, such as `ogld build --genotypes` |
| `commands` | the commands, in order, as run from the repository's root |

All four are required. `commands` holds only commands that can be run; a step
known only as prose, such as one reconstructed for a Store built before the
registry, goes in the record's `notes`, naming its source.

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
- It then checks the record and exits 1, naming each field, while the record
  breaks the spec: a new record does so until a person has written its fields.

## The index

`pixi run index` (the Snakefile's `index` rule) runs
`ld_registry.index.generate_ld_index(ld_root, repo_root, resources_root)`, which writes `ld-resources.tsv` and
renders STORES.md's section, then `ogstores.index.generate_index`, which writes
STORES.md with that section after the Store Releases. Each file has one writer.

- It reads the records alone, never a Store.
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
ld_id	label	kind	provenance	status	used_for	ancestry	source_panel	n	k	reference_assembly	format_version	representation	n_variants	n_blocks	store_bytes	genotype_bytes	r_scale	near_bp	far_bp	t_far	t_partner	created_at	build_wall_s	built_with	source	licence	location	record	issue	notes
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
