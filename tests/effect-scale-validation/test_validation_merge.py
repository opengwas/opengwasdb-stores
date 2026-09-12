#!/usr/bin/env python3
"""Unit test for the shared validation.yaml merge logic.

The retired `build-store.py` adapter used to unconditionally overwrite
`checks.ancestry`, `checks.effect_scale`, and `checks.sd_estimation` with
hardcoded values every time it ran, silently discarding whatever the
generator's `--mode=effect-scale` stage had just computed (issue #21). The fix
moved the merge logic into
`resources/lib/release_yaml.py::merge_validation_yaml`, shared by every writer
so no stage clobbers another's checks, reports, or warnings. The writers today
are the effect-scale generator stage and the workflow's validate phase
(`workflow/phase.py::phase_validate`, issue #103); this proves the merge
behaviour directly against the shared module.

Run from the repository root:
    python3 tests/effect-scale-validation/test_validation_merge.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from resources.lib.release_yaml import merge_validation_yaml  # noqa: E402


def main() -> None:
    n_checks = 0

    def check(cond: bool, message: str) -> None:
        nonlocal n_checks
        n_checks += 1
        if not cond:
            raise AssertionError(message)

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "validation.yaml"
        path.write_text(
            "\n".join([
                "status: failed",
                "validated_at: 2026-08-03T00:00:00Z",
                "validator:",
                "  name: resources/generators/gwas-ssf-ragged/generate.R",
                "  version: null",
                "checks:",
                "  schema: not_run",
                "  files: not_run",
                "  reader_smoke_test: not_run",
                "  ancestry: not_run",
                "  effect_scale: failed",
                "  sd_estimation: failed",
                "  sparse_regions: not_run",
                "reports: {}",
                "warnings:",
                "  - GCST123: scale_inconsistent (effect_scale_failed)",
                "errors: []",
                "",
            ]),
            encoding="utf-8",
        )

        # Simulate the workflow's validate phase running after the R effect-scale stage.
        merge_validation_yaml(
            path,
            validator_name="workflow/phase.py:validate_observed_release",
            default_checks={"ancestry": "not_run", "effect_scale": "not_run", "sd_estimation": "not_run"},
            updated_checks={
                "schema": "passed", "files": "passed", "reader_smoke_test": "passed", "sparse_regions": "passed",
            },
            updated_reports={"build_report": "sidecars/build-report.tsv"},
            new_warnings=["build warning: nothing serious"],
        )
        rewritten = path.read_text(encoding="utf-8")

        check("effect_scale: failed" in rewritten,
              "checks.effect_scale from the generator stage must survive the workflow validate rewrite")
        check("sd_estimation: failed" in rewritten,
              "checks.sd_estimation from the generator stage must survive the workflow validate rewrite")
        check("- GCST123: scale_inconsistent (effect_scale_failed)" in rewritten,
              "prior effect-scale warnings must be preserved")
        check("- build warning: nothing serious" in rewritten,
              "new build-time warnings must also be included")
        check("schema: passed" in rewritten, "the validate phase should still record its own passed checks")
        check("build_report: sidecars/build-report.tsv" in rewritten,
              "the validate phase's own report pointer must be written")
        check("status: failed" in rewritten,
              "overall status must reflect the worse of build checks and preserved effect-scale checks")

        # Simulate a *second* run: an ancestry-assignment stage (issue #25)
        # updating checks.ancestry afterwards must not discard the validate
        # phase's reports.build_report or checks.schema/files/reader_smoke_test.
        merge_validation_yaml(
            path,
            validator_name="resources/generators/gwas-ssf-ragged/ancestry-assign.py",
            updated_checks={"ancestry": "passed"},
            updated_reports={},
            new_warnings=[],
        )
        rewritten_again = path.read_text(encoding="utf-8")
        check("ancestry: passed" in rewritten_again, "the ancestry stage's own check must be applied")
        check("schema: passed" in rewritten_again,
              "the validate phase's checks must survive a later ancestry-stage rewrite")
        check("build_report: sidecars/build-report.tsv" in rewritten_again,
              "the validate phase's report pointer must survive a later ancestry-stage rewrite")

    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
