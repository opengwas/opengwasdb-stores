# The LD Store Registry: one LD Store record per LD Store under `stores/ld/`, with `OGL-` ids

Nothing recorded which LD Stores exist. The Data API serves the HGDP+1kGP EUR
LD Store from a path in its own configuration (`OGDB_LD_STORES`); four more
HGDP+1kGP LD Stores, five Genotype Stores and the Pan-UKB EUR import sit on the
host with no record here. Epic #496 moves LD Store builds and their registry
into this repository. Its owner, Gibran Hemani (`explodecomputer`), added a
generated index and a draft of its rows on 25 November 2026 (#496's body and
its comment), and settled the layout in #536's body the same day.

## Decision

**An LD Store record per LD Store, in this repository's output tree.** The
**LD Store Registry** is the set of LD Store records, and an LD Store's record
is `stores/ld/<ld_id>/record.yaml`. `stores/ld/` holds nothing else. `stores/` holds what this repository
produces (ADR 0038), and its Release Bundles stay where they are:
`stores/OGS-*/`. Everything that walks the Release Bundles already selects
directories that are `OGS-` ids with a `release.yaml`, so `stores/ld/` is
invisible to them.

**Flat, opaque `OGL-` ids** (owner, #536). `OGL-` and five digits, assigned once
and never reused, as ADR 0022 rules for `OGS-`. An LD Store is not a Store
Release, so it does not share `OGS-`'s sequence. The draft's numbering
stands: OGL-00001 is the served EUR LD Store, and later tickets keep the
numbers the draft gave the others.

**Each field has one writer** (owner, #496: "a person writes only what the Store
can't know"). The record writer, `python3 -m ld_registry write`, fills the
Store's kind, its provenance and the provenance's digest from
`opengwasdb ld info`, its location and its size on disk. A person writes the
label, status, licence, use, notes and issue. The build that made a Store
writes its `rebuild` section through the writer (#541); a person writes it
only for a Store built before the registry. That section is not a Release
Bundle's Build Recipe, so it does not take that name. The writer keeps what a
person wrote, and never changes a `rebuild` silently.

**A record is written only from an existing Store's provenance**, so a Store
still building has no record and `building` is no status. A published Store
never changes: once a record holds a Store's provenance, the writer refuses a
Store whose kind, location, provenance or provenance digest differs, naming
each field. Only the size on disk may change. The draft's OGL-00001, OGL-00006
and OGL-00007 share a panel, ancestry, n and k, so those four alone could not
tell them apart.

**The engine's command line is the only way in** (ADR 0023). The writer reads
an LD Store through `opengwasdb ld info` and a stat of its files, and the index
reads records alone. A record carries every value the index shows, so the
index never opens a Store.

**The record indexes the provenance, and carries its digest** (owner,
25 November 2026, ruling on #536's review). It keeps the values the index shows
and `provenance_sha256`, the sha256 of the provenance as `opengwasdb ld info`
prints it, in canonical JSON. It does not copy the source checksums, the
variant filters or the PC checks, which stay in the Store, and it hashes none of
the Store's files: their integrity is `opengwasdb ld validate`'s job.

**`licence` is required and never empty** (owner, recorded on #538). `to
confirm` is a valid licence, shown as written, because the HGDP+1kGP licence is
undecided. A record never leaves the question silent.

**A generated index, `ld-resources.tsv`, and a section of `STORES.md`** (owner,
#536: the name, because the file will list the LD panels too). `pixi run index`
writes both from the records, and CI's clean-tree check fails when either is
stale, as for `stores.tsv`. A value a record lacks is an empty cell, never `0`.
`ld_registry` writes the TSV and renders the section; `ogstores.index` places
the section in `STORES.md`, so each file has one writer and `ogstores` never
imports `ld_registry`.

## Consequences

- The record's format, its writer and the index are specified in
  [docs/spec/ld-store-registry.md](../spec/ld-store-registry.md).
- Genotype Stores (#539), the LD panels' rows from their `resource.yaml` (#540)
  and the Analysis Catalog's `ld_stores` table (#537) extend the record and the
  index; they do not change OGL-00001's record.
- Pan-UKB EUR (the draft's OGL-00012) is published, so its record will say
  `built`.
- The HGDP+1kGP build recipe lives here (#541). The owner, Gibran Hemani
  (`explodecomputer`), ruled its scope on 25 November 2026, in a comment on
  opengwas/opengwasdb-stores#541 (issuecomment-7172100831): `tools/ld-stores/`
  builds an LD Store or a Genotype Store from the converted inputs through
  `opengwasdb ld build` and `validate`, and writes the record with its
  `rebuild`. It rebuilds a recorded Store from that section into a new
  directory, and accepts it on what `opengwasdb ld validate` and
  `opengwasdb ld info` show. The plink2 conversion and the checks that need
  the engine's internals stay in opengwasdb.
- The acceptance compares `ld info` exactly, but for the build's own fields
  and five diagnostic statistics of the PC Check's scan (`ratio`,
  `rest_null_q99`, `rest_share`, `window_null_q99`, `window_share`), held to
  a relative tolerance of 1e-9 (the owner, 25 November 2026, ruling on #541's
  acceptance run). Their last digits depend on the numerical library: the
  rebuilds of OGL-00009 at opengwasdb 0.20.0 differ from it at about 1e-15.
  The PCs used, the scan's `pc` and `window`, `pc_check.dropped` and every
  value the Store holds are compared exactly, and a difference within the
  tolerance is still reported.
- The Pan-UKB import recipe lives here too (#542). The owner, Gibran Hemani
  (`explodecomputer`), ruled its scope on 25 November 2026, in a comment on
  opengwas/opengwasdb-stores#542 (issuecomment-7176102258):
  `opengwasdb ld import` gains `--plan-only`, which writes the plan and the
  lifted tables with no network access, in opengwasdb, replacing ogld#102's
  private `run._plan` driver; `tools/ld-stores/` runs the import, full or
  plan-only, through the command line alone, and writes the record after a
  full import, with a runnable `rebuild` naming its inputs; every ancestry but
  EUR is refused, naming ogld#104; and the Hail variant export stays in
  opengwasdb as input preparation, like #541's plink2 conversion. Its
  acceptance is a plan-only re-plan from OGL-00012's recorded inputs, compared
  with ogld#10's plan chromosome by chromosome, with no download, run once the
  owner has approved it. The writer pins a `rebuild`'s inputs from the Store's
  own `source.sha256`, and an imported matrix's rebuild is accepted on the
  field-by-field comparison alone, without the HGDP+1kGP provenance checks.
