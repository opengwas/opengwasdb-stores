#!/usr/bin/env python3
"""Tests for ogstores.plan: pure Bundle -> list[Step] orchestration mapping.

Verifies:
- OGS-00003 Dense observed-only golden test against external JSON fixture.
- OGS-00004 and OGS-00005 Hybrid observed-only golden tests against external JSON fixtures.
- OGS-00006 and OGS-00007 Ragged SSF observed-only golden tests against external JSON fixtures.
- Post-step sequencing: build -> top-hits (dense/ragged) -> rho (dense only) -> overview -> validate.
- Hybrid top-hits policy: top hits are built inline during build-hybrid, so no redundant/invalid
  dense-root build-dense-top-hits step is planned; setting post.top_hits=true on Hybrid is rejected.
- Ragged top-hits policy: build-ragged-top-hits is planned for ragged layout when post.top_hits=true.
- Negative assertions:
  - no dense-root top-hits command is planned for hybrid releases.
  - no dense-root top-hits command is planned for ragged releases.
  - no rho command is planned for non-dense layouts.
- Pass-through of unknown build.options keys verbatim without interpretation:
  - Exact preservation of keys (no underscore conversion, no semantic stripping of no- prefix).
  - Uniform --no-<key> for False booleans.
  - List-valued option repetition.
  - Generic --reference-panel option flow from build.options with zero special handling.
- Identity flags --store-id <family> --release-id <store-id>.
- Dense-only rho enforcement (forbidden on Hybrid and other non-Dense layouts).
- Shared post-step builder mechanism parameterised by planner-specific commands.
- Dispatch table keyed on (layout, completion_state) with Hybrid as table entry.
- Real pinned CLI validation for every planned step argv.
- Pure function execution with tripwire proof for Path.stat, Path.exists, Path.is_file, etc.
"""

from __future__ import annotations

import builtins
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import typer.main
from opengwasdb.cli import main as opengwasdb_cli

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ogstores import paths
from ogstores.bundle import Bundle, load
from ogstores.plan import (
    DISPATCH_TABLE,
    RAGGED_BUILD_DISPATCH,
    Step,
    plan,
    render_options,
)

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


def assert_steps_match_golden(
    test_case: unittest.TestCase,
    actual_steps: list[Step],
    golden_path: Path,
) -> None:
    """Helper to assert that actual_steps match an external JSON golden fixture exactly."""
    test_case.assertTrue(golden_path.is_file(), f"Golden file {golden_path} must exist")
    with open(golden_path, "r", encoding="utf-8") as f:
        golden_raw = json.load(f)

    expected_steps = [
        Step(
            name=item["name"],
            argv=item["argv"],
            inputs=[Path(p) for p in item["inputs"]],
            outputs=[Path(p) for p in item["outputs"]],
        )
        for item in golden_raw
    ]

    record_check()
    test_case.assertEqual(len(actual_steps), len(expected_steps))

    for idx, (actual, expected) in enumerate(zip(actual_steps, expected_steps)):
        record_check()
        test_case.assertEqual(
            actual.name,
            expected.name,
            f"Step {idx} name mismatch: {actual.name} != {expected.name}",
        )
        test_case.assertEqual(
            actual.argv,
            expected.argv,
            f"Step {idx} ({actual.name}) argv mismatch:\nActual:   {actual.argv}\nExpected: {expected.argv}",
        )
        test_case.assertEqual(
            actual.inputs,
            expected.inputs,
            f"Step {idx} ({actual.name}) inputs mismatch:\nActual:   {actual.inputs}\nExpected: {expected.inputs}",
        )
        test_case.assertEqual(
            actual.outputs,
            expected.outputs,
            f"Step {idx} ({actual.name}) outputs mismatch:\nActual:   {actual.outputs}\nExpected: {expected.outputs}",
        )
        test_case.assertEqual(actual, expected)


def validate_step_argv_against_cli(test_case: unittest.TestCase, step: Step) -> None:
    """Validate that step.argv parses against the real pinned opengwasdb Click/Typer CLI."""
    click_group = typer.main.get_command(opengwasdb_cli.app)
    test_case.assertGreaterEqual(len(step.argv), 2, "argv must contain at least ['opengwasdb', <cmd>]")
    test_case.assertEqual(step.argv[0], "opengwasdb")

    cmd_name = step.argv[1]
    cmd_args = step.argv[2:]

    test_case.assertIn(
        cmd_name,
        click_group.commands,
        f"Command {cmd_name!r} not found in active opengwasdb CLI",
    )
    cmd = click_group.commands[cmd_name]
    try:
        ctx = cmd.make_context(cmd_name, list(cmd_args))
        record_check()
        test_case.assertIsNotNone(ctx)
    except Exception as exc:
        test_case.fail(
            f"Failed to parse argv for step {step.name!r} ({cmd_name}) against CLI:\n"
            f"Argv: {step.argv}\nError: {exc}"
        )


class TestPlanDense(unittest.TestCase):
    """Test suite for plan() focusing on Dense observed-only layout (OGS-00003)."""

    def setUp(self) -> None:
        self.bundle_00003 = load("OGS-00003")
        self.golden_path = REPO_ROOT / "tests" / "plan" / "golden" / "OGS-00003.json"

    def test_ogs_00003_golden_steps(self) -> None:
        """OGS-00003 produces exact golden Steps matching tests/plan/golden/OGS-00003.json."""
        actual_steps = plan(self.bundle_00003)
        assert_steps_match_golden(self, actual_steps, self.golden_path)

    def test_dense_argv_validation_against_real_cli(self) -> None:
        """Every argv produced for OGS-00003 parses against the real pinned opengwasdb CLI."""
        steps = plan(self.bundle_00003)
        for step in steps:
            validate_step_argv_against_cli(self, step)

    def test_render_options_passthrough_and_preservation(self) -> None:
        """render_options() preserves option keys verbatim and handles booleans uniformly."""
        options = {
            "source-reader-capability": "opengwasdb.finngen-r13",
            "source-assembly": "hg38",
            "n-workers": 16,
            "chunk-variants": 5000,
            "min-cor": 0.85,
            "allow-unverified-eaf": True,
            "overwrite": False,
            # Unknown key with underscore preserved
            "custom_underscore_flag": "custom_val",
            # Unknown boolean with underscore
            "custom_bool_flag": False,
            # Existing no- prefix with True
            "no-overwrite": True,
            # Existing no- prefix with False -> uniform --no-no-overwrite (no semantic stripping)
            "no-something": False,
            # Integer and float scalar values
            "opaque-int": 100,
            "opaque-float": 0.123,
        }
        rendered = render_options(options)
        expected = [
            "--source-reader-capability",
            "opengwasdb.finngen-r13",
            "--source-assembly",
            "hg38",
            "--n-workers",
            "16",
            "--chunk-variants",
            "5000",
            "--min-cor",
            "0.85",
            "--allow-unverified-eaf",
            "--no-overwrite",
            "--custom_underscore_flag",
            "custom_val",
            "--no-custom_bool_flag",
            "--no-overwrite",
            "--no-no-something",
            "--opaque-int",
            "100",
            "--opaque-float",
            "0.123",
        ]
        record_check()
        self.assertEqual(rendered, expected)

    def test_render_options_list_valued(self) -> None:
        """render_options() repeats the flag for each item in list/tuple-valued options."""
        options = {
            "filter": ["maf>0.01", "info>0.8"],
            "chrom": (1, 2, 22),
            "single": "val",
        }
        rendered = render_options(options)
        expected = [
            "--filter",
            "maf>0.01",
            "--filter",
            "info>0.8",
            "--chrom",
            "1",
            "--chrom",
            "2",
            "--chrom",
            "22",
            "--single",
            "val",
        ]
        record_check()
        self.assertEqual(rendered, expected)

    def test_render_options_empty_and_none(self) -> None:
        """render_options handles None, empty dicts, and None values."""
        record_check()
        self.assertEqual(render_options(None), [])
        record_check()
        self.assertEqual(render_options({}), [])
        record_check()
        self.assertEqual(render_options({"skipped": None, "active": "yes"}), ["--active", "yes"])

    def test_identity_flags_composition(self) -> None:
        """Identity flags --store-id <family> --release-id <store_id> are composed from registry facts."""
        synthetic_bundle = Bundle(
            store_id="OGS-00099",
            root=Path("stores/OGS-00099"),
            release={
                "store_id": "OGS-00099",
                "family": "custom-family-name",
                "label": "test-label",
                "status": "accepted",
            },
            build={
                "store_id": "OGS-00099",
                "layout": "dense",
                "completion_state": "observed_only",
                "build": {
                    "command": "build-dense-vcf",
                    "options": {"n-workers": 4},
                },
                "post": {"top_hits": False, "rho": False, "overview": False, "validate": False},
                "artifacts": {"root": "/custom/artifact/root"},
            },
            analyses_path=Path("stores/OGS-00099/analyses.tsv"),
        )
        steps = plan(synthetic_bundle)
        record_check()
        self.assertEqual(len(steps), 1)
        build_step = steps[0]
        self.assertEqual(build_step.name, "build")
        self.assertIn("--store-id", build_step.argv)
        self.assertIn("custom-family-name", build_step.argv)
        self.assertIn("--release-id", build_step.argv)
        self.assertIn("OGS-00099", build_step.argv)

        idx_store = build_step.argv.index("--store-id")
        self.assertEqual(build_step.argv[idx_store + 1], "custom-family-name")
        idx_rel = build_step.argv.index("--release-id")
        self.assertEqual(build_step.argv[idx_rel + 1], "OGS-00099")

        # Check custom artifact root propagation
        record_check()
        self.assertEqual(
            build_step.outputs, [Path("/custom/artifact/root/OGS-00099/store.opengwasdb")]
        )

    def test_dense_rho_enabled_order(self) -> None:
        """When rho is enabled on Dense, build-dense-rho runs between top-hits and overview."""
        bundle_with_rho = Bundle(
            store_id="OGS-00003",
            root=Path("stores/OGS-00003"),
            release=self.bundle_00003.release,
            build={
                **self.bundle_00003.build,
                "post": {
                    "top_hits": True,
                    "rho": True,
                    "overview": True,
                    "validate": True,
                },
            },
            analyses_path=self.bundle_00003.analyses_path,
        )
        steps = plan(bundle_with_rho)
        record_check()
        self.assertEqual([s.name for s in steps], ["build", "top-hits", "rho", "overview", "validate"])
        store_p = Path("/data/opengwasdb/stores/OGS-00003/store.opengwasdb")
        record_check()
        self.assertEqual(
            steps[2],
            Step(
                name="rho",
                argv=["opengwasdb", "build-dense-rho", str(store_p)],
                inputs=[store_p],
                outputs=[store_p],
            ),
        )

    def test_post_flags_selective_toggle(self) -> None:
        """Individual post flags toggle corresponding steps on and off."""
        bundle_val_only = Bundle(
            store_id="OGS-00003",
            root=Path("stores/OGS-00003"),
            release=self.bundle_00003.release,
            build={
                **self.bundle_00003.build,
                "post": {
                    "top_hits": False,
                    "rho": False,
                    "overview": False,
                    "validate": True,
                },
            },
            analyses_path=self.bundle_00003.analyses_path,
        )
        steps = plan(bundle_val_only)
        record_check()
        self.assertEqual([s.name for s in steps], ["build", "validate"])

    def test_dispatch_table_keying(self) -> None:
        """Dispatch table is keyed by (layout, completion_state) across all 6 layout/state pairs."""
        # Dispatch table contains all 6 valid combinations
        expected_keys = [
            ("dense", "observed_only"),
            ("hybrid", "observed_only"),
            ("ragged", "observed_only"),
            ("dense", "reference_completed"),
            ("hybrid", "reference_completed"),
            ("ragged", "reference_completed"),
        ]
        for key in expected_keys:
            record_check()
            self.assertIn(key, DISPATCH_TABLE)
            self.assertTrue(callable(DISPATCH_TABLE[key]))

        # Calling with unconfigured combination raises NotImplementedError
        unsupported_bundle = Bundle(
            store_id="OGS-00003",
            root=Path("stores/OGS-00003"),
            release={"store_id": "OGS-00003", "family": "finngen-r13"},
            build={
                "store_id": "OGS-00003",
                "layout": "dense",
                "completion_state": "unknown_state",
                "build": {"command": "build-dense-vcf"},
                "post": {},
                "artifacts": {"root": "/data/opengwasdb/stores"},
            },
            analyses_path=Path("stores/OGS-00003/analyses.tsv"),
        )
        with self.assertRaises(NotImplementedError) as ctx:
            plan(unsupported_bundle)
        record_check()
        self.assertIn("('dense', 'unknown_state')", str(ctx.exception))

    def test_pure_function_no_io_and_tripwire_proof(self) -> None:
        """plan() performs no file or network I/O; prove tripwires catch any violations."""

        def forbidden_io(*args, **kwargs):
            raise AssertionError(f"Forbidden I/O called with args={args}, kwargs={kwargs}")

        # 1. Guard all filesystem inspection methods during plan()
        with mock.patch("builtins.open", side_effect=forbidden_io):
            with mock.patch.object(Path, "open", side_effect=forbidden_io):
                with mock.patch.object(Path, "exists", side_effect=forbidden_io):
                    with mock.patch.object(Path, "is_file", side_effect=forbidden_io):
                        with mock.patch.object(Path, "is_dir", side_effect=forbidden_io):
                            with mock.patch.object(Path, "stat", side_effect=forbidden_io):
                                with mock.patch("os.path.exists", side_effect=forbidden_io):
                                    with mock.patch("os.stat", side_effect=forbidden_io):
                                        steps1 = plan(self.bundle_00003)
                                        steps2 = plan(self.bundle_00003)
                                        record_check()
                                        self.assertEqual(steps1, steps2)

        # 2. Prove each tripwire actively catches calls when they occur
        with mock.patch("builtins.open", side_effect=forbidden_io):
            with self.assertRaises(AssertionError):
                open("/tmp/forbidden_test", "r")
            record_check()

        with mock.patch.object(Path, "exists", side_effect=forbidden_io):
            with self.assertRaises(AssertionError):
                Path("/tmp/forbidden_test").exists()
            record_check()

        with mock.patch.object(Path, "is_file", side_effect=forbidden_io):
            with self.assertRaises(AssertionError):
                Path("/tmp/forbidden_test").is_file()
            record_check()

        with mock.patch.object(Path, "is_dir", side_effect=forbidden_io):
            with self.assertRaises(AssertionError):
                Path("/tmp/forbidden_test").is_dir()
            record_check()

        with mock.patch.object(Path, "stat", side_effect=forbidden_io):
            with self.assertRaises(AssertionError):
                Path("/tmp/forbidden_test").stat()
            record_check()

    def test_override_artifact_root_parameter(self) -> None:
        """Passing explicit artifact_root to plan() overrides build.yaml artifacts.root."""
        steps = plan(self.bundle_00003, artifact_root="/temporary/scratch/root")
        expected_store_p = Path("/temporary/scratch/root/OGS-00003/store.opengwasdb")
        record_check()
        self.assertEqual(steps[0].outputs, [expected_store_p])
        self.assertEqual(steps[0].argv[3], str(expected_store_p))
        self.assertEqual(steps[1].argv[2], str(expected_store_p))


class TestPlanHybrid(unittest.TestCase):
    """Test suite for plan() focusing on Hybrid observed-only layout (OGS-00004 and OGS-00005)."""

    def setUp(self) -> None:
        self.bundle_00004 = load("OGS-00004")
        self.bundle_00005 = load("OGS-00005")
        self.golden_path_00004 = REPO_ROOT / "tests" / "plan" / "golden" / "OGS-00004.json"
        self.golden_path_00005 = REPO_ROOT / "tests" / "plan" / "golden" / "OGS-00005.json"

    def test_ogs_00004_golden_steps(self) -> None:
        """OGS-00004 produces exact golden Steps matching tests/plan/golden/OGS-00004.json."""
        actual_steps = plan(self.bundle_00004)
        assert_steps_match_golden(self, actual_steps, self.golden_path_00004)

    def test_ogs_00005_golden_steps(self) -> None:
        """OGS-00005 produces exact golden Steps matching tests/plan/golden/OGS-00005.json."""
        actual_steps = plan(self.bundle_00005)
        assert_steps_match_golden(self, actual_steps, self.golden_path_00005)

    def test_no_dense_root_top_hits_command_planned(self) -> None:
        """Negative assertion: no dense-root top-hits command is planned for hybrid releases."""
        for bundle in (self.bundle_00004, self.bundle_00005):
            steps = plan(bundle)
            step_names = [s.name for s in steps]
            record_check()
            self.assertNotIn("top-hits", step_names, f"{bundle.store_id} should not plan a top-hits step")
            for step in steps:
                record_check()
                self.assertNotIn("build-dense-top-hits", step.argv)
                self.assertNotIn("build-ragged-top-hits", step.argv)

    def test_hybrid_argv_validation_against_real_cli(self) -> None:
        """Every argv produced for OGS-00004 and OGS-00005 parses against the real pinned CLI."""
        for bundle in (self.bundle_00004, self.bundle_00005):
            steps = plan(bundle)
            for step in steps:
                validate_step_argv_against_cli(self, step)

    def test_reference_panel_flows_generically(self) -> None:
        """--reference-panel flows generically from build.options with zero special handling."""
        synthetic_bundle = Bundle(
            store_id="OGS-00094",
            root=Path("stores/OGS-00094"),
            release={
                "store_id": "OGS-00094",
                "family": "custom-hybrid-family",
                "status": "accepted",
            },
            build={
                "store_id": "OGS-00094",
                "layout": "hybrid",
                "completion_state": "observed_only",
                "build": {
                    "command": "build-hybrid",
                    "options": {
                        "reference-panel": "/custom/path/to/panel_alids.txt",
                        "source-reader-capability": "opengwasdb.gwas-ssf",
                        "source-assembly": "hg38",
                        "chunk-variants": 2000,
                        "allow-unverified-eaf": True,
                    },
                },
                "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
                "artifacts": {"root": "/custom/artifact/root"},
            },
            analyses_path=Path("stores/OGS-00094/analyses.tsv"),
        )
        steps = plan(synthetic_bundle)
        build_step = steps[0]
        record_check()
        self.assertIn("--reference-panel", build_step.argv)
        idx = build_step.argv.index("--reference-panel")
        self.assertEqual(build_step.argv[idx + 1], "/custom/path/to/panel_alids.txt")

        # Verify against CLI parser too
        validate_step_argv_against_cli(self, build_step)

    def test_hybrid_top_hits_post_step_rejected(self) -> None:
        """Configuring top_hits: true on a Hybrid layout raises ValueError (built inline by build-hybrid)."""
        bundle_invalid_top_hits = Bundle(
            store_id="OGS-00004",
            root=Path("stores/OGS-00004"),
            release=self.bundle_00004.release,
            build={
                **self.bundle_00004.build,
                "post": {
                    "top_hits": True,
                    "rho": False,
                    "overview": True,
                    "validate": True,
                },
            },
            analyses_path=self.bundle_00004.analyses_path,
        )
        with self.assertRaises(ValueError) as ctx:
            plan(bundle_invalid_top_hits)
        record_check()
        self.assertIn("top_hits post-processing is not supported or built inline for hybrid layout", str(ctx.exception))

    def test_hybrid_rho_rejected(self) -> None:
        """Configuring rho: true on a Hybrid layout raises ValueError (rho is Dense only)."""
        bundle_invalid_rho = Bundle(
            store_id="OGS-00004",
            root=Path("stores/OGS-00004"),
            release=self.bundle_00004.release,
            build={
                **self.bundle_00004.build,
                "post": {
                    "top_hits": False,
                    "rho": True,
                    "overview": True,
                    "validate": True,
                },
            },
            analyses_path=self.bundle_00004.analyses_path,
        )
        with self.assertRaises(ValueError) as ctx:
            plan(bundle_invalid_rho)
        record_check()
        self.assertIn("rho post-processing is valid only for dense layout, not 'hybrid'", str(ctx.exception))

    def test_hybrid_post_flags_selective_toggle(self) -> None:
        """Individual post flags toggle corresponding steps for hybrid layout."""
        bundle_val_only = Bundle(
            store_id="OGS-00004",
            root=Path("stores/OGS-00004"),
            release=self.bundle_00004.release,
            build={
                **self.bundle_00004.build,
                "post": {
                    "top_hits": False,
                    "rho": False,
                    "overview": False,
                    "validate": True,
                },
            },
            analyses_path=self.bundle_00004.analyses_path,
        )
        steps = plan(bundle_val_only)
        record_check()
        self.assertEqual([s.name for s in steps], ["build", "validate"])

    def test_hybrid_override_artifact_root(self) -> None:
        """Passing explicit artifact_root to plan() overrides build.yaml artifacts.root for hybrid."""
        steps = plan(self.bundle_00004, artifact_root="/temporary/scratch/root")
        expected_store_p = Path("/temporary/scratch/root/OGS-00004/store.opengwasdb")
        record_check()
        self.assertEqual(steps[0].outputs, [expected_store_p])
        self.assertEqual(steps[0].argv[3], str(expected_store_p))
        self.assertEqual(steps[1].argv[2], str(expected_store_p))
        self.assertEqual(steps[2].argv[2], str(expected_store_p))

    def test_hybrid_pure_function_no_io_and_tripwires(self) -> None:
        """plan() on hybrid performs no file or network I/O; tripwires guard calls."""

        def forbidden_io(*args, **kwargs):
            raise AssertionError(f"Forbidden I/O called with args={args}, kwargs={kwargs}")

        with mock.patch("builtins.open", side_effect=forbidden_io):
            with mock.patch.object(Path, "open", side_effect=forbidden_io):
                with mock.patch.object(Path, "exists", side_effect=forbidden_io):
                    with mock.patch.object(Path, "is_file", side_effect=forbidden_io):
                        with mock.patch.object(Path, "is_dir", side_effect=forbidden_io):
                            with mock.patch.object(Path, "stat", side_effect=forbidden_io):
                                with mock.patch("os.path.exists", side_effect=forbidden_io):
                                    with mock.patch("os.stat", side_effect=forbidden_io):
                                        steps4_a = plan(self.bundle_00004)
                                        steps4_b = plan(self.bundle_00004)
                                        steps5_a = plan(self.bundle_00005)
                                        steps5_b = plan(self.bundle_00005)
                                        record_check()
                                        self.assertEqual(steps4_a, steps4_b)
                                        record_check()
                                        self.assertEqual(steps5_a, steps5_b)


class TestPlanRagged(unittest.TestCase):
    """Test suite for plan() focusing on Ragged layout (BESD OGS-00001 and SSF OGS-00006, OGS-00007)."""

    def setUp(self) -> None:
        self.bundle_00001 = load("OGS-00001")
        self.bundle_00006 = load("OGS-00006")
        self.bundle_00007 = load("OGS-00007")
        self.golden_path_00001 = REPO_ROOT / "tests" / "plan" / "golden" / "OGS-00001.json"
        self.golden_path_00006 = REPO_ROOT / "tests" / "plan" / "golden" / "OGS-00006.json"
        self.golden_path_00007 = REPO_ROOT / "tests" / "plan" / "golden" / "OGS-00007.json"

    def test_ogs_00001_golden_steps(self) -> None:
        """OGS-00001 (BESD) produces exact golden Steps matching tests/plan/golden/OGS-00001.json."""
        actual_steps = plan(self.bundle_00001)
        assert_steps_match_golden(self, actual_steps, self.golden_path_00001)

    def test_ogs_00006_golden_steps(self) -> None:
        """OGS-00006 (SSF) produces exact golden Steps matching tests/plan/golden/OGS-00006.json."""
        actual_steps = plan(self.bundle_00006)
        assert_steps_match_golden(self, actual_steps, self.golden_path_00006)

    def test_ogs_00007_golden_steps(self) -> None:
        """OGS-00007 (SSF) produces exact golden Steps matching tests/plan/golden/OGS-00007.json."""
        actual_steps = plan(self.bundle_00007)
        assert_steps_match_golden(self, actual_steps, self.golden_path_00007)

    def test_ragged_argv_validation_against_real_cli(self) -> None:
        """Every argv produced for OGS-00001, OGS-00006, and OGS-00007 parses against real CLI."""
        for bundle in (self.bundle_00001, self.bundle_00006, self.bundle_00007):
            steps = plan(bundle)
            for step in steps:
                validate_step_argv_against_cli(self, step)

    def test_ragged_ssf_top_hits_step_planned(self) -> None:
        """Ragged SSF observed-only with top_hits: true plans build-ragged-top-hits."""
        for bundle in (self.bundle_00006, self.bundle_00007):
            steps = plan(bundle)
            step_names = [s.name for s in steps]
            record_check()
            self.assertIn("top-hits", step_names)
            top_hit_step = next(s for s in steps if s.name == "top-hits")
            self.assertEqual(
                top_hit_step.argv,
                ["opengwasdb", "build-ragged-top-hits", str(paths.store_path(bundle.store_id))],
            )
            # Negative assertions: no dense top-hits, no rho command
            for s in steps:
                record_check()
                self.assertNotIn("build-dense-top-hits", s.argv)
                self.assertNotIn("build-dense-rho", s.argv)

    def test_ragged_besd_top_hits_rejected(self) -> None:
        """Configuring top_hits: true on a Ragged BESD layout raises ValueError (built inline)."""
        bundle_invalid_top_hits = Bundle(
            store_id=self.bundle_00001.store_id,
            root=self.bundle_00001.root,
            release=self.bundle_00001.release,
            build={
                **self.bundle_00001.build,
                "post": {
                    "top_hits": True,
                    "rho": False,
                    "overview": True,
                    "validate": True,
                },
            },
            analyses_path=self.bundle_00001.analyses_path,
        )
        with self.assertRaises(ValueError) as ctx:
            plan(bundle_invalid_top_hits)
        record_check()
        self.assertIn(
            "top_hits post-processing is not supported or built inline for ragged-besd layout",
            str(ctx.exception),
        )

    def test_ragged_rho_rejected(self) -> None:
        """Configuring rho: true on a Ragged layout raises ValueError (rho is Dense only)."""
        for bundle, expected_layout_name in (
            (self.bundle_00001, "ragged-besd"),
            (self.bundle_00006, "ragged-ssf"),
        ):
            bundle_invalid_rho = Bundle(
                store_id=bundle.store_id,
                root=bundle.root,
                release=bundle.release,
                build={
                    **bundle.build,
                    "post": {
                        "top_hits": False,
                        "rho": True,
                        "overview": True,
                        "validate": True,
                    },
                },
                analyses_path=bundle.analyses_path,
            )
            with self.assertRaises(ValueError) as ctx:
                plan(bundle_invalid_rho)
            record_check()
            self.assertIn(
                f"rho post-processing is valid only for dense layout, not '{expected_layout_name}'",
                str(ctx.exception),
            )

    def test_ragged_post_flags_selective_toggle(self) -> None:
        """Individual post flags toggle corresponding steps for ragged layout."""
        for bundle in (self.bundle_00001, self.bundle_00006):
            bundle_val_only = Bundle(
                store_id=bundle.store_id,
                root=bundle.root,
                release=bundle.release,
                build={
                    **bundle.build,
                    "post": {
                        "top_hits": False,
                        "rho": False,
                        "overview": False,
                        "validate": True,
                    },
                },
                analyses_path=bundle.analyses_path,
            )
            steps = plan(bundle_val_only)
            record_check()
            self.assertEqual([s.name for s in steps], ["build", "validate"])

    def test_ragged_override_artifact_root(self) -> None:
        """Passing explicit artifact_root to plan() overrides build.yaml artifacts.root for ragged."""
        # Test SSF override
        steps_ssf = plan(self.bundle_00006, artifact_root="/temporary/scratch/root")
        expected_store_p_6 = Path("/temporary/scratch/root/OGS-00006/store.opengwasdb")
        expected_source_p_6 = Path("/temporary/scratch/root/OGS-00006/source")
        record_check()
        self.assertEqual(steps_ssf[0].outputs, [expected_store_p_6])
        self.assertEqual(steps_ssf[0].argv[3], str(expected_source_p_6))
        self.assertEqual(steps_ssf[0].argv[4], str(expected_store_p_6))
        self.assertEqual(steps_ssf[1].argv[2], str(expected_store_p_6))
        self.assertEqual(steps_ssf[2].argv[2], str(expected_store_p_6))
        self.assertEqual(steps_ssf[3].argv[2], str(expected_store_p_6))

        # Test BESD override
        steps_besd = plan(self.bundle_00001, artifact_root="/temporary/scratch/root")
        expected_store_p_1 = Path("/temporary/scratch/root/OGS-00001/store.opengwasdb")
        expected_prefix_1 = Path("/temporary/scratch/root/OGS-00001/source/pilot-10")
        record_check()
        self.assertEqual(steps_besd[0].outputs, [expected_store_p_1])
        self.assertEqual(steps_besd[0].argv[2], str(expected_prefix_1))
        self.assertEqual(steps_besd[0].argv[3], str(expected_store_p_1))
        self.assertEqual(
            steps_besd[0].inputs,
            [
                Path(f"{expected_prefix_1}.esi"),
                Path(f"{expected_prefix_1}.epi"),
                Path(f"{expected_prefix_1}.besd"),
                self.bundle_00001.analyses_path,
            ],
        )
        self.assertEqual(steps_besd[1].argv[2], str(expected_store_p_1))
        self.assertEqual(steps_besd[2].argv[2], str(expected_store_p_1))

    def test_ragged_besd_specific_arguments(self) -> None:
        """OGS-00001 (BESD) build step receives correct positionals, --analyses, and 4 explicit inputs."""
        steps = plan(self.bundle_00001)
        build_step = steps[0]
        record_check()
        self.assertEqual(build_step.argv[0], "opengwasdb")
        self.assertEqual(build_step.argv[1], "build-ragged-besd")
        self.assertEqual(build_step.argv[2], "/data/opengwasdb/stores/OGS-00001/source/pilot-10")
        self.assertEqual(build_step.argv[3], "/data/opengwasdb/stores/OGS-00001/store.opengwasdb")
        self.assertIn("--store-id", build_step.argv)
        idx_store = build_step.argv.index("--store-id")
        self.assertEqual(build_step.argv[idx_store + 1], "eqtlgen-cis-pilot")
        self.assertIn("--release-id", build_step.argv)
        idx_rel = build_step.argv.index("--release-id")
        self.assertEqual(build_step.argv[idx_rel + 1], "OGS-00001")
        self.assertIn("--analyses", build_step.argv)
        idx_ana = build_step.argv.index("--analyses")
        self.assertEqual(build_step.argv[idx_ana + 1], "stores/OGS-00001/analyses.tsv")
        self.assertIn("--source-build", build_step.argv)
        idx_sb = build_step.argv.index("--source-build")
        self.assertEqual(build_step.argv[idx_sb + 1], "hg19")
        self.assertIn("--tissue", build_step.argv)
        idx_tis = build_step.argv.index("--tissue")
        self.assertEqual(build_step.argv[idx_tis + 1], "whole_blood")

        # Explicit sibling inputs + analyses.tsv
        expected_inputs = [
            Path("/data/opengwasdb/stores/OGS-00001/source/pilot-10.esi"),
            Path("/data/opengwasdb/stores/OGS-00001/source/pilot-10.epi"),
            Path("/data/opengwasdb/stores/OGS-00001/source/pilot-10.besd"),
            Path("stores/OGS-00001/analyses.tsv"),
        ]
        self.assertEqual(build_step.inputs, expected_inputs)

    def test_ragged_besd_source_build_tied_to_provenance(self) -> None:
        """--source-build hg19 is independently asserted against real source provenance."""
        # 1. Authoritative metadata in release.yaml source_snapshot records hg19
        source_snapshot = self.bundle_00001.release.get("source_snapshot")
        self.assertIsInstance(source_snapshot, dict)
        record_check()
        self.assertEqual(source_snapshot.get("source_genome_build"), "hg19")

        # 2. Planned --source-build option matches this provenance exactly
        steps = plan(self.bundle_00001)
        build_step = steps[0]
        self.assertIn("--source-build", build_step.argv)
        idx_sb = build_step.argv.index("--source-build")
        planned_source_build = build_step.argv[idx_sb + 1]
        record_check()
        self.assertEqual(planned_source_build, source_snapshot.get("source_genome_build"))

        # 3. Syntactic prefix and sibling validation (portable CI semantics)
        self.assertEqual(build_step.argv[2], "/data/opengwasdb/stores/OGS-00001/source/pilot-10")
        self.assertNotIn("eqtlgen-cis-pilot/releases", build_step.argv[2])
        self.assertIn(Path("/data/opengwasdb/stores/OGS-00001/source/pilot-10.epi"), build_step.inputs)
        record_check()

    def test_ragged_ssf_vs_besd_seam_separation(self) -> None:
        """Regressions separating SSF and BESD ragged observed-only planning semantics."""
        steps_besd = plan(self.bundle_00001)
        steps_ssf = plan(self.bundle_00006)

        # 1. Step counts: BESD has 3 steps (build, overview, validate); SSF has 4 steps (build, top-hits, overview, validate)
        record_check()
        self.assertEqual([s.name for s in steps_besd], ["build", "overview", "validate"])
        self.assertEqual([s.name for s in steps_ssf], ["build", "top-hits", "overview", "validate"])

        # 2. Build positional arguments: BESD takes prefix and output; SSF takes manifest, source_dir, and output
        build_besd = steps_besd[0]
        build_ssf = steps_ssf[0]
        record_check()
        self.assertEqual(build_besd.argv[1], "build-ragged-besd")
        self.assertEqual(build_besd.argv[2], "/data/opengwasdb/stores/OGS-00001/source/pilot-10")
        self.assertEqual(build_besd.argv[3], "/data/opengwasdb/stores/OGS-00001/store.opengwasdb")

        self.assertEqual(build_ssf.argv[1], "build-ragged-ssf")
        self.assertEqual(build_ssf.argv[2], "stores/OGS-00006/analyses.tsv")
        self.assertEqual(build_ssf.argv[3], "/data/opengwasdb/stores/OGS-00006/source")
        self.assertEqual(build_ssf.argv[4], "/data/opengwasdb/stores/OGS-00006/store.opengwasdb")

        # 3. BESD passes --analyses option for analytical metadata overlay; SSF takes manifest as positional 1
        self.assertIn("--analyses", build_besd.argv)
        self.assertNotIn("--analyses", build_ssf.argv)

        # 4. Inputs: BESD enumerates sibling files + manifest; SSF has manifest
        record_check()
        self.assertEqual(
            build_besd.inputs,
            [
                Path("/data/opengwasdb/stores/OGS-00001/source/pilot-10.esi"),
                Path("/data/opengwasdb/stores/OGS-00001/source/pilot-10.epi"),
                Path("/data/opengwasdb/stores/OGS-00001/source/pilot-10.besd"),
                Path("stores/OGS-00001/analyses.tsv"),
            ],
        )
        self.assertEqual(build_ssf.inputs, [Path("stores/OGS-00006/analyses.tsv")])

    def test_ragged_build_subdispatch_table(self) -> None:
        """Sub-dispatch table for ragged observed-only contains expected command handlers."""
        record_check()
        self.assertIn("build-ragged-ssf", RAGGED_BUILD_DISPATCH)
        self.assertTrue(callable(RAGGED_BUILD_DISPATCH["build-ragged-ssf"].build_fn))
        self.assertIn("build-ragged-besd", RAGGED_BUILD_DISPATCH)
        self.assertTrue(callable(RAGGED_BUILD_DISPATCH["build-ragged-besd"].build_fn))

        # Calling with unconfigured ragged build command raises NotImplementedError
        unsupported_ragged_bundle = Bundle(
            store_id="OGS-00001",
            root=Path("stores/OGS-00001"),
            release=self.bundle_00001.release,
            build={
                "store_id": "OGS-00001",
                "layout": "ragged",
                "completion_state": "observed_only",
                "build": {"command": "build-ragged-unknown"},
                "post": {},
                "artifacts": {"root": "/data/opengwasdb/stores"},
            },
            analyses_path=self.bundle_00001.analyses_path,
        )
        with self.assertRaises(NotImplementedError) as ctx:
            plan(unsupported_ragged_bundle)
        record_check()
        self.assertIn("Unsupported ragged build command 'build-ragged-unknown'", str(ctx.exception))

    def test_ragged_options_passthrough(self) -> None:
        """build.options flags pass through generically without interpretation."""
        synthetic_bundle = Bundle(
            store_id="OGS-00096",
            root=Path("stores/OGS-00096"),
            release={
                "store_id": "OGS-00096",
                "family": "custom-ragged-family",
                "status": "accepted",
            },
            build={
                "store_id": "OGS-00096",
                "layout": "ragged",
                "completion_state": "observed_only",
                "build": {
                    "command": "build-ragged-ssf",
                    "options": {
                        "stored-effect-scale": "log_or",
                        "allow-unverified-eaf": True,
                        "eaf-reference-ancestry": "EUR",
                    },
                },
                "post": {"top_hits": True, "rho": False, "overview": True, "validate": True},
                "artifacts": {"root": "/custom/artifact/root"},
            },
            analyses_path=Path("stores/OGS-00096/analyses.tsv"),
        )
        steps = plan(synthetic_bundle)
        build_step = steps[0]
        record_check()
        self.assertIn("--stored-effect-scale", build_step.argv)
        idx_scale = build_step.argv.index("--stored-effect-scale")
        self.assertEqual(build_step.argv[idx_scale + 1], "log_or")

        self.assertIn("--allow-unverified-eaf", build_step.argv)
        self.assertIn("--eaf-reference-ancestry", build_step.argv)
        idx_anc = build_step.argv.index("--eaf-reference-ancestry")
        self.assertEqual(build_step.argv[idx_anc + 1], "EUR")

        validate_step_argv_against_cli(self, build_step)

    def test_ragged_pure_function_no_io_and_tripwires(self) -> None:
        """plan() on ragged performs no file or network I/O; tripwires guard calls."""

        def forbidden_io(*args, **kwargs):
            raise AssertionError(f"Forbidden I/O called with args={args}, kwargs={kwargs}")

        with mock.patch("builtins.open", side_effect=forbidden_io):
            with mock.patch.object(Path, "open", side_effect=forbidden_io):
                with mock.patch.object(Path, "exists", side_effect=forbidden_io):
                    with mock.patch.object(Path, "is_file", side_effect=forbidden_io):
                        with mock.patch.object(Path, "is_dir", side_effect=forbidden_io):
                            with mock.patch.object(Path, "stat", side_effect=forbidden_io):
                                with mock.patch("os.path.exists", side_effect=forbidden_io):
                                    with mock.patch("os.stat", side_effect=forbidden_io):
                                        steps1_a = plan(self.bundle_00001)
                                        steps1_b = plan(self.bundle_00001)
                                        steps6_a = plan(self.bundle_00006)
                                        steps6_b = plan(self.bundle_00006)
                                        steps7_a = plan(self.bundle_00007)
                                        steps7_b = plan(self.bundle_00007)
                                        record_check()
                                        self.assertEqual(steps1_a, steps1_b)
                                        record_check()
                                        self.assertEqual(steps6_a, steps6_b)
                                        record_check()
                                        self.assertEqual(steps7_a, steps7_b)


class TestPlanReferenceCompleted(unittest.TestCase):
    """Test suite for plan() focusing on Reference-Completed layouts (OGS-00002 and synthetic dense/hybrid)."""

    def setUp(self) -> None:
        self.bundle_00002 = load("OGS-00002")
        self.golden_path_00002 = REPO_ROOT / "tests" / "plan" / "golden" / "OGS-00002.json"

    def test_ogs_00002_golden_steps(self) -> None:
        """OGS-00002 (Reference-Completed Ragged) produces exact golden Steps matching OGS-00002.json."""
        actual_steps = plan(self.bundle_00002)
        assert_steps_match_golden(self, actual_steps, self.golden_path_00002)

    def test_ogs_00002_argv_validation_against_real_cli(self) -> None:
        """Every argv produced for OGS-00002 parses against the real pinned opengwasdb CLI."""
        steps = plan(self.bundle_00002)
        for step in steps:
            validate_step_argv_against_cli(self, step)

    def test_parent_store_path_derived_solely_from_derived_from(self) -> None:
        """Parent store path is derived purely from release.yaml derived_from and artifact root."""
        synthetic_bundle = Bundle(
            store_id="OGS-00088",
            root=Path("stores/OGS-00088"),
            release={
                "store_id": "OGS-00088",
                "family": "custom-family",
                "derived_from": "OGS-00077",
                "status": "accepted",
            },
            build={
                "store_id": "OGS-00088",
                "layout": "ragged",
                "completion_state": "reference_completed",
                "complete": {
                    "command": "complete-ragged",
                    "options": {
                        "ld-panel": "/data/test/panel",
                        "ancestry": "EUR",
                    },
                },
                "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
                "artifacts": {"root": "/custom/artifact/root"},
            },
            analyses_path=Path("stores/OGS-00088/analyses.tsv"),
        )
        steps = plan(synthetic_bundle)
        complete_step = steps[0]
        record_check()
        self.assertEqual(complete_step.name, "complete")
        self.assertEqual(complete_step.argv[0], "opengwasdb")
        self.assertEqual(complete_step.argv[1], "complete-ragged")
        # Positional 1 is parent store path
        self.assertEqual(complete_step.argv[2], "/custom/artifact/root/OGS-00077/store.opengwasdb")
        # Positional 2 is child store path
        self.assertEqual(complete_step.argv[3], "/custom/artifact/root/OGS-00088/store.opengwasdb")
        # Flags
        self.assertIn("--release-id", complete_step.argv)
        idx_rel = complete_step.argv.index("--release-id")
        self.assertEqual(complete_step.argv[idx_rel + 1], "OGS-00088")
        # Inputs & outputs
        self.assertEqual(complete_step.inputs, [Path("/custom/artifact/root/OGS-00077/store.opengwasdb")])
        self.assertEqual(complete_step.outputs, [Path("/custom/artifact/root/OGS-00088/store.opengwasdb")])

    def test_missing_derived_from_rejected(self) -> None:
        """Reference-completed bundle missing derived_from in release.yaml raises ValueError."""
        for missing_val in (None, ""):
            bundle_no_parent = Bundle(
                store_id="OGS-00088",
                root=Path("stores/OGS-00088"),
                release={
                    "store_id": "OGS-00088",
                    "family": "custom-family",
                    "derived_from": missing_val,
                    "status": "accepted",
                },
                build={
                    "store_id": "OGS-00088",
                    "layout": "ragged",
                    "completion_state": "reference_completed",
                    "complete": {"command": "complete-ragged"},
                    "post": {},
                    "artifacts": {"root": "/data/opengwasdb/stores"},
                },
                analyses_path=Path("stores/OGS-00088/analyses.tsv"),
            )
            with self.assertRaises(ValueError) as ctx:
                plan(bundle_no_parent)
            record_check()
            self.assertIn("missing required 'derived_from'", str(ctx.exception))

    def test_malformed_derived_from_rejected(self) -> None:
        """Reference-completed bundle with malformed derived_from raises ValueError."""
        bundle_bad_parent = Bundle(
            store_id="OGS-00088",
            root=Path("stores/OGS-00088"),
            release={
                "store_id": "OGS-00088",
                "family": "custom-family",
                "derived_from": "bad_parent_id",
                "status": "accepted",
            },
            build={
                "store_id": "OGS-00088",
                "layout": "ragged",
                "completion_state": "reference_completed",
                "complete": {"command": "complete-ragged"},
                "post": {},
                "artifacts": {"root": "/data/opengwasdb/stores"},
            },
            analyses_path=Path("stores/OGS-00088/analyses.tsv"),
        )
        with self.assertRaises(ValueError) as ctx:
            plan(bundle_bad_parent)
        record_check()
        self.assertIn("Invalid store_id format", str(ctx.exception))

    def test_complete_dense_planning_and_cli_validation(self) -> None:
        """Dense reference completion plans complete-dense step, optional rho, and parses against real CLI."""
        synthetic_bundle = Bundle(
            store_id="OGS-00030",
            root=Path("stores/OGS-00030"),
            release={
                "store_id": "OGS-00030",
                "family": "finngen-r13",
                "derived_from": "OGS-00003",
                "status": "accepted",
            },
            build={
                "store_id": "OGS-00030",
                "layout": "dense",
                "completion_state": "reference_completed",
                "complete": {
                    "command": "complete-dense",
                    "options": {
                        "ld-panel": "/data/opengwasdb/reference/hgdp1kgp-hg38/panel",
                        "ancestry": "EUR",
                        "min-cor": 0.8,
                        "thresh": 0.95,
                        "n-workers": 16,
                    },
                },
                "post": {"top_hits": False, "rho": True, "overview": True, "validate": True},
                "artifacts": {"root": "/data/opengwasdb/stores"},
            },
            analyses_path=Path("stores/OGS-00030/analyses.tsv"),
        )
        steps = plan(synthetic_bundle)
        record_check()
        self.assertEqual([s.name for s in steps], ["complete", "rho", "overview", "validate"])

        complete_step = steps[0]
        self.assertEqual(
            complete_step.argv,
            [
                "opengwasdb",
                "complete-dense",
                "/data/opengwasdb/stores/OGS-00003/store.opengwasdb",
                "/data/opengwasdb/stores/OGS-00030/store.opengwasdb",
                "--release-id",
                "OGS-00030",
                "--ld-panel",
                "/data/opengwasdb/reference/hgdp1kgp-hg38/panel",
                "--ancestry",
                "EUR",
                "--min-cor",
                "0.8",
                "--thresh",
                "0.95",
                "--n-workers",
                "16",
            ],
        )
        self.assertEqual(
            complete_step.inputs, [Path("/data/opengwasdb/stores/OGS-00003/store.opengwasdb")]
        )
        self.assertEqual(
            complete_step.outputs, [Path("/data/opengwasdb/stores/OGS-00030/store.opengwasdb")]
        )

        # Validate every planned step argv against real CLI
        for step in steps:
            validate_step_argv_against_cli(self, step)

    def test_complete_hybrid_planning_and_cli_validation(self) -> None:
        """Hybrid reference completion plans complete-hybrid step and parses against real CLI."""
        synthetic_bundle = Bundle(
            store_id="OGS-00040",
            root=Path("stores/OGS-00040"),
            release={
                "store_id": "OGS-00040",
                "family": "gwas-catalog-eur-hybrid",
                "derived_from": "OGS-00004",
                "status": "accepted",
            },
            build={
                "store_id": "OGS-00040",
                "layout": "hybrid",
                "completion_state": "reference_completed",
                "complete": {
                    "command": "complete-hybrid",
                    "options": {
                        "ld-panel": "/data/opengwasdb/reference/hgdp1kgp-hg38/panel",
                        "ancestry": "EUR",
                        "min-cor": 0.7,
                        "thresh": 0.9,
                        "n-workers": 8,
                    },
                },
                "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
                "artifacts": {"root": "/data/opengwasdb/stores"},
            },
            analyses_path=Path("stores/OGS-00040/analyses.tsv"),
        )
        steps = plan(synthetic_bundle)
        record_check()
        self.assertEqual([s.name for s in steps], ["complete", "overview", "validate"])

        complete_step = steps[0]
        self.assertEqual(
            complete_step.argv,
            [
                "opengwasdb",
                "complete-hybrid",
                "/data/opengwasdb/stores/OGS-00004/store.opengwasdb",
                "/data/opengwasdb/stores/OGS-00040/store.opengwasdb",
                "--release-id",
                "OGS-00040",
                "--ld-panel",
                "/data/opengwasdb/reference/hgdp1kgp-hg38/panel",
                "--ancestry",
                "EUR",
                "--min-cor",
                "0.7",
                "--thresh",
                "0.9",
                "--n-workers",
                "8",
            ],
        )
        self.assertEqual(
            complete_step.inputs, [Path("/data/opengwasdb/stores/OGS-00004/store.opengwasdb")]
        )
        self.assertEqual(
            complete_step.outputs, [Path("/data/opengwasdb/stores/OGS-00040/store.opengwasdb")]
        )

        for step in steps:
            validate_step_argv_against_cli(self, step)

    def test_complete_ragged_planning_and_cli_validation(self) -> None:
        """Ragged reference completion plans complete-ragged step and parses against real CLI."""
        synthetic_bundle = Bundle(
            store_id="OGS-00050",
            root=Path("stores/OGS-00050"),
            release={
                "store_id": "OGS-00050",
                "family": "custom-ragged",
                "derived_from": "OGS-00006",
                "status": "accepted",
            },
            build={
                "store_id": "OGS-00050",
                "layout": "ragged",
                "completion_state": "reference_completed",
                "complete": {
                    "command": "complete-ragged",
                    "options": {
                        "ld-panel": "/data/opengwasdb/reference/hgdp1kgp-hg38/panel",
                        "ancestry": "AFR",
                        "cis-window-bp": 500000,
                        "min-cor": 0.75,
                    },
                },
                "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
                "artifacts": {"root": "/data/opengwasdb/stores"},
            },
            analyses_path=Path("stores/OGS-00050/analyses.tsv"),
        )
        steps = plan(synthetic_bundle)
        record_check()
        self.assertEqual([s.name for s in steps], ["complete", "overview", "validate"])

        complete_step = steps[0]
        self.assertEqual(
            complete_step.argv,
            [
                "opengwasdb",
                "complete-ragged",
                "/data/opengwasdb/stores/OGS-00006/store.opengwasdb",
                "/data/opengwasdb/stores/OGS-00050/store.opengwasdb",
                "--release-id",
                "OGS-00050",
                "--ld-panel",
                "/data/opengwasdb/reference/hgdp1kgp-hg38/panel",
                "--ancestry",
                "AFR",
                "--cis-window-bp",
                "500000",
                "--min-cor",
                "0.75",
            ],
        )
        self.assertEqual(
            complete_step.inputs, [Path("/data/opengwasdb/stores/OGS-00006/store.opengwasdb")]
        )
        self.assertEqual(
            complete_step.outputs, [Path("/data/opengwasdb/stores/OGS-00050/store.opengwasdb")]
        )

        for step in steps:
            validate_step_argv_against_cli(self, step)

    def test_reference_completed_top_hits_rejected(self) -> None:
        """Configuring top_hits: true on any reference-completed layout raises ValueError (built inline)."""
        configs = [
            ("dense", "complete-dense", "dense-completed"),
            ("hybrid", "complete-hybrid", "hybrid-completed"),
            ("ragged", "complete-ragged", "ragged-completed"),
        ]
        for layout, cmd, expected_layout_name in configs:
            bundle_invalid_top_hits = Bundle(
                store_id="OGS-00060",
                root=Path("stores/OGS-00060"),
                release={
                    "store_id": "OGS-00060",
                    "family": "test-family",
                    "derived_from": "OGS-00001",
                    "status": "accepted",
                },
                build={
                    "store_id": "OGS-00060",
                    "layout": layout,
                    "completion_state": "reference_completed",
                    "complete": {"command": cmd, "options": {}},
                    "post": {
                        "top_hits": True,
                        "rho": False,
                        "overview": True,
                        "validate": True,
                    },
                    "artifacts": {"root": "/data/opengwasdb/stores"},
                },
                analyses_path=Path("stores/OGS-00060/analyses.tsv"),
            )
            with self.assertRaises(ValueError) as ctx:
                plan(bundle_invalid_top_hits)
            record_check()
            self.assertIn(
                f"top_hits post-processing is not supported or built inline for {expected_layout_name} layout",
                str(ctx.exception),
            )

    def test_reference_completed_rho_dense_only(self) -> None:
        """rho: true is rejected on hybrid-completed and ragged-completed, but allowed on dense-completed."""
        for layout, cmd, expected_layout_name in [
            ("hybrid", "complete-hybrid", "hybrid-completed"),
            ("ragged", "complete-ragged", "ragged-completed"),
        ]:
            bundle_invalid_rho = Bundle(
                store_id="OGS-00060",
                root=Path("stores/OGS-00060"),
                release={
                    "store_id": "OGS-00060",
                    "family": "test-family",
                    "derived_from": "OGS-00001",
                    "status": "accepted",
                },
                build={
                    "store_id": "OGS-00060",
                    "layout": layout,
                    "completion_state": "reference_completed",
                    "complete": {"command": cmd, "options": {}},
                    "post": {
                        "top_hits": False,
                        "rho": True,
                        "overview": True,
                        "validate": True,
                    },
                    "artifacts": {"root": "/data/opengwasdb/stores"},
                },
                analyses_path=Path("stores/OGS-00060/analyses.tsv"),
            )
            with self.assertRaises(ValueError) as ctx:
                plan(bundle_invalid_rho)
            record_check()
            self.assertIn(
                f"rho post-processing is valid only for dense layout, not '{expected_layout_name}'",
                str(ctx.exception),
            )

    def test_reference_completed_post_flags_selective_toggle(self) -> None:
        """Individual post flags toggle corresponding steps on reference-completed releases."""
        bundle_val_only = Bundle(
            store_id=self.bundle_00002.store_id,
            root=self.bundle_00002.root,
            release=self.bundle_00002.release,
            build={
                **self.bundle_00002.build,
                "post": {
                    "top_hits": False,
                    "rho": False,
                    "overview": False,
                    "validate": True,
                },
            },
            analyses_path=self.bundle_00002.analyses_path,
        )
        steps = plan(bundle_val_only)
        record_check()
        self.assertEqual([s.name for s in steps], ["complete", "validate"])

    def test_reference_completed_override_artifact_root(self) -> None:
        """Passing explicit artifact_root to plan() overrides build.yaml artifacts.root for completion."""
        steps = plan(self.bundle_00002, artifact_root="/temporary/scratch/root")
        expected_parent_p = Path("/temporary/scratch/root/OGS-00001/store.opengwasdb")
        expected_store_p = Path("/temporary/scratch/root/OGS-00002/store.opengwasdb")
        record_check()
        self.assertEqual(steps[0].inputs, [expected_parent_p])
        self.assertEqual(steps[0].outputs, [expected_store_p])
        self.assertEqual(steps[0].argv[2], str(expected_parent_p))
        self.assertEqual(steps[0].argv[3], str(expected_store_p))
        self.assertEqual(steps[1].argv[2], str(expected_store_p))
        self.assertEqual(steps[2].argv[2], str(expected_store_p))

    def test_reference_completed_pure_function_no_io_and_tripwires(self) -> None:
        """plan() on reference-completed performs no file or network I/O; tripwires guard calls."""

        def forbidden_io(*args, **kwargs):
            raise AssertionError(f"Forbidden I/O called with args={args}, kwargs={kwargs}")

        with mock.patch("builtins.open", side_effect=forbidden_io):
            with mock.patch.object(Path, "open", side_effect=forbidden_io):
                with mock.patch.object(Path, "exists", side_effect=forbidden_io):
                    with mock.patch.object(Path, "is_file", side_effect=forbidden_io):
                        with mock.patch.object(Path, "is_dir", side_effect=forbidden_io):
                            with mock.patch.object(Path, "stat", side_effect=forbidden_io):
                                with mock.patch("os.path.exists", side_effect=forbidden_io):
                                    with mock.patch("os.stat", side_effect=forbidden_io):
                                        steps2_a = plan(self.bundle_00002)
                                        steps2_b = plan(self.bundle_00002)
                                        record_check()
                                        self.assertEqual(steps2_a, steps2_b)

    def test_unsupported_completion_command_rejected(self) -> None:
        """Unsupported completion command raises NotImplementedError."""
        for layout, state in [
            ("dense", "complete-dense-unknown"),
            ("hybrid", "complete-hybrid-unknown"),
            ("ragged", "complete-ragged-unknown"),
        ]:
            bad_bundle = Bundle(
                store_id="OGS-00060",
                root=Path("stores/OGS-00060"),
                release={
                    "store_id": "OGS-00060",
                    "family": "test-family",
                    "derived_from": "OGS-00001",
                    "status": "accepted",
                },
                build={
                    "store_id": "OGS-00060",
                    "layout": layout,
                    "completion_state": "reference_completed",
                    "complete": {"command": state},
                    "post": {},
                    "artifacts": {"root": "/data/opengwasdb/stores"},
                },
                analyses_path=Path("stores/OGS-00060/analyses.tsv"),
            )
            with self.assertRaises(NotImplementedError) as ctx:
                plan(bad_bundle)
            record_check()
            self.assertIn("Unsupported", str(ctx.exception))

    def test_plan_passes_when_host_source_paths_absent(self) -> None:
        """Regression test for CI (PR #121): all 7 bundles plan successfully when host paths are absent."""
        orig_exists = Path.exists

        def absent_host_exists(self: Path) -> bool:
            p_str = str(self)
            if p_str.startswith("/data/besd") or p_str.startswith("/data/opengwasdb"):
                return False
            return orig_exists(self)

        with mock.patch.object(Path, "exists", absent_host_exists):
            # Assert physical path is reported absent under mock
            self.assertFalse(Path("/data/besd/eqtlgen-sparse.epi").exists())
            self.assertFalse(Path("/data/opengwasdb/eqtlgen-cis-pilot/releases/pilot-10/source/pilot-10.epi").exists())

            # Verify plan() succeeds for all seven bundles without raising or requiring host existence
            for i in range(1, 8):
                sid = f"OGS-{i:05d}"
                b = load(sid)
                steps = plan(b)
                record_check()
                self.assertGreater(len(steps), 0, f"{sid} produced steps when host paths are absent")


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPlanDense))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPlanHybrid))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPlanRagged))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPlanReferenceCompleted))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
