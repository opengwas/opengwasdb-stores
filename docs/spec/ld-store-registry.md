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
| `licence` | person | yes | the data's licence, never empty. `to confirm` is valid, and is shown as written, while a licence is undecided |
| `used_for` | person | yes | what the Store is used for |
| `issue` | person | no | the issue that built it, such as `ogld#7` |
| `notes` | person | no | anything else a reader needs |
| `rebuild` | person | yes | how the Store is rebuilt; below |
| `location` | writer | yes | the Store's absolute path, as the writer was given it |
| `provenance` | writer | yes | the Store's own provenance; below |
| `provenance_sha256` | writer | yes | the sha256, in lower-case hex, of the provenance as `opengwasdb ld info` prints it, in canonical form: `json.dumps(info, sort_keys=True, separators=(",", ":"))`, encoded as UTF-8 |
| `measured.store_bytes` | writer | yes | the bytes of every file under `location`, from a stat of each (`ogstores.store_size.store_size_on_disk`). A Genotype Store's is its sidecar alone |
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

All four are required.

### `provenance`

Copied from `opengwasdb ld info <location>`'s JSON. A record indexes the
provenance and carries its digest; it does not copy all of it, so the source
checksums, the variant filters and the PC checks stay in the Store.

| field | from `ld info` |
|---|---|
| `source_kind` | `source.kind` |
| `source_description` | `source.description` |
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
| `software` | `software`, as the engine records it; `software.ogld` is required |
| `created_at` | `build.created_at`, an ISO 8601 date and time in UTC, such as `2026-11-13T04:27:31Z` |
| `wall_seconds` | `build.wall_seconds` |

Every field but `t_partner` and `counts` is required. A required one `ld info`
does not print is refused by the writer, never written as empty.

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
- `measured.store_bytes` may change on a rewrite; the writer prints the old and
  new values when it does.
- It then checks the record and exits 1, naming each field, while the record
  breaks the spec: a new record does so until a person has written its fields.

## The index

`pixi run index` (the Snakefile's `index` rule) runs
`ld_registry.index.generate_ld_index`, which writes `ld-resources.tsv` and
renders STORES.md's section, then `ogstores.index.generate_index`, which writes
STORES.md with that section after the Store Releases. Each file has one writer.

- It reads the records alone, never a Store.
- A record that breaks the spec, or anything else under `stores/ld/`, stops it
  before anything is written, and the error names the record and the field.
- CI's `index-ci` runs the same rule and fails on a dirty tree, so a hand edit
  to either file, or a record committed without regenerating them, fails CI.

### The values

`ld_registry.rows.ld_values` gives each record's values in the columns below,
typed (whole numbers, numbers, text), with `None` for a value the record does
not hold. The TSV and STORES.md render them; the Analysis Catalog's table
(#537) reads them as they are, so a missing value stays `NULL` there.

### `ld-resources.tsv`

One row per record, in id order, under this header:

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
| `n_blocks` | empty: an LD panel's (#540) |
| `store_bytes` | `measured.store_bytes` |
| `genotype_bytes` | `measured.genotype_bytes`; empty for an LD Store |
| `build_wall_s` | `provenance.wall_seconds`, rounded to a whole second |
| `built_with` | `ogld <provenance.software.ogld> (<rebuild.script>)` |
| `source` | `provenance.source_description` |
| `record` | `stores/ld/<ld_id>/record.yaml` |

A number is written as Python writes it: `t_far` keeps every digit the Store
recorded.

### STORES.md

An `## LD resources` section after the Store Releases, with a subset of the
columns: id, label, kind, status, ancestry, n, k, format, variants, bytes and
licence. An empty cell is `-` there, as in the rest of STORES.md.
