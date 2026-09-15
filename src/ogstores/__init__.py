"""Registry-side orchestration for OpenGWASDB Store Releases.

Four modules, and a hard rule between them and `opengwasdb` (ADR 0023): the
only thing this package computes on the path from an accepted Release Bundle
to a built Store Release is an `opengwasdb` command line. It does not read,
rewrite, project, or validate a row of association or Analysis data, and it
does not inspect a built Store's internals.

    bundle.py   load a Release Bundle; the release status vocabulary
    plan.py     Bundle -> [Step]; the entire adapter layer, and a pure function
    paths.py    artifact layout, derived from the store id alone (ADR 0022)
    run.py      execute one Step; write its completion record; keep it safe

See docs/spec/store-release-workflow.md.
"""

from ogstores import bundle, index, paths, plan, register, run
from ogstores.bundle import Bundle
from ogstores.index import (
    COLUMNS as STORES_COLUMNS,
    build_index,
    generate_by_label_symlinks,
    generate_index,
    regenerate_index,
    render_stores_md,
    render_stores_row,
    render_stores_tsv,
)
from ogstores.plan import Step, plan
from ogstores.register import (
    ArgvDriftError,
    MissingRecordError,
    RegisterError,
    StepFailedError,
    check_argv_drift,
    harvest_observed_measurements,
    normalize_executed_argv_for_staging,
    register_release,
)
from ogstores.run import (
    VALID_STEP_NAMES,
    MissingCommandError,
    StepExecutionError,
    StepResult,
    StoreExistsError,
    execute_step,
    get_opengwasdb_executable,
    get_opengwasdb_revision,
    get_opengwasdb_version,
    is_exact_commit_hash,
    load_record,
    publish_store,
    run_plan,
    run_step,
    validate_step_name,
)

__all__ = [
    "ArgvDriftError",
    "Bundle",
    "MissingCommandError",
    "MissingRecordError",
    "RegisterError",
    "Step",
    "StepExecutionError",
    "StepFailedError",
    "StepResult",
    "StoreExistsError",
    "VALID_STEP_NAMES",
    "build_index",
    "bundle",
    "check_argv_drift",
    "execute_step",
    "generate_by_label_symlinks",
    "generate_index",
    "get_opengwasdb_executable",
    "get_opengwasdb_revision",
    "get_opengwasdb_version",
    "harvest_observed_measurements",
    "index",
    "is_exact_commit_hash",
    "load_record",
    "normalize_executed_argv_for_staging",
    "paths",
    "plan",
    "publish_store",
    "regenerate_index",
    "register",
    "register_release",
    "render_stores_md",
    "render_stores_row",
    "render_stores_tsv",
    "run",
    "run_plan",
    "run_step",
    "validate_step_name",
]
