"""The seam: a Release Bundle in, a sequence of `opengwasdb` argv out.

    plan(bundle) -> [Step(name, argv, inputs, outputs)]

A pure function. It reads `build.yaml` and `release.yaml`; it opens no Store,
reads no `analyses.tsv` row, and performs no I/O beyond path construction, so
it is deterministic and testable by string comparison.

Dispatch is a table keyed on `(layout, completion_state)`. `build.options`
keys are `opengwasdb` flag names rendered verbatim -- this module never
interprets one (ADR 0023). The only arguments it composes are the registry's
own facts: Store identity, the bundle's `analyses.tsv` path, artifact paths.

This is the only place in the repository that knows how to invoke
`opengwasdb`, which is why the Snakefile's rules and the master list's
`build_command` are two renderings of one thing and cannot disagree.
"""

from __future__ import annotations

raise NotImplementedError("Phase A implementation pending; see docs/spec/store-release-workflow.md")
