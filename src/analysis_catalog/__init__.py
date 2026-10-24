"""The Analysis Catalog: a generated, versioned SQLite database over Release Bundles.

It lists every Analysis of every served Store Release (ADR 0039, epic #313). It
reads Release Bundles through the Release Bundle contract (`ogstores.bundle`,
`ogstores.paths`) and never opens a Store (ADR 0023). Nothing in `ogstores` or
`manifest_generation` imports it, so it can move to the API's repository.

Entry point: `analysis_catalog.build.build_catalog`, or
`python3 -m analysis_catalog` (`pixi run catalog`).
"""

# Part of the catalog's version: bump it when a change to the builder's output
# is not visible in its source files, which are hashed in any case.
BUILDER_VERSION = "1"
