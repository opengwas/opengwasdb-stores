#!/usr/bin/env python3
"""Tests for Phase A workflow/Snakefile orchestration (Issues #115, #116).

Verifies the central contracts of ADR 0022, ADR 0023, and ADR 0024:
1. Snakefile wires dependencies only (ADR 0023):
   - No Store Family name or target alias hardcoded.
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
   - The index target proposes 0 build jobs (depends only on bundle files).
7. End-to-end fixture build, idempotency, interruption resumption, and record deletion.
8. A run that would rebuild a published release is refused before any job writes (#195).
11. Reference completion end to end, under two ids (#303): one entry-point run
   naming only the completed child builds and publishes its parent, then
   completes, validates and publishes the child; the held-out position comes
   back imputed, `observed_only=True` matches the source's observations, and a
   re-run executes 0 jobs.
12. Reference completion end to end, under one id (#304, ADR 0037): one
   entry-point run builds and publishes the Observed-Only Store, then
   completes it into the id's staged path and publishes the completed Store
   at the same path, archiving the replaced Store, its register record and its
   Validation Record; a re-run executes 0 jobs.
13. Completing a published Observed-Only Store later (#305, ADR 0037): a
   Store built by the workflow as it was before #304 completes, once its
   bundle gains a `complete` block, with no build-phase job scheduled; an
   unforced rebuild of an amended release's build phase is still refused;
   and a forced run of a completed single-id release rebuilds both phases.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

from ogstores import bundle, manifest, paths, run
from ogstores.plan import plan

# The completed child is asserted on through opengwasdb's published query
# facade, never on its internal arrays (#303).
from opengwasdb.query import query_store

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
    """Create a minimal 2-variant GRCh38 GWAS-VCF fixture file.

    `FORMAT/EZ` is declared and written because opengwasdb's VCF reader queries
    it unconditionally: without the tag bcftools fails the whole query, and the
    build reports success while writing a Store with no association cells at
    all (#303, opengwas/opengwasdb#290).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "##fileformat=VCFv4.2\n"
        '##FILTER=<ID=PASS,Description="All filters passed">\n'
        '##INFO=<ID=AF,Number=A,Type=Float,Description="Allele Frequency">\n'
        '##FORMAT=<ID=EZ,Number=A,Type=Float,Description="Z-score">\n'
        '##FORMAT=<ID=ES,Number=A,Type=Float,Description="Effect size">\n'
        '##FORMAT=<ID=SE,Number=A,Type=Float,Description="Standard error">\n'
        '##FORMAT=<ID=LP,Number=A,Type=Float,Description="-log10(p-value)">\n'
        '##FORMAT=<ID=AF,Number=A,Type=Float,Description="Allele frequency">\n'
        '##FORMAT=<ID=SS,Number=A,Type=Float,Description="Sample size">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRAIT\n"
        "chr1\t10000\trs1\tA\tG\t.\tPASS\tAF=0.2\tEZ:ES:SE:LP:AF:SS\t5.0:0.1:0.02:5.0:0.2:10000\n"
        "chr1\t20000\trs2\tC\tT\t.\tPASS\tAF=0.4\tEZ:ES:SE:LP:AF:SS\t-6.666667:-0.2:0.03:8.0:0.4:10000\n"
    )
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(content)
    return path


# The signal fixture (#303): twelve chr1 positions, one held out of the VCF and
# covered by the LD panel, over a smooth z profile. Kept at module level so the
# tests and later tickets (#304-#309) can state their expected values as these
# literals rather than reading them back out of a built Store.
SIGNAL_POSITIONS: list[int] = [100_000 * (i + 1) for i in range(12)]
SIGNAL_HELD_OUT_INDEX: int = 6
SIGNAL_SE: float = 0.2
SIGNAL_Z: list[float] = [math.sin(3 * i / 11) * 5 + i * 0.1 for i in range(12)]
SIGNAL_ALID: list[str] = [f"1:{position}:A:G" for position in SIGNAL_POSITIONS]


def create_signal_fixture_vcf(path: Path) -> Path:
    """Create the 12-position signal GWAS-VCF, with one position held out (#303).

    `create_fixture_vcf` above is enough to build a Store, but reference
    completion imputes nothing against it: a block needs at least four observed
    points, and a two-position block leaves no signal to fit. Completion tests
    need a held-out position that genuinely comes back imputed, so they use the
    AR(1) *signal* profile opengwasdb's own completion tests use: a smooth z
    curve over twelve chr1 positions, SE 0.2, which projects onto a handful of
    eigenvectors and is therefore recoverable.

    One `TRAIT` sample column, as in `create_fixture_vcf`. The position at
    `SIGNAL_HELD_OUT_INDEX` is in `SIGNAL_POSITIONS` but not in the VCF, so it
    is observed nowhere and must be imputed from the panel.

    Two details the reader forces:

    - `FORMAT/EZ` is declared and written. opengwasdb's VCF reader queries `EZ`
      unconditionally, and bcftools fails the whole query when a header does
      not declare it; without it the build reports success and writes a Store
      with no association cells at all.
    - `REF=G`, `ALT=A`. The stored effect allele is the canonical `a1 = min(A, G)
      = A`, and the reader negates z when ALT sorts above REF, so ALT=A keeps
      every z at the value written here (opengwasdb ADR 0036) and keeps the
      allele frequency on the same allele the panel's `EA` names.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "##fileformat=VCFv4.2\n",
        '##FILTER=<ID=PASS,Description="All filters passed">\n',
        '##contig=<ID=chr1,length=248956422>\n',
        '##INFO=<ID=AF,Number=A,Type=Float,Description="Allele Frequency">\n',
        '##FORMAT=<ID=EZ,Number=A,Type=Float,Description="Z-score">\n',
        '##FORMAT=<ID=ES,Number=A,Type=Float,Description="Effect size">\n',
        '##FORMAT=<ID=SE,Number=A,Type=Float,Description="Standard error">\n',
        '##FORMAT=<ID=LP,Number=A,Type=Float,Description="-log10(p-value)">\n',
        '##FORMAT=<ID=AF,Number=A,Type=Float,Description="Allele frequency">\n',
        '##FORMAT=<ID=SS,Number=A,Type=Integer,Description="Sample size">\n',
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRAIT\n",
    ]
    for i, (position, z) in enumerate(zip(SIGNAL_POSITIONS, SIGNAL_Z, strict=True)):
        if i == SIGNAL_HELD_OUT_INDEX:
            continue
        es = z * SIGNAL_SE
        lp = -math.log10(math.erfc(abs(z) / math.sqrt(2)))
        lines.append(
            f"chr1\t{position}\trs{i}\tG\tA\t.\tPASS\tAF=0.3\tEZ:ES:SE:LP:AF:SS\t"
            f"{z:.6f}:{es:.6f}:{SIGNAL_SE}:{lp:.6f}:0.3:10000\n"
        )
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("".join(lines))
    return path


def create_signal_ld_panel(panel_root: Path) -> Path:
    """Write the fixture LD reference panel over `SIGNAL_POSITIONS` (#303).

    One block in the flat layout opengwasdb's completion reads
    (`<panel>/<ancestry>/<chrom>/<block>.tsv` plus `<block>.ldeig.npz`), with
    AR(1) correlation `0.9 ** |i - j|`: its leading eigenvectors are smooth
    basis functions, which is what lets an elastic net recover the held-out
    position at all. The eigendecomposition is written in descending
    eigenvalue order, as the panel contract requires, and no LD matrix is
    written -- an eigendecomposition-only panel is the production shape
    (opengwasdb ADR 0031).

    Copy of the recipe in opengwasdb's `tests/test_dense_completion.py`
    (`write_signal_panel`); those tests are not packaged, so it is copied rather
    than imported.
    """
    import numpy as np

    block_dir = panel_root / "EUR" / "1"
    block_dir.mkdir(parents=True, exist_ok=True)
    block = "100000-1200000"

    tsv_lines = ["CHR\tSNP\tOA\tEA\tEAF\tBP"]
    for position, alid in zip(SIGNAL_POSITIONS, SIGNAL_ALID, strict=True):
        chrom, _pos, a1, a2 = alid.split(":")
        tsv_lines.append(f"{chrom}\t{alid}\t{a2}\t{a1}\t0.3\t{position}")
    (block_dir / f"{block}.tsv").write_text("\n".join(tsv_lines) + "\n", encoding="utf-8")

    index = np.arange(len(SIGNAL_POSITIONS))
    ld = 0.9 ** np.abs(index[:, None] - index[None, :])
    values, vectors = np.linalg.eigh(ld)
    np.savez_compressed(
        block_dir / f"{block}.ldeig.npz",
        values=values[::-1],
        vectors=vectors[:, ::-1],
    )
    return panel_root


def create_completed_child_bundle(
    stores_dir: Path,
    child_id: str = "OGS-00102",
    *,
    parent_id: str,
    ld_panel: Path,
    artifact_root: Path | None = None,
) -> Path:
    """Create the Reference-Completed child Release Bundle of `parent_id` under stores_dir.

    Today's two-id completion: `derived_from` names the Observed-Only parent and
    `status: accepted` lets Store Release Build take it (#247). `build.yaml`
    carries a `complete` block instead of a `build` one, pointing `ld-panel` at
    the panel *root* (completion resolves `<root>/<ancestry>/<chrom>/...`) with
    `min-cor: 0.0` so a twelve-position fixture block still fits.

    `post` follows what a completion supports: `top_hits` is false because
    `complete-dense` builds the Top-Hit Index inline and the planner refuses a
    separate step; rho is false for the same reason dense rho is optional;
    `overview` is false because no completion assertion reads the overview page
    and the workflow suite budgets one fixture-scale end-to-end run per
    process; `validate` stays on, because validation is the gate that publishes
    the child. The bundle's `analyses.tsv` is the parent's: completion keeps the
    parent's Analyses, and the membership table stays with the release even
    though the completion step never reads it.

    `derived_from`/`status` are the only keys the workflow reads; the rest are
    what a real Release Bundle carries.
    """
    import yaml

    child_dir = stores_dir / child_id
    child_dir.mkdir(parents=True, exist_ok=True)
    art_root_str = str(artifact_root) if artifact_root else str(paths.DEFAULT_ARTIFACT_ROOT)

    release_yaml = {
        "store_id": child_id,
        "label": f"fixture-completed-{child_id}",
        "status": "accepted",
        "source_collection_id": "test-collection",
        "association_coverage": "full_gwas",
        "derived_from": parent_id,
        "created_at": "2026-08-18T16:00:00Z",
        "description": f"Fixture reference-completed store {child_id} for workflow testing",
        "source_snapshot_id": "test-snapshot",
        "release_kind": "pilot",
        "generator": {"command": "test-generator"},
    }
    build_yaml = {
        "store_id": child_id,
        "layout": "dense",
        "completion_state": "reference_completed",
        "complete": {
            "command": "complete-dense",
            "options": {
                "ld-panel": str(ld_panel),
                "ancestry": "EUR",
                "min-cor": 0.0,
            },
        },
        "post": {
            "top_hits": False,
            "rho": False,
            "overview": False,
            "validate": True,
        },
        "artifacts": {"root": art_root_str},
    }

    with open(child_dir / "release.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(release_yaml, f)
    with open(child_dir / "build.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(build_yaml, f)
    shutil.copyfile(stores_dir / parent_id / "analyses.tsv", child_dir / "analyses.tsv")
    return child_dir


def amend_to_complete_own_store(stores_dir: Path, store_id: str, *, ld_panel: Path) -> Path:
    """Give an Observed-Only fixture bundle a `complete` block: one id, published twice (ADR 0037).

    The bundle keeps its `build` block and `post` and gains what a single-id
    Reference-Completed release adds: `completion_state: reference_completed`
    and a `complete` block, with no `derived_from`. That is also the one
    amendment ADR 0037 allows an accepted bundle, so the same helper serves
    the later completion of a Store built earlier (#305). The `complete`
    options are `create_completed_child_bundle`'s.
    """
    import yaml

    build_yaml_p = stores_dir / store_id / "build.yaml"
    build_yaml = yaml.safe_load(build_yaml_p.read_text(encoding="utf-8"))
    build_yaml["completion_state"] = "reference_completed"
    build_yaml["complete"] = {
        "command": "complete-dense",
        "options": {"ld-panel": str(ld_panel), "ancestry": "EUR", "min-cor": 0.0},
    }
    build_yaml_p.write_text(yaml.safe_dump(build_yaml), encoding="utf-8")
    return build_yaml_p


def create_dense_fixture_store(
    stores_dir: Path,
    store_id: str = "OGS-00099",
    *,
    vcf_path: Path | None = None,
    artifact_root: Path | None = None,
    post_top_hits: bool = True,
    post_rho: bool = False,
    post_overview: bool = True,
    post_validate: bool = True,
    variant_reference_output: str | None = None,
    variant_reference_options: dict | None = None,
    status: str = "accepted",
) -> Path:
    """Create a complete fixture Release Bundle under stores_dir/<store_id>.

    It is `accepted` unless `status` says otherwise: the release workflow
    refuses a candidate (#247).
    """
    store_dir = stores_dir / store_id
    store_dir.mkdir(parents=True, exist_ok=True)

    if vcf_path is None:
        vcf_path = store_dir / "test.vcf.gz"
        create_fixture_vcf(vcf_path)

    art_root_str = str(artifact_root) if artifact_root else str(paths.DEFAULT_ARTIFACT_ROOT)

    release_yaml = {
        "store_id": store_id,
        "label": f"fixture-dense-{store_id}",
        "status": status,
        "source_collection_id": "test-collection",
        "association_coverage": "full_gwas",
        "derived_from": None,
        "created_at": "2026-08-18T08:51:59Z",
        "description": f"Fixture dense store {store_id} for workflow testing",
        "source_snapshot_id": "test-snapshot",
        "release_kind": "pilot",
        "generator": {"command": "test-generator"},
    }

    build_options: dict = {
        "source-reader-capability": "opengwasdb.gwas-vcf",
        "source-assembly": "hg38",
        "allow-unverified-eaf": True,
    }
    build_block: dict = {
        "command": "build-dense-vcf",
        "options": build_options,
    }
    if variant_reference_output is not None:
        build_options["variant-reference"] = variant_reference_output
        declaration: dict = {"output": variant_reference_output}
        if variant_reference_options:
            declaration["options"] = variant_reference_options
        build_block["variant_reference"] = declaration

    build_yaml = {
        "store_id": store_id,
        "layout": "dense",
        "completion_state": "observed_only",
        "build": build_block,
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
    config: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke snakemake CLI on Snakefile with explicit registry_root and artifact_root.

    `config` adds keys to the same `--config`: a second `--config` would
    replace the first.
    """
    cmd = find_snakemake_cmd() + [
        "--snakefile",
        str(snakefile),
        "--config",
        f"registry_root={registry_root}",
        f"artifact_root={artifact_root}",
        *(f"{key}={value}" for key, value in (config or {}).items()),
    ]
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd.extend(["--cores", "1"])
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(targets)

    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)


RELEASE_ENTRY_POINT: Path = REPO_ROOT / "workflow" / "release.py"


def release_argv(
    words: list[str],
    *,
    registry_root: Path,
    artifact_root: Path,
    config: dict[str, str] | None = None,
) -> list[str]:
    """The supported entry point's command line, as `pixi run release` runs it (#195)."""
    return [
        sys.executable,
        str(RELEASE_ENTRY_POINT),
        *words,
        "--cores",
        "1",
        "--config",
        f"registry_root={registry_root}",
        f"artifact_root={artifact_root}",
        *(f"{key}={value}" for key, value in (config or {}).items()),
    ]


def run_release(
    words: list[str],
    *,
    registry_root: Path,
    artifact_root: Path,
    config: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    new_session: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the supported entry point, `pixi run release`'s workflow/release.py (#195)."""
    argv = release_argv(words, registry_root=registry_root, artifact_root=artifact_root, config=config)
    run_env = dict(os.environ)
    run_env.update(env or {})
    return subprocess.run(
        argv, cwd=REPO_ROOT, capture_output=True, text=True, env=run_env, start_new_session=new_session
    )


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

    def test_artifact_root_comes_from_configuration_not_build_recipe(self) -> None:
        """The Snakefile resolves the artifact root from configuration, never a Build Recipe (#126)."""
        self.assertNotIn('build.get("artifacts")', self.code_text)
        self.assertNotIn("build.get('artifacts')", self.code_text)
        self.assertIn("paths.artifact_root(", self.code_text)

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
    """End-to-end execution, idempotency, and resumption over one fixture-scale Dense build.

    The workflow specification budgets *one* fixture-scale end-to-end run
    ("Fixture-scale end-to-end runs stay, as one smoke test"). That run happens
    once here, in `setUpClass`, and the tests that need a completed Store copy
    its result rather than rebuilding. Rebuilding per test cost 68s to assert
    three different things about the same built Store.

    `test_interrupted_step_resumes_without_restarting` deliberately keeps its
    own partial build: it needs a Store interrupted mid-way, and deriving that
    by deleting records from a completed build would not exercise the same state.
    """

    STORE_ID = "OGS-00099"

    @classmethod
    def setUpClass(cls) -> None:
        """Build the one fixture-scale Store this class shares, with a single snakemake target."""
        cls._class_tmp = tempfile.TemporaryDirectory()
        root = Path(cls._class_tmp.name)
        cls.template_stores = root / "stores"
        cls.template_stores.mkdir()
        cls.template_artifacts = root / "artifacts"
        cls.template_artifacts.mkdir()

        create_dense_fixture_store(
            cls.template_stores,
            store_id=cls.STORE_ID,
            artifact_root=cls.template_artifacts,
            post_top_hits=True,
            post_overview=True,
            post_validate=True,
        )
        cls.build_result = run_snakemake(
            [cls.STORE_ID],
            registry_root=cls.template_stores,
            artifact_root=cls.template_artifacts,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._class_tmp.cleanup()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.artifact_root = self.td / "artifacts"
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def use_completed_build(self) -> None:
        """Copy the class's one completed build into this test's own directories.

        Each test may then mutate records and re-run snakemake freely without
        disturbing the shared template or another test.
        """
        shutil.rmtree(self.stores_dir)
        shutil.rmtree(self.artifact_root)
        shutil.copytree(self.template_stores, self.stores_dir)
        shutil.copytree(self.template_artifacts, self.artifact_root)

    def test_dense_store_builds_end_to_end_with_one_command(self) -> None:
        """A fixture Dense store builds and registers end-to-end with a single snakemake target.

        This is the specification's one fixture-scale smoke test. The build it
        asserts on is the class's shared build, performed with exactly one
        snakemake invocation naming one target.
        """
        store_id = self.STORE_ID
        res = self.build_result
        self.assertEqual(
            res.returncode,
            0,
            f"Snakemake failed (exit {res.returncode}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )

        rec_dir = paths.records_dir(store_id, root=self.template_artifacts)
        expected_records = ["build.json", "top-hits.json", "overview.json", "validate.json", "register.json"]
        for rec_name in expected_records:
            rec_p = rec_dir / rec_name
            self.assertTrue(rec_p.is_file(), f"Expected record {rec_p} does not exist")
            data = json.loads(rec_p.read_text(encoding="utf-8"))
            self.assertTrue(data.get("success"), f"Record {rec_name} reported success=False: {data}")
            self.assertEqual(data.get("exit_code"), 0)
            self.assertEqual(data.get("store_id"), store_id)

        final_store = paths.store_path(store_id, root=self.template_artifacts)
        partial_store = paths.partial_store_path(store_id, root=self.template_artifacts)
        self.assertTrue(final_store.is_dir(), f"Published Store not found at {final_store}")
        self.assertFalse(partial_store.exists(), f"Transient partial store still exists at {partial_store}")

        val_res = subprocess.run(["opengwasdb", "validate", str(final_store)], capture_output=True, text=True)
        self.assertEqual(val_res.returncode, 0, f"opengwasdb validate failed: {val_res.stderr}")

    def test_rerun_after_success_is_idempotent_no_jobs(self) -> None:
        """Re-running snakemake over an already completed release re-runs 0 jobs."""
        store_id = self.STORE_ID
        self.use_completed_build()

        rec_dir = paths.records_dir(store_id, root=self.artifact_root)
        mtimes_before = {p: p.stat().st_mtime_ns for p in rec_dir.glob("*.json")}
        self.assertTrue(mtimes_before, "copied build produced no records to compare")

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

    def test_rebuilding_a_published_release_is_refused_before_any_job_writes(self) -> None:
        """A run that would rebuild a published release refuses before any job runs (#195).

        A fresh checkout gives every bundle file a newer mtime than the records
        beside its published Store, so Snakemake schedules the whole chain
        again. Before #195 that run rebuilt into `.partial`, rewrote the
        release's `work/` and `records/`, and only then failed at publication
        with `StoreExistsError`: the Store survived, its evidence did not.
        """
        store_id = self.STORE_ID
        self.use_completed_build()
        store_dir = paths.store_dir(store_id, root=self.artifact_root)
        validation_p = self.stores_dir / store_id / "validation.yaml"

        def snapshot() -> dict[str, tuple[int, bytes]]:
            files = [p for p in store_dir.rglob("*") if p.is_file()] + [validation_p]
            return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in files}

        # Age the published release by a day, keeping its files' relative
        # order, so the bundle is newer than its records as after a checkout.
        day_ns = 86_400 * 10**9
        for p in store_dir.rglob("*"):
            if p.is_file():
                st = p.stat()
                os.utime(p, ns=(st.st_atime_ns - day_ns, st.st_mtime_ns - day_ns))

        res_dry = run_snakemake(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res_dry.returncode, 0, res_dry.stderr)
        self.assertIn("build", scheduled_targets(res_dry.stdout), "the fixture must schedule a rebuild")

        before = snapshot()
        res = run_snakemake([store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root)

        self.assertNotEqual(res.returncode, 0, f"a rebuild of a published release must be refused:\n{res.stdout}")
        output = res.stdout + res.stderr
        self.assertIn(f"{store_id}: {paths.store_path(store_id, root=self.artifact_root)}", output)
        self.assertIn("Nothing was written", output)
        self.assertEqual(snapshot(), before, "the refused run changed the published release's files")
        self.assertFalse(paths.partial_store_path(store_id, root=self.artifact_root).exists())

    def age_published_release(self, store_id: str) -> None:
        """Make the bundle newer than the published release's files, as a fresh checkout does."""
        day_ns = 86_400 * 10**9
        for p in paths.store_dir(store_id, root=self.artifact_root).rglob("*"):
            if p.is_file():
                st = p.stat()
                os.utime(p, ns=(st.st_atime_ns - day_ns, st.st_mtime_ns - day_ns))

    def release_snapshot(self, store_id: str) -> dict[str, tuple[int, bytes]]:
        """Every file of the release beside its bundle's validation.yaml, by bytes and mtime."""
        store_dir = paths.store_dir(store_id, root=self.artifact_root)
        files = [p for p in store_dir.rglob("*") if p.is_file()]
        files.append(self.stores_dir / store_id / "validation.yaml")
        return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in files}

    def records_of(self, records_dir: Path) -> dict[str, tuple[int, bytes]]:
        return {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(records_dir.iterdir())}

    def test_forced_rebuild_archives_the_old_release_and_publishes_the_new(self) -> None:
        """`--config force=1` replaces a named release; the old Store, records and record are archived (#195)."""
        store_id = self.STORE_ID
        self.use_completed_build()
        self.age_published_release(store_id)
        store_dir = paths.store_dir(store_id, root=self.artifact_root)
        store_p = paths.store_path(store_id, root=self.artifact_root)
        validation_p = self.stores_dir / store_id / "validation.yaml"
        old_manifest = (store_p / "manifest.json").read_bytes()
        old_records = self.records_of(paths.records_dir(store_id, root=self.artifact_root))
        old_validation = validation_p.read_bytes()

        res = run_release(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            config={"force": "1"},
        )

        self.assertEqual(res.returncode, 0, f"forced run failed:\n{res.stdout}\n{res.stderr}")
        archives = sorted((store_dir / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)
        archive = archives[0]
        self.assertEqual((archive / "store.opengwasdb" / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual(self.records_of(archive / "records"), old_records)
        self.assertEqual((archive / "validation.yaml").read_bytes(), old_validation)
        self.assertNotEqual((store_p / "manifest.json").read_bytes(), old_manifest, "a new Store is published")
        self.assertEqual(sorted(p.name for p in store_dir.glob("records.before-force-*")), [])
        self.assertFalse(paths.partial_store_path(store_id, root=self.artifact_root).exists())
        self.assertFalse((store_dir / "store.opengwasdb.backup").exists())

        import yaml
        written = yaml.safe_load(validation_p.read_text(encoding="utf-8"))
        self.assertEqual(written["replaced"]["archive"], str(archive))
        register_rec = json.loads(paths.record_path(store_id, "register", root=self.artifact_root).read_text())
        self.assertEqual(register_rec["replaced_archive"], str(archive))

    def test_a_forced_run_failing_midway_restores_the_records(self) -> None:
        """A forced run whose build fails leaves the Store, validation.yaml and records/ as they were."""
        store_id = self.STORE_ID
        self.use_completed_build()
        self.age_published_release(store_id)
        # Point this test's copy of the bundle at a broken source; the template's
        # VCF, which the copied manifest names, is shared with other tests.
        broken = self.td / "broken.vcf.gz"
        broken.write_bytes(b"not a gzip file\n")
        analyses_p = self.stores_dir / store_id / "analyses.tsv"
        template_vcf = str(self.template_stores / store_id / "test.vcf.gz")
        self.assertIn(template_vcf, analyses_p.read_text(encoding="utf-8"))
        analyses_p.write_text(
            analyses_p.read_text(encoding="utf-8").replace(template_vcf, str(broken)), encoding="utf-8"
        )
        store_dir = paths.store_dir(store_id, root=self.artifact_root)
        records = paths.records_dir(store_id, root=self.artifact_root)
        before_records = self.records_of(records)
        before_store = {
            str(p): p.read_bytes()
            for p in paths.store_path(store_id, root=self.artifact_root).rglob("*") if p.is_file()
        }
        validation_p = self.stores_dir / store_id / "validation.yaml"
        before_validation = validation_p.read_bytes()

        res = run_release(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            config={"force": "1"},
        )

        self.assertNotEqual(res.returncode, 0, "the broken source must fail the build")
        output = res.stdout + res.stderr
        self.assertNotIn("Refusing to run", output, "the forced run must get as far as the build")
        self.assertIn("Error in rule step", output)
        self.assertIn("step=build", output)
        self.assertEqual(self.records_of(records), before_records)
        self.assertEqual(
            {
                str(p): p.read_bytes()
                for p in paths.store_path(store_id, root=self.artifact_root).rglob("*") if p.is_file()
            },
            before_store,
        )
        self.assertEqual(validation_p.read_bytes(), before_validation)
        self.assertEqual(sorted(p.name for p in store_dir.glob("records.before-force-*")), [])
        self.assertFalse((store_dir / "replaced").exists())

    def test_a_leftover_snapshot_blocks_the_next_run(self) -> None:
        """After a killed forced run, every run of that release refuses until an operator resolves it."""
        store_id = self.STORE_ID
        self.use_completed_build()
        self.age_published_release(store_id)
        leftover = paths.force_snapshot_path(store_id, "20261006T235959Z", root=self.artifact_root)
        shutil.copytree(paths.records_dir(store_id, root=self.artifact_root), leftover)
        before = self.release_snapshot(store_id)

        for config in ({}, {"force": "1"}):
            with self.subTest(config=config):
                res = run_release(
                    [store_id],
                    registry_root=self.stores_dir,
                    artifact_root=self.artifact_root,
                    config=config,
                )
                self.assertNotEqual(res.returncode, 0)
                output = res.stdout + res.stderr
                self.assertIn(str(leftover), output)
                self.assertIn("restore it", output)
                self.assertIn("delete it", output)
                self.assertEqual(self.release_snapshot(store_id), before)

    def test_the_entry_point_refuses_an_unforced_rebuild_before_any_write(self) -> None:
        """`pixi run release <published id>` refuses in its preflight, before Snakemake runs a job."""
        store_id = self.STORE_ID
        self.use_completed_build()
        self.age_published_release(store_id)
        before = self.release_snapshot(store_id)

        res = run_release([store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root)

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn(f"{store_id}: {paths.store_path(store_id, root=self.artifact_root)}", res.stderr)
        self.assertIn("--config force=1", res.stderr)
        self.assertEqual(self.release_snapshot(store_id), before)
        self.assertFalse(paths.partial_store_path(store_id, root=self.artifact_root).exists())

    def test_no_hooks_and_other_unsafe_options_are_refused_before_any_write(self) -> None:
        """Options that would bypass or weaken the guard never reach Snakemake (#195 review)."""
        store_id = self.STORE_ID
        self.use_completed_build()
        self.age_published_release(store_id)
        before = self.release_snapshot(store_id)

        for option in ("--no-hooks", "--touch", "--forceall", "--nolock", "--ignore-incomplete", "--snakefile=other"):
            for config in ({}, {"force": "1"}):
                with self.subTest(option=option, config=config):
                    res = run_release(
                        [store_id, option],
                        registry_root=self.stores_dir,
                        artifact_root=self.artifact_root,
                        config=config,
                    )
                    self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                    self.assertIn("refused", res.stderr)
                    self.assertEqual(self.release_snapshot(store_id), before)
        self.assertFalse(paths.partial_store_path(store_id, root=self.artifact_root).exists())
        self.assertEqual(run.pending_force_snapshots(store_id, self.artifact_root), [])

    def test_an_operator_cannot_set_the_release_run(self) -> None:
        store_id = self.STORE_ID
        self.use_completed_build()
        res = run_release(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            config={"release_run": "20261007T000000Z"},
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("release_run", res.stderr)

    def test_resolve_snapshot_refuses_misuse_without_writing(self) -> None:
        """`--resolve-snapshot` needs one id and a leftover snapshot; it never runs Snakemake."""
        store_id = self.STORE_ID
        self.use_completed_build()
        before = self.release_snapshot(store_id)
        cases = (
            ([store_id, "--resolve-snapshot", "restore"], 1, "0 records snapshot"),
            ([store_id, "--resolve-snapshot", "maybe"], 2, "restore` or `delete"),
            (["all", "--resolve-snapshot", "delete"], 2, "exactly one release id"),
            ([store_id, "--resolve-snapshot", "delete", "--dry-run"], 2, "exactly one release id"),
        )
        for words, code, message in cases:
            with self.subTest(words=words):
                res = run_release(words, registry_root=self.stores_dir, artifact_root=self.artifact_root)
                self.assertEqual(res.returncode, code, res.stdout + res.stderr)
                self.assertIn(message, res.stderr)
                self.assertEqual(self.release_snapshot(store_id), before)

    def test_a_register_job_killed_mid_publication_is_completed_by_the_same_run(self) -> None:
        """Only the register job dies; the entry point survives and finishes the publication itself."""
        store_id = self.STORE_ID
        self.use_completed_build()
        self.age_published_release(store_id)
        store_dir = paths.store_dir(store_id, root=self.artifact_root)
        old_manifest = (paths.store_path(store_id, root=self.artifact_root) / "manifest.json").read_bytes()

        res = run_release(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            config={"force": "1"},
            env={run.KILL_AT_ENV: "store-published"},
        )

        self.assertNotEqual(res.returncode, 0, "Snakemake saw its register job die")
        self.assertIn(f"finished the publication of {store_id}", res.stdout)
        archives = sorted((store_dir / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)
        self.assertEqual((archives[0] / "store.opengwasdb" / "manifest.json").read_bytes(), old_manifest)
        self.assertTrue((archives[0] / "records").is_dir())
        self.assertFalse(paths.publication_marker(store_id, root=self.artifact_root).exists())
        self.assertTrue(paths.record_path(store_id, "register", root=self.artifact_root).is_file())
        nothing = run_release([store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(nothing.returncode, 0, nothing.stdout + nothing.stderr)
        self.assertIn("nothing to be done", nothing.stdout)

    def test_deleting_single_record_file_reruns_exact_step_and_downstream(self) -> None:
        """Deleting records/validate.json triggers only validate, register, and store target in dry-run."""
        store_id = self.STORE_ID
        self.use_completed_build()

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


_TWO_ID_COMPLETION: dict[str, Any] | None = None


def two_id_completion_template() -> dict[str, Any]:
    """The one two-id completion pair this process builds, built on first use (#303).

    Returns `parent_id`, `child_id`, `analysis_id`, the built `stores` and
    `artifacts` directories, the entry point's `release_result`, and the
    `TemporaryDirectory` that holds them (kept here so the template outlives
    every test that copies it). A test that may mutate the pair copies `stores`
    and `artifacts` into its own directories; the template is never written
    again.

    Later completion tickets (#304-#309) need this same fixture shape, so the
    pair is built at most once per process rather than once per class: that is
    what keeps the workflow suite's fixture-scale cost at one parent build plus
    one completion rather than one per completion test.

    The parent's `top_hits` and `overview` post steps and the child's `overview`
    are off, because no assertion here reads them; completion never reads them
    either. `validate` and publication stay on -- they are the gate and the
    state under test.
    """
    global _TWO_ID_COMPLETION
    if _TWO_ID_COMPLETION is not None:
        return _TWO_ID_COMPLETION

    parent_id = "OGS-00101"
    child_id = "OGS-00102"
    temp_dir = tempfile.TemporaryDirectory()
    root = Path(temp_dir.name)
    stores = root / "stores"
    stores.mkdir()
    artifacts = root / "artifacts"
    artifacts.mkdir()

    signal_vcf = create_signal_fixture_vcf(root / "signal.vcf.gz")
    create_dense_fixture_store(
        stores,
        store_id=parent_id,
        artifact_root=artifacts,
        vcf_path=signal_vcf,
        post_top_hits=False,
        post_overview=False,
    )
    ld_panel = create_signal_ld_panel(root / "ld-panel")
    create_completed_child_bundle(
        stores,
        child_id=child_id,
        parent_id=parent_id,
        ld_panel=ld_panel,
        artifact_root=artifacts,
    )
    release_result = run_release([child_id], registry_root=stores, artifact_root=artifacts)

    _TWO_ID_COMPLETION = {
        "temp_dir": temp_dir,
        "stores": stores,
        "artifacts": artifacts,
        "release_result": release_result,
        "parent_id": parent_id,
        "child_id": child_id,
        "analysis_id": f"test_analysis_{parent_id}",
    }
    return _TWO_ID_COMPLETION


class TestWorkflowTwoIdReferenceCompletion(unittest.TestCase):
    """Reference completion end to end, through the entry point (#303).

    Today (ADR 0007) a Reference-Completed Store is a second Store Release
    derived from its Observed-Only parent; ADR 0037 replaces that with one id
    published twice. Both shapes run the same completion, so this class proves
    the two-id shape against today's code: one entry-point invocation naming
    only the child builds and publishes the parent, then completes, validates
    and publishes the child under its own id.

    The completion is real, not vacuous: the fixture VCF holds out one of the
    twelve `SIGNAL_POSITIONS` that the LD panel covers, so the held-out
    association must come back imputed. A Store that reported
    `reference_completed` while imputing nothing would pass every weaker
    assertion here.

    The pair costs one parent build plus one completion, so it comes from the
    module-level `two_id_completion_template` -- built once per process, shared
    with later completion tickets -- and each test copies it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Take this process's one built pair, building it on the first class that asks."""
        template = two_id_completion_template()
        cls.PARENT_ID: str = template["parent_id"]
        cls.CHILD_ID: str = template["child_id"]
        cls.ANALYSIS_ID: str = template["analysis_id"]
        cls.template_stores: Path = template["stores"]
        cls.template_artifacts: Path = template["artifacts"]
        cls.release_result = template["release_result"]

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.artifact_root = self.td / "artifacts"
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def use_completed_pair(self) -> None:
        """Copy the shared template pair into this test's own directories."""
        shutil.rmtree(self.stores_dir)
        shutil.rmtree(self.artifact_root)
        shutil.copytree(self.template_stores, self.stores_dir)
        shutil.copytree(self.template_artifacts, self.artifact_root)

    def test_one_entry_point_run_publishes_the_parent_and_the_completed_child(self) -> None:
        """Naming only the child builds the parent, then completes, validates and publishes the child.

        The completion reads a published Store, so the parent's own register
        record has to exist first: that ordering is what naming one id buys.
        """
        res = self.release_result
        self.assertEqual(
            res.returncode,
            0,
            f"The entry point failed (exit {res.returncode}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )

        expected_steps = {
            self.PARENT_ID: ["build", "validate", "register"],
            self.CHILD_ID: ["complete", "validate", "register"],
        }
        for store_id, steps in expected_steps.items():
            rec_dir = paths.records_dir(store_id, root=self.template_artifacts)
            for step in steps:
                rec_p = rec_dir / f"{step}.json"
                self.assertTrue(rec_p.is_file(), f"Expected record {rec_p} does not exist")
                data = json.loads(rec_p.read_text(encoding="utf-8"))
                self.assertTrue(data.get("success"), f"Record {rec_p.name} reported success=False: {data}")
                self.assertEqual(data.get("exit_code"), 0)
                self.assertEqual(data.get("store_id"), store_id)

            final_store = paths.store_path(store_id, root=self.template_artifacts)
            self.assertTrue(final_store.is_dir(), f"Published Store not found at {final_store}")
            self.assertFalse(
                paths.partial_store_path(store_id, root=self.template_artifacts).exists(),
                f"Transient partial store still exists for {store_id}",
            )
            val_res = subprocess.run(["opengwasdb", "validate", str(final_store)], capture_output=True, text=True)
            self.assertEqual(
                val_res.returncode,
                0,
                f"opengwasdb validate failed on {store_id}:\nSTDOUT:\n{val_res.stdout}\nSTDERR:\n{val_res.stderr}",
            )

    def test_the_child_is_a_reference_completion_that_imputes_the_held_out_position(self) -> None:
        """The child's manifest says completed, and the held-out position comes back imputed.

        `store_id` is deliberately not asserted to be the child's id. A
        completion inherits the source manifest's `store_id` -- the parent's --
        and only takes its own `release_id`. ADR 0037 gives the completed Store
        the id's own Store, which is #302's job; today's two-id shape keeps the
        parent's identity, and asserting otherwise would fail for a reason this
        ticket does not change.
        """
        self.use_completed_pair()
        child_store = paths.store_path(self.CHILD_ID, root=self.artifact_root)
        manifest_doc = json.loads((child_store / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest_doc["completion_state"], "reference_completed")
        self.assertEqual(manifest_doc["release_id"], self.CHILD_ID)
        self.assertEqual(manifest_doc["store_id"], self.PARENT_ID)
        self.assertIn("completion", manifest_doc.get("provenance") or {})

        q = query_store(child_store)
        try:
            rows = q.analysis(self.ANALYSIS_ID)
            alids = [q.variants_table()[int(index)]["alid"] for index in rows["variant_index"]]
        finally:
            q.close()

        status_by_alid = dict(zip(alids, rows["association_status"].tolist(), strict=True))
        held_out = SIGNAL_ALID[SIGNAL_HELD_OUT_INDEX]
        self.assertIn(held_out, status_by_alid, "the held-out position must be on the completed axis")
        self.assertEqual(
            status_by_alid[held_out],
            "imputed",
            f"the held-out position came back {status_by_alid[held_out]!r}, not imputed: {status_by_alid}",
        )
        self.assertEqual(
            sum(status == "observed" for status in status_by_alid.values()),
            len(SIGNAL_POSITIONS) - 1,
            f"exactly the VCF's observed positions should be observed: {status_by_alid}",
        )

    def test_observed_only_returns_exactly_the_parents_observed_associations(self) -> None:
        """`observed_only=True` returns the parent's decoded z, se and eaf unchanged, by ALID.

        Completion reproducing every observed cell bit for bit is the property
        ADR 0037 Decision 2 gates a replacement on, so the parent Store is the
        right expected value here and the comparison is exact: a tolerance would
        accept a one-quantum change to an observed value, which is exactly what
        completion must never make.

        Beside it, the fixture's own meaningfulness: the parent's decoded z and
        se must each sit within one quantum of the VCF literals
        `create_signal_fixture_vcf` writes -- one step of the `int16` fixed-point
        z plane (`opengwasdb.encoding.plan.DEFAULT_Z_SCALE`, 1/1024) and one
        `float16` step for se, the encoding the Store's manifest declares. Without
        that, a Store holding no observed cells at all would pass the exactness
        check vacuously.
        """
        import numpy as np
        from opengwasdb.encoding.plan import DEFAULT_Z_SCALE

        self.use_completed_pair()

        def decoded(store_p: Path) -> dict[str, tuple[float, float, float]]:
            q = query_store(store_p)
            try:
                rows = q.analysis(self.ANALYSIS_ID, observed_only=True)
                table = q.variants_table()
            finally:
                q.close()
            self.assertNotIn("imputed", set(rows["association_status"].tolist()))
            return {
                table[int(index)]["alid"]: (float(z), float(se), float(eaf))
                for index, z, se, eaf in zip(
                    rows["variant_index"], rows["z"], rows["se"], rows["eaf"], strict=True
                )
            }

        parent = decoded(paths.store_path(self.PARENT_ID, root=self.artifact_root))
        child = decoded(paths.store_path(self.CHILD_ID, root=self.artifact_root))
        expected_alids = [
            SIGNAL_ALID[i] for i in range(len(SIGNAL_POSITIONS)) if i != SIGNAL_HELD_OUT_INDEX
        ]
        self.assertEqual(sorted(parent), sorted(expected_alids), "the parent holds exactly the VCF's observations")
        self.assertEqual(sorted(child), sorted(expected_alids), "observed_only returns the observed associations only")

        z_quantum = 1.0 / DEFAULT_Z_SCALE
        se_quantum = float(np.spacing(np.float16(SIGNAL_SE)))
        index_by_alid = {alid: i for i, alid in enumerate(SIGNAL_ALID)}
        for alid in expected_alids:
            parent_z, parent_se, parent_eaf = parent[alid]
            child_z, child_se, child_eaf = child[alid]
            self.assertEqual(child_z, parent_z, f"z changed for {alid}")
            self.assertEqual(child_se, parent_se, f"se changed for {alid}")
            self.assertTrue(
                child_eaf == parent_eaf or (np.isnan(child_eaf) and np.isnan(parent_eaf)),
                f"eaf changed for {alid}: {child_eaf!r} != {parent_eaf!r}",
            )

            i = index_by_alid[alid]
            self.assertLessEqual(
                abs(parent_z - SIGNAL_Z[i]), z_quantum, f"decoded z for {alid} is not the VCF literal"
            )
            self.assertLessEqual(
                abs(parent_se - SIGNAL_SE), se_quantum, f"decoded se for {alid} is not the VCF literal"
            )

    def test_a_rerun_executes_zero_jobs(self) -> None:
        """Re-running the entry point over a copy of the published pair executes 0 jobs.

        A copy stands in for an operator's other checkout: it has no Snakemake
        provenance for its paths, so the dry run decides from mtimes alone. Nothing
        may be rewritten either -- the entry point short-circuits before Snakemake
        when nothing is scheduled, and no record's mtime may move.
        """
        self.use_completed_pair()

        mtimes_before: dict[str, dict[Path, int]] = {}
        for store_id in (self.PARENT_ID, self.CHILD_ID):
            rec_dir = paths.records_dir(store_id, root=self.artifact_root)
            mtimes_before[store_id] = {p: p.stat().st_mtime_ns for p in rec_dir.glob("*.json")}
            self.assertTrue(mtimes_before[store_id], f"the copied pair has no records for {store_id}")

        res = run_release([self.CHILD_ID], registry_root=self.stores_dir, artifact_root=self.artifact_root)

        self.assertEqual(res.returncode, 0, f"Re-run failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("nothing to be done", res.stdout)
        for store_id, before in mtimes_before.items():
            for rec_p, mtime in before.items():
                self.assertEqual(
                    rec_p.stat().st_mtime_ns,
                    mtime,
                    f"Record {rec_p.name} of {store_id} was rewritten by an idempotent re-run",
                )


_SINGLE_ID_COMPLETION: dict[str, Any] | None = None


def single_id_completion_template() -> dict[str, Any]:
    """The one single-id completed release this process builds, built on first use (#304).

    One fresh entry-point run builds, publishes and completes it, and a
    second run in place follows at once. Returns `store_id`, `analysis_id`,
    the built `stores` and `artifacts`, both runs' results, the record
    mtimes the second run found (`records_before_rerun`), and the
    `TemporaryDirectory` that holds them. A test that may write copies
    `stores` and `artifacts`; the template is never written again.

    The rerun is part of the template, not of a test, because only a rerun in
    place reads the Snakemake metadata the build recorded, and a test that
    ran it against the shared template could write to it. The same shape as
    `two_id_completion_template`: `top_hits` and `overview` are off, and
    `validate` and publication stay on in both phases. Later completion
    tickets (#305-#309) need this shape too, and copy it.
    """
    global _SINGLE_ID_COMPLETION
    if _SINGLE_ID_COMPLETION is not None:
        return _SINGLE_ID_COMPLETION

    store_id = "OGS-00110"
    temp_dir = tempfile.TemporaryDirectory()
    root = Path(temp_dir.name)
    stores = root / "stores"
    stores.mkdir()
    artifacts = root / "artifacts"
    artifacts.mkdir()

    create_dense_fixture_store(
        stores,
        store_id=store_id,
        artifact_root=artifacts,
        vcf_path=create_signal_fixture_vcf(root / "signal.vcf.gz"),
        post_top_hits=False,
        post_overview=False,
    )
    amend_to_complete_own_store(stores, store_id, ld_panel=create_signal_ld_panel(root / "ld-panel"))
    release_result = run_release([store_id], registry_root=stores, artifact_root=artifacts)
    records_before_rerun = {
        p: p.stat().st_mtime_ns for p in paths.records_dir(store_id, root=artifacts).glob("*.json")
    }
    rerun_result = run_release([store_id], registry_root=stores, artifact_root=artifacts)

    _SINGLE_ID_COMPLETION = {
        "temp_dir": temp_dir,
        "stores": stores,
        "artifacts": artifacts,
        "release_result": release_result,
        "rerun_result": rerun_result,
        "records_before_rerun": records_before_rerun,
        "store_id": store_id,
        "analysis_id": f"test_analysis_{store_id}",
    }
    return _SINGLE_ID_COMPLETION


class TestWorkflowSingleIdReferenceCompletion(unittest.TestCase):
    """Reference completion under one id, end to end through the entry point (#304, ADR 0037).

    One Release Bundle declares the build and the completion of a Dense Store.
    One `pixi run release <id>` builds and publishes the Observed-Only Store,
    completes it from that published Store into the id's staged path,
    validates it there, and publishes the completed Store at the same path.
    The replaced Store is archived, as a forced run archives it, under a
    `replaced/<stamp>/` that holds its register record and Validation Record
    too.

    The Observed-Only Store is asserted on in its archive: that it is there,
    observed-only and holding the VCF's observations, is the evidence that it
    was published first and then replaced. The fixture is #303's signal
    profile, whose completion `TestWorkflowTwoIdReferenceCompletion` already
    shows to be real, so this class asserts only what one id changes.
    """

    PHASE_RECORDS = ["build", "validate", "register", "complete", "completion-validate", "completion-register"]

    @classmethod
    def setUpClass(cls) -> None:
        template = single_id_completion_template()
        cls.STORE_ID: str = template["store_id"]
        cls.ANALYSIS_ID: str = template["analysis_id"]
        cls.template_stores: Path = template["stores"]
        cls.template_artifacts: Path = template["artifacts"]
        cls.release_result = template["release_result"]
        cls.rerun_result = template["rerun_result"]
        cls.records_before_rerun: dict[Path, int] = template["records_before_rerun"]

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.artifact_root = self.td / "artifacts"
        shutil.copytree(self.template_stores, self.stores_dir)
        shutil.copytree(self.template_artifacts, self.artifact_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def archive(self, root: Path | None = None) -> Path:
        """The one archive the run left, the completion phase's replacement, under this test's copy or `root`."""
        archives = sorted((paths.store_dir(self.STORE_ID, root=root or self.artifact_root) / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)
        return archives[0]

    def manifest(self, store_p: Path) -> dict[str, Any]:
        return json.loads((store_p / "manifest.json").read_text(encoding="utf-8"))

    def observed(self, store_p: Path) -> dict[str, tuple[float, float, float]]:
        """`observed_only=True` associations by ALID: decoded z, se and eaf."""
        q = query_store(store_p)
        try:
            rows = q.analysis(self.ANALYSIS_ID, observed_only=True)
            table = q.variants_table()
        finally:
            q.close()
        self.assertNotIn("imputed", set(rows["association_status"].tolist()))
        return {
            table[int(index)]["alid"]: (float(z), float(se), float(eaf))
            for index, z, se, eaf in zip(rows["variant_index"], rows["z"], rows["se"], rows["eaf"], strict=True)
        }

    def test_one_run_publishes_the_observed_only_store_then_the_completed_store_at_its_path(self) -> None:
        res = self.release_result
        self.assertEqual(
            res.returncode, 0, f"The entry point failed (exit {res.returncode}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
        rec_dir = paths.records_dir(self.STORE_ID, root=self.artifact_root)
        for step in self.PHASE_RECORDS:
            data = json.loads((rec_dir / f"{step}.json").read_text(encoding="utf-8"))
            self.assertTrue(data.get("success"), f"{step}: {data}")
            self.assertEqual(data.get("exit_code"), 0)
            self.assertEqual(data.get("store_id"), self.STORE_ID)

        # Records name the paths the template was built at.
        built_store = paths.store_path(self.STORE_ID, root=self.template_artifacts)
        build_register = json.loads((rec_dir / "register.json").read_text(encoding="utf-8"))
        completion_register = json.loads((rec_dir / "completion-register.json").read_text(encoding="utf-8"))
        self.assertEqual(build_register["published_store"], str(built_store))
        self.assertIsNone(build_register["replaced_archive"], "the build phase publishes first")
        self.assertEqual(completion_register["published_store"], str(built_store))
        self.assertEqual(completion_register["replaced_archive"], str(self.archive(self.template_artifacts)))

        store_p = paths.store_path(self.STORE_ID, root=self.artifact_root)

        self.assertEqual(self.manifest(store_p)["completion_state"], "reference_completed")
        replaced = self.archive() / "store.opengwasdb"
        self.assertEqual(self.manifest(replaced)["completion_state"], "observed_only")
        self.assertEqual(self.manifest(replaced)["release_id"], self.STORE_ID)
        for leftover in (
            paths.partial_store_path(self.STORE_ID, root=self.artifact_root),
            paths.backup_store_path(self.STORE_ID, root=self.artifact_root),
            paths.publication_marker(self.STORE_ID, root=self.artifact_root),
        ):
            self.assertFalse(leftover.exists(), leftover)

        # The completion read the published Store and wrote the staged one.
        complete_argv = json.loads((rec_dir / "complete.json").read_text(encoding="utf-8"))["argv"]
        self.assertEqual(
            complete_argv[2:4],
            [str(built_store), str(paths.partial_store_path(self.STORE_ID, root=self.template_artifacts))],
        )

    def test_the_completed_store_keeps_the_id_and_takes_its_own_release_id(self) -> None:
        """Completion inherits its source's Store identity, here the id itself (opengwasdb ADR 0011)."""
        manifest_doc = self.manifest(paths.store_path(self.STORE_ID, root=self.artifact_root))
        self.assertEqual(manifest_doc["store_id"], self.STORE_ID)
        self.assertEqual(manifest_doc["release_id"], f"{self.STORE_ID}-completed")

    def test_observed_only_returns_the_observed_only_stores_associations_unchanged(self) -> None:
        """By ALID, z, se and eaf are exactly those of the Store it replaced.

        That the fixture's decoded values are the VCF's, and that the held-out
        position comes back imputed, is #303's class's to prove: the build
        and the completion are the same commands here.
        """
        import numpy as np

        completed = self.observed(paths.store_path(self.STORE_ID, root=self.artifact_root))
        replaced = self.observed(self.archive() / "store.opengwasdb")
        expected_alids = sorted(SIGNAL_ALID[i] for i in range(len(SIGNAL_POSITIONS)) if i != SIGNAL_HELD_OUT_INDEX)
        self.assertEqual(sorted(replaced), expected_alids)
        self.assertEqual(sorted(completed), expected_alids)
        for alid in expected_alids:
            z, se, eaf = completed[alid]
            before_z, before_se, before_eaf = replaced[alid]
            self.assertEqual(z, before_z, f"z changed for {alid}")
            self.assertEqual(se, before_se, f"se changed for {alid}")
            self.assertTrue(eaf == before_eaf or (np.isnan(eaf) and np.isnan(before_eaf)), alid)

    def test_the_replaced_release_is_archived_and_the_build_phases_records_stay(self) -> None:
        import yaml

        archive = self.archive()
        rec_dir = paths.records_dir(self.STORE_ID, root=self.artifact_root)
        self.assertEqual((archive / "records" / "register.json").read_bytes(), (rec_dir / "register.json").read_bytes())
        archived_record = yaml.safe_load((archive / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(archived_record["observed"]["n_variants"], len(SIGNAL_POSITIONS) - 1)
        self.assertNotIn("replaced", archived_record)
        for step in ("build", "validate", "register"):
            self.assertTrue((rec_dir / f"{step}.json").is_file(), step)

    def test_validation_yaml_describes_the_completed_store(self) -> None:
        """The completed axis is source union panel: the held-out position adds a variant."""
        import yaml

        record = yaml.safe_load((self.stores_dir / self.STORE_ID / "validation.yaml").read_text(encoding="utf-8"))
        self.assertIn(record["status"], ("passed", "passed_with_warnings"))
        self.assertEqual(record["observed"]["n_variants"], len(SIGNAL_POSITIONS))
        self.assertEqual(record["replaced"]["archive"], str(self.archive(self.template_artifacts)))

    def test_a_rerun_executes_zero_jobs_in_place_and_from_a_copy(self) -> None:
        """In place, Snakemake reads the build's own metadata; a copy has none and decides by mtime."""
        res = self.rerun_result
        self.assertEqual(res.returncode, 0, f"In-place rerun failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("nothing to be done", res.stdout)
        for rec_p, mtime in self.records_before_rerun.items():
            self.assertEqual(rec_p.stat().st_mtime_ns, mtime, f"{rec_p.name} was rewritten in place")

        rec_dir = paths.records_dir(self.STORE_ID, root=self.artifact_root)
        before = {p: p.stat().st_mtime_ns for p in rec_dir.glob("*.json")}
        res = run_release([self.STORE_ID], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(res.returncode, 0, f"Copied rerun failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("nothing to be done", res.stdout)
        for rec_p, mtime in before.items():
            self.assertEqual(rec_p.stat().st_mtime_ns, mtime, f"{rec_p.name} was rewritten in the copy")


# `dev` before epic #302: the workflow as it was before #304 made a release
# complete its own Store. It stays reachable from `dev` and `main` however the
# epic is merged; CI fetches full history for this suite to have it.
PRE_304_REVISION: str = "ff204b6"

_PRE_304_COMPLETION: dict[str, Any] | None = None


def snakemake_metadata(output: Path) -> Path:
    """Where Snakemake keeps what it recorded about the job that wrote `output`.

    Snakemake's own layout (`snakemake/persistence/file.py`): the path,
    urlsafe-base64 encoded, split into chunks a file name can hold. Used only
    to show a fixture really kept an earlier run's metadata.
    """
    encoded = base64.urlsafe_b64encode(str(output).encode()).decode()
    max_len = os.pathconf(REPO_ROOT, "PC_NAME_MAX") - 1
    chunks = [encoded[i:i + max_len] for i in range(0, len(encoded), max_len)]
    return REPO_ROOT.joinpath(".snakemake", "metadata", *(["@" + c for c in chunks[:-1]] + chunks[-1:]))


def pre_304_completion_template() -> dict[str, Any]:
    """A Store built before #304, then completed by the current code, once per process (#305).

    The Observed-Only Store is built and published by the workflow as it was
    at `PRE_304_REVISION`. That revision runs from an archive of itself, with
    its own `ogstores` ahead of this checkout's editable install, and is
    repinned to the runtime this environment provides (below). The archive's
    `.snakemake` is this checkout's, so the current code later reads that
    run's Snakemake metadata, as an operator's checkout would after a pull.

    A copy of the published Observed-Only release is taken (`observed_stores`,
    `observed_artifacts`) for tests that may write. Then the bundle gains a
    `complete` block in place, the one amendment ADR 0037 allows, and the
    current entry point is dry-run and run on it once, with Snakemake's default
    rerun triggers. The build phase's files as they stood before that run are
    `build_phase_before`.
    """
    global _PRE_304_COMPLETION
    if _PRE_304_COMPLETION is not None:
        return _PRE_304_COMPLETION

    present = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{PRE_304_REVISION}^{{commit}}"], capture_output=True
    )
    if present.returncode != 0:
        raise RuntimeError(
            f"revision {PRE_304_REVISION} is not in this clone, so no Store can be built with the "
            "workflow as it was before #304. A shallow clone lacks it: fetch the full history "
            "(`git fetch --unshallow`; CI checks this suite out with `fetch-depth: 0`)."
        )

    store_id = "OGS-00120"
    temp_dir = tempfile.TemporaryDirectory()
    root = Path(temp_dir.name)
    checkout = root / "pre-304"
    checkout.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", PRE_304_REVISION], capture_output=True, check=True
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(checkout, filter="data")

    # The old guard refuses any runtime but the one it pinned. Repinning is
    # that code's own way to run another runtime ("repin the repository
    # deliberately", stores#218): here, to the runtime this environment
    # provides, which this checkout's guard proves. Nothing of this checkout's
    # guard changes.
    from ogstores import runtime

    runtime_py = checkout / "src" / "ogstores" / "runtime.py"
    pin = re.compile(r'^PINNED_OPENGWASDB_REVISION: str = "[0-9a-f]{40}"$', re.MULTILINE)
    text = runtime_py.read_text(encoding="utf-8")
    if len(pin.findall(text)) != 1:
        raise RuntimeError(f"{runtime_py} no longer states its pin as this test expects")
    runtime_py.write_text(
        pin.sub(f'PINNED_OPENGWASDB_REVISION: str = "{runtime.PINNED_OPENGWASDB_REVISION}"', text),
        encoding="utf-8",
    )

    shared_metadata = REPO_ROOT / ".snakemake"
    shared_metadata.mkdir(exist_ok=True)
    (checkout / ".snakemake").symlink_to(shared_metadata, target_is_directory=True)
    # The archived revision's code, not this checkout's: put it ahead of the
    # editable install for every process the old entry point starts.
    old_code = checkout / "src"
    old_env = {**os.environ, "PYTHONPATH": str(old_code)}
    imported = subprocess.run(
        [sys.executable, "-c", "import ogstores; print(ogstores.__file__)"],
        cwd=checkout, env=old_env, capture_output=True, text=True, check=True,
    ).stdout.strip()

    stores = root / "stores"
    stores.mkdir()
    artifacts = root / "artifacts"
    artifacts.mkdir()
    create_dense_fixture_store(
        stores,
        store_id=store_id,
        artifact_root=artifacts,
        vcf_path=create_signal_fixture_vcf(root / "signal.vcf.gz"),
        post_top_hits=False,
        post_overview=False,
    )
    old_result = subprocess.run(
        [
            sys.executable, str(checkout / "workflow" / "release.py"), store_id, "--cores", "1",
            "--config", f"registry_root={stores}", f"artifact_root={artifacts}",
        ],
        cwd=checkout, env=old_env, capture_output=True, text=True,
    )

    observed = root / "observed"
    shutil.copytree(stores, observed / "stores")
    shutil.copytree(artifacts, observed / "artifacts")
    store_dir = paths.store_dir(store_id, root=artifacts)
    build_phase_before = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for directory in ("records", "work") for p in sorted((store_dir / directory).iterdir())
    }

    amend_to_complete_own_store(stores, store_id, ld_panel=create_signal_ld_panel(root / "ld-panel"))
    dry_result = run_release([store_id, "--dry-run"], registry_root=stores, artifact_root=artifacts)
    release_result = run_release([store_id], registry_root=stores, artifact_root=artifacts)

    _PRE_304_COMPLETION = {
        "temp_dir": temp_dir,
        "store_id": store_id,
        "imported": imported,
        "old_code": old_code,
        "old_result": old_result,
        "observed_stores": observed / "stores",
        "observed_artifacts": observed / "artifacts",
        "stores": stores,
        "artifacts": artifacts,
        "build_phase_before": build_phase_before,
        "dry_result": dry_result,
        "release_result": release_result,
    }
    return _PRE_304_COMPLETION


class PublishedReleaseCopies(unittest.TestCase):
    """Copies of published releases for a test to change, and the check that a refused run wrote nothing.

    Each copy has a root of its own, so the records name another artifact
    root than the run's.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def published_copy(self, stores: Path, artifacts: Path) -> tuple[Path, Path]:
        copy_root = Path(tempfile.mkdtemp(dir=self.td))
        copied_stores, copied_artifacts = copy_root / "stores", copy_root / "artifacts"
        shutil.copytree(stores, copied_stores)
        shutil.copytree(artifacts, copied_artifacts)
        return copied_stores, copied_artifacts

    def assert_refused_before_any_write(
        self, stores: Path, artifacts: Path, store_id: str, *, release_only: bool = False
    ) -> str:
        """Run the entry point; it must refuse with nothing under either root written. Returns its stderr.

        `release_only` narrows that to the release's own files, as the #195
        refusal promises: it comes after the run has taken its release lock.
        """
        roots = (stores / store_id, paths.store_dir(store_id, root=artifacts)) if release_only else (stores, artifacts)

        def files() -> dict[str, tuple[int, bytes]]:
            found = [p for root in roots for p in root.rglob("*") if p.is_file()]
            return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in found}

        before = files()
        res = run_release([store_id], registry_root=stores, artifact_root=artifacts)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("Nothing was written", res.stderr)
        self.assertEqual(files(), before, "a refused run wrote under the registry or artifact root")
        return res.stderr


class TestWorkflowCompletingAStoreBuiltBefore304(PublishedReleaseCopies):
    """A Store built and published before #304 is completed later under its id, with no rebuild (#305).

    Its accepted `observed_only` bundle gains a `complete` block, the one
    amendment ADR 0037 allows, and an unforced `pixi run release <id>` then
    runs only the completion phase over the published Observed-Only Store.
    The Store is built by the workflow of `PRE_304_REVISION` itself, and the
    current code reads that run's Snakemake metadata, so this holds under
    Snakemake's default rerun triggers (code, params, input, mtime), not only
    when every file is untouched. A copy of the release, aged as after a fresh
    checkout, shows that a run rebuilding its build phase is still refused.
    """

    BUILD_PHASE_RECORDS = {"build.json", "validate.json", "register.json"}
    COMPLETION_RECORDS = ["complete", "completion-validate", "completion-register"]

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = pre_304_completion_template()
        cls.STORE_ID: str = cls.template["store_id"]
        cls.artifacts: Path = cls.template["artifacts"]

    def observed_only_copy(self) -> tuple[Path, Path, str]:
        """A copy of the Observed-Only release `PRE_304_REVISION` published, its bundle unamended."""
        stores, artifacts = self.published_copy(self.template["observed_stores"], self.template["observed_artifacts"])
        return stores, artifacts, self.STORE_ID

    def test_the_observed_only_store_was_built_by_the_old_code(self) -> None:
        old = self.template["old_result"]
        self.assertEqual(old.returncode, 0, f"the pre-#304 build failed:\n{old.stdout}\n{old.stderr}")
        self.assertTrue(
            self.template["imported"].startswith(str(self.template["old_code"])),
            f"the old entry point imported {self.template['imported']}",
        )
        built = {p.name for p in self.template["build_phase_before"] if p.parent.name == "records"}
        self.assertEqual(built, self.BUILD_PHASE_RECORDS)
        # Settlement clears the register job's metadata (`--cleanup-metadata`),
        # in both revisions, so only the step jobs' is kept.
        for name in self.BUILD_PHASE_RECORDS - {"register.json"}:
            record_p = paths.records_dir(self.STORE_ID, root=self.artifacts) / name
            self.assertTrue(snakemake_metadata(record_p).is_file(), f"no Snakemake metadata kept for {name}")

    def test_the_amended_bundle_schedules_only_the_completion_phase(self) -> None:
        dry = self.template["dry_result"]
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        records = [t for t in scheduled_targets(dry.stdout) if t != self.STORE_ID]
        self.assertEqual(records, self.COMPLETION_RECORDS)
        self.assertNotIn("would refuse", dry.stdout)

    def test_an_unforced_run_completes_the_store_and_rebuilds_nothing(self) -> None:
        res = self.template["release_result"]
        self.assertEqual(res.returncode, 0, f"the completion failed:\n{res.stdout}\n{res.stderr}")
        for p, (mtime, content) in self.template["build_phase_before"].items():
            self.assertEqual((p.stat().st_mtime_ns, p.read_bytes()), (mtime, content), f"{p} was rewritten")
        rec_dir = paths.records_dir(self.STORE_ID, root=self.artifacts)
        for step in self.COMPLETION_RECORDS:
            self.assertTrue(json.loads((rec_dir / f"{step}.json").read_text(encoding="utf-8"))["success"], step)
        store_p = paths.store_path(self.STORE_ID, root=self.artifacts)
        manifest_doc = json.loads((store_p / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest_doc["completion_state"], "reference_completed")
        archives = list((paths.store_dir(self.STORE_ID, root=self.artifacts) / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)
        replaced = json.loads((archives[0] / "store.opengwasdb" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(replaced["completion_state"], "observed_only")

    def test_an_unforced_run_rebuilding_its_build_phase_is_still_refused_unchanged(self) -> None:
        """Only completion-phase jobs are let through: a fresh checkout reschedules the build phase too (#195)."""
        stores, artifacts, store_id = self.observed_only_copy()
        amend_to_complete_own_store(stores, store_id, ld_panel=self.td / "ld-panel")
        day_ns = 86_400 * 10**9
        for p in paths.store_dir(store_id, root=artifacts).rglob("*"):
            if p.is_file():
                st = p.stat()
                os.utime(p, ns=(st.st_atime_ns - day_ns, st.st_mtime_ns - day_ns))
        dry = run_release([store_id, "--dry-run"], registry_root=stores, artifact_root=artifacts)
        self.assertIn("build", scheduled_targets(dry.stdout), "the fixture must reschedule the build phase")
        self.assertIn("complete", scheduled_targets(dry.stdout))

        refusal = self.assert_refused_before_any_write(stores, artifacts, store_id, release_only=True)

        self.assertEqual(
            refusal,
            "release: Refusing to run: these releases are already published, and this run would rebuild "
            f"them, rewriting their work/ and records/:\n  {store_id}: {paths.store_path(store_id, root=artifacts)}\n"
            "Nothing was written. To replace one, name its id as a target and add `--config force=1`; the "
            "replaced Store, its records and its validation.yaml are archived under replaced/<UTC>/ beside it "
            "(#195).\n",
        )


class TestWorkflowForcedRunOfACompletedSingleIdRelease(unittest.TestCase):
    """`--config force=1` on a completed single-id release rebuilds both phases (#305, ADR 0037).

    Re-completing against a new reference panel is a forced run: the build
    phase rebuilds the Observed-Only Store and replaces the completed one, as
    #195 replaces any release, and the completion phase then completes the new
    Store and replaces it in turn. The completion's records are rebuilt with
    it, never left over from the completion they described.
    """

    def setUp(self) -> None:
        template = single_id_completion_template()
        self.store_id: str = template["store_id"]
        self.temp_dir = tempfile.TemporaryDirectory()
        td = Path(self.temp_dir.name)
        self.stores, self.artifacts = td / "stores", td / "artifacts"
        shutil.copytree(template["stores"], self.stores)
        shutil.copytree(template["artifacts"], self.artifacts)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_a_forced_run_rebuilds_both_phases_and_replaces_the_store(self) -> None:
        store_dir = paths.store_dir(self.store_id, root=self.artifacts)
        # A fresh checkout: every bundle file newer than the release's files.
        day_ns = 86_400 * 10**9
        for p in store_dir.rglob("*"):
            if p.is_file():
                st = p.stat()
                os.utime(p, ns=(st.st_atime_ns - day_ns, st.st_mtime_ns - day_ns))
        records = paths.records_dir(self.store_id, root=self.artifacts)
        old_records = {p.name: p.read_bytes() for p in records.iterdir()}
        old_manifest = (paths.store_path(self.store_id, root=self.artifacts) / "manifest.json").read_bytes()
        old_archives = {p.name for p in (store_dir / "replaced").iterdir()}

        res = run_release(
            [self.store_id], registry_root=self.stores, artifact_root=self.artifacts, config={"force": "1"}
        )

        self.assertEqual(res.returncode, 0, f"forced run failed:\n{res.stdout}\n{res.stderr}")
        for name in TestWorkflowSingleIdReferenceCompletion.PHASE_RECORDS:
            record = json.loads((records / f"{name}.json").read_text(encoding="utf-8"))
            self.assertTrue(record["success"], name)
            self.assertNotEqual((records / f"{name}.json").read_bytes(), old_records[f"{name}.json"], f"{name} was not rebuilt")
        store_p = paths.store_path(self.store_id, root=self.artifacts)
        self.assertEqual(json.loads((store_p / "manifest.json").read_text())["completion_state"], "reference_completed")
        self.assertNotEqual((store_p / "manifest.json").read_bytes(), old_manifest, "a new completed Store is published")

        # The build phase archived the completed Store with the records that
        # described it; the completion phase archived the Observed-Only Store it replaced.
        new_archives = sorted(p for p in (store_dir / "replaced").iterdir() if p.name not in old_archives)
        self.assertEqual(len(new_archives), 2, new_archives)
        forced, completed = new_archives
        self.assertEqual((forced / "store.opengwasdb" / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual({p.name: p.read_bytes() for p in (forced / "records").iterdir()}, old_records)
        observed_only = json.loads((completed / "store.opengwasdb" / "manifest.json").read_text())
        self.assertEqual(observed_only["completion_state"], "observed_only")
        self.assertEqual(
            json.loads((records / "completion-register.json").read_text())["replaced_archive"], str(completed)
        )
        for leftover in (
            paths.partial_store_path(self.store_id, root=self.artifacts),
            paths.publication_marker(self.store_id, root=self.artifacts),
            paths.completion_checkpoint_path(self.store_id, root=self.artifacts),
        ):
            self.assertFalse(leftover.exists(), leftover)
        self.assertEqual(run.pending_force_snapshots(self.store_id, self.artifacts), [])


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

    def test_several_store_release_ids_in_one_invocation_schedule_every_step(self) -> None:
        """Several Store Release IDs in one snakemake invocation schedule the full chain for each.

        Asserted as a dry run. That one release really builds is the smoke test
        in `TestWorkflowEndToEndAndResumption`; what is specific to several
        targets in one invocation is that each gets its own complete step chain
        scheduled, which the DAG answers without building.
        """
        s1 = "OGS-00061"
        s2 = "OGS-00062"
        create_dense_fixture_store(self.stores_dir, store_id=s1, artifact_root=self.artifact_root)
        create_dense_fixture_store(self.stores_dir, store_id=s2, artifact_root=self.artifact_root)

        res = run_snakemake(
            [s1, s2],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res.returncode, 0, f"Multi-target dry run failed:\n{res.stderr}")

        scheduled = scheduled_targets(res.stdout)
        for sid in (s1, s2):
            self.assertIn(sid, scheduled, f"target alias for {sid} was not scheduled")
        for step in ("build", "register"):
            self.assertEqual(
                scheduled.count(step),
                2,
                f"expected {step!r} scheduled once per release, got {scheduled.count(step)}: {scheduled}",
            )

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
            "status": "accepted",
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
            "status": "accepted",
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



class TestWorkflowVariantReferenceDAG(unittest.TestCase):
    """The optional variant-reference pre-stage is sequenced before the build (#145/#147)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.artifact_root = self.td / "artifacts"
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_declared_pre_stage_is_sequenced_before_build(self) -> None:
        """Dry-run DAG orders manifest derivation, extraction, build, validate, register."""
        store_id = "OGS-00060"
        ref = str(self.artifact_root / store_id / "work" / "variant-ref.tsv.gz")
        create_dense_fixture_store(
            self.stores_dir,
            store_id=store_id,
            artifact_root=self.artifact_root,
            variant_reference_output=ref,
            variant_reference_options={"n-workers": 2},
        )

        res = run_snakemake(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res.returncode, 0, f"Dry run failed:\n{res.stderr}")

        scheduled = scheduled_targets(res.stdout)
        for step in ("variant-reference", "build", "validate", "register"):
            self.assertIn(step, scheduled, f"{step!r} was not scheduled: {scheduled}")
        self.assertLess(
            scheduled.index("variant-reference"),
            scheduled.index("build"),
            f"extraction must precede the build: {scheduled}",
        )
        self.assertLess(scheduled.index("build"), scheduled.index("validate"))
        self.assertLess(scheduled.index("validate"), scheduled.index("register"))
        if "analyses.exclusions" in scheduled:
            self.assertLess(
                scheduled.index("analyses.exclusions"),
                scheduled.index("variant-reference"),
                f"manifest derivation must precede extraction: {scheduled}",
            )

    def test_undeclared_recipe_plans_no_variant_reference_step(self) -> None:
        """A recipe without a declaration schedules no extraction job."""
        store_id = "OGS-00061"
        create_dense_fixture_store(
            self.stores_dir,
            store_id=store_id,
            artifact_root=self.artifact_root,
        )
        res = run_snakemake(
            [store_id],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            dry_run=True,
        )
        self.assertEqual(res.returncode, 0, f"Dry run failed:\n{res.stderr}")
        self.assertNotIn("variant-reference", scheduled_targets(res.stdout))


class TestPixiReleaseTaskPassthrough(unittest.TestCase):
    """`pixi run release <ID> --config key=value` reaches Snakemake's config (#195).

    Pixi appends a task's extra arguments to its `cmd`, so each task is run
    here as its `pixi.toml` cmd plus the operator's words, in the current
    environment. A bare trailing `--` in the cmd made Snakemake read
    `--config` and every `key=value` as targets.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.artifact_root = self.td / "artifacts"
        self.artifact_root.mkdir()
        with open(REPO_ROOT / "pixi.toml", "rb") as f:
            self.tasks = tomllib.load(f)["feature"]["workflow"]["tasks"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_task(self, task: str, *operator_args: str) -> subprocess.CompletedProcess[str]:
        argv = shlex.split(self.tasks[task]["cmd"]) + list(operator_args)
        return subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True)

    def test_release_tasks_pass_config_after_the_target(self) -> None:
        """Both tasks accept `<ID> --config ...`; the dry run plans under the configured root."""
        store_id = "OGS-00099"
        create_dense_fixture_store(self.stores_dir, store_id=store_id, artifact_root=self.artifact_root)
        config = ["--config", f"registry_root={self.stores_dir}", f"artifact_root={self.artifact_root}"]

        for task, extra in (("release-dry", []), ("release", ["--dry-run"])):
            with self.subTest(task=task):
                res = self.run_task(task, store_id, *extra, *config)
                self.assertEqual(res.returncode, 0, f"{task} rejected its config:\n{res.stdout}\n{res.stderr}")
                self.assertIn(
                    str(paths.record_path(store_id, "register", root=self.artifact_root)),
                    res.stdout,
                    f"{task} did not plan under the configured artifact root",
                )
                self.assertFalse(
                    paths.records_dir(store_id, root=self.artifact_root).exists(),
                    f"{task} wrote records during a dry run",
                )


class TestReleaseWorkflowRefusesUnbuildableBundles(unittest.TestCase):
    """The release workflow schedules only bundles whose Release Status allows a build (#247).

    None of these runs builds a Store: each is refused, or is a dry run.
    """

    ACCEPTED = "OGS-00071"
    CANDIDATE = "OGS-00072"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.artifact_root = self.td / "artifacts"
        self.artifact_root.mkdir()
        create_dense_fixture_store(self.stores_dir, store_id=self.ACCEPTED, artifact_root=self.artifact_root)
        create_dense_fixture_store(
            self.stores_dir, store_id=self.CANDIDATE, artifact_root=self.artifact_root, status="candidate"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def written(self) -> list[Path]:
        return sorted(self.artifact_root.rglob("*"))

    def test_direct_snakemake_refuses_a_candidate_at_onstart(self) -> None:
        res = run_snakemake([self.CANDIDATE], registry_root=self.stores_dir, artifact_root=self.artifact_root)

        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        output = res.stdout + res.stderr
        self.assertIn(f"{self.CANDIDATE} has Release Status 'candidate'", output)
        self.assertIn("status: accepted", output)
        self.assertEqual(self.written(), [], "a refused run wrote under the artifact root")

    def test_direct_snakemake_all_expands_to_buildable_bundles_only(self) -> None:
        res = run_snakemake(["all"], registry_root=self.stores_dir, artifact_root=self.artifact_root, dry_run=True)

        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(self.ACCEPTED, res.stdout)
        self.assertNotIn(self.CANDIDATE, res.stdout)

    def test_the_entry_point_refuses_a_named_candidate_before_anything_is_written(self) -> None:
        res = run_release([self.CANDIDATE], registry_root=self.stores_dir, artifact_root=self.artifact_root)

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn(f"{self.CANDIDATE} has Release Status 'candidate'", res.stderr)
        self.assertIn("status: accepted", res.stderr)
        self.assertEqual(self.written(), [], "a refused run wrote under the artifact root")

    def test_the_entry_point_still_reports_an_unregistered_id_as_unknown(self) -> None:
        """An id with no bundle has no Release Status to refuse; the registry's guidance stands."""
        res = run_release(["OGS-00042"], registry_root=self.stores_dir, artifact_root=self.artifact_root)

        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        output = res.stdout + res.stderr
        self.assertIn("Unknown Store Release ID 'OGS-00042'", output)
        self.assertNotIn("Release Status", output)

    def test_the_entry_point_refuses_the_candidate_parent_an_accepted_child_would_build(self) -> None:
        """Naming an accepted child schedules its unbuilt parent, so the parent's status is checked too."""
        child = "OGS-00073"
        child_dir = self.stores_dir / child
        child_dir.mkdir()
        import yaml
        (child_dir / "release.yaml").write_text(yaml.safe_dump({
            "store_id": child,
            "label": "completed-child",
            "status": "accepted",
            "derived_from": self.CANDIDATE,
            "created_at": "2026-08-18T16:00:00Z",
            "description": "Completed child of a candidate",
            "source_snapshot_id": "test-snapshot",
            "generator": {"command": "test-gen"},
        }), encoding="utf-8")
        (child_dir / "build.yaml").write_text(yaml.safe_dump({
            "store_id": child,
            "layout": "dense",
            "completion_state": "reference_completed",
            "complete": {"command": "complete-dense", "options": {"ld-panel": "/fake/ld/panel", "ancestry": "EUR"}},
            "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
        }), encoding="utf-8")
        (child_dir / "analyses.tsv").write_text("analysis_id\tsource_file\n", encoding="utf-8")

        res = run_release([child], registry_root=self.stores_dir, artifact_root=self.artifact_root)

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn(f"{self.CANDIDATE} has Release Status 'candidate'", res.stderr)
        self.assertNotIn(f"{child} has Release Status", res.stderr)
        # The run's lock files are its only trace: no record, manifest or Store.
        self.assertEqual([p for p in self.written() if p.is_file() and ".release-locks" not in p.parts], [])

    def test_the_dry_run_of_all_reports_left_out_bundles_and_why(self) -> None:
        res = run_release(["all", "--dry-run"], registry_root=self.stores_dir, artifact_root=self.artifact_root)

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(f"wildcards: root={self.artifact_root}, store_id={self.ACCEPTED}", res.stdout)
        self.assertNotIn(f"store_id={self.CANDIDATE}", res.stdout)
        self.assertIn(f"release-dry: left out of `all`: {self.CANDIDATE} has Release Status 'candidate'", res.stdout)
        self.assertNotIn(f"left out of `all`: {self.ACCEPTED}", res.stdout)
        self.assertEqual(self.written(), [])

    def test_the_dry_run_of_a_named_candidate_says_the_real_run_would_refuse(self) -> None:
        res = run_release([self.CANDIDATE, "--dry-run"], registry_root=self.stores_dir, artifact_root=self.artifact_root)

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("release-dry: the real run would refuse", res.stdout)
        self.assertIn(f"{self.CANDIDATE} has Release Status 'candidate'", res.stdout)


class TestWorkflowOperatorInterface(unittest.TestCase):
    """Operator interface: rule all, store_id target, and index target (Issue #116)."""

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
