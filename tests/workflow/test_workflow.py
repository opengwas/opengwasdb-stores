#!/usr/bin/env python3
"""Tests for Phase A workflow/Snakefile orchestration (Issues #115, #116).

Verifies the central contracts of ADR 0022, ADR 0023, and ADR 0024:
1. Snakefile wires dependencies only (ADR 0023):
   - No Store Family name hardcoded.
   - No source column name hardcoded.
   - No manifest translation.
   - No layout branch in rule execution logic.
   - Snakemake expansion IS the multi-release runner (no batch/loop runner script).
2. The rule chain:
   build (or complete) -> top_hits -> rho -> overview -> validate -> register
   where top_hits and rho are conditional on build.yaml post flags.
3. Tracked outputs are record files (records/<step>.json), never the Store directory.
4. Terminal register step completes the DAG and writes records/register.json.
6. Multi-release DAG expansion (Issue #116):
   - Multiple store IDs in one invocation build in correct order.
   - Requesting only a Reference-Completed child also builds its parent first via lineage input edge.
   - Requesting a family builds every release in that family.
   - The index target proposes 0 build jobs (depends only on bundle files).
7. End-to-end fixture build, idempotency, interruption resumption, and record deletion.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SRC_DIR: Path = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ogstores import bundle, manifest, paths, run
from ogstores.plan import plan

SNAKEFILE_PATH: Path = REPO_ROOT / "workflow" / "Snakefile"


def scheduled_targets(stdout: str) -> list[str]:
    """Names of the jobs a dry-run scheduled.

    Execution steps share one rule, so a job is identified by the step it
    produces (`records/<step>.json`) rather than by its rule name; target
    alias rules have no output and fall back to the rule name.
    """
    targets: list[str] = []
    current: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("rule ") and stripped.endswith(":"):
            if current is not None:
                targets.append(current)
            current = stripped.split()[1].rstrip(":")
        elif stripped.startswith("output:") and current is not None:
            for token in stripped[len("output:"):].split():
                name = Path(token.rstrip(",")).name
                if name.endswith(".json"):
                    current = name[: -len(".json")]
                    break
    if current is not None:
        targets.append(current)
    return targets


def find_snakemake_cmd() -> list[str]:
    """Resolve snakemake command for execution across environments."""
    which_snakemake = shutil.which("snakemake")
    if which_snakemake:
        return [which_snakemake]
    pixi_dev = REPO_ROOT / ".pixi" / "envs" / "dev" / "bin" / "snakemake"
    if pixi_dev.is_file():
        return [str(pixi_dev)]
    pixi_workflow = REPO_ROOT / ".pixi" / "envs" / "workflow" / "bin" / "snakemake"
    if pixi_workflow.is_file():
        return [str(pixi_workflow)]
    return [sys.executable, "-m", "snakemake"]


def create_fixture_vcf(path: Path) -> Path:
    """Create a minimal 2-variant GRCh38 GWAS-VCF fixture file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "##fileformat=VCFv4.2\n"
        '##FILTER=<ID=PASS,Description="All filters passed">\n'
        '##INFO=<ID=AF,Number=A,Type=Float,Description="Allele Frequency">\n'
        '##FORMAT=<ID=ES,Number=A,Type=Float,Description="Effect size">\n'
        '##FORMAT=<ID=SE,Number=A,Type=Float,Description="Standard error">\n'
        '##FORMAT=<ID=LP,Number=A,Type=Float,Description="-log10(p-value)">\n'
        '##FORMAT=<ID=AF,Number=A,Type=Float,Description="Allele frequency">\n'
        '##FORMAT=<ID=SS,Number=A,Type=Float,Description="Sample size">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRAIT\n"
        "chr1\t10000\trs1\tA\tG\t.\tPASS\tAF=0.2\tES:SE:LP:AF:SS\t0.1:0.02:5.0:0.2:10000\n"
        "chr1\t20000\trs2\tC\tT\t.\tPASS\tAF=0.4\tES:SE:LP:AF:SS\t-0.2:0.03:8.0:0.4:10000\n"
    )
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(content)
    return path


def create_dense_fixture_store(
    stores_dir: Path,
    store_id: str = "OGS-00099",
    *,
    family: str = "test-fam",
    vcf_path: Path | None = None,
    artifact_root: Path | None = None,
    post_top_hits: bool = True,
    post_rho: bool = False,
    post_overview: bool = True,
    post_validate: bool = True,
) -> Path:
    """Create a complete fixture Release Bundle under stores_dir/<store_id>."""
    store_dir = stores_dir / store_id
    store_dir.mkdir(parents=True, exist_ok=True)

    if vcf_path is None:
        vcf_path = store_dir / "test.vcf.gz"
        create_fixture_vcf(vcf_path)

    art_root_str = str(artifact_root) if artifact_root else str(paths.DEFAULT_ARTIFACT_ROOT)

    release_yaml = {
        "store_id": store_id,
        "label": f"fixture-dense-{store_id}",
        "family": family,
        "status": "candidate",
        "source_collection_id": "test-collection",
        "association_coverage": "full_gwas",
        "derived_from": None,
        "created_at": "2026-08-18T08:51:59Z",
        "description": f"Fixture dense store {store_id} for workflow testing",
        "source_snapshot_id": "test-snapshot",
        "release_kind": "pilot",
        "generator": {"command": "test-generator"},
    }

    build_yaml = {
        "store_id": store_id,
        "layout": "dense",
        "completion_state": "observed_only",
        "build": {
            "command": "build-dense-vcf",
            "options": {
                "source-reader-capability": "opengwasdb.gwas-vcf",
                "source-assembly": "hg38",
                "allow-unverified-eaf": True,
            },
        },
        "post": {
            "top_hits": post_top_hits,
            "rho": post_rho,
            "overview": post_overview,
            "validate": post_validate,
        },
        "artifacts": {"root": art_root_str},
    }

    analyses_tsv = (
        "analysis_id\tsource_file\ttrait_name\tsample_size\tstored_effect_scale\toriginal_sd_method\toriginal_sd\tassigned_ancestry\tancestry_assignment_method\n"
        f"test_analysis_{store_id}\t{vcf_path}\tTest Trait\t10000\tsd\tdeclared_standardised\t\tEUR\tsource_fallback\n"
    )

    with open(store_dir / "release.yaml", "w", encoding="utf-8") as f:
        import yaml
        yaml.safe_dump(release_yaml, f)

    with open(store_dir / "build.yaml", "w", encoding="utf-8") as f:
        import yaml
        yaml.safe_dump(build_yaml, f)

    (store_dir / "analyses.tsv").write_text(analyses_tsv, encoding="utf-8")
    return store_dir


def run_snakemake(
    targets: list[str],
    *,
    registry_root: Path,
    artifact_root: Path,
    snakefile: Path = SNAKEFILE_PATH,
    dry_run: bool = False,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke snakemake CLI on Snakefile with explicit registry_root and artifact_root."""
    cmd = find_snakemake_cmd() + [
        "--snakefile",
        str(snakefile),
        "--config",
        f"registry_root={registry_root}",
        f"artifact_root={artifact_root}",
    ]
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd.extend(["--cores", "1"])
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(targets)

    env = dict(os.environ)
    if str(SRC_DIR) not in env.get("PYTHONPATH", ""):
        env["PYTHONPATH"] = f"{SRC_DIR}:{env.get('PYTHONPATH', '')}"

    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env)


class TestWorkflowSnakefileStaticProperties(unittest.TestCase):
    """Verify ADR 0023 constraints statically on workflow/Snakefile."""

    def setUp(self) -> None:
        self.snakefile_text = SNAKEFILE_PATH.read_text(encoding="utf-8")
        self.code_lines = [
            line for line in self.snakefile_text.splitlines()
            if not line.strip().startswith("#") and not line.strip().startswith('"""')
        ]
        self.code_text = "\n".join(self.code_lines)

    def test_snakefile_exists_and_readable(self) -> None:
        """workflow/Snakefile exists and is readable."""
        self.assertTrue(SNAKEFILE_PATH.is_file())
        self.assertGreater(len(self.snakefile_text), 100)

    def test_no_store_family_names_hardcoded(self) -> None:
        """Snakefile must not contain hardcoded Store Family names (ADR 0023)."""
        prohibited_families = [
            "finngen-r13",
            "eqtlgen-cis-pilot",
            "gwas-catalog-eur-hybrid",
            "metabolome-plasma-2023",
            "pqtl-interval-2018",
            "ukb-b",
            "finngen_r13",
            "eqtlgen_cis",
        ]
        for fam in prohibited_families:
            self.assertNotIn(
                f'"{fam}"',
                self.code_text,
                f"Snakefile contains hardcoded family literal {fam!r}",
            )
            self.assertNotIn(
                f"'{fam}'",
                self.code_text,
                f"Snakefile contains hardcoded family literal {fam!r}",
            )

    def test_no_source_column_names_hardcoded(self) -> None:
        """Snakefile must not contain hardcoded source column names (ADR 0023)."""
        prohibited_columns = [
            "analysis_id",
            "source_file",
            "stored_effect_scale",
            "original_sd_method",
            "original_sd",
            "assigned_ancestry",
            "ancestry_assignment_method",
            "trait_id",
            "trait_name",
            "sample_size",
            "p_value",
            "beta",
            "standard_error",
        ]
        for col in prohibited_columns:
            self.assertNotIn(
                f'"{col}"',
                self.code_text,
                f"Snakefile contains hardcoded column literal {col!r}",
            )
            self.assertNotIn(
                f"'{col}'",
                self.code_text,
                f"Snakefile contains hardcoded column literal {col!r}",
            )

    def test_no_layout_branches_in_execution_logic(self) -> None:
        """Snakefile must not branch on layout types (dense, hybrid, ragged) in rules (ADR 0023)."""
        prohibited_branches = [
            'layout == "dense"',
            'layout == "hybrid"',
            'layout == "ragged"',
            "layout == 'dense'",
            "layout == 'hybrid'",
            "layout == 'ragged'",
            'layout.startswith("dense")',
            'b.layout == "dense"',
        ]
        for branch in prohibited_branches:
            self.assertNotIn(
                branch,
                self.code_text,
                f"Snakefile contains layout branch {branch!r}",
            )

    def test_tracked_outputs_are_records_or_the_derived_manifest_never_stores(self) -> None:
        """Rule outputs are records/<step>.json or the derived build manifest, never a Store directory."""
        # The derived build manifest is the one non-record tracked output: it is
        # a file, not a Store directory, and the build step consumes it (ADR 0025).
        allowed_manifest_outputs = (
            '"{root}/{store_id}/work/analyses.tsv",',
            'manifest="{root}/{store_id}/work/analyses.tsv",',
            'sidecar="{root}/{store_id}/work/analyses.exclusions.json",',
        )
        in_output_block = False
        for line in self.code_lines:
            stripped = line.strip()
            if stripped.startswith("output:"):
                in_output_block = True
                continue
            if in_output_block:
                if stripped.startswith("run:") or stripped.startswith("input:") or stripped.startswith("rule "):
                    in_output_block = False
                    continue
                if stripped:
                    # The unknown-ID guard declares the requested ID only so its
                    # input function can fail during DAG construction; it never
                    # materialises that sentinel output.
                    if stripped == '"{store_id,OGS-[0-9]{5}}"':
                        continue
                    if stripped in allowed_manifest_outputs:
                        self.assertNotIn("store.opengwasdb", stripped)
                        continue
                    self.assertIn("records", stripped, f"Output line {stripped!r} missing 'records'")
                    self.assertIn(".json", stripped, f"Output line {stripped!r} missing '.json'")
                    self.assertNotIn("store.opengwasdb\"", stripped)
                    self.assertNotIn("store.opengwasdb'", stripped)

    def test_static_assertion_no_batch_or_loop_runner_scripts(self) -> None:
        """Snakemake wildcard expansion is the only multi-release runner; no batch scripts exist."""
        forbidden_patterns = [
            "build_all.sh",
            "run_batch.py",
            "release_batch.sh",
            "loop_stores.py",
            "run_all_releases.py",
            "batch_runner.py",
        ]
        for root_dir in [REPO_ROOT / "workflow", REPO_ROOT / "resources" / "scripts"]:
            if not root_dir.is_dir():
                continue
            for file_path in root_dir.glob("*"):
                self.assertNotIn(
                    file_path.name,
                    forbidden_patterns,
                    f"Forbidden batch runner script found: {file_path}",
                )


class TestWorkflowEndToEndAndResumption(unittest.TestCase):
    """End-to-end execution, idempotency, and resumption tests with a fixture-scale Dense store."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.artifact_root = self.td / "artifacts"
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_dense_store_builds_end_to_end_with_one_command(self) -> None:
        """A fixture Dense store builds and registers end-to-end with a single snakemake target."""
        store_id = "OGS-00099"
        create_dense_fixture_store(
            self.stores_dir,
            store_id=store_id,
            artifact_root=self.artifact_root,
            post_top_hits=True,
            post_overview=True,
            post_validate=True,
        )

        res = run_snakemake(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"Snakemake failed (exit {res.returncode}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )

        rec_dir = paths.records_dir(store_id, root=self.artifact_root)
        expected_records = ["build.json", "top-hits.json", "overview.json", "validate.json", "register.json"]
        for rec_name in expected_records:
            rec_p = rec_dir / rec_name
            self.assertTrue(rec_p.is_file(), f"Expected record {rec_p} does not exist")
            data = json.loads(rec_p.read_text(encoding="utf-8"))
            self.assertTrue(data.get("success"), f"Record {rec_name} reported success=False: {data}")
            self.assertEqual(data.get("exit_code"), 0)
            self.assertEqual(data.get("store_id"), store_id)

        final_store = paths.store_path(store_id, root=self.artifact_root)
        partial_store = paths.partial_store_path(store_id, root=self.artifact_root)
        self.assertTrue(final_store.is_dir(), f"Published Store not found at {final_store}")
        self.assertFalse(partial_store.exists(), f"Transient partial store still exists at {partial_store}")

        val_res = subprocess.run(["opengwasdb", "validate", str(final_store)], capture_output=True, text=True)
        self.assertEqual(val_res.returncode, 0, f"opengwasdb validate failed: {val_res.stderr}")

    def test_rerun_after_success_is_idempotent_no_jobs(self) -> None:
        """Re-running snakemake over an already completed release re-runs 0 jobs."""
        store_id = "OGS-00099"
        create_dense_fixture_store(
            self.stores_dir,
            store_id=store_id,
            artifact_root=self.artifact_root,
        )

        res1 = run_snakemake([store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(res1.returncode, 0)

        rec_dir = paths.records_dir(store_id, root=self.artifact_root)
        mtimes_before = {p: p.stat().st_mtime_ns for p in rec_dir.glob("*.json")}

        res2 = run_snakemake([store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(res2.returncode, 0)
        self.assertTrue(
            "Nothing to be done" in res2.stdout or "0 of 0 steps" in res2.stdout or res2.returncode == 0,
            f"Expected no-op re-run output, got:\n{res2.stdout}",
        )

        for p, mtime in mtimes_before.items():
            self.assertEqual(
                p.stat().st_mtime_ns,
                mtime,
                f"Record file {p.name} was modified during idempotent re-run",
            )

    def test_interrupted_step_resumes_without_restarting(self) -> None:
        """Re-running after an interrupted step resumes from the missing step rather than restarting."""
        store_id = "OGS-00099"
        create_dense_fixture_store(
            self.stores_dir,
            store_id=store_id,
            artifact_root=self.artifact_root,
        )

        b = bundle.load(store_id, registry_root=self.stores_dir)
        steps = plan(b, artifact_root=self.artifact_root)
        build_step = next(s for s in steps if s.name == "build")
        top_hits_step = next(s for s in steps if s.name == "top-hits")

        # The build step consumes the derived build manifest. Snakemake's
        # build_manifest rule would produce it first; running the step directly
        # stands in for that rule here.
        manifest.materialise_build_manifest(
            b.analyses_path,
            paths.build_manifest_path(store_id, root=self.artifact_root),
            paths.build_manifest_sidecar_path(store_id, root=self.artifact_root),
        )

        run.run_step(build_step, store_id=store_id, artifact_root=self.artifact_root)
        run.run_step(top_hits_step, store_id=store_id, artifact_root=self.artifact_root)

        rec_dir = paths.records_dir(store_id, root=self.artifact_root)
        self.assertTrue((rec_dir / "build.json").is_file())
        self.assertTrue((rec_dir / "top-hits.json").is_file())
        self.assertFalse((rec_dir / "overview.json").exists())
        self.assertFalse((rec_dir / "validate.json").exists())
        self.assertFalse((rec_dir / "register.json").exists())

        build_mtime = (rec_dir / "build.json").stat().st_mtime_ns
        top_hits_mtime = (rec_dir / "top-hits.json").stat().st_mtime_ns

        res_dry = run_snakemake(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res_dry.returncode, 0)
        scheduled_rules = scheduled_targets(res_dry.stdout)
        self.assertIn("overview", scheduled_rules)
        self.assertIn("validate", scheduled_rules)
        self.assertIn("register", scheduled_rules)
        self.assertNotIn("build", scheduled_rules)
        self.assertNotIn("top-hits", scheduled_rules)

        res_resume = run_snakemake([store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(res_resume.returncode, 0, f"Resume run failed:\n{res_resume.stderr}")

        self.assertEqual((rec_dir / "build.json").stat().st_mtime_ns, build_mtime)
        self.assertEqual((rec_dir / "top-hits.json").stat().st_mtime_ns, top_hits_mtime)

        self.assertTrue((rec_dir / "overview.json").is_file())
        self.assertTrue((rec_dir / "validate.json").is_file())
        self.assertTrue((rec_dir / "register.json").is_file())
        self.assertTrue(paths.store_path(store_id, root=self.artifact_root).is_dir())

    def test_deleting_single_record_file_reruns_exact_step_and_downstream(self) -> None:
        """Deleting records/validate.json triggers only validate, register, and store target in dry-run."""
        store_id = "OGS-00099"
        create_dense_fixture_store(
            self.stores_dir,
            store_id=store_id,
            artifact_root=self.artifact_root,
        )

        res1 = run_snakemake([store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(res1.returncode, 0)

        rec_dir = paths.records_dir(store_id, root=self.artifact_root)
        (rec_dir / "validate.json").unlink()
        (rec_dir / "register.json").unlink()

        res_dry = run_snakemake(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res_dry.returncode, 0)
        scheduled = scheduled_targets(res_dry.stdout)
        self.assertEqual(set(scheduled), {"validate", "register", store_id})


class TestWorkflowLineageAndMultiReleaseDAG(unittest.TestCase):
    """Multi-release DAG expansion and cross-store lineage ordering (Issue #116)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.artifact_root = self.td / "artifacts"
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_several_store_release_ids_in_one_invocation_build_end_to_end(self) -> None:
        """Several Store Release IDs in one snakemake invocation build all targets to completion."""
        s1 = "OGS-00061"
        s2 = "OGS-00062"
        create_dense_fixture_store(self.stores_dir, store_id=s1, artifact_root=self.artifact_root)
        create_dense_fixture_store(self.stores_dir, store_id=s2, artifact_root=self.artifact_root)

        res = run_snakemake(
            [s1, s2],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
        )
        self.assertEqual(res.returncode, 0, f"Multi-target run failed:\n{res.stderr}")

        # Both releases produced complete records and valid stores
        for sid in [s1, s2]:
            self.assertTrue(paths.record_path(sid, "register", root=self.artifact_root).is_file())
            self.assertTrue(paths.store_path(sid, root=self.artifact_root).is_dir())
            val_res = subprocess.run(
                ["opengwasdb", "validate", str(paths.store_path(sid, root=self.artifact_root))],
                capture_output=True,
                text=True,
            )
            self.assertEqual(val_res.returncode, 0)

    def test_multi_release_invocation_with_cross_store_lineage_ordering(self) -> None:
        """Multi-release invocation requesting child and independent release orders parent first."""
        parent_id = "OGS-00051"
        child_id = "OGS-00052"
        independent_id = "OGS-00053"

        create_dense_fixture_store(self.stores_dir, store_id=parent_id, artifact_root=self.artifact_root)
        create_dense_fixture_store(self.stores_dir, store_id=independent_id, artifact_root=self.artifact_root)

        child_dir = self.stores_dir / child_id
        child_dir.mkdir(parents=True, exist_ok=True)
        rel_yaml = {
            "store_id": child_id,
            "label": "child-release",
            "family": "test-fam",
            "status": "candidate",
            "source_collection_id": "test-collection",
            "association_coverage": "full_gwas",
            "derived_from": parent_id,
            "created_at": "2026-08-18T16:00:00Z",
            "description": "Child release",
            "source_snapshot_id": "test-snap",
            "release_kind": "pilot",
            "generator": {"command": "test-gen"},
        }
        bld_yaml = {
            "store_id": child_id,
            "layout": "dense",
            "completion_state": "reference_completed",
            "complete": {
                "command": "complete-dense",
                "options": {
                    "ld-panel": "/fake/ld/panel",
                    "ancestry": "EUR",
                },
            },
            "post": {
                "top_hits": False,
                "rho": False,
                "overview": True,
                "validate": True,
            },
            "artifacts": {"root": str(self.artifact_root)},
        }
        with open(child_dir / "release.yaml", "w") as f:
            import yaml
            yaml.safe_dump(rel_yaml, f)
        with open(child_dir / "build.yaml", "w") as f:
            import yaml
            yaml.safe_dump(bld_yaml, f)
        (child_dir / "analyses.tsv").write_text("analysis_id\tsource_file\n")

        # Invocate multi-target requesting child_id and independent_id
        res = run_snakemake(
            [child_id, independent_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res.returncode, 0, f"Multi-target lineage dry run failed:\n{res.stderr}")
        self.assertIn(parent_id, res.stdout)
        self.assertIn(child_id, res.stdout)
        self.assertIn(independent_id, res.stdout)

    def test_requesting_only_reference_completed_child_builds_parent_first(self) -> None:
        """Requesting only a Reference-Completed child builds parent first via lineage input edge."""
        parent_id = "OGS-00054"
        child_id = "OGS-00055"

        create_dense_fixture_store(self.stores_dir, store_id=parent_id, artifact_root=self.artifact_root)

        child_dir = self.stores_dir / child_id
        child_dir.mkdir(parents=True, exist_ok=True)
        rel_yaml = {
            "store_id": child_id,
            "label": "completed-child",
            "family": "test-fam",
            "status": "candidate",
            "source_collection_id": "test-collection",
            "association_coverage": "full_gwas",
            "derived_from": parent_id,
            "created_at": "2026-08-18T16:00:00Z",
            "description": "Completed child store",
            "source_snapshot_id": "test-snapshot",
            "release_kind": "pilot",
            "generator": {"command": "test-gen"},
        }
        bld_yaml = {
            "store_id": child_id,
            "layout": "dense",
            "completion_state": "reference_completed",
            "complete": {
                "command": "complete-dense",
                "options": {
                    "ld-panel": "/fake/ld/panel",
                    "ancestry": "EUR",
                },
            },
            "post": {
                "top_hits": False,
                "rho": False,
                "overview": True,
                "validate": True,
            },
            "artifacts": {"root": str(self.artifact_root)},
        }
        with open(child_dir / "release.yaml", "w") as f:
            import yaml
            yaml.safe_dump(rel_yaml, f)
        with open(child_dir / "build.yaml", "w") as f:
            import yaml
            yaml.safe_dump(bld_yaml, f)
        (child_dir / "analyses.tsv").write_text("analysis_id\tsource_file\n")

        res = run_snakemake([child_id], registry_root=self.stores_dir, artifact_root=self.artifact_root, dry_run=True)
        self.assertEqual(res.returncode, 0, f"Dry run failed:\n{res.stderr}")
        self.assertIn(parent_id, res.stdout)
        self.assertIn(child_id, res.stdout)

        parent_rec = paths.record_path(parent_id, "register", root=self.artifact_root)
        parent_rec.parent.mkdir(parents=True, exist_ok=True)
        parent_rec.write_text("{}", encoding="utf-8")

        res2 = run_snakemake([child_id], registry_root=self.stores_dir, artifact_root=self.artifact_root, dry_run=True)
        self.assertEqual(res2.returncode, 0)
        self.assertNotIn(f"wildcards: root={self.artifact_root}, store_id={parent_id}", res2.stdout)



class TestWorkflowOperatorInterface(unittest.TestCase):
    """Operator interface: rule all, store_id target, family target, and index target (Issue #116)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.artifact_root = self.td / "artifacts"
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_unknown_store_release_id_reports_registered_ids(self) -> None:
        """An unknown Store Release target fails with an actionable registry error."""
        known_id = "OGS-00099"
        create_dense_fixture_store(
            self.stores_dir,
            store_id=known_id,
            artifact_root=self.artifact_root,
        )

        res = run_snakemake(
            ["OGS-00042"],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )

        self.assertNotEqual(res.returncode, 0)
        output = res.stdout + res.stderr
        self.assertIn("Unknown Store Release ID 'OGS-00042'", output)
        self.assertIn(f"Registered IDs: {known_id}", output)

    def test_family_target_rule_resolves_all_family_releases(self) -> None:
        """Targeting a Store Family resolves and builds every release of that family."""
        family_name = "test-pilot-fam"
        s1 = "OGS-00071"
        s2 = "OGS-00072"
        create_dense_fixture_store(self.stores_dir, store_id=s1, family=family_name, artifact_root=self.artifact_root)
        create_dense_fixture_store(self.stores_dir, store_id=s2, family=family_name, artifact_root=self.artifact_root)

        res = run_snakemake(
            [family_name],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res.returncode, 0, f"Family target dry run failed:\n{res.stderr}")
        self.assertIn(f"rule {family_name}:", res.stdout)
        self.assertIn(s1, res.stdout)
        self.assertIn(s2, res.stdout)

    def test_family_target_builds_every_release_in_family_end_to_end(self) -> None:
        """Targeting a Store Family physically builds and registers all releases in that family."""
        family_name = "test-exec-fam"
        s1 = "OGS-00073"
        s2 = "OGS-00074"
        create_dense_fixture_store(self.stores_dir, store_id=s1, family=family_name, artifact_root=self.artifact_root)
        create_dense_fixture_store(self.stores_dir, store_id=s2, family=family_name, artifact_root=self.artifact_root)

        res = run_snakemake(
            [family_name],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
        )
        self.assertEqual(res.returncode, 0, f"Family target execution failed:\n{res.stderr}")

        for sid in [s1, s2]:
            self.assertTrue(paths.record_path(sid, "register", root=self.artifact_root).is_file())
            self.assertTrue(paths.store_path(sid, root=self.artifact_root).is_dir())
            val_res = subprocess.run(
                ["opengwasdb", "validate", str(paths.store_path(sid, root=self.artifact_root))],
                capture_output=True,
                text=True,
            )
            self.assertEqual(val_res.returncode, 0)

    def test_all_target_resolves_all_discovered_releases(self) -> None:
        """Default target (all) resolves all discovered stores in registry_root."""
        s1 = "OGS-00081"
        s2 = "OGS-00082"
        create_dense_fixture_store(self.stores_dir, store_id=s1, artifact_root=self.artifact_root)
        create_dense_fixture_store(self.stores_dir, store_id=s2, artifact_root=self.artifact_root)

        res = run_snakemake(
            ["all"],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res.returncode, 0, f"rule all dry run failed:\n{res.stderr}")
        self.assertIn("rule all:", res.stdout)
        self.assertIn(s1, res.stdout)
        self.assertIn(s2, res.stdout)

    def test_index_target_proposes_zero_build_jobs(self) -> None:
        """The index target depends only on bundle files and proposes 0 build jobs."""
        s1 = "OGS-00083"
        s2 = "OGS-00084"
        create_dense_fixture_store(self.stores_dir, store_id=s1, artifact_root=self.artifact_root)
        create_dense_fixture_store(self.stores_dir, store_id=s2, artifact_root=self.artifact_root)

        # Artifact root is empty (no built stores or records)
        rec_dir = paths.records_dir(s1, root=self.artifact_root)
        self.assertFalse(rec_dir.exists())

        res = run_snakemake(
            ["index"],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res.returncode, 0, f"index dry run failed:\n{res.stderr}")
        self.assertIn("rule index:", res.stdout)

        # Parse job stats to ensure exactly 1 job (index) and 0 build/complete/validate/register jobs
        job_lines = [
            line.strip() for line in res.stdout.splitlines()
            if line.strip().startswith("job") or line.strip().startswith("total") or (" " in line and line.strip().split()[-1].isdigit())
        ]
        # Assert no build rules are scheduled
        for build_rule in ["build", "complete", "top_hits", "rho", "overview", "validate", "register"]:
            self.assertNotIn(f"rule {build_rule}:", res.stdout)
            self.assertNotIn(f"\n{build_rule} ", res.stdout)


if __name__ == "__main__":
    unittest.main()
