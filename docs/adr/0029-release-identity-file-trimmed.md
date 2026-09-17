# Release identity file trimmed to identity, lineage, status and prose

The `release.yaml` identity file carried nineteen top-level keys, ten of which
were read by no code. Several were machine-shaped fields maintained by hand, so
they looked authoritative and rotted quietly. Issue #136 trims the file to
identity, lineage, Release Status, creation time, source snapshot identity, the
generation command log, and the prose that explains the Store Release.

## Decision

Six fields are removed from every `release.yaml`, and the generation record is
reshaped:

| Removed | Why it is safe to drop |
|---|---|
| `association_coverage` | Read by no registry or workflow code. ADR 0028 already established the four-value coverage vocabulary cannot reach a built Store (the builder derives coverage from the layout), and the Build Recipe carries the load-bearing shape. |
| `release_kind` | Read by no code; 2/7 releases already disagreed with the retired family's declared cadence (ADR 0028). |
| `source_collection_id` | Read only by `bundle.check()`'s required-key list, which made it look load-bearing. It is a string restated from the retired Store Family tier (ADR 0028), not a per-release fact. |
| `source_defaults` | Every value is already materialised per row in `analyses.tsv`; builders read the row, not the YAML inheritance. |
| `sidecars` | The pointer map duplicated both the directory listing and the Validation Record's `reports` map, and in one release gave one file two different names. Each pointer that existed only here was folded into that release's `validation.yaml` `reports` map in the same commit, so no sidecar lost its only pointer. |
| `build_environment` | A standalone generation-time block superseded by the Validation Record's register-written `build_environment`, which names the revision that actually validated the Store. The master list's `opengwasdb_rev` column now reads that single source. |

The generation record changes from `generator: {name, version, command}` (a
single command string plus a path) to `generator: {version, commands}`, where
`commands` is the executed command log in run order. The previous `name` path
and the `--config=families/...` argument pointed at files that no longer exist;
they were corrected to the generator's current location under
`resources/generators/lib/source-formats/` and the config under
`resources/generators/<family-id>/`, not carried forward as dead paths.

## What is kept

`store_id`, `label`, `access_posture`, `status`, `derived_from`, `created_at`,
`accepted_at`, `source_snapshot_id`, `source_snapshot`, `generator`,
`description`, `notes`, and the one-time `migration` provenance block.

The documented BESD source-identity integrity gap (issue #134) is unaffected:
`source_snapshot.besd_prefix` remains an unverified host path prefix for
`OGS-00001`, and `OGS-00002` still inherits source identity via `derived_from`.
This decision does not silently drop or upgrade that gap.

## Consequences

- `bundle.check()`'s `RELEASE_REQUIRED_KEYS` is
  `store_id, label, status, source_snapshot_id, created_at, description,
  generator`, and it requires `generator.commands` to be a non-empty list of
  non-empty strings. It no longer validates a `sidecars` pointer map.
- The master list's `opengwasdb_rev` column is rendered from
  `validation.yaml:build_environment.opengwasdb_commit`, and
  `generator_command` is rendered from the `generator.commands` log.
- The `release.yaml` sections of `docs/release-metadata-schema.md` and
  `docs/spec/store-release-workflow.md` describe the trimmed file.

## Supersedes

ADR 0028's evidence table cited "source_collection — already recorded —
`release.yaml:source_collection_id`, 7/7 identical" as a reason the Store Family
tier could be retired. That evidence row is superseded here: the
`source_collection_id` field itself is now removed, because a string restated
from a retired tier is not a fact that must survive its source. The Store Family
tier remains retired; nothing here restores it.

Amends ADR 0014: `release.yaml`'s "identity and status" is now explicitly the
narrow field set above.

**Amended by [0030](0030-artifact-path-is-the-identifier-migration-retired.md).** The one-time `migration` provenance block that this ADR kept is itself removed (issue #137): the published artifact path is a pure function of the identifier, so the migration note is a superseded-layout record the indexer must no longer prefer, and the physical relocation of existing artifacts is a separate follow-up issue.
