# Flat opaque Store Release IDs

Supersedes [0006](0006-family-scoped-release-ids.md) and [0018](0018-family-first-external-artifact-layout.md). Does not affect [0004](0004-store-release-identity.md), which governs *when* a new Store Release is required, not what it is called.

Every Store Release has one globally unique, opaque identifier of the form `OGS-` followed by five digits, allocated sequentially: `OGS-00042`. That identifier is the directory name in the registry (`stores/OGS-00042/`), the directory name under the artifact root (`<artifact-root>/OGS-00042/`), both the `store_id` and `release_id` written into an observed-only built store's manifest, and the join key between the two. Reference Completion takes the child `release_id` while preserving its source Store identity under the upstream completion interface. The identifier is assigned once and never reused, reassigned, or re-derived.

Store Family stops being a directory level and becomes a field. A Store Family is an *attribute* of a Store Release — its query promise, access posture, release cadence, and build priority — and encoding an attribute in a path makes it unchangeable and unrepeatable. `stores/OGS-00042/release.yaml` carries `family: finngen-r13`, resolved against `families/finngen-r13.yaml`. The prior `(Store Family ID, Family Release ID)` pair is replaced by a single key, so no signature, path join, workflow wildcard, log line, or cross-family lineage link has to carry two strings to name one thing.

The identifier is opaque on purpose. Source-natural release names invite metadata to be stuffed into the identifier, where it cannot be validated and goes stale: this repository already carries `dense-observed-vcf-c128-completed-issue34`, which encodes source format, store layout, a zarr chunk size, completion state, and a GitHub issue number, all five of which are also recorded in `build.yaml`. An opaque identifier makes that impossible by construction, and it makes the generated master list (`stores.tsv`) the only way to find a release — so the master list is load-bearing rather than documentation, and cannot quietly rot.

Human legibility is restored by generated views rather than by identity. Every release carries a required `label` (the source-natural name that would previously have been its Family Release ID); tools display `OGS-00042 (finngen-r13 / r13-pilot-20)`; and `stores/by-label/` and `<artifact-root>/by-label/` hold generated symlinks. Those views are derived and may be regenerated or discarded at any time. Nothing may resolve a release by label.

The label is descriptive provenance, not a replacement identity. The
`opengwasdb` build CLI cannot currently populate it in the Store manifest's
free-form provenance mapping; upstream issue
[opengwasdb#181](https://github.com/opengwas/opengwasdb/issues/181) tracks that
capability. Until then it remains in the Release Bundle and generated views and
is not placed in either manifest identity field.

Release Lineage is expressed once, as `derived_from: OGS-00042` in the child's `release.yaml`. The previous convention of naming a Reference-Completed child `<parent>-completed` expressed the same fact a second time through string adjacency; two mechanisms for one fact is how they drift apart. Because the artifact layout is now uniform, a store's artifact path is a pure function of its identifier, so resolving a parent needs the identifier alone and never a registry lookup.

Existing published releases are renamed rather than preserved. This is a one-time mechanical migration of twenty bundles and their artifacts, recorded in the master list; the prior IDs remain discoverable through each release's `label` and its migration note.
