#!/usr/bin/env python3
"""Every moved tool resolves its default paths inside this checkout (#317, ADR 0038).

ADR 0038 moved scripts out of `resources/scripts/` and `curation/` to new
depths. A script that derives the repository root from `__file__` with its old
depth reads and writes above the checkout, and `--help` never notices. Each
case runs that tool's own default-path resolution from the checkout:
`--plan-only` where the tool has one, the site build with stand-in renderers,
and otherwise the defaults the tool computes when it is loaded.

The tools under `tools/` are scripts, not modules, so they are loaded by path
(`tests/packaging/test_imports.py` lists this file as exempt).

Run from the repository root:
    python3 tests/packaging/test_tool_paths.py
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]

# The pages tools/build-site.sh publishes, under the names the site links to.
PUBLISHED_PAGES = (
    "store-curation.html",
    "beta-scale-estimation.html",
    "query-walkthrough.html",
    "trait-mapping-report.html",
    "prioritisation-dashboard.html",
)

# Stand-ins for the renderers, so the site build runs without data or Quarto.
STUB_QUARTO = """#!/usr/bin/env bash
[ "$1" = render ] && [ -f "$2" ] || { echo "no report source $2" >&2; exit 1; }
echo stub > "${2%.qmd}.html"
"""
STUB_RSCRIPT = """#!/usr/bin/env bash
[ -f "$1" ] || { echo "no script $1" >&2; exit 1; }
mkdir -p resources/data/derived && echo stub > resources/data/derived/store-prioritisation-dashboard.html
"""


def load_tool(relative: str) -> ModuleType:
    path = REPO_ROOT / relative
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


class MovedToolPathsTest(unittest.TestCase):
    def assertInCheckout(self, path: Path) -> None:
        self.assertTrue(Path(path).resolve().is_relative_to(REPO_ROOT), f"{path} is outside {REPO_ROOT}")
        self.assertTrue(Path(path).exists(), f"{path} does not exist")

    def test_allele_orientation_check_plans_from_the_committed_registry(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            result = subprocess.run(
                [sys.executable, "tools/check_allele_orientation.py", "--plan-only", "--limit", "5", "--out-dir", out],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("match plan:", result.stdout)
        self.assertInCheckout(load_tool("tools/check_allele_orientation.py").DEFAULT_OUT_DIR.parent)

    def test_benchmark_report_reads_its_default_anchors_and_registry(self) -> None:
        report = load_tool("tools/benchmarks/release-benchmark-report.py")

        self.assertEqual(report.REPO_ROOT, REPO_ROOT)
        self.assertIn("OGS-00003", report.load_anchors(report.REPO_ROOT / report.DEFAULT_ANCHORS))
        self.assertTrue(report.registered_bundles(report.REPO_ROOT / "stores"))

    def test_hm3_pipeline_proof_writes_its_evidence_into_this_checkout(self) -> None:
        proof = load_tool("tools/benchmarks/hm3-pipeline-proof.py")

        self.assertEqual(proof.REPO_ROOT, REPO_ROOT)
        for path in (proof.OUTPUT_JSON, proof.OUTPUT_MD, proof.REPO_ROOT / "stores" / proof.STORE_ID):
            self.assertInCheckout(path)

    def test_hm3_verifier_reads_its_default_declaration(self) -> None:
        verifier = load_tool("tools/reference-resources/hapmap3-hg38/verify_hm3_resource.py")

        self.assertInCheckout(verifier.DEFAULT_RESOURCE)
        self.assertEqual(verifier.read_expected(verifier.DEFAULT_RESOURCE)["row_count"], 1206903)

    def test_curation_and_the_test_runner_find_this_checkout(self) -> None:
        for name in ("coverage", "curation_round", "promotion", "round"):
            self.assertEqual(importlib.import_module(f"curation.{name}").REPO_ROOT, REPO_ROOT, name)
        self.assertInCheckout(importlib.import_module("curation.promotion").DEFAULT_RESOURCE_DIR)
        self.assertInCheckout(importlib.import_module("curation.export_trait_mapping_report").ROOT / "docs" / "data")
        self.assertEqual(load_tool("tests/run_all_tests.py").REPO_ROOT, REPO_ROOT)

    def test_site_build_renders_docs_reports_into_the_published_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout, stubs = Path(tmp) / "checkout", Path(tmp) / "bin"
            shutil.copytree(REPO_ROOT / "docs" / "reports", checkout / "docs" / "reports")
            (checkout / "tools").mkdir()
            shutil.copy2(REPO_ROOT / "tools" / "build-site.sh", checkout / "tools" / "build-site.sh")
            stubs.mkdir()
            for name, body in (("quarto", STUB_QUARTO), ("Rscript", STUB_RSCRIPT)):
                (stubs / name).write_text(body, encoding="utf-8")
                (stubs / name).chmod(0o755)

            result = subprocess.run(
                ["bash", str(checkout / "tools" / "build-site.sh")],
                cwd=tmp, capture_output=True, text=True,
                env={**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"},
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for page in PUBLISHED_PAGES:
                self.assertTrue((checkout / "docs" / page).is_file(), page)


if __name__ == "__main__":
    unittest.main()
