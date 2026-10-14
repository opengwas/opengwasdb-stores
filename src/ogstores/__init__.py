"""Registry-side orchestration for OpenGWASDB Store Releases.

Small modules, and a hard rule between them and `opengwasdb` (ADR 0023): the
only thing this package computes on the path from an accepted Release Bundle
to a built Store Release is an `opengwasdb` command line. It does not read,
rewrite, project, or validate a row of association or Analysis data, and it
does not inspect a built Store's internals.

    bundle.py   load/check/summarise a Release Bundle; status vocabulary
    plan.py     Bundle -> [Step]; the entire adapter layer, and a pure function
    paths.py    artifact layout, derived from the store id alone (ADR 0022)
    run.py      execute one Step; write its completion record; keep it safe

Import a submodule directly (`from ogstores import bundle`); the package
re-exports nothing (#257). See docs/spec/store-release-workflow.md.
"""
