# Store Family tier retired

Supersedes [0024](0024-one-family-record-no-source-collection-tier.md). Amends the "Store Family is a field" statement in [0022](0022-flat-opaque-store-ids.md).

The Store Family record tier is deleted. It carried nothing that is both unique to it and true, so the one fact it genuinely declared becomes a descriptive field on the Store Release and everything else is retired on the evidence.

## Evidence

Every field in `resources/families.yaml` resolves into one of four buckets -- already recorded elsewhere, descriptive and already carried by the Release Bundle, false, or the single surviving store-level fact:

| family field | resolution | evidence |
|---|---|---|
| `default_license` | already recorded per Analysis | every `analyses.tsv` carries a `license` column materialised from the family default (ADR 0024 acknowledged this) |
| `source_reader_capability` | already recorded at the release/build level | a `build.yaml` option (`build.options.source-reader-capability`) in the Dense/Hybrid releases; Ragged-SSF and BESD encode it in the command name (`build-ragged-ssf`) or declare it null -- it is never unique to a family record |
| `provider` | descriptive / derivable | descriptive, or derivable from the Attribution Metadata `consortium` column |
| `source_collection` | already recorded | `release.yaml:source_collection_id`, 7/7 identical |
| `label`, `description`, `source_description` | descriptive | per-release `label`, `description` and `notes` already say this at the level that matters |
| `query_promise.coverage` | false | the registry's four-value Association Coverage vocabulary cannot reach a built Store: `opengwasdb.model.enums.AssociationCoverage` has two values hardcoded from the layout, so the family's promise disagrees with the built Store's manifest in 7/7 releases |
| `query_promise.expected_layouts` | derivable | the Build Recipe's `layout` names it in 7/7 releases |
| `release_cadence` | false | 2/7 releases disagree with the family's declared cadence |
| `priority` | issue tracker | ADR 0024's own argument: a proposal with no candidate release attached is a roadmap item and belongs in an issue tracker |
| `access_posture` | **survives** | the one genuinely declared store-level fact; becomes a descriptive `release.yaml` field |

## Decision

- `resources/families.yaml` is deleted.
- The `family` field is removed from every Release Bundle; a Store Release has one identifier, its `OGS-` id, and no second tier to name.
- The `family` column is removed from the generated master list (`stores.tsv`, `STORES.md`).
- The family target alias (`pixi run release-family`) is removed from the workflow and the task runner; an operator names Store Release ids only.
- `access_posture` survives as descriptive `release.yaml` metadata on each Store Release.
- Build priority is no longer registry metadata; what should be built next lives in the issue tracker.
- `CONTEXT.md` stops defining the Store Family vocabulary, including the family-scoped release identifier.

## Consequences

- `bundle.check()` no longer requires or reads a `family` key.
- The master list has one fewer derived column; the family column no longer invites a hand-maintained second identity to rot.
- Phase A's operator interface has no family target. Phase B's generator directories are still named by their historical slugs; they are rename-or-rehome work for Phase B's design, not part of this decision.

---

**Note on direct-read source identity (issue #134).** While source reading capability is encoded in build commands or declared null, monolithic direct-read BESD releases (`OGS-00001` and `OGS-00002`) currently record source identity only as an unverified host filesystem path prefix (`source_snapshot.besd_prefix` in `OGS-00001`; `OGS-00002` carries only `source_snapshot_id` with lineage via `derived_from`) without checksums, file lists, or sizes, and `bundle.check()` only asserts that `besd_prefix` is a non-empty string for `build-ragged-besd`. This is a known integrity gap tracked by open issue #134, not an equivalent alternative to checksum verification.
