#!/usr/bin/env python3
"""Release-plan loader suite (issue #95). Run from the repository root:

    pixi run python tests/release-plan/run_tests.py

Covers the happy path for the new CLI build.yaml schema and every refusal the
loader makes (unknown build.command, rho on a non-Dense layout, an unresolved
Reference Resource, a missing/mismatched source file, a missing
source.root/source.analyses), the legacy schema staying accepted, and path
resolution relative to source.root. It drives the public CLI as well as the
module directly, matching this repository's convention of asserting on the
observable surface rather than internals.

Requires opengwasdb (to enumerate its CLI subcommands), e.g. via `pixi run
test` (feature: store-build).
"""
from __future__ import annotations

import csv
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOADER = REPO_ROOT / "resources" / "lib" / "release_plan.py"
sys.path.insert(0, str(REPO_ROOT))
from resources.lib.release_plan import (  # noqa: E402
    check_release,
    known_commands,
    load_plan,
)

n_checks = 0


def check(condition: bool, message: str) -> None:
    global n_checks
    n_checks += 1
    if not condition:
        raise AssertionError(message)


HAPPY_BUILD_YAML = """\
store_family_id: example-dense
family_release_id: r1-observed
store_layout: dense-observed
source:
  root: source
  analyses: analyses.tsv
build:
  command: build-dense-vcf
  arguments:
    store-id: example-dense
    release-id: r1-observed
    n-workers: 16
    feature-flags: [alpha, beta]
rho:
  enabled: true
reference_completion:
  enabled: true
  family_release_id: r1-completed
  command: complete-dense
"""

LEGACY_BUILD_YAML = """\
store_family_id: example-legacy
family_release_id: r1-legacy
store_layout: dense-observed
completion_state: observed-only
builder:
  package: opengwasdb
  entrypoint: opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest
source:
  source_format: gwas-vcf
artifacts:
  artifact_root: /data/opengwasdb
  source_dir: source
"""


def write_release(base: Path, build_yaml: str, rows: list[dict[str, str]]) -> Path:
    release = base / "release"
    (release / "source").mkdir(parents=True, exist_ok=True)
    with (release / "analyses.tsv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["analysis_id", "source_file", "checksum", "checksum_algorithm", "exclude_from_build"],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    (release / "build.yaml").write_text(build_yaml, encoding="utf-8")
    return release


def source_row(release: Path, analysis_id: str, name: str, content: bytes, *, checksum: str | None = None,
               exclude: bool = False) -> dict[str, str]:
    (release / "source" / name).write_bytes(content)
    return {
        "analysis_id": analysis_id,
        "source_file": name,
        "checksum": hashlib.sha256(content).hexdigest() if checksum is None else checksum,
        "checksum_algorithm": "sha256",
        "exclude_from_build": "true" if exclude else "",
    }


def run_loader(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LOADER), *args],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_known_commands() -> None:
    commands = known_commands()
    for expected in ("build-dense-vcf", "complete-dense", "build-hybrid", "build-ragged-ssf"):
        check(expected in commands, f"opengwasdb CLI should expose {expected!r}")


def test_happy_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = []
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, rows)
        rows.append(source_row(release, "A", "a.vcf", b"variant-a\n"))
        rows.append(source_row(release, "B", "b.vcf", b"variant-b\n"))
        # Rewrite now that the source files exist and their checksums are known.
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, rows)

        plan = load_plan(release)
        check(plan.schema == "cli", f"happy path should be CLI schema, got {plan.schema!r}")
        check(plan.build_command == "build-dense-vcf", f"unexpected build command {plan.build_command!r}")
        check(plan.store_layout == "dense-observed", f"unexpected layout {plan.store_layout!r}")
        check(plan.rho_enabled is True, "rho should be enabled")
        check(plan.reference_completion_enabled is True, "reference completion should be enabled")
        check(plan.completed_release_id == "r1-completed", f"unexpected completed release {plan.completed_release_id!r}")
        check(plan.completion_command == "complete-dense", f"unexpected completion command {plan.completion_command!r}")
        check(plan.source_root == release / "source", f"source.root should resolve next to build.yaml, got {plan.source_root}")
        check(plan.analyses_path == release / "analyses.tsv", f"unexpected analyses path {plan.analyses_path}")
        # Arguments are an opaque passthrough: keys and list values are preserved verbatim.
        check(plan.build_arguments["store-id"] == "example-dense", "store-id argument should pass through")
        check(plan.build_arguments["feature-flags"] == ["alpha", "beta"], "list argument should pass through opaque")

        result = check_release(release)
        check(result.ok, f"happy path should pass, errors={result.errors}")
        # A direct build.yaml path is accepted as well as the release directory.
        check(check_release(release / "build.yaml").ok, "loader should accept a direct build.yaml path")

        completed = run_loader(str(release))
        check(completed.returncode == 0, f"CLI should exit 0 on happy path: {completed.stdout}")
        check("PASS" in completed.stdout, f"CLI should report PASS: {completed.stdout}")
        check("build.command: build-dense-vcf" in completed.stdout, f"CLI should report the command: {completed.stdout}")


def test_absolute_source_paths_resolve() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows: list[dict[str, str]] = []
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, rows)
        absolute = release / "source" / "a.vcf"
        absolute.write_bytes(b"variant-a\n")
        rows.append({
            "analysis_id": "ABS",
            "source_file": str(absolute),
            "checksum": hashlib.sha256(b"variant-a\n").hexdigest(),
            "checksum_algorithm": "sha256",
            "exclude_from_build": "",
        })
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, rows)
        # Recreate the file the second write_release call wiped.
        (release / "source" / "a.vcf").write_bytes(b"variant-a\n")
        result = check_release(release)
        check(result.ok, f"absolute source paths should resolve: {result.errors}")


def test_unknown_command_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        release = write_release(Path(tmp), HAPPY_BUILD_YAML.replace("command: build-dense-vcf", "command: frobnicate"), [])
        result = check_release(release)
        check(not result.ok, "unknown build.command should be refused")
        check(any("build.command" in error and "frobnicate" in error for error in result.errors), result.errors)
        check(run_loader(str(release)).returncode == 1, "CLI should exit 1 on unknown command")


def test_rho_on_hybrid_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = HAPPY_BUILD_YAML.replace("store_layout: dense-observed", "store_layout: hybrid-observed").replace(
            "command: build-dense-vcf", "command: build-hybrid"
        )
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(not result.ok, "rho should be refused on a Hybrid layout")
        check(any("rho.enabled" in error and "Dense-only" in error for error in result.errors), result.errors)
        check(run_loader(str(release)).returncode == 1, "CLI should exit 1 on Dense-incompatible rho")


def test_rho_on_ragged_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = HAPPY_BUILD_YAML.replace("store_layout: dense-observed", "store_layout: ragged-observed").replace(
            "command: build-dense-vcf", "command: build-ragged-ssf"
        )
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(not result.ok, "rho should be refused on a Ragged layout")
        check(any("rho.enabled" in error for error in result.errors), result.errors)


def test_rho_inferred_from_command_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = HAPPY_BUILD_YAML.replace("store_layout: dense-observed\n", "").replace(
            "command: build-dense-vcf", "command: build-hybrid"
        )
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(not result.ok, "rho should be refused when layout is inferred from a Hybrid command")
        check(any("rho.enabled" in error for error in result.errors), result.errors)


def test_rho_disabled_on_ragged_ok() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = HAPPY_BUILD_YAML.replace("store_layout: dense-observed", "store_layout: ragged-observed").replace(
            "command: build-dense-vcf", "command: build-ragged-ssf"
        ).replace("rho:\n  enabled: true\n", "rho:\n  enabled: false\n")
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(result.ok, f"rho disabled on Ragged should pass: {result.errors}")


def test_unresolved_reference_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = HAPPY_BUILD_YAML + (
            "ancestry_assignment:\n"
            "  enabled: yes\n"
            "  reference_resource_id: missing-resource\n"
            "reference_resources:\n"
            "- resource_id: declared-resource\n"
            "  kind: ancestry_mixture\n"
        )
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(not result.ok, "an undeclared Reference Resource should be refused")
        check(
            any("ancestry_assignment.reference_resource_id" in error and "missing-resource" in error for error in result.errors),
            result.errors,
        )
        check(run_loader(str(release)).returncode == 1, "CLI should exit 1 on an unresolved Reference Resource")


def test_missing_source_file_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, [])
        (release / "source" / "present.vcf").write_bytes(b"x\n")
        rows = [{
            "analysis_id": "PRESENT",
            "source_file": "present.vcf",
            "checksum": "",
            "checksum_algorithm": "sha256",
            "exclude_from_build": "",
        }, {
            "analysis_id": "MISSING",
            "source_file": "missing.vcf",
            "checksum": "",
            "checksum_algorithm": "sha256",
            "exclude_from_build": "",
        }]
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, rows)
        (release / "source" / "present.vcf").write_bytes(b"x\n")
        result = check_release(release)
        check(not result.ok, "a missing source file should be refused")
        check(any("source.analyses" in error and "MISSING" in error for error in result.errors), result.errors)
        check(run_loader(str(release)).returncode == 1, "CLI should exit 1 on a missing source file")


def test_checksum_mismatch_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, [])
        rows = [source_row(release, "A", "a.vcf", b"real bytes\n", checksum="0" * 64)]
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, rows)
        result = check_release(release)
        check(not result.ok, "a checksum mismatch should be refused")
        check(any("checksum mismatch" in error and "A" in error for error in result.errors), result.errors)
        # The same release passes when checksum verification is explicitly skipped.
        check(check_release(release, verify_checksums=False).ok, "structural check should pass without checksums")
        check(run_loader(str(release)).returncode == 1, "CLI should exit 1 on a checksum mismatch")


def test_missing_source_root_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = HAPPY_BUILD_YAML.replace("root: source", "root: does-not-exist")
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(not result.ok, "a missing source.root should be refused")
        check(any("source.root" in error for error in result.errors), result.errors)


def test_missing_analyses_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, [])
        (release / "analyses.tsv").unlink()
        result = check_release(release)
        check(not result.ok, "a missing analyses.tsv should be refused")
        check(any("source.analyses" in error for error in result.errors), result.errors)


def test_excluded_rows_are_not_checked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, [])
        rows = [source_row(release, "BUILT", "built.vcf", b"b\n")]
        rows.append({
            "analysis_id": "EXCLUDED",
            "source_file": "absent.vcf",
            "checksum": "0" * 64,
            "checksum_algorithm": "sha256",
            "exclude_from_build": "true",
        })
        release = write_release(Path(tmp), HAPPY_BUILD_YAML, rows)
        (release / "source" / "built.vcf").write_bytes(b"b\n")
        result = check_release(release)
        check(result.ok, f"excluded rows should not be checked: {result.errors}")


def test_unknown_completion_command_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = HAPPY_BUILD_YAML.replace("command: complete-dense", "command: bogus-complete")
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(not result.ok, "an unknown reference_completion.command should be refused")
        check(any("reference_completion.command" in error for error in result.errors), result.errors)


def test_missing_completion_release_id_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = HAPPY_BUILD_YAML.replace("  family_release_id: r1-completed\n", "")
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(not result.ok, "an enabled reference_completion needs a family_release_id")
        check(any("reference_completion.family_release_id" in error for error in result.errors), result.errors)


def test_arguments_must_be_mapping() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = HAPPY_BUILD_YAML.replace(
            "  arguments:\n    store-id: example-dense\n    release-id: r1-observed\n    n-workers: 16\n    feature-flags: [alpha, beta]\n",
            "  arguments: []\n",
        )
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(not result.ok, "a non-mapping build.arguments should be refused")
        check(any("build.arguments" in error for error in result.errors), result.errors)


def test_no_shape_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        release = Path(tmp) / "release"
        release.mkdir()
        (release / "build.yaml").write_text("store_family_id: x\n", encoding="utf-8")
        (release / "analyses.tsv").write_text("analysis_id\n", encoding="utf-8")
        result = check_release(release)
        check(not result.ok, "a build.yaml with neither build.command nor builder.entrypoint should be refused")
        check(any("build.command" in error for error in result.errors), result.errors)


def test_legacy_rho_on_ragged_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = LEGACY_BUILD_YAML.replace("store_layout: dense-observed", "store_layout: ragged-observed") + (
            "rho:\n  enabled: true\n"
        )
        release = write_release(Path(tmp), yaml_text, [])
        result = check_release(release)
        check(not result.ok, "rho should be refused on a Ragged layout regardless of schema")
        check(any("rho.enabled" in error for error in result.errors), result.errors)


def test_legacy_shape_accepted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        release = write_release(Path(tmp), LEGACY_BUILD_YAML, [])
        rows = [source_row(release, "A", "a.vcf", b"a\n")]
        release = write_release(Path(tmp), LEGACY_BUILD_YAML, rows)

        plan = load_plan(release)
        check(plan.schema == "legacy", f"legacy build.yaml should load as legacy, got {plan.schema!r}")
        check(plan.build_command is None, "legacy plan should have no build.command")
        check(plan.builder_entrypoint == "opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest", plan.builder_entrypoint)
        check(plan.source_root == release / "source", f"legacy source_dir should resolve, got {plan.source_root}")
        check(plan.analyses_path == release / "analyses.tsv", f"unexpected analyses path {plan.analyses_path}")

        result = check_release(release)
        check(result.ok, f"legacy release should be accepted: {result.errors}")
        check(any("legacy" in warning for warning in result.warnings), result.warnings)

        completed = run_loader(str(release / "build.yaml"))
        check(completed.returncode == 0, f"CLI should accept legacy build.yaml: {completed.stdout}")
        check("legacy schema" in completed.stdout, completed.stdout)


def test_checked_in_legacy_release_loads() -> None:
    release = REPO_ROOT / "families" / "finngen-r13" / "releases" / "r13-pilot-20"
    plan = load_plan(release)
    check(plan.schema == "legacy", f"checked-in pre-#95 release should load as legacy, got {plan.schema!r}")
    check(plan.build_command is None, "pre-#95 release should not claim a CLI command")
    result = check_release(release, verify_checksums=False)
    check(result.ok, f"checked-in pre-#95 release should be accepted: {result.errors}")


def main() -> None:
    tests = [
        test_known_commands,
        test_happy_path,
        test_absolute_source_paths_resolve,
        test_unknown_command_refused,
        test_rho_on_hybrid_refused,
        test_rho_on_ragged_refused,
        test_rho_inferred_from_command_refused,
        test_rho_disabled_on_ragged_ok,
        test_unresolved_reference_refused,
        test_missing_source_file_refused,
        test_checksum_mismatch_refused,
        test_missing_source_root_refused,
        test_missing_analyses_refused,
        test_excluded_rows_are_not_checked,
        test_unknown_completion_command_refused,
        test_missing_completion_release_id_refused,
        test_arguments_must_be_mapping,
        test_no_shape_refused,
        test_legacy_rho_on_ragged_refused,
        test_legacy_shape_accepted,
        test_checked_in_legacy_release_loads,
    ]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} release-plan tests passed ({n_checks} checks)")


if __name__ == "__main__":
    main()
