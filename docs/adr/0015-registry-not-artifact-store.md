# Registry not artifact store

This repository stores registry metadata, manifests, recipes, summaries, and small reports, but not OpenGWASDB store artifacts, source data, large logs, or large benchmark outputs. Release bundles may point to external artifact locations so the repository remains reviewable and usable as an audit record.

**Amended by issue #126.** A Release Bundle no longer records the artifact root. The root is deployment configuration resolved by `paths.artifact_root()`, not a bundle field, so the same immutable bundle can be built on CI, a developer laptop, or the production host without editing it. Bundles still point to external *inputs* that are genuine release facts -- for example a BESD source prefix recorded in `release.yaml:source_snapshot.besd_prefix` -- but where this release's *artifacts* land is not one of them.
