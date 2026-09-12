"""Artifact layout.

Every path is a pure function of the Store Release id, because ADR 0022 made
the id the directory name under the artifact root as well as in the registry.
Nothing else in this package constructs an artifact path, so a completed
release can resolve its parent's Store from `derived_from` alone, with no
registry lookup.

    <artifact-root>/OGS-00042/
        source/                 acquired or filtered source files
        work/                   checkpoints, scratch
        records/<step>.json     one per executed step
        store.opengwasdb        the Store Release
        store.opengwasdb.partial    transient; renamed into place on success
"""

from __future__ import annotations

raise NotImplementedError("Phase A implementation pending; see docs/spec/store-release-workflow.md")
