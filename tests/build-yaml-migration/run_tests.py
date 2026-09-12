#!/usr/bin/env python3
"""Migration of the seven Trial Store Releases to the executable `build.yaml`
schema (issue #97, ADR 0022). Run from the repository root:

    pixi run python tests/build-yaml-migration/run_tests.py

`scripts/migrate-build-yaml-executable.py` rewrites each accepted release's
`build.yaml` in place: the inert `builder.entrypoint` becomes `build.command`
(a real `opengwasdb` CLI subcommand) plus `build.arguments`, and the existing
`source:` block gains the fixed-input boundary (`root`, `analyses`). This suite
pins the ticket's contract:

* the authoritative seven-path inventory, and that no non-migrated sibling was
  swept up in it;
* the exact before/after text of the migration, so the diff stays reviewable
  (only the intended key changes, no reordering churn) and existing keys are
  carried over rather than replaced;
* idempotency -- migrating already-migrated text is a no-op;
* the entrypoint -> CLI subcommand derivation for every recorded entrypoint;
* the generator/migrated schema agreement for `eqtlgen-besd-ragged` (the R
  generators are covered by the sibling run_tests.R);
* all seven releases loading -- and, where their source files are present,
  validating -- under the #95 loader.

Requires opengwasdb (to enumerate its CLI subcommands), e.g. via `pixi run
test` (feature: store-build).
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "scripts" / "migrate-build-yaml-executable.py"

import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))
from resources.lib.release_plan import check_release, known_commands, load_plan  # noqa: E402
from resources.lib.release_yaml import parse_yaml_subset  # noqa: E402

# The authoritative seven Trial Store Releases: the epic matrix enumerated by
# issue #93 and docs/release-seam-audit.md. Held here, separately from the
# migration script, so a change to either list is caught rather than blessed.
SEVEN = [
    "families/eqtlgen-cis-pilot/releases/pilot-10",
    "families/eqtlgen-cis-pilot/releases/pilot-10-completed",
    "families/finngen-r13/releases/r13-pilot-20",
    "families/gwas-catalog-eur-hybrid/releases/eur-hybrid-pilot-10",
    "families/gwas-catalog-eur-hybrid/releases/eur-hybrid-quant-pilot-10",
    "families/metabolome-plasma-2023/releases/2023-chen-full-european",
    "families/pqtl-interval-2018/releases/2018-sun-pilot-10",
]

# Every recorded `builder.entrypoint` -> (CLI subcommand, carries store-id).
ENTRYPOINT_CASES = [
    ("opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest", "build-dense-vcf", True),
    ("opengwasdb.layouts.hybrid.build:build_hybrid_from_vcf_manifest", "build-hybrid", True),
    ("opengwasdb.layouts.ragged.build_ssf:build_ragged_from_ssf", "build-ragged-ssf", True),
    ("opengwasdb.layouts.ragged.build_besd:build_ragged_from_besd", "build-ragged-besd", True),
    ("opengwasdb.layouts.dense.complete:complete_dense_store", "complete-dense", False),
    ("opengwasdb.layouts.hybrid.complete:complete_hybrid_store", "complete-hybrid", False),
    ("opengwasdb.layouts.ragged.complete:complete_ragged_store", "complete-ragged", False),
]

_dense_legacy = """\
store_family_id: example-dense
family_release_id: r1-observed
store_layout: dense-observed
completion_state: observed-only
builder:
  package: opengwasdb
  entrypoint: opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest
source:
  source_format: gwas-vcf
  source_reader_capability: opengwasdb.gwas-vcf
  source_genome_build: GRCh37
normalisation:
  target_reference_assembly: GRCh38
  liftover: hg19-to-hg38
artifacts:
  artifact_root: /data/opengwasdb
  release_subdir: example/releases/r1
  source_dir: /data/opengwasdb/example/releases/r1/source
  store_uri: /data/opengwasdb/example/releases/r1/store.opengwasdb
notes: keep me
"""

# The exact expected output: only `builder:` -> `build:` and the two fixed-input
# `source:` keys move, in place. Nothing is reordered, nothing else changes.
_dense_expected = """\
store_family_id: example-dense
family_release_id: r1-observed
store_layout: dense-observed
completion_state: observed-only
build:
  command: build-dense-vcf
  arguments:
    store-id: example-dense
    release-id: r1-observed
source:
  root: /data/opengwasdb/example/releases/r1/source
  analyses: analyses.tsv
  source_format: gwas-vcf
  source_reader_capability: opengwasdb.gwas-vcf
  source_genome_build: GRCh37
normalisation:
  target_reference_assembly: GRCh38
  liftover: hg19-to-hg38
artifacts:
  artifact_root: /data/opengwasdb
  release_subdir: example/releases/r1
  source_dir: /data/opengwasdb/example/releases/r1/source
  store_uri: /data/opengwasdb/example/releases/r1/store.opengwasdb
notes: keep me
"""

n_checks = 0


def check(condition: bool, message: str) -> None:
    global n_checks
    n_checks += 1
    if not condition:
        raise AssertionError(message)


def load_migration() -> object:
    spec = importlib.util.spec_from_file_location("migrate_build_yaml", MIGRATION)
    assert spec is not None and spec.loader is not None, f"cannot load {MIGRATION}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_legacy(entrypoint: str, artifacts: dict[str, str]) -> str:
    """A minimal pre-#95 build.yaml recording `entrypoint` and `artifacts`."""
    artifact_lines = "\n".join(f"  {key}: {value}" for key, value in artifacts.items())
    return (
        "store_family_id: example-family\n"
        "family_release_id: r1\n"
        "store_layout: dense-observed\n"
        "completion_state: observed-only\n"
        f"builder:\n  package: opengwasdb\n  entrypoint: {entrypoint}\n"
        "source:\n  source_format: gwas-vcf\n"
        f"artifacts:\n{artifact_lines}\n"
    )


def test_inventory_is_the_documented_seven() -> None:
    migration = load_migration()
    check(migration.RELEASES == SEVEN, f"migration inventory drifted: {migration.RELEASES}")
    for relative in SEVEN:
        check((REPO_ROOT / relative / "build.yaml").is_file(), f"{relative}/build.yaml is missing")


def test_non_migrated_siblings_stay_legacy() -> None:
    # The migration is deliberately a closed list: ukb-b is a full-scale release
    # family, not one of the seven Trial Store Releases, and stays loadable as a
    # legacy plan (issue #95's compatibility promise). If this ever starts
    # failing, the migration has quietly widened its scope.
    sibling = REPO_ROOT / "families/ukb-b/releases/dense-observed-vcf-pilot-10/build.yaml"
    text = sibling.read_text(encoding="utf-8")
    check("builder:\n" in text and "entrypoint:" in text, "a non-migrated sibling was migrated")
    check("build:\n" not in text, "a non-migrated sibling grew a build: block")


def test_all_seven_are_executable() -> None:
    for relative in SEVEN:
        build_yaml = REPO_ROOT / relative / "build.yaml"
        text = build_yaml.read_text(encoding="utf-8")
        check("builder:" not in text, f"{relative}: builder.entrypoint survived the migration")
        check("entrypoint:" not in text, f"{relative}: entrypoint survived the migration")
        plan = load_plan(REPO_ROOT / relative)
        check(plan.schema == "cli", f"{relative}: still loads as {plan.schema!r} schema")
        check(plan.build_command in known_commands(), f"{relative}: unknown command {plan.build_command!r}")
        check(bool(plan.build_arguments), f"{relative}: build.arguments is empty")
        check(plan.source_root is not None, f"{relative}: no source.root")
        check(plan.analyses_path is not None, f"{relative}: no source.analyses")


def test_seven_load_and_validate_under_the_loader() -> None:
    validated: list[str] = []
    loaded_only: list[str] = []
    for relative in SEVEN:
        release = REPO_ROOT / relative
        plan = load_plan(release)
        assert plan.source_root is not None
        if plan.source_root.is_dir():
            result = check_release(release, verify_checksums=False)
            check(result.ok, f"{relative}: loader refused a migrated release: {result.errors}")
            validated.append(relative)
        else:
            # CI has no /data/opengwasdb, so the fixed input cannot be checked
            # there; the plan must still load, and that must be the only reason
            # a full check is skipped.
            loaded_only.append(relative)
    check(len(validated) + len(loaded_only) == len(SEVEN), "not every release was exercised")
    print(f"  seven releases: {len(validated)} fully validated, {len(loaded_only)} loaded (source data absent)")


def test_migration_is_idempotent() -> None:
    migration = load_migration()
    # Already-migrated text is byte-identical after another migration pass.
    check(migration.migrate_text(_dense_expected) == _dense_expected, "re-migrating migrated text changed it")
    for relative in SEVEN:
        build_yaml = REPO_ROOT / relative / "build.yaml"
        before = build_yaml.read_text(encoding="utf-8")
        check(migration.migrate_text(before) == before, f"{relative}: migration is not idempotent")
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / "release"
            copied.mkdir()
            (copied / "build.yaml").write_text(before, encoding="utf-8")
            check(migration.migrate_release(copied) is False, f"{relative}: re-ran the migration")
            check((copied / "build.yaml").read_text(encoding="utf-8") == before, f"{relative}: bytes changed")


def test_exact_before_after_text() -> None:
    migration = load_migration()
    check(
        migration.migrate_text(_dense_legacy) == _dense_expected,
        "the migration changed more than the intended keys (or reordered lines)",
    )
    # Existing keys are carried over verbatim, not re-shaped.
    before = parse_yaml_subset(_dense_legacy)
    after = parse_yaml_subset(migration.migrate_text(_dense_legacy))
    for key in ("store_family_id", "family_release_id", "store_layout", "completion_state",
                "normalisation", "artifacts", "notes"):
        check(after.get(key) == before.get(key), f"{key} was not preserved")
    source_before = before["source"]
    source_after = after["source"]
    check(
        {k: v for k, v in source_after.items() if k not in {"root", "analyses"}} == source_before,
        "the source block lost or reshaped an existing key",
    )


def test_entrypoint_derivation() -> None:
    migration = load_migration()
    check(
        set(migration.ENTRYPOINT_COMMANDS) == {case[0] for case in ENTRYPOINT_CASES},
        "the entrypoint table drifted from the recorded entrypoints",
    )
    for entrypoint, command, carries_store_id in ENTRYPOINT_CASES:
        migrated = parse_yaml_subset(migration.migrate_text(synthetic_legacy(
            entrypoint, {"source_dir": "/data/example/source"}
        )))
        build = migrated["build"]
        check(build["command"] == command, f"{entrypoint}: wrong command {build['command']!r}")
        arguments = build["arguments"]
        check(arguments["release-id"] == "r1", f"{entrypoint}: release-id not derived")
        check(
            ("store-id" in arguments) is carries_store_id,
            f"{entrypoint}: store-id incorrectly {'missing' if carries_store_id else 'present'}",
        )
        check(command in known_commands(), f"{command!r} is not a real opengwasdb CLI subcommand")


def test_source_root_derived_from_artifacts() -> None:
    migration = load_migration()
    entrypoint = "opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest"
    cases = [
        ({"source_dir": "/a/source"}, "/a/source"),
        ({"filtered_dir": "/b/filtered"}, "/b/filtered"),
        ({"download_dir": "/c/download"}, "/c/download"),
        ({"source_dir": "/a/source", "filtered_dir": "/b/filtered"}, "/a/source"),
    ]
    for artifacts, expected in cases:
        migrated = parse_yaml_subset(migration.migrate_text(synthetic_legacy(entrypoint, artifacts)))
        check(migrated["source"]["root"] == expected, f"{artifacts}: root {migrated['source']['root']!r}")
        check(migrated["source"]["analyses"] == "analyses.tsv", "analyses must be the bundle-local TSV")


def test_no_source_dir_is_refused() -> None:
    migration = load_migration()
    try:
        migration.migrate_text(synthetic_legacy(
            "opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest", {"store_uri": "/x/store"}
        ))
    except SystemExit as error:
        check("source_dir" in str(error), f"unhelpful refusal: {error}")
    else:
        raise AssertionError("a release with no source path must not silently migrate")


def test_eqtlgen_generator_agrees_with_migrated_schema() -> None:
    """The Python generator's emitted schema matches the migrated release."""
    spec = importlib.util.spec_from_file_location(
        "eqtlgen_generate", REPO_ROOT / "resources/generators/eqtlgen-besd-ragged/generate.py"
    )
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    cfg = generator.read_release_yaml(
        REPO_ROOT / "families/eqtlgen-cis-pilot/generators/config-pilot-10.yaml"
    )
    artifact_dir = Path(generator.require_text(cfg, "output", "artifact_root")) / generator.require_text(
        cfg, "output", "artifact_subdir"
    )
    document = generator.build_yaml_document(
        store_family_id=generator.require_text(cfg, "store_family_id"),
        family_release_id=generator.require_text(cfg, "family_release_id"),
        association_coverage=generator.require_text(cfg, "association_coverage"),
        source_genome_build=generator.require_text(cfg, "source", "source_genome_build"),
        source_dir=artifact_dir / "source",
        artifact_root=generator.require_text(cfg, "output", "artifact_root"),
        artifact_subdir=generator.require_text(cfg, "output", "artifact_subdir"),
        store_dir=Path("/unused/store"),
    )
    migrated = parse_yaml_subset(
        (REPO_ROOT / "families/eqtlgen-cis-pilot/releases/pilot-10/build.yaml").read_text(encoding="utf-8")
    )
    check(document["build"] == migrated["build"], "generator and migrated releases disagree on build.command")
    for key in ("root", "analyses", "source_format", "source_reader_capability"):
        check(
            document["source"][key] == migrated["source"][key],
            f"generator and migrated releases disagree on source.{key}",
        )
    check("builder" not in document, "the generator still emits the legacy builder block")


def main() -> None:
    tests = [
        test_inventory_is_the_documented_seven,
        test_non_migrated_siblings_stay_legacy,
        test_all_seven_are_executable,
        test_seven_load_and_validate_under_the_loader,
        test_migration_is_idempotent,
        test_exact_before_after_text,
        test_entrypoint_derivation,
        test_source_root_derived_from_artifacts,
        test_no_source_dir_is_refused,
        test_eqtlgen_generator_agrees_with_migrated_schema,
    ]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} build.yaml migration tests passed ({n_checks} checks)")


if __name__ == "__main__":
    main()
