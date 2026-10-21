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


class AlleleOrientationPoolTest(unittest.TestCase):
    """The UK Biobank pool is the queried Store's own Analyses (#312 review H2).

    A lookup of an Analysis the Store lacks returns no rows, which would read
    as "no effect" rather than as a missing Analysis, so the check refuses
    first and names how many are missing.
    """

    def setUp(self) -> None:
        self.tool = load_tool("tools/check_allele_orientation.py")

    def study(self, analysis_id: str):
        return self.tool.ComparisonStudy(
            source_bundle="OGS-00009",
            analysis_id=analysis_id,
            trait=analysis_id,
            publication_pmid="",
            ontology_ids=frozenset(),
            label_tokens=frozenset(),
            normalised_label=analysis_id,
        )

    def test_a_pool_analysis_the_store_lacks_is_refused_with_the_count(self) -> None:
        pool = [self.study(analysis_id) for analysis_id in ("ukb-b-1", "ukb-b-2", "ukb-b-3")]
        with self.assertRaisesRegex(SystemExit, r"2 of the 3 .*ukb-b-2.*ukb-b-3") as raised:
            self.tool.require_pool_in_store(pool, ["ukb-b-1"], Path("/fixture/store.opengwasdb"))
        self.assertIn("/fixture/store.opengwasdb", str(raised.exception))

    def test_a_pool_inside_the_store_passes(self) -> None:
        pool = [self.study(analysis_id) for analysis_id in ("ukb-b-1", "ukb-b-2")]
        self.tool.require_pool_in_store(pool, ["ukb-b-1", "ukb-b-2", "ukb-b-9"], Path("/fixture/store.opengwasdb"))

    def test_a_run_refuses_before_scanning_when_the_store_lacks_pool_analyses(self) -> None:
        _catalogue, ukb = self.tool.load_comparison_pools()
        held = [study.analysis_id for study in ukb[3:]]
        self.tool.store_built_ids = lambda store: (tuple(held), "fixture runtime")


        def no_scanning(*args, **kwargs):
            raise AssertionError("scanned sources before the pool was checked")

        self.tool.ProcessPoolExecutor = no_scanning
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(SystemExit, rf"3 of the {len(ukb)} "):
                self.tool.main(["--limit", "1", "--workers", "1", "--out-dir", out])


if __name__ == "__main__":
    unittest.main()
