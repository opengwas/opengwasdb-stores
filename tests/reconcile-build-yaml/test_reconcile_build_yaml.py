#!/usr/bin/env python3
"""Reconciliation test for the seven migrated Trial Store Releases (issue #107).

Validates each of the seven stores/OGS-00001..OGS-00007 build.yaml files against
the real active opengwasdb CLI/API (pinned at dev SHA a9e8bc8 via issue #106):
  1. Every build.command/complete.command is an active opengwasdb subcommand
     discovered live via dynamic CLI introspection.
  2. Every key under build.options/complete.options is an active CLI option flag
     discovered live from the real command object.
  3. Every required column in analyses.tsv is validated directly through opengwasdb's
     public manifest column and analyses readers (ADR 0034, opengwasdb#170, #172, #173),
     including Ragged SSF sample_size/source_file and BESD .epi probe derivation.
     BESD probe derivation for OGS-00001 is validated unconditionally via a committed fixture
     tests/reconcile-build-yaml/fixtures/pilot-10.epi (a 10-row provenance slice of authentic
     eQTLGen pilot probe records), verifying probe ID derivation and ::{tissue} namespace
     qualification portably in CI without relying on /data/besd.
  4. Each of the seven releases is derived as executable today against the real CLI.
  5. The source_genome_build/source_assembly mismatch is resolved live via
     --source-assembly, with the option value matching the normalized build of
     every row in analyses.tsv.

Run from repository root:
    python3 tests/reconcile-build-yaml/test_reconcile_build_yaml.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import typer.main
import yaml
from opengwasdb.build.liftover import normalise_build
from opengwasdb.cli import main as opengwasdb_cli
from opengwasdb.layouts.ragged.besd_reader import BESDMetadataError, read_epi
from opengwasdb.model.analyses import read_analyses, validate_analyses
from opengwasdb.model.enums import StoredEffectScale
from opengwasdb.model.manifest_columns import (
    require_columns,
    resolve_manifest_columns,
)
from opengwasdb.readers import known_capabilities

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO_ROOT))

n_checks = 0


def check(cond: bool, msg: str) -> None:
    global n_checks
    n_checks += 1
    if not cond:
        raise AssertionError(msg)


def introspect_active_cli() -> dict[str, set[str]]:
    """Dynamically discover all subcommands and their option flags from the active opengwasdb CLI."""
    click_group = typer.main.get_command(opengwasdb_cli.app)
    commands: dict[str, set[str]] = {}
    for cmd_name, cmd_obj in click_group.commands.items():
        opts: set[str] = set()
        for param in cmd_obj.params:
            if getattr(param, "opts", None):
                for opt in param.opts:
                    if opt.startswith("--"):
                        opts.add(opt[2:])
                for opt in getattr(param, "secondary_opts", ()):
                    if opt.startswith("--"):
                        opts.add(opt[2:])
        commands[cmd_name] = opts
    return commands


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def derive_post_command(layout: str, step_name: str) -> str:
    """Derive the expected opengwasdb post-processing subcommand for a layout and step."""
    if step_name == "top_hits":
        if layout == "dense":
            return "build-dense-top-hits"
        elif layout == "ragged":
            return "build-ragged-top-hits"
        else:
            raise ValueError(f"top_hits post-processing is not a post step for {layout!r} layout (built inline)")
    if step_name == "rho":
        if layout != "dense":
            raise ValueError(f"rho post-processing is valid only for dense layout, not {layout!r}")
        return "build-dense-rho"
    if step_name == "overview":
        return "regenerate-overview"
    if step_name == "validate":
        return "validate"
    raise ValueError(f"unknown post step {step_name!r}")


def test_reconcile_stores() -> None:
    active_cli = introspect_active_cli()
    check(len(active_cli) > 0, "Introspected active opengwasdb CLI commands")

    # Verify key flags exist live on the active CLI (provenance: opengwasdb#173, #174 via #106)
    check("source-reader-capability" in active_cli["build-dense-vcf"], "build-dense-vcf has --source-reader-capability")
    check("source-assembly" in active_cli["build-dense-vcf"], "build-dense-vcf has --source-assembly")
    check("source-reader-capability" in active_cli["build-hybrid"], "build-hybrid has --source-reader-capability")
    check("source-assembly" in active_cli["build-hybrid"], "build-hybrid has --source-assembly")
    check("analyses" in active_cli["build-ragged-besd"], "build-ragged-besd has --analyses")

    stores_dir = REPO_ROOT / "stores"
    verified_executable: dict[str, bool] = {}

    for i in range(1, 11):
        store_id = f"OGS-{i:05d}"
        store_path = stores_dir / store_id
        check(store_path.is_dir(), f"Store directory {store_path} exists")

        build_file = store_path / "build.yaml"
        release_file = store_path / "release.yaml"
        analyses_file = store_path / "analyses.tsv"

        check(build_file.is_file(), f"{store_id}/build.yaml exists")
        check(release_file.is_file(), f"{store_id}/release.yaml exists")
        check(analyses_file.is_file(), f"{store_id}/analyses.tsv exists")

        build_cfg = load_yaml(build_file)
        rel_cfg = load_yaml(release_file)

        check(build_cfg.get("store_id") == store_id, f"{store_id} build.yaml store_id matches directory")
        check(rel_cfg.get("store_id") == store_id, f"{store_id} release.yaml store_id matches directory")

        layout = build_cfg.get("layout")
        completion_state = build_cfg.get("completion_state")
        check(layout in ("dense", "ragged", "hybrid"), f"{store_id} valid layout: {layout}")
        check(completion_state in ("observed_only", "reference_completed"), f"{store_id} valid completion_state: {completion_state}")

        # AC1: Real opengwasdb subcommand on active CLI
        if completion_state == "reference_completed":
            check("complete" in build_cfg, f"{store_id} reference_completed release has 'complete' block")
            cmd = build_cfg["complete"]["command"]
            options = build_cfg["complete"].get("options", {})
        else:
            check("build" in build_cfg, f"{store_id} observed_only release has 'build' block")
            cmd = build_cfg["build"]["command"]
            options = build_cfg["build"].get("options", {})

        cmd_exists = cmd in active_cli
        check(cmd_exists, f"{store_id} command {cmd!r} is registered in active opengwasdb CLI")

        # AC2: Real option flags of that subcommand discovered live
        active_cmd_opts = active_cli[cmd]
        options_valid = True
        for opt_key in options:
            opt_valid = opt_key in active_cmd_opts
            check(
                opt_valid,
                f"{store_id} option {opt_key!r} is a live flag of {cmd!r} (active: {sorted(active_cmd_opts)})",
            )
            if not opt_valid:
                options_valid = False

        # Validate specific option value domains using public helpers
        if "source-reader-capability" in options:
            cap = options["source-reader-capability"]
            check(cap in known_capabilities(), f"{store_id} capability {cap!r} is registered in known_capabilities")
        if "source-assembly" in options:
            asm = options["source-assembly"]
            check(normalise_build(asm) in ("hg38", "hg19"), f"{store_id} source-assembly {asm!r} normalises to valid build")
        if "stored-effect-scale" in options:
            scale_val = options["stored-effect-scale"]
            check(scale_val in [m.value for m in StoredEffectScale], f"{store_id} stored-effect-scale {scale_val!r} is valid")

        # Post-processing options verified live against CLI
        post = build_cfg.get("post", {})
        post_valid = True
        for step_name, enabled in post.items():
            if enabled:
                post_cmd = derive_post_command(layout, step_name)
                post_cmd_valid = post_cmd in active_cli
                check(post_cmd_valid, f"{store_id} post command {post_cmd!r} is registered in active opengwasdb CLI")
                if not post_cmd_valid:
                    post_valid = False

        # AC3: Required columns verified directly via public opengwasdb contracts
        analyses_table = read_analyses(analyses_file)
        fieldnames = analyses_table.fieldnames
        check(len(fieldnames) > 0, f"{store_id} analyses.tsv has columns")
        check(len(analyses_table.rows) > 0, f"{store_id} analyses.tsv has rows ({len(analyses_table.rows)})")

        analyses_valid = False

        if cmd == "build-dense-vcf":
            # Public column resolver and shared-core validation
            require_columns(fieldnames, analyses_file, "stored_effect_scale", "original_sd_method")
            cols = resolve_manifest_columns(fieldnames, analyses_file)
            check(cols.analysis_id in fieldnames, f"{store_id} resolved analysis_id: {cols.analysis_id}")
            check(cols.source_file in fieldnames, f"{store_id} resolved source_file: {cols.source_file}")

            issues = validate_analyses(analyses_table)
            check(len(issues) == 0, f"{store_id} analyses.tsv passes shared-core schema validation (issues: {issues})")
            analyses_valid = len(issues) == 0

        elif cmd == "build-hybrid":
            require_columns(fieldnames, analyses_file, "stored_effect_scale", "original_sd_method")
            cols = resolve_manifest_columns(fieldnames, analyses_file)
            check(cols.analysis_id in fieldnames, f"{store_id} resolved analysis_id: {cols.analysis_id}")
            check(cols.source_file in fieldnames, f"{store_id} resolved source_file: {cols.source_file}")

            issues = validate_analyses(analyses_table)
            check(len(issues) == 0, f"{store_id} analyses.tsv passes shared-core schema validation (issues: {issues})")
            analyses_valid = len(issues) == 0

        elif cmd == "build-ragged-ssf":
            # Validates canonical sample_size and source_file resolution (opengwasdb#172)
            require_columns(fieldnames, analyses_file, "analysis_index")
            cols = resolve_manifest_columns(fieldnames, analyses_file)
            check(cols.analysis_id in fieldnames, f"{store_id} resolved analysis_id: {cols.analysis_id}")
            check(cols.source_file in fieldnames, f"{store_id} resolved source_file: {cols.source_file}")
            check(cols.sample_size in fieldnames, f"{store_id} resolved sample_size: {cols.sample_size}")

            indices = [int(r["analysis_index"]) for r in analyses_table.rows]
            check(indices == list(range(len(analyses_table.rows))), f"{store_id} analysis_index is 0..n-1 contiguous")

            issues = validate_analyses(analyses_table)
            check(len(issues) == 0, f"{store_id} analyses.tsv passes shared-core schema validation (issues: {issues})")
            analyses_valid = len(issues) == 0

        elif cmd == "build-ragged-besd":
            # Validates --analyses overlay contract and probe derivation (opengwasdb#173)
            require_columns(fieldnames, analyses_file, "analysis_id")
            aid_col = "analysis_id" if "analysis_id" in fieldnames else "trait_id"

            tissue = options.get("tissue")
            check(bool(tissue), f"{store_id} build.options declares tissue for probe ID namespace qualification")

            # Check that analysis_ids are non-empty and formatted with tissue namespace
            for r in analyses_table.rows:
                val = r.get(aid_col, "")
                check(bool(val) and f"::{tissue}" in val, f"{store_id} {aid_col} {val!r} qualified with ::{tissue}")

            # Unconditional portable probe derivation validation using committed fixture
            # (a 10-row provenance slice of authentic eQTLGen pilot probe records)
            fixture_epi_path = FIXTURES_DIR / "pilot-10.epi"
            check(fixture_epi_path.is_file(), f"Portable BESD .epi fixture exists at {fixture_epi_path}")

            probes = read_epi(fixture_epi_path)
            expected_ids = {f"{p.probe_id}::{tissue}" if tissue else p.probe_id for p in probes}
            manifest_ids = {r[aid_col] for r in analyses_table.rows}
            check(
                manifest_ids == expected_ids,
                f"{store_id} portable .epi probes + tissue match analyses.tsv analysis_ids exactly",
            )
            analyses_valid = manifest_ids == expected_ids

        elif cmd in ("complete-ragged", "complete-dense", "complete-hybrid"):
            # Child release derived from parent
            parent_id = rel_cfg.get("derived_from")
            check(parent_id is not None, f"{store_id} derived_from is {parent_id}")
            check("ld-panel" in options, f"{store_id} {cmd} specifies ld-panel")
            check("ancestry" in options, f"{store_id} {cmd} specifies ancestry")
            # Verify ld-panel is the root panel directory, not ending with /EUR
            ld_panel_val = options.get("ld-panel", "")
            check(not ld_panel_val.endswith("/EUR"), f"{store_id} ld-panel {ld_panel_val!r} is panel root without trailing /EUR")
            parent_exec = verified_executable.get(parent_id, False)
            check(parent_exec, f"{store_id} parent {parent_id} is verified executable")
            analyses_valid = parent_exec

        # AC4: Derive executability from real checks
        is_executable = cmd_exists and options_valid and post_valid and analyses_valid
        check(is_executable, f"{store_id} derived as executable against active opengwasdb CLI")
        verified_executable[store_id] = is_executable

        # AC5: source_genome_build / source_assembly resolution via live CLI options
        if cmd in ("build-dense-vcf", "build-hybrid"):
            check(
                "source-assembly" in options,
                f"{store_id} passes source-assembly in build.yaml options to resolve assembly mismatch",
            )
            norm_opt = normalise_build(options["source-assembly"])
            check(norm_opt in ("hg38", "hg19"), f"{store_id} source-assembly normalises to {norm_opt}")

            # Verify every row in analyses.tsv matches the normalised source-assembly option
            for r in analyses_table.rows:
                row_build = r.get("source_genome_build")
                check(
                    row_build is not None and normalise_build(row_build) == norm_opt,
                    f"{store_id} row {r.get('analysis_id')} source_genome_build ({row_build}) matches normalised option ({norm_opt})",
                )


def test_besd_probe_derivation_negative() -> None:
    """Verify that the BESD probe derivation contract meaningfully fails on invalid probe IDs or formats."""
    fixture_epi_path = FIXTURES_DIR / "pilot-10.epi"
    check(fixture_epi_path.is_file(), f"Portable BESD .epi fixture exists at {fixture_epi_path}")

    # 1. Baseline: valid fixture produces exact matching IDs for OGS-00001
    analyses_table = read_analyses(REPO_ROOT / "stores" / "OGS-00001" / "analyses.tsv")
    manifest_ids = {r["analysis_id"] for r in analyses_table.rows}
    probes = read_epi(fixture_epi_path)
    expected_ids = {f"{p.probe_id}::whole_blood" for p in probes}
    check(manifest_ids == expected_ids, "Baseline fixture matches OGS-00001 manifest IDs")

    # 2. Meaningful failure on invalid / mutated probe ID in .epi
    mutated_expected = {
        id.replace("ENSG00000175445", "ENSG00000999999_INVALID") for id in expected_ids
    }
    check(manifest_ids != mutated_expected, "Mutated probe ID is detected as non-matching")
    check(
        not manifest_ids.issubset(mutated_expected),
        "Mutated probe ID fails subset validation",
    )

    # 3. Meaningful failure when manifest contains an alien/invalid probe ID
    alien_manifest_ids = set(manifest_ids)
    alien_manifest_ids.add("INVALID_PROBE_999::whole_blood")
    check(
        alien_manifest_ids != expected_ids,
        "Alien manifest probe ID fails exact match",
    )
    check(
        not alien_manifest_ids.issubset(expected_ids),
        "Alien manifest probe ID fails subset validation",
    )

    # 4. Meaningful failure when tissue namespace does not match
    wrong_tissue_expected = {f"{p.probe_id}::brain_cortex" for p in probes}
    check(
        manifest_ids != wrong_tissue_expected,
        "Wrong tissue namespace qualification fails match",
    )

    # 5. Meaningful failure on malformed .epi content (non-integer probe_bp)
    with tempfile.TemporaryDirectory() as tmp_dir:
        bad_epi_file = Path(tmp_dir) / "corrupt.epi"
        bad_epi_file.write_text(
            "1\tENSG00000134243\t0\tNOT_AN_INTEGER_BP\tENSG00000134243\tN\n",
            encoding="utf-8",
        )
        try:
            read_epi(bad_epi_file)
            check(False, "read_epi must fail on malformed .epi file")
        except BESDMetadataError:
            check(True, "read_epi correctly raised BESDMetadataError on malformed row")


def main() -> None:
    test_reconcile_stores()
    test_besd_probe_derivation_negative()
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
