"""Render the master list from the bundles.

`stores.tsv`, `STORES.md` and the `by-label/` symlink trees are generated,
committed, and verified in CI by regenerating and failing on a dirty tree.

This module reads git, never the artifact root -- which is the whole
constraint, and a narrower one than it sounds. Measurements are welcome in
the index; they just have to reach it through the bundle. `register` writes
`n_variants`, `n_analyses`, elapsed and the validate verdict into
`validation.yaml` at build time, git records them, and this module reads them
from there. Only facts that change without a commit stay out.

The `build_command` column is rendered by `plan()`, so the published command
is derived rather than maintained.
"""

from __future__ import annotations

raise NotImplementedError("Phase A implementation pending; see docs/spec/store-release-workflow.md")
