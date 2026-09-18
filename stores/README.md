# Store Releases

One directory per Store Release, named by its globally unique opaque
identifier (ADR 0022). The identifier is also the directory name under the
artifact root and the `release_id` written into the built Store's manifest, so
a Store found on disk joins back to its registry record without a lookup table.

```text
OGS-00042/
  release.yaml      identity, label, status, lineage, provenance
  build.yaml        the recipe: an opengwasdb subcommand and its flags
  analyses.tsv      membership; opengwasdb owns the schema
  summary.yaml       generated description derived only from analyses.tsv
  validation.yaml   evidence, written back by the run
```

Nothing may resolve a release by its `label`. The label exists for display and
for the generated `by-label/` symlinks; identity is the id.

## Artifact location

A Store Release's published artifact path is a pure function of its identifier:
`<artifact-root>/<store-id>/store.opengwasdb` (ADRs 0022 and 0030). The master
list derives `store_uri` from the identifier alone; there is no migration-note
fallback.

The artifact root is deployment configuration (issue #126), not a bundle fact,
so where a built Store's bytes actually sit is an operational fact this
repository can only record for the host it is running on, never assert for
every deployment. The physical relocation of the seven Trial Store Releases is
tracked by #139; what is verified on this host is recorded below, not a blanket
claim about the state of every deployment.

Verified on this host (artifact root `/data/opengwasdb/stores` from the
tracked `ogstores.yaml`):

| Store | At published target | At legacy path |
|---|---|---|
| OGS-00001 | yes | no |
| OGS-00002 | yes | no |
| OGS-00003 | yes | no |
| OGS-00004 | no | no |
| OGS-00005 | no | no |
| OGS-00006 | no | no |
| OGS-00007 | no | no |

On this host, three of the seven releases are already at their published
target (`<artifact-root>/<store-id>/store.opengwasdb`) and their legacy paths
no longer exist; the other four are at neither location. The derived path is
still what a build would produce and what a Store found at
`<artifact-root>/<store-id>/` joins back to.

Thirteen further bundles are still under `families/*/releases/` awaiting
triage.

| | label |
|---|---|
| `OGS-00001` | pilot-10 |
| `OGS-00002` | pilot-10-completed |
| `OGS-00003` | r13-pilot-20 |
| `OGS-00004` | eur-hybrid-pilot-10 |
| `OGS-00005` | eur-hybrid-quant-pilot-10 |
| `OGS-00006` | 2023-chen-full-european |
| `OGS-00007` | 2018-sun-pilot-10 |

`summary.yaml`, `stores.tsv`, and `STORES.md` are generated from these bundles
by `pixi run index`. A summary preserves absent or empty Analysis metadata as
`NA`; it is a review view, not another declared source of metadata.
