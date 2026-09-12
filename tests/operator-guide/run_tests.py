#!/usr/bin/env python3
"""Operator-guide consistency suite (issue #102).

The operator guide's job is to be true about the shipped workflow without a
reader having to open workflow source. So this suite extracts the claims a
reader would act on and checks them against the code and the *real* installed
`opengwasdb` CLI:

* every `opengwasdb <subcommand>` the guide tells the reader to use is a real
  registered subcommand (enumerated from the installed package, the same way
  `resources/lib/release_plan.py` validates `build.command`);
* every `pixi run <task>` the guide names is a real task in `pixi.toml` (with
  `python`/`Rscript`/`opengwasdb` allowed as one-off commands run inside an
  environment, per `README.md`);
* the guide's complete `build.yaml` skeleton loads through the production
  release-plan loader and maps to the layout, command, and branches the guide
  describes;
* the guide documents every phase the workflow can run (`workflow/phase.py`);
* the artifact-layout names the guide documents (`store.opengwasdb`, the
  `.partial` sibling, the Reference-Completed child suffix) match
  `workflow/model.py`;
* the guide's worked example is the real FinnGen R13 release and its plan still
  declares the command and both optional branches the guide describes;
* the guide is discoverable from `README.md`.

Run from the repository root:

    pixi run python tests/operator-guide/run_tests.py

Requires `opengwasdb` (the `store-build` feature), i.e. `pixi run test`.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "docs" / "operator-guide.md"
WORKED_EXAMPLE = REPO_ROOT / "families" / "finngen-r13" / "releases" / "r13-pilot-20"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "workflow"))

from resources.lib.release_plan import known_commands, load_plan  # noqa: E402

import model  # noqa: E402  -- workflow/model.py
import phase  # noqa: E402  -- workflow/phase.py

n_checks = 0


def check(condition: bool, message: str) -> None:
    global n_checks
    n_checks += 1
    if not condition:
        raise AssertionError(message)


_FENCE = re.compile(r"```([A-Za-z0-9_-]*)\n(.*?)```", re.DOTALL)
_OPENGWASDB_CALL = re.compile(r"(?<![/\w])opengwasdb[ \t]+([a-z][a-z0-9-]*)(?![\w-])")
_PIXI_RUN = re.compile(r"pixi run (?:--environment [A-Za-z0-9_-]+ )?([A-Za-z0-9_-]+)")


def guide_text() -> str:
    check(GUIDE.is_file(), f"operator guide {GUIDE} should exist")
    return GUIDE.read_text(encoding="utf-8")


def fenced_blocks(text: str) -> list[tuple[str, str]]:
    return [(lang, body) for lang, body in _FENCE.findall(text)]


def pixi_tasks() -> set[str]:
    data = tomllib.loads((REPO_ROOT / "pixi.toml").read_text(encoding="utf-8"))
    tasks: set[str] = set()
    for feature in data.get("feature", {}).values():
        tasks.update(feature.get("tasks", {}).keys())
    return tasks


def guide_build_yaml(text: str) -> str:
    """The guide's complete `build.yaml` skeleton, identified by its required keys."""
    for _lang, body in fenced_blocks(text):
        if all(key in body for key in ("store_family_id:", "source:", "build:", "command:")):
            return body
    raise AssertionError("the guide should contain a complete build.yaml skeleton")


def test_guide_exists_and_names_its_scope() -> None:
    text = guide_text()
    for label, needle in (
        ("issue #102", "issue #102"),
        ("issue #101 worked example", "issue #101"),
        ("fixed input", "fixed input"),
        ("family-specific versus shared", "you write it"),
        ("shared for free", "you get it unchanged"),
        ("command selection", "Choose the build command"),
        ("opaque arguments", "opaque passthrough"),
        ("interruption", "Interruption and resumption"),
        ("resumption", "re-run the same command"),
        ("invalidation", "invalidate"),
        ("built versus validated", "built"),
        ("validated", "validated"),
        ("ADR 0015", "ADR 0015"),
        ("ADR 0018", "ADR 0018"),
        ("never commit a Store", "Never `git add` a Store"),
        ("no family-specific build script", "no family-specific build script"),
    ):
        check(needle in text, f"guide should cover {label!r} (missing {needle!r})")


def test_guide_opengwasdb_commands_are_real() -> None:
    """Every `opengwasdb <subcommand>` the guide names is a real CLI subcommand."""
    text = guide_text()
    named = {match for match in _OPENGWASDB_CALL.findall(text)}
    commands = known_commands()
    unknown = sorted(named - commands)
    check(not unknown, f"guide names opengwasdb subcommands that do not exist: {unknown}")
    # The guide's worked path must cover the real commands an operator runs by
    # hand or that the workflow invokes on their behalf, and each must be a real
    # subcommand (the table/skeleton may name them without the `opengwasdb`
    # prefix, so they are checked against the text and the CLI separately).
    for required in ("build-dense-vcf", "complete-dense", "complete-dense-resume", "build-dense-rho", "info"):
        check(required in text, f"guide should name {required!r}")
        check(required in commands, f"{required!r} should be a real opengwasdb subcommand")


def test_guide_pixi_tasks_are_real() -> None:
    """Every `pixi run <task>` the guide names is a real task (or a bare command)."""
    text = guide_text()
    tasks = pixi_tasks()
    allowed_one_offs = {"python", "Rscript", "opengwasdb"}
    named = set(_PIXI_RUN.findall(text))
    unknown = sorted(named - tasks - allowed_one_offs)
    check(not unknown, f"guide names pixi tasks that do not exist: {unknown}")
    check("release" in named, "guide should document `pixi run release`")


def test_guide_build_yaml_skeleton_loads() -> None:
    skeleton = guide_build_yaml(guide_text())
    with tempfile.TemporaryDirectory() as tmp:
        release = Path(tmp) / "release"
        release.mkdir()
        (release / "build.yaml").write_text(skeleton, encoding="utf-8")
        plan = load_plan(release)
    check(plan.schema == "cli", f"guide skeleton should be the CLI schema, got {plan.schema!r}")
    check(plan.build_command == "build-dense-vcf", f"unexpected skeleton command {plan.build_command!r}")
    check(plan.store_layout == "dense-observed", f"unexpected skeleton layout {plan.store_layout!r}")
    check(plan.rho_enabled, "guide skeleton should enable rho (Dense)")
    check(plan.reference_completion_enabled, "guide skeleton should enable Reference Completion")
    check(plan.completed_release_id == "r1-completed", f"unexpected child id {plan.completed_release_id!r}")
    check(plan.completion_command == "complete-dense", f"unexpected completion command {plan.completion_command!r}")


def test_guide_documents_every_phase() -> None:
    text = guide_text()
    missing = sorted(phase_id for phase_id in phase.PHASES if phase_id not in text)
    check(not missing, f"guide does not document workflow phase(s): {missing}")
    # The guide's command-selection table must cover the layouts the plan loader
    # can infer, so an operator can pick a command for each.
    for layout in ("dense", "hybrid", "ragged"):
        check(layout in text, f"guide should mention the {layout!r} layout")


def test_guide_artifact_names_match_model() -> None:
    text = guide_text()
    check(model.STORE_DIR_NAME in text, f"guide should name the Store dir {model.STORE_DIR_NAME!r}")
    check(model.STORE_PARTIAL_SUFFIX in text, f"guide should name the partial suffix {model.STORE_PARTIAL_SUFFIX!r}")
    check(
        model.CHILD_LAYOUT_SUFFIX in text,
        f"guide should name the child layout suffix {model.CHILD_LAYOUT_SUFFIX!r}",
    )
    check(
        "<artifact-root>/<store-family-id>/releases/<family-release-id>/" in text,
        "guide should document the ADR 0018 artifact path template",
    )
    # The paths the workflow writes back into the repository are the bundle's
    # sidecars and validation.yaml, per ADR 0015.
    check("sidecars/" in text, "guide should document where per-phase reports live")
    check("validation.yaml" in text, "guide should document validation.yaml")


def test_guide_prebuild_check_command_runs_real_cli() -> None:
    """The documented pre-build check really runs against a real release.

    Drives `resources/lib/release_plan.py` as a subprocess the way the guide
    tells the reader to, on the checked-in r13 fixture (self-contained, so this
    stays CI-safe), and asserts it reaches the real `opengwasdb` command
    enumeration and checksum verification.
    """
    fixture = REPO_ROOT / "tests" / "release-workflow" / "fixtures" / "r13-fixture"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "resources" / "lib" / "release_plan.py"), str(fixture)],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    check(result.returncode == 0, f"documented pre-build check should pass on the fixture: {result.stdout}")
    check("PASS" in result.stdout, f"pre-build check should report PASS: {result.stdout}")
    check("build.command: build-dense-vcf" in result.stdout, result.stdout)


def test_worked_example_is_the_real_finngen_release() -> None:
    text = guide_text()
    plan = load_plan(WORKED_EXAMPLE)
    check(plan.schema == "cli", f"worked example should be a CLI plan, got {plan.schema!r}")
    check(plan.build_command == "build-dense-vcf", f"unexpected worked-example command {plan.build_command!r}")
    check(plan.rho_enabled, "worked example should still enable rho")
    check(plan.reference_completion_enabled, "worked example should still enable Reference Completion")
    check(plan.completed_release_id == "r13-pilot-20-completed", f"unexpected child {plan.completed_release_id!r}")
    for needle in ("r13-pilot-20", "build-dense-vcf", "r13-pilot-20-completed", "HEIGHT_IRN"):
        check(needle in text, f"guide should name the real worked-example element {needle!r}")


def test_guide_relative_links_resolve() -> None:
    """Every relative link in the guide points at a file that exists."""
    text = guide_text()
    links = re.findall(r"\]\(([^)]+)\)", text)
    relative = [link for link in links if not link.startswith(("http://", "https://", "#", "mailto:"))]
    check(relative, "guide should link to the spec, the workflow, and the worked example")
    missing = sorted(link for link in relative if not (GUIDE.parent / link).resolve().exists())
    check(not missing, f"guide has broken relative link(s): {missing}")


def test_guide_is_discoverable() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    check(
        "docs/operator-guide.md" in readme,
        "README.md should link to docs/operator-guide.md",
    )


def main() -> None:
    tests = [
        test_guide_exists_and_names_its_scope,
        test_guide_opengwasdb_commands_are_real,
        test_guide_pixi_tasks_are_real,
        test_guide_build_yaml_skeleton_loads,
        test_guide_documents_every_phase,
        test_guide_artifact_names_match_model,
        test_guide_prebuild_check_command_runs_real_cli,
        test_worked_example_is_the_real_finngen_release,
        test_guide_relative_links_resolve,
        test_guide_is_discoverable,
    ]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} operator-guide tests passed ({n_checks} checks)")


if __name__ == "__main__":
    main()
