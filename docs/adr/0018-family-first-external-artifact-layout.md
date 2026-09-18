# Family-first external artifact layout

Large release artifacts live under the configured artifact root using `<store-family-id>/releases/<family-release-id>/`, mirroring the tracked Release Bundle path. Source format, generator name, and store layout do not appear above the Store Family in durable artifact paths because those are build details, while the Store Family and Family Release ID are the stable identity.

A local `<artifact-root>/<store-family-id>/latest` symlink may be maintained as an operational convenience for humans and ad hoc inspection, but tracked Release Bundles, Build Recipes, validation reports, and reproducible build inputs must record explicit Family Release IDs and concrete artifact paths. The default served release remains a service catalogue concern rather than registry identity.

---

**Superseded by [0022](0022-flat-opaque-store-ids.md).** Artifacts live at `<artifact-root>/<store-id>/`, so a store artifact path is a pure function of its identifier.
