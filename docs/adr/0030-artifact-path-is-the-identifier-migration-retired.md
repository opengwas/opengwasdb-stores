# Artifact path is the identifier; the migration block is retired

Amends the migration-note references in [0022](0022-flat-opaque-store-ids.md) and the kept-field list in [0029](0029-release-identity-file-trimmed.md).

The flat opaque Store id made a Store's artifact path a pure function of its identifier: `<artifact-root>/<store-id>/` (ADR 0022). In practice it was not: every Release Bundle carried a one-time `migration` block recording where the Store actually lived under the superseded family-first layout, and the indexer preferred that note when rendering `store_uri`, so the published master list pointed at legacy family-first paths in all seven rows. A one-time migration had become a permanent metadata tier.

## Decision

- The artifact path published in the master list (`stores.tsv:store_uri`) is a pure function of the identifier: `<artifact-root>/<store-id>/store.opengwasdb`, resolved from deployment configuration by `paths.artifact_root()` (issue #126) and never from a bundle field.
- The migration-note fallback is removed from the indexer. `render_stores_row` no longer reads `release.yaml:store_uri` or `release.yaml:migration.previous_store_uri`.
- The `migration` block is removed from every Release Bundle.
- Store artifacts are **not** relocated in this ticket. Relocating the built Stores is hundreds of gigabytes of operational work and is out of scope for a registry change; it is raised as its own follow-up issue (#139), and this ticket records that dependency.

## What now guarantees the path holds

- `paths.py` is the single module that constructs an artifact path, and `store_path(store_id, root)` is that pure function. Both renderings of the truth derive from it: `plan()` renders `build_command` (the Snakefile's rules and the master list's `build_command` column are one thing), and the indexer renders `store_uri`. They cannot disagree.
- `bundle.check()` already rejects any `artifacts` block in a Build Recipe (issue #126), and there is no remaining bundle field that names an artifact location, so nothing can reintroduce a hand-maintained path into the master list.
- The master list, every bundle's `summary.yaml`, and both `by-label/` trees are generated and verified in CI by regenerating and failing on a dirty tree, so a stale or hand-edited `store_uri` cannot quietly rot (ADR 0022's "load-bearing master list" argument, now actually enforced for the artifact location).

## Consequences

The registry now publishes where a Store *should* live under the flat opaque-id layout. Until the separate relocation happens, that published location will not match where the seven Trial Store Releases' bytes currently sit on disk. This gap is recorded honestly rather than papered over: the derived `store_uri` states the truth of the layout, and the on-disk mismatch is an operational fact owned by the follow-up relocation issue, not a registry fact this column may silently re-encode.

The legacy family-first paths remain discoverable through the follow-up issue and git history, not through a metadata field that the indexer would otherwise prefer forever.

## Supersedes

The `migration` provenance block's `previous_store_uri` was the last thing the indexer could read to prefer a legacy path over the identifier. Removing it completes the promise ADR 0022 made ("a store artifact path is a pure function of its identifier") and trims the field list ADR 0029 recorded as kept.
