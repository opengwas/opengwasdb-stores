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
13. The Observed-View Check gates that swap (#306): a staged completed Store
   that fails it is refused, `completion-register` never runs, and the
   published Observed-Only Store is byte-for-byte untouched; a passing check
   leaves its verdict as the step's record.
14. Completing a published Observed-Only Store later (#305, ADR 0037): a
   Store built by the workflow as it was before #304 completes, once its
   bundle gains a `complete` block, with no build-phase job scheduled; a
   bundle that completes its own Store and no longer plans the Store it
   published is refused before any job runs, naming it, while post steps and
   observed-only or two-id releases are not checked; an unforced rebuild of an
   amended release's build phase is still refused; and a forced run of a
   completed single-id release rebuilds both phases.
15. Moving a published release to another Artifact Root (#331):
   `tools/relocate_store.py` leaves the release up to date at the new root,
   with only its records' path fields moved and their mtimes kept, and its next
   register passes; the same release moved by a plain rename is refused at
   that register, as drift naming the old root.
16. A Hybrid release completes its own Store too (#333): one entry-point run
   builds and publishes the Observed-Only Hybrid Store, completes it, moving an
   Overflow association into the Dense Component, passes the Observed-View
   Check across both components and publishes the completed Store at its path,
   its HM3 Indexed Variant Subset and overview built again on it;
   a perturbed crossover fails the check, naming it, and the Observed-Only Store
   stays published; and a published Observed-Only Hybrid release whose bundle
   gains a `complete-hybrid` block runs only its completion phase.
17. A Ragged release completes its own Store too (#336): one entry-point run
   builds and publishes the Observed-Only Ragged Store from a GWAS-SSF source,
   completes it, passes the Ragged Observed-View Check, given the LD panel
   and ancestry the bundle completes against, and publishes the completed
   Store at its path; a perturbed observed Z fails the check, naming it, and
   the Observed-Only Store stays published; a published Observed-Only Ragged
   release whose bundle gains a `complete-ragged` block runs only its
   completion phase; and a forced run rebuilds and completes it in one run.
18. Association Coverage is a build input (#341): a Ragged release is built and
    published at the coverage its `release.yaml` declares, which `plan()` puts
    into the build argv, `opengwasdb info` reports back from the Store, and the
    Validation Record records; a Ragged Store published before the flag existed
    stays up to date under the current workflow, with default rerun triggers,
    because no trigger reads an argv token; and that Store may still gain its
    own completion, the amendment check tolerating the declaration-derived flag
    alone (ADR 0040) and refusing a record that names another coverage.
    Each scenario is one test method, so a fixture is built once however the
    suite is sharded.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import importlib.resources
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import mock
from typing import Any

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

from ogstores import (
    amendment,
    bundle,
    manifest,
    paths,
    register,
    release,
    resources,
    run,
)
from ogstores.plan import plan

# The flag the planner adds to a Ragged build for its declared Association
# Coverage (#341), which the pre-#341 amendment scenario reads.
import ogstores.plan as plan_module

# The completed child is asserted on through opengwasdb's published query
# facade, never on its internal arrays (#303).
from opengwasdb.query import query_store
# The counters whose nonzero count fails the Observed-View Check (#306).
from opengwasdb.validation.observed_view import FAILING_COUNTERS

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


def create_signal_ld_panel(panel_root: Path, *, indexes: Sequence[int] | None = None) -> Path:
    """Write the fixture LD reference panel over `SIGNAL_POSITIONS` (#303).

    One block in the flat layout opengwasdb's completion reads
    (`<panel>/<ancestry>/<chrom>/<block>.tsv` plus `<block>.ldeig.npz`), with
    AR(1) correlation `0.9 ** |i - j|`: its leading eigenvectors are smooth
    basis functions, which is what lets an elastic net recover the held-out
    position at all. The eigendecomposition is written in descending
    eigenvalue order, as the panel contract requires, and no LD matrix is
    written -- an eigendecomposition-only panel is the production shape
    (opengwasdb ADR 0031).

    `indexes` narrows the block to those positions of `SIGNAL_POSITIONS`
    (#333); by default it covers all twelve.

    Copy of the recipe in opengwasdb's `tests/test_dense_completion.py`
    (`write_signal_panel`); those tests are not packaged, so it is copied rather
    than imported.
    """
    import numpy as np

    covered = [
        (position, alid)
        for i, (position, alid) in enumerate(zip(SIGNAL_POSITIONS, SIGNAL_ALID, strict=True))
        if indexes is None or i in indexes
    ]
    block_dir = panel_root / "EUR" / "1"
    block_dir.mkdir(parents=True, exist_ok=True)
    block = f"{covered[0][0]}-{covered[-1][0]}"

    tsv_lines = ["CHR\tSNP\tOA\tEA\tEAF\tBP"]
    for position, alid in covered:
        chrom, _pos, a1, a2 = alid.split(":")
        tsv_lines.append(f"{chrom}\t{alid}\t{a2}\t{a1}\t0.3\t{position}")
    (block_dir / f"{block}.tsv").write_text("\n".join(tsv_lines) + "\n", encoding="utf-8")

    index = np.arange(len(covered))
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
        "association_coverage": "gwas",
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


def revise_registry_analyses(stores_dir: Path, store_id: str) -> None:
    """Change a trait's name in the bundle's analyses.tsv: the content a rebuild is scheduled for (#339).

    A bundle file that is merely newer than the release's records no longer
    schedules a rebuild, so a test of a forced or refused rebuild changes
    what the bundle says, as a revised release does.
    """
    analyses_p = stores_dir / store_id / "analyses.tsv"
    lines = analyses_p.read_text(encoding="utf-8").splitlines()
    column = lines[0].split("\t").index("trait_name")
    row = lines[1].split("\t")
    row[column] += " (revised)"
    lines[1] = "\t".join(row)
    analyses_p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def amend_to_complete_own_store(
    stores_dir: Path, store_id: str, *, ld_panel: Path, command: str = "complete-dense"
) -> Path:
    """Give an Observed-Only fixture bundle a `complete` block: one id, published twice (ADR 0037).

    The bundle keeps its `build` block and `post` and gains what a single-id
    Reference-Completed release adds: `completion_state: reference_completed`
    and a `complete` block, with no `derived_from`. That is also the one
    amendment ADR 0037 allows an accepted bundle, so the same helper serves
    the later completion of a Store built earlier (#305). The `complete`
    options are `create_completed_child_bundle`'s; `command` is the bundle's
    layout's completion command (#333).
    """
    import yaml

    build_yaml_p = stores_dir / store_id / "build.yaml"
    build_yaml = yaml.safe_load(build_yaml_p.read_text(encoding="utf-8"))
    build_yaml["completion_state"] = "reference_completed"
    build_yaml["complete"] = {
        "command": command,
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
        "association_coverage": "gwas",
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

    return subprocess.run(cmd, cwd=release.snakemake_directory(), capture_output=True, text=True)


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


def declare_variant_list_resource(stores_dir: Path, resource_id: str, variant_list: Path) -> Path:
    """Maintain `variant_list` as a Reference Resource of the repository that holds `stores_dir` (#333).

    The entry point builds an Indexed Variant Subset only from a variant list
    that a maintained resource of the registry's repository declares:
    `resources/reference-resources/<resource_id>/resource.yaml` beside the
    registry. A fixture registry's repository is its parent directory, so the
    declaration goes there. Returns its path.
    """
    import hashlib

    import yaml

    content = variant_list.read_bytes()
    declaration = {
        "resource_id": resource_id,
        "kind": "variant_list",
        "genome_build": "GRCh38",
        "location": str(variant_list),
        "row_count": sum(1 for line in content.decode("utf-8").splitlines() if line.strip()),
        "checksums": {"alid_sha256": hashlib.sha256(content).hexdigest(), "alid_bytes": len(content)},
    }
    declaration_p = stores_dir.parent / "resources" / "reference-resources" / resource_id / "resource.yaml"
    declaration_p.parent.mkdir(parents=True)
    declaration_p.write_text(yaml.safe_dump(declaration), encoding="utf-8")
    return declaration_p


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
            # A carried Column Map, staged for the build (#292): a file too.
            '"{root}/{store_id}/work/column_maps/{file}"',
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
        for root_dir in [REPO_ROOT / "workflow", REPO_ROOT / "tools"]:
            if not root_dir.is_dir():
                continue
            for file_path in root_dir.glob("*"):
                self.assertNotIn(
                    file_path.name,
                    forbidden_patterns,
                    f"Forbidden batch runner script found: {file_path}",
                )


_DENSE_RELEASE: dict[str, Any] | None = None


def dense_release_template() -> dict[str, Any]:
    """The one fixture-scale Dense release this process builds, with a single snakemake target.

    Returns `store_id`, the built `stores` and `artifacts` directories, the
    snakemake `build_result`, and the `TemporaryDirectory` that holds them
    (kept here so the template outlives every test that copies it). A test
    that changes the release copies it first; the template is never written
    again. The end-to-end tests and the relocation tests (#331) share it, so a
    process that runs both builds it once.
    """
    global _DENSE_RELEASE
    if _DENSE_RELEASE is not None:
        return _DENSE_RELEASE

    store_id = "OGS-00099"
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
        post_top_hits=True,
        post_overview=True,
        post_validate=True,
    )
    build_result = run_snakemake([store_id], registry_root=stores, artifact_root=artifacts)

    _DENSE_RELEASE = {
        "temp_dir": temp_dir,
        "stores": stores,
        "artifacts": artifacts,
        "build_result": build_result,
        "store_id": store_id,
    }
    return _DENSE_RELEASE


class TestWorkflowEndToEndAndResumption(unittest.TestCase):
    """End-to-end execution, idempotency, and resumption over one fixture-scale Dense build.

    The workflow specification budgets *one* fixture-scale end-to-end run
    ("Fixture-scale end-to-end runs stay, as one smoke test"). That run happens
    once per process (`dense_release_template`), and the tests that need a
    completed Store copy its result rather than rebuilding. Rebuilding per test
    cost 68s to assert three different things about the same built Store.

    `test_interrupted_step_resumes_without_restarting` deliberately keeps its
    own partial build: it needs a Store interrupted mid-way, and deriving that
    by deleting records from a completed build would not exercise the same state.
    """

    STORE_ID = "OGS-00099"

    @classmethod
    def setUpClass(cls) -> None:
        """Take the one fixture-scale Store this process builds."""
        template = dense_release_template()
        cls.template_stores = template["stores"]
        cls.template_artifacts = template["artifacts"]
        cls.build_result = template["build_result"]

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

        A bundle revised after its Store was published (or one whose records
        are older than its inputs) makes Snakemake schedule the whole chain
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
        # order, so the bundle is newer than its records, and revise the bundle
        # as a changed release would: a newer file alone schedules nothing (#339).
        revise_registry_analyses(self.stores_dir, store_id)
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
        """Make the bundle newer than the published release's files and revise its analyses.tsv.

        A newer bundle alone is a fresh checkout, which schedules nothing (#339).
        """
        revise_registry_analyses(self.stores_dir, store_id)
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

    def rewrite_registry_analyses(self, store_id: str, *, extra_row: bool = False) -> Path:
        """Write the bundle's analyses.tsv again, as a fresh checkout does: a newer mtime, the same bytes unless `extra_row`."""
        analyses_p = self.stores_dir / store_id / "analyses.tsv"
        text = analyses_p.read_text(encoding="utf-8")
        if extra_row:
            first_row = text.splitlines()[1].split("\t")
            first_row[0] = "extra-analysis"
            text += "\t".join(first_row) + "\n"
        later = time.time_ns()
        analyses_p.write_text(text, encoding="utf-8")
        os.utime(analyses_p, ns=(later, later))
        return analyses_p

    def test_a_registry_file_rewritten_with_the_same_content_schedules_nothing(self) -> None:
        """A newer analyses.tsv with the content the work/ copy came from is no change (#339)."""
        store_id = self.STORE_ID
        self.use_completed_build()
        self.rewrite_registry_analyses(store_id)
        records_dir = paths.records_dir(store_id, root=self.artifact_root)
        before = self.records_of(records_dir)
        work_before = self.records_of(paths.store_dir(store_id, root=self.artifact_root) / "work")

        dry = run_release([store_id, "--dry-run"], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(scheduled_targets(dry.stdout), [], dry.stdout)

        res = run_release([store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.records_of(records_dir), before)
        self.assertEqual(
            self.records_of(paths.store_dir(store_id, root=self.artifact_root) / "work"), work_before
        )

    def assert_scheduled_and_refused(self, store_id: str) -> None:
        dry = run_release([store_id, "--dry-run"], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        scheduled = scheduled_targets(dry.stdout)
        self.assertIn("analyses.exclusions", scheduled, dry.stdout)
        self.assertIn("build", scheduled, dry.stdout)
        self.assertIn("would refuse", dry.stdout)
        self.assertIn("already published", dry.stdout)
        res = run_release([store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("already published", res.stderr)
        self.assertIn("Nothing was written", res.stderr)

    def test_changed_content_with_an_older_mtime_still_schedules_the_build_and_is_refused(self) -> None:
        """A content mismatch invalidates the job whatever the mtimes say (#339)."""
        self.use_completed_build()
        revise_registry_analyses(self.stores_dir, self.STORE_ID)
        os.utime(self.stores_dir / self.STORE_ID / "analyses.tsv", ns=(10**9, 10**9))
        self.assert_scheduled_and_refused(self.STORE_ID)

    def test_a_corrupt_sidecar_with_older_mtimes_schedules_the_build_and_is_refused(self) -> None:
        self.use_completed_build()
        sidecar = paths.build_manifest_sidecar_path(self.STORE_ID, root=self.artifact_root)
        written = sidecar.stat()
        sidecar.write_text("{not json", encoding="utf-8")
        os.utime(sidecar, ns=(written.st_atime_ns, written.st_mtime_ns))
        self.assert_scheduled_and_refused(self.STORE_ID)

    def test_identical_content_schedules_nothing_whatever_the_mtimes(self) -> None:
        self.use_completed_build()
        analyses_p = self.stores_dir / self.STORE_ID / "analyses.tsv"
        for ns in (10**9, 4 * 10**18):
            os.utime(analyses_p, ns=(ns, ns))
            dry = run_release([self.STORE_ID, "--dry-run"], registry_root=self.stores_dir, artifact_root=self.artifact_root)
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            self.assertEqual(scheduled_targets(dry.stdout), [], dry.stdout)

    def test_a_registry_file_with_changed_content_schedules_the_build_and_is_refused(self) -> None:
        self.use_completed_build()
        self.rewrite_registry_analyses(self.STORE_ID, extra_row=True)
        self.assert_scheduled_and_refused(self.STORE_ID)

    def test_a_forced_unregistered_id_is_left_to_the_snakefiles_unknown_id_guidance(self) -> None:
        self.use_completed_build()
        dry = run_release(
            ["OGS-99999", "--dry-run"],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            config={"force": "1"},
        )
        self.assertNotIn("Traceback", dry.stderr)
        self.assertIn("Unknown Store Release ID 'OGS-99999'", dry.stdout + dry.stderr)

    def assert_forced_run_rebuilds(self, store_id: str) -> None:
        """Dry run and real run of `force=1` both rebuild the published release and archive its Store (#339)."""
        build_record = paths.record_path(store_id, "build", root=self.artifact_root)
        old_start = json.loads(build_record.read_text(encoding="utf-8"))["start_time"]

        dry = run_release(
            [store_id, "--dry-run"],
            registry_root=self.stores_dir,
            artifact_root=self.artifact_root,
            config={"force": "1"},
        )
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(
            scheduled_targets(dry.stdout),
            ["analyses.exclusions", "build", "top-hits", "overview", "validate", "register", store_id],
            dry.stdout,
        )
        self.assertIn("would snapshot the records", dry.stdout)

        res = run_release(
            [store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root, config={"force": "1"}
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotEqual(json.loads(build_record.read_text(encoding="utf-8"))["start_time"], old_start)
        archives = list((paths.store_dir(store_id, root=self.artifact_root) / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)

    def test_a_forced_run_rebuilds_an_unchanged_release_from_the_same_checkout(self) -> None:
        self.use_completed_build()
        self.assert_forced_run_rebuilds(self.STORE_ID)

    def test_a_forced_run_rebuilds_an_unchanged_release_from_a_fresh_registry_copy(self) -> None:
        self.use_completed_build()
        self.rewrite_registry_analyses(self.STORE_ID)
        self.assert_forced_run_rebuilds(self.STORE_ID)

    def test_a_forced_run_rebuilds_the_store_from_a_changed_registry_file(self) -> None:
        store_id = self.STORE_ID
        self.use_completed_build()
        analyses_p = self.stores_dir / store_id / "analyses.tsv"
        analyses_p.write_text(analyses_p.read_text(encoding="utf-8").replace("Test Trait", "Renamed Trait"), encoding="utf-8")

        res = run_release(
            [store_id], registry_root=self.stores_dir, artifact_root=self.artifact_root, config={"force": "1"}
        )

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        built_from = paths.build_manifest_path(store_id, root=self.artifact_root).read_text(encoding="utf-8")
        self.assertIn("Renamed Trait", built_from)
        archives = list((paths.store_dir(store_id, root=self.artifact_root) / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)

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


RELOCATE_STORE: Path = REPO_ROOT / "tools" / "relocate_store.py"

# The record fields that name where a step ran, which a relocation moves (#331).
RECORD_PATH_FIELDS: tuple[str, ...] = ("argv", "planned_argv", "inputs", "outputs", "record_path", "published_store")


def relocate_store(store_id: str, old_root: Path, new_root: Path, *words: str) -> subprocess.CompletedProcess[str]:
    """Run `tools/relocate_store.py` as an operator would."""
    return subprocess.run(
        [sys.executable, str(RELOCATE_STORE), store_id, "--from", str(old_root), "--to", str(new_root), *words],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )


def tree_state(root: Path) -> dict[str, tuple[int, bytes] | None]:
    """Every path under `root`: a file's mtime and bytes, None for a directory."""
    return {
        str(p.relative_to(root)): None if p.is_dir() else (p.lstat().st_mtime_ns, p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


def strings_under(value: Any, directory: Path) -> int:
    """How many strings in `value` are `directory` or a path under it."""
    if isinstance(value, str):
        return int(value == str(directory) or value.startswith(f"{directory}/"))
    if isinstance(value, list):
        return sum(strings_under(item, directory) for item in value)
    return 0


class TestWorkflowEndToEndRelocation(unittest.TestCase):
    """A published release moved to another Artifact Root by `tools/relocate_store.py` (#331).

    Each test takes the end-to-end tests' published release (`dense_release_template`)
    into a root of its own, its records naming that root as if it had been
    built there (`copy_snapshot`), and moves it to a second root. The tool's
    other refusals, and an interrupted move resumed, are `tests/relocate-store`'s,
    on synthetic releases.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = dense_release_template()
        cls.store_id = cls.template["store_id"]

    def setUp(self) -> None:
        built = self.template["build_result"]
        self.assertEqual(built.returncode, 0, f"the template release failed:\n{built.stdout}\n{built.stderr}")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def take_release(self) -> None:
        """The template release as if built in this test's directory, with an empty second root beside it."""
        self.stores, self.old_root, self.new_root = self.release_built_in(self.td)
        self.old_dir = paths.store_dir(self.store_id, root=self.old_root)
        self.new_dir = paths.store_dir(self.store_id, root=self.new_root)

    def release_built_in(self, into: Path) -> tuple[Path, Path, Path]:
        """The template release as if built under `into`: its registry, its root, and an empty second root."""
        stores, old_root = copy_snapshot(self.template, built=self.template, into=into / "built")
        new_root = into / "published"
        new_root.mkdir()
        return stores, old_root, new_root

    def next_register(self) -> dict[str, Any]:
        """The build phase's next register at the new root: what the Snakefile's register job calls."""
        return register.register_release(self.store_id, registry_root=self.stores, artifact_root=self.new_root)

    def test_a_relocated_release_is_up_to_date_at_its_new_root(self) -> None:
        """Only the records' path fields change; the release at the new root has nothing to do."""
        import yaml

        self.take_release()
        records_before = {p.name: p.read_bytes() for p in sorted((self.old_dir / "records").glob("*.json"))}
        mtimes_before = {
            str(p.relative_to(self.old_dir)): p.stat().st_mtime_ns for p in self.old_dir.rglob("*") if p.is_file()
        }

        moved = relocate_store(self.store_id, self.old_root, self.new_root)

        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assertFalse(self.old_dir.exists())
        receipts = sorted((self.new_dir / "relocations").glob("*.json"))
        self.assertEqual(len(receipts), 1, receipts)
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual((receipt["old_root"], receipt["new_root"]), (str(self.old_root), str(self.new_root)))
        self.assertEqual(
            {
                str(p.relative_to(self.new_dir)): p.stat().st_mtime_ns
                for p in self.new_dir.rglob("*")
                if p.is_file() and p.parent.name != "relocations"
            },
            mtimes_before,
            "a moved file's mtime changed",
        )
        receipt_records = {entry["path"]: entry for entry in receipt["records"]}
        self.assertEqual(sorted(receipt_records), [f"records/{name}" for name in records_before])
        for name, before in records_before.items():
            after = (self.new_dir / "records" / name).read_bytes()
            record = json.loads(before)
            # As `run` writes a record, with only the path fields moved: stdout,
            # stderr and every other field are what the step wrote.
            moved_record = {
                field: moved_paths(value, {str(self.old_dir): str(self.new_dir)}) if field in RECORD_PATH_FIELDS else value
                for field, value in record.items()
            }
            self.assertEqual(after.decode("utf-8"), run.record_text(moved_record), name)
            for field in RECORD_PATH_FIELDS:
                self.assertEqual(strings_under(moved_record[field], self.old_root), 0, f"{name}: {field}")
            entry = receipt_records[f"records/{name}"]
            self.assertEqual(entry["sha256_before"], hashlib.sha256(before).hexdigest(), name)
            self.assertEqual(entry["sha256_after"], hashlib.sha256(after).hexdigest(), name)
            self.assertEqual(
                entry["replaced"], {field: strings_under(record[field], self.old_dir) for field in RECORD_PATH_FIELDS}, name
            )
        self.assertTrue(any(sum(entry["replaced"].values()) for entry in receipt["records"]))

        rerun = run_release([self.store_id], registry_root=self.stores, artifact_root=self.new_root)
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("nothing to be done", rerun.stdout)
        self.assertEqual(
            {str(p.relative_to(self.new_dir)): p.stat().st_mtime_ns for p in (self.new_dir / "records").glob("*.json")},
            {f"records/{name}": mtimes_before[f"records/{name}"] for name in records_before},
        )
        registered = yaml.safe_load((self.stores / self.store_id / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(self.next_register()["status"], registered["status"])

    def test_a_release_moved_by_plain_rename_drifts_at_its_next_register(self) -> None:
        """Without the tool the release still looks up to date, and its next register refuses it, naming the old root."""
        self.take_release()
        os.rename(self.old_dir, self.new_dir)  # what `mv` does on one filesystem
        rerun = run_release([self.store_id], registry_root=self.stores, artifact_root=self.new_root)
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("nothing to be done", rerun.stdout)
        before = tree_state(self.td)

        with self.assertRaises(register.ArgvDriftError) as drift:
            self.next_register()

        self.assertIn(str(self.old_dir), str(drift.exception))
        self.assertEqual(tree_state(self.td), before, "a refused register wrote something")

    def test_each_refusal_moves_and_writes_nothing(self) -> None:
        """A pending publication, a staged Store, an existing target, or records naming another root."""

        def pending_publication(old_dir: Path, new_dir: Path) -> Path:
            (old_dir / paths.PUBLICATION_MARKER).write_text("{}\n", encoding="utf-8")
            return old_dir / paths.PUBLICATION_MARKER

        def staged_store(old_dir: Path, new_dir: Path) -> Path:
            (old_dir / "store.opengwasdb.partial").mkdir()
            return old_dir / "store.opengwasdb.partial"

        def existing_target(old_dir: Path, new_dir: Path) -> Path:
            new_dir.mkdir()
            return new_dir

        def records_naming_another_root(old_dir: Path, new_dir: Path) -> Path:
            # A copy whose records were never moved still names the root it was built in.
            shutil.rmtree(old_dir)
            shutil.copytree(paths.store_dir(self.store_id, root=self.template["artifacts"]), old_dir)
            return self.template["artifacts"]

        for case, arrange in enumerate((pending_publication, staged_store, existing_target, records_naming_another_root)):
            with self.subTest(arrange.__name__):
                _, old_root, new_root = self.release_built_in(self.td / f"case-{case}")
                named = arrange(paths.store_dir(self.store_id, root=old_root), paths.store_dir(self.store_id, root=new_root))
                before = tree_state(self.td)

                refused = relocate_store(self.store_id, old_root, new_root)

                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertIn(str(named), refused.stderr)
                self.assertIn("Nothing was moved", refused.stderr)
                self.assertEqual(tree_state(self.td), before)

    def test_a_dry_run_changes_nothing_and_names_what_would_change(self) -> None:
        self.take_release()
        before = tree_state(self.td)

        dry = relocate_store(self.store_id, self.old_root, self.new_root, "--dry-run")

        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(tree_state(self.td), before)
        for record_p in (self.old_dir / "records").glob("*.json"):
            self.assertIn(f"records/{record_p.name}", dry.stdout)
        self.assertIn("nothing was moved", dry.stdout)


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


def prepare_single_id_signal_release(root: Path, store_id: str) -> tuple[Path, Path]:
    """A fresh release that completes its own Store, ready to run, in `root` (#304, #309).

    #303's signal profile: the fixture VCF and LD panel under `root`, and a
    Dense bundle built from that VCF with `top_hits` and `overview` off,
    amended to complete its own Store. Returns the new `root/stores` and
    `root/artifacts`, so every caller has directories of its own.
    """
    stores, artifacts = root / "stores", root / "artifacts"
    stores.mkdir()
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
    return stores, artifacts


# The Hybrid signal fixture (#333). Its variant reference leaves out the last
# two signal positions, so the Observed-Only Store holds their associations in
# its Overflow, and the LD panel covers every position but the last. Completion
# then adds the eleventh position to the Dense axis and moves its Overflow
# association there, as an observed cell: one crossover. The twelfth stays in
# the Overflow.
HYBRID_OFF_REFERENCE_INDEXES: tuple[int, ...] = (10, 11)
HYBRID_PANEL_INDEXES: tuple[int, ...] = tuple(range(11))
# Its HM3-style variant list: five positions of the Observed-Only Dense
# Component, the held-out one among them, and one ALID no Store holds, which
# `build-indexed-subset` counts as absent. None is on the Overflow axis, which
# that command refuses for a Hybrid Store.
HYBRID_SUBSET_INDEXES: tuple[int, ...] = (0, 2, 4, 6, 8)
HYBRID_SUBSET_ALIDS: tuple[str, ...] = (*(SIGNAL_ALID[i] for i in HYBRID_SUBSET_INDEXES), "1:5000000:A:G")

def prepare_hybrid_single_id_signal_release(root: Path, store_id: str) -> tuple[Path, Path]:
    """`prepare_single_id_signal_release`'s release as a Hybrid one (#333).

    The same VCF and bundle, built by `build-hybrid` against a plain ALID
    variant reference without `HYBRID_OFF_REFERENCE_INDEXES`, and completed by
    `complete-hybrid` against an LD panel over `HYBRID_PANEL_INDEXES`.
    """
    import yaml

    stores, artifacts = root / "stores", root / "artifacts"
    stores.mkdir()
    artifacts.mkdir()
    create_dense_fixture_store(
        stores,
        store_id=store_id,
        artifact_root=artifacts,
        vcf_path=create_signal_fixture_vcf(root / "signal.vcf.gz"),
        post_top_hits=False,
        post_overview=False,
    )
    reference = root / "variant-reference.txt"
    reference.write_text(
        "".join(f"{alid}\n" for i, alid in enumerate(SIGNAL_ALID) if i not in HYBRID_OFF_REFERENCE_INDEXES),
        encoding="utf-8",
    )
    build_yaml_p = stores / store_id / "build.yaml"
    build_yaml = yaml.safe_load(build_yaml_p.read_text(encoding="utf-8"))
    build_yaml["layout"] = "hybrid"
    build_yaml["build"]["command"] = "build-hybrid"
    build_yaml["build"]["options"]["variant-reference"] = str(reference)
    build_yaml_p.write_text(yaml.safe_dump(build_yaml), encoding="utf-8")
    amend_to_complete_own_store(
        stores,
        store_id,
        ld_panel=create_signal_ld_panel(root / "ld-panel", indexes=HYBRID_PANEL_INDEXES),
        command="complete-hybrid",
    )
    return stores, artifacts


def prepare_hybrid_subset_signal_release(root: Path, store_id: str) -> tuple[Path, Path]:
    """`prepare_hybrid_single_id_signal_release`'s release with OGS-00011's post steps (#333).

    It regenerates its overview and builds an `hm3` Indexed Variant Subset in
    both phases, from `HYBRID_SUBSET_ALIDS`, which the fixture maintains as a
    Reference Resource of its registry's repository
    (`declare_variant_list_resource`).
    """
    import yaml

    stores, artifacts = prepare_hybrid_single_id_signal_release(root, store_id)
    variant_list = root / "hm3.alid.txt"
    variant_list.write_text("".join(f"{alid}\n" for alid in HYBRID_SUBSET_ALIDS), encoding="utf-8")
    declare_variant_list_resource(stores, "fixture-hm3", variant_list)
    build_yaml_p = stores / store_id / "build.yaml"
    build_yaml = yaml.safe_load(build_yaml_p.read_text(encoding="utf-8"))
    build_yaml["post"]["overview"] = True
    build_yaml["indexed_subsets"] = [
        {
            "name": "hm3",
            "resource_id": "fixture-hm3",
            "variant_list": str(variant_list),
            "reference_assembly": "GRCh38",
        }
    ]
    build_yaml_p.write_text(yaml.safe_dump(build_yaml), encoding="utf-8")
    return stores, artifacts


# The Ragged signal fixture (#336). The signal profile as one GWAS-SSF file,
# with an allele frequency at every position but this one.
RAGGED_NO_EAF_INDEX: int = 11


def create_signal_fixture_ssf(path: Path) -> Path:
    """The signal profile as one filtered GWAS-SSF file, for `build-ragged-ssf` (#336).

    The same eleven positions as `create_signal_fixture_vcf`, the held-out one
    left out, with `beta = z * SE`. `effect_allele` is the canonical `a1 = A`,
    so no z is negated (opengwasdb ADR 0036) and the frequency is the one the
    panel's `EA` names.

    The position at `RAGGED_NO_EAF_INDEX` reports no frequency, which keeps
    SE `float16`: opengwasdb stores SE as `int8_residual`, predicted from
    EAF, only when every association with an SE has an EAF. `complete-ragged`
    refits and re-quantises a residual-coded SE, which on real data moves
    observed SE slightly, and the Observed-View Check compares observed SE
    exactly (opengwas/opengwasdb#301). This fixture's constant SE comes
    through the refit unchanged, so it would pass either way; `float16` keeps
    the test from depending on that.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta\tstandard_error\t"
        "effect_allele_frequency\tp_value\trsid\n"
    ]
    for i, (position, z) in enumerate(zip(SIGNAL_POSITIONS, SIGNAL_Z, strict=True)):
        if i == SIGNAL_HELD_OUT_INDEX:
            continue
        eaf = "NA" if i == RAGGED_NO_EAF_INDEX else "0.3"
        p = math.erfc(abs(z) / math.sqrt(2))
        lines.append(f"1\t{position}\tA\tG\t{z * SIGNAL_SE:.6f}\t{SIGNAL_SE}\t{eaf}\t{p:.6g}\trs{i}\n")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("".join(lines))
    return path


def prepare_ragged_single_id_signal_release(root: Path, store_id: str) -> tuple[Path, Path]:
    """`prepare_single_id_signal_release`'s release as a Ragged one, shaped as OGS-00006's (#336).

    `build-ragged-ssf` builds it from `create_signal_fixture_ssf`'s file, a
    Top-Hit Index is built as a post step, and `complete-ragged` completes it
    against `create_signal_ld_panel`'s one block. Its one Analysis has no Trait
    position, as no OGS-00006 Analysis does, so completion takes the block it
    already holds enough observations in as its one retained region. The
    bundle is `create_dense_fixture_store`'s with its layout, build block,
    post steps and membership table replaced; the VCF it names is never
    written.
    """
    import yaml

    stores, artifacts = root / "stores", root / "artifacts"
    stores.mkdir()
    artifacts.mkdir()
    create_dense_fixture_store(stores, store_id=store_id, artifact_root=artifacts, vcf_path=root / "unused.vcf.gz")
    store_dir = stores / store_id
    build_yaml_p = store_dir / "build.yaml"
    build_yaml = yaml.safe_load(build_yaml_p.read_text(encoding="utf-8"))
    build_yaml["layout"] = "ragged"
    build_yaml["build"] = {"command": "build-ragged-ssf", "options": {"stored-effect-scale": "sd"}}
    build_yaml["post"] = {"top_hits": True, "overview": False, "validate": True}
    build_yaml_p.write_text(yaml.safe_dump(build_yaml), encoding="utf-8")
    ssf = create_signal_fixture_ssf(root / "signal.tsv.gz")
    (store_dir / "analyses.tsv").write_text(
        "analysis_index\tanalysis_id\tsource_file\ttrait_name\tsample_size\tstored_effect_scale\t"
        "assigned_ancestry\tancestry_assignment_method\n"
        f"0\ttest_analysis_{store_id}\t{ssf}\tTest Trait\t10000\tsd\tEUR\tsource_fallback\n",
        encoding="utf-8",
    )
    amend_to_complete_own_store(
        stores, store_id, ld_panel=create_signal_ld_panel(root / "ld-panel"), command="complete-ragged"
    )
    return stores, artifacts


def observed_view(store_p: Path, analysis_id: str) -> dict[str, tuple[float, float, float, str]]:
    """`observed_only=True` associations of one Analysis by ALID: decoded z, se, eaf and status."""
    q = query_store(store_p)
    try:
        rows = q.analysis(analysis_id, observed_only=True)
        table = q.variants_table()
    finally:
        q.close()
    return {
        table[int(index)]["alid"]: (float(z), float(se), float(eaf), str(status))
        for index, z, se, eaf, status in zip(
            rows["variant_index"], rows["z"], rows["se"], rows["eaf"], rows["association_status"], strict=True
        )
    }


_SINGLE_ID_COMPLETION: dict[str, Any] | None = None


def kill_release_run(proc: subprocess.Popen, pause: Path) -> None:
    """SIGKILL an entry-point run started in a session of its own, and the job paused at `pause`; reap it.

    A paused job is in Snakemake's session, which the entry point's death
    takes down only afterwards, through its supervisor (ADR 0026), so its
    group is killed directly too. The caller's own group never is.
    """
    groups = {proc.pid}
    with contextlib.suppress(OSError, ValueError):
        groups.add(os.getpgid(int(pause.read_text(encoding="utf-8").strip())))
    groups.discard(os.getpgrp())
    for group in groups:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)
    proc.wait()


def run_single_id_completion(
    store_id: str,
    prepare: Callable[[Path, str], tuple[Path, Path]],
    *,
    copy_complete_started: bool = False,
) -> dict[str, Any]:
    """One fresh entry-point run of a release that completes its own Store, paused twice (#304, #333).

    `prepare(root, store_id)` writes the release under a new
    `TemporaryDirectory` and returns its `stores` and `artifacts`, so the
    bundle it writes decides the layout and its completion command. Returns
    `store_id`, `analysis_id`, the built `stores` and `artifacts`, the run's
    result (`release_result`) and the `TemporaryDirectory` that holds them.

    The run is paused twice. As `complete` starts, once the build phase has
    published the Observed-Only Store, its observed view is read
    (`observed_before`, from `observed_view`), because the swap deletes that
    Store (#309); with `copy_complete_started`, both trees are also copied,
    mtimes kept, to `complete_started` (`stores`, `artifacts`), for a test
    that completes a published Store. As the Observed-View Check starts, with
    the completed Store staged and validated, both trees are copied to
    `check_started`, for a test that tampers with the staged Store before its
    own run checks it. Each is None when the run never got there or was not
    asked to copy. No snapshot is written again:
    a test copies it. Should anything raise meanwhile, the run is killed and
    reaped (`kill_release_run`), its pause file removed, and the error raised.
    """
    analysis_id = f"test_analysis_{store_id}"
    temp_dir = tempfile.TemporaryDirectory()
    root = Path(temp_dir.name)
    stores, artifacts = prepare(root, store_id)

    argv = release_argv([store_id], registry_root=stores, artifact_root=artifacts)
    pause = root / "paused"
    pause_points = ("complete-started", "completion-observed-view-check-started")
    env = {**os.environ, run.PAUSE_AT_ENV: ",".join(pause_points), run.PAUSE_FILE_ENV: str(pause)}
    observed_before = None
    snapshots: dict[str, dict[str, Path]] = {}
    with (root / "release.out").open("w+") as out, (root / "release.err").open("w+") as err:
        proc = subprocess.Popen(argv, cwd=REPO_ROOT, stdout=out, stderr=err, env=env, start_new_session=True)
        try:
            deadline = time.monotonic() + 600
            for point in pause_points:
                while not pause.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                if not pause.exists():
                    break
                if point == "complete-started":
                    observed_before = observed_view(paths.store_path(store_id, root=artifacts), analysis_id)
                if point != "complete-started" or copy_complete_started:
                    snapshots[point] = {tree: root / point / tree for tree in ("stores", "artifacts")}
                    shutil.copytree(stores, snapshots[point]["stores"])
                    shutil.copytree(artifacts, snapshots[point]["artifacts"])
                pause.unlink()
            if proc.poll() is None and time.monotonic() >= deadline:
                kill_release_run(proc, pause)
            returncode = proc.wait(timeout=600)
        except BaseException:
            # A run given up on is never let go: unpaused, it would stop at its
            # next pause point and wait there for good.
            kill_release_run(proc, pause)
            raise
        finally:
            pause.unlink(missing_ok=True)
        out.seek(0)
        err.seek(0)
        release_result = subprocess.CompletedProcess(argv, returncode, out.read(), err.read())
    return {
        "temp_dir": temp_dir,
        "stores": stores,
        "artifacts": artifacts,
        "release_result": release_result,
        "observed_before": observed_before,
        "complete_started": snapshots.get("complete-started"),
        "check_started": snapshots.get("completion-observed-view-check-started"),
        "store_id": store_id,
        "analysis_id": analysis_id,
    }


def single_id_completion_template() -> dict[str, Any]:
    """The one single-id completed Dense release this process builds, built on first use (#304).

    `run_single_id_completion` builds, publishes and completes it from
    `prepare_single_id_signal_release`, and a second run in place follows at
    once. Adds that run's result (`rerun_result`) and the record mtimes it
    found (`records_before_rerun`). A test that may write copies `stores` and
    `artifacts`; the template is never written again.

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

    built = run_single_id_completion("OGS-00110", prepare_single_id_signal_release)
    store_id, stores, artifacts = built["store_id"], built["stores"], built["artifacts"]
    records_before_rerun = {
        p: p.stat().st_mtime_ns for p in paths.records_dir(store_id, root=artifacts).glob("*.json")
    }
    rerun_result = run_release([store_id], registry_root=stores, artifact_root=artifacts)

    _SINGLE_ID_COMPLETION = {**built, "rerun_result": rerun_result, "records_before_rerun": records_before_rerun}
    return _SINGLE_ID_COMPLETION


_HYBRID_SINGLE_ID_COMPLETION: dict[str, Any] | None = None


def hybrid_single_id_completion_template() -> dict[str, Any]:
    """The one single-id completed Hybrid release this process builds, built on first use (#333).

    `run_single_id_completion` from `prepare_hybrid_single_id_signal_release`,
    copying both trees as `complete` starts, with no rerun: what a rerun shows
    does not depend on the layout, and `single_id_completion_template` shows
    it. A test that may write copies a tree; the template is never written
    again. Its post steps are validation only; the overview and an Indexed
    Variant Subset cost every test that builds it some 20 s, so one class
    builds a release of its own with them (`prepare_hybrid_subset_signal_release`).
    """
    global _HYBRID_SINGLE_ID_COMPLETION
    if _HYBRID_SINGLE_ID_COMPLETION is None:
        _HYBRID_SINGLE_ID_COMPLETION = run_single_id_completion(
            "OGS-00130", prepare_hybrid_single_id_signal_release, copy_complete_started=True
        )
    return _HYBRID_SINGLE_ID_COMPLETION


_RAGGED_SINGLE_ID_COMPLETION: dict[str, Any] | None = None


def ragged_single_id_completion_template() -> dict[str, Any]:
    """The one single-id completed Ragged release this process builds, built on first use (#336).

    `run_single_id_completion` from `prepare_ragged_single_id_signal_release`,
    copying both trees as `complete` starts, with no rerun, as
    `hybrid_single_id_completion_template` does. A test that may write copies
    a tree; the template is never written again.
    """
    global _RAGGED_SINGLE_ID_COMPLETION
    if _RAGGED_SINGLE_ID_COMPLETION is None:
        _RAGGED_SINGLE_ID_COMPLETION = run_single_id_completion(
            "OGS-00140", prepare_ragged_single_id_signal_release, copy_complete_started=True
        )
    return _RAGGED_SINGLE_ID_COMPLETION


def copy_snapshot(snapshot: dict[str, Path], *, built: dict[str, Any], into: Path) -> tuple[Path, Path]:
    """Copy a snapshot taken mid-run into `into`, as if its release had been built there.

    Returns the copy's `stores` and `artifacts`. A snapshot's records name the
    directories its run was built in (`built["stores"]`, `built["artifacts"]`),
    and the steps left to run check their predecessors' argv against the plan
    under the run's own root. So each path in a record under either of them
    is moved to the copy's (`moved_paths`), keeping the record's mtime, which
    a run on a copy decides by (#303).
    Inputs the bundle names outside those two trees, such as the LD panel,
    are the template's and are only read.
    """
    stores, artifacts = into / "stores", into / "artifacts"
    shutil.copytree(snapshot["stores"], stores)
    shutil.copytree(snapshot["artifacts"], artifacts)
    moves = {str(built["stores"]): str(stores), str(built["artifacts"]): str(artifacts)}
    for record_p in artifacts.glob("*/records/*.json"):
        written = record_p.stat()
        record = json.loads(record_p.read_text(encoding="utf-8"))
        # As `run` writes a record.
        record_p.write_text(json.dumps(moved_paths(record, moves), indent=2) + "\n", encoding="utf-8")
        os.utime(record_p, ns=(written.st_atime_ns, written.st_mtime_ns))
    return stores, artifacts


def moved_paths(value: Any, moves: dict[str, str]) -> Any:
    """`value` with every string that is one of `moves`' roots, or a path under one, under its new root.

    Only whole paths move: a string that merely contains a root, or names a
    sibling that shares its prefix (`/x/stores-extra` beside `/x/stores`), is
    left as it is.
    """
    if isinstance(value, str):
        for old, new in moves.items():
            if value == old or value.startswith(old + "/"):
                return new + value[len(old):]
        return value
    if isinstance(value, list):
        return [moved_paths(item, moves) for item in value]
    if isinstance(value, dict):
        return {key: moved_paths(item, moves) for key, item in value.items()}
    return value


class TestWorkflowSingleIdTemplateMachinery(unittest.TestCase):
    """What `single_id_completion_template` and `copy_snapshot` do around a release, shown without building one."""

    # Stands in for an entry-point run: a process of its own group whose job
    # runs in a session of its own, as Snakemake's does, and crosses the
    # template's two pause points through `run.fault_boundary`, as a step does.
    ENTRY_POINT = (
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "sys.exit(subprocess.Popen([sys.executable, sys.argv[2]], start_new_session=True).wait())\n"
    )
    JOB = (
        "from ogstores import run\n"
        "for point in ('complete-started', 'completion-observed-view-check-started'):\n"
        "    run.fault_boundary(point)\n"
    )

    def test_a_failure_while_the_run_is_paused_kills_and_reaps_it_and_raises(self) -> None:
        """Nothing resumes into the next pause: the run's processes are killed and reaped, the pause file removed."""
        scripts = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scripts, True)
        (scripts / "entry_point.py").write_text(self.ENTRY_POINT, encoding="utf-8")
        (scripts / "job.py").write_text(self.JOB, encoding="utf-8")
        entry_pid_p = scripts / "entry_point.pid"
        seen: dict[str, Any] = {}

        def prepare(root: Path, store_id: str) -> tuple[Path, Path]:
            seen["pause"] = root / "paused"
            for tree in ("stores", "artifacts"):
                (root / tree).mkdir()
            return root / "stores", root / "artifacts"

        def unreadable(store_p: Path, analysis_id: str) -> dict:
            seen["job"] = int(seen["pause"].read_text(encoding="utf-8").strip())
            raise OSError("the observed view could not be read")

        def kill_leftovers() -> None:
            """Only while the run is known to be paused: a pid already reaped may be reused."""
            for pid in (seen.get("job"), int(entry_pid_p.read_text()) if entry_pid_p.exists() else None):
                if pid is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)

        # The template's own directory is kept, so the pause file's absence
        # shows something; this test cleans it, not the garbage collector.
        made: list[tempfile.TemporaryDirectory] = []
        real_temporary_directory = tempfile.TemporaryDirectory

        def temporary_directory() -> tempfile.TemporaryDirectory:
            made.append(real_temporary_directory())
            return made[-1]

        self.addCleanup(lambda: [directory.cleanup() for directory in made])
        outcome: dict[str, BaseException] = {}

        def build() -> None:
            try:
                single_id_completion_template()
            except BaseException as exc:
                outcome["raised"] = exc

        with mock.patch.multiple(
            sys.modules[__name__],
            _SINGLE_ID_COMPLETION=None,
            prepare_single_id_signal_release=prepare,
            release_argv=lambda *args, **kwargs: [
                sys.executable, str(scripts / "entry_point.py"), str(entry_pid_p), str(scripts / "job.py")
            ],
            observed_view=unreadable,
            run_release=mock.Mock(side_effect=AssertionError("the template ran on past the failure")),
        ), mock.patch.object(tempfile, "TemporaryDirectory", temporary_directory):
            builder = threading.Thread(target=build, daemon=True)
            builder.start()
            builder.join(timeout=30)
            hung = builder.is_alive()
            if hung:
                kill_leftovers()
                builder.join(timeout=30)

        self.assertFalse(hung, "the template did not raise within 30 s of a failure at its first pause")
        self.assertIsInstance(outcome.get("raised"), OSError, outcome)
        self.assertEqual(str(outcome["raised"]), "the observed view could not be read")
        self.assertTrue(seen["pause"].parent.is_dir(), "the template's directory outlives the failure")
        self.assertFalse(seen["pause"].exists(), "the pause file is removed")
        for name, pid in (("entry point", int(entry_pid_p.read_text())), ("job", seen["job"])):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
                self.fail(f"the stand-in {name} (pid {pid}) is still there")

    def test_copy_snapshot_moves_whole_paths_only(self) -> None:
        """A template root is replaced as a path or a path's prefix, never inside another name or string."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        snapshot = {tree: root / "snapshot" / tree for tree in ("stores", "artifacts")}
        snapshot["stores"].mkdir(parents=True)
        record_p = snapshot["artifacts"] / "OGS-00001" / "records" / "step.json"
        record_p.parent.mkdir(parents=True)
        record = {
            "record_path": "/x/artifacts/OGS-00001/records/step.json",
            "argv": ["opengwasdb", "validate", "/x/artifacts/OGS-00001/store.opengwasdb", "/x/stores-extra/OGS-00001"],
            "inputs": ["/x/stores"],
            "stdout": "checked /x/artifacts/OGS-00001/store.opengwasdb",
            "nested": {"paths": ["/x/stores/OGS-00001/build.yaml", "/x/artifactsX/OGS-00001"]},
            "elapsed_seconds": 1.5,
        }
        written = json.dumps(record, indent=2) + "\n"
        record_p.write_text(written, encoding="utf-8")
        os.utime(record_p, ns=(1_000_000_000, 2_000_000_000))

        stores, artifacts = copy_snapshot(
            snapshot, built={"stores": Path("/x/stores"), "artifacts": Path("/x/artifacts")}, into=root / "copy"
        )

        copied_p = artifacts / "OGS-00001" / "records" / "step.json"
        self.assertEqual(
            json.loads(copied_p.read_text(encoding="utf-8")),
            {
                "record_path": f"{artifacts}/OGS-00001/records/step.json",
                "argv": ["opengwasdb", "validate", f"{artifacts}/OGS-00001/store.opengwasdb", "/x/stores-extra/OGS-00001"],
                "inputs": [str(stores)],
                "stdout": "checked /x/artifacts/OGS-00001/store.opengwasdb",
                "nested": {"paths": [f"{stores}/OGS-00001/build.yaml", "/x/artifactsX/OGS-00001"]},
                "elapsed_seconds": 1.5,
            },
        )
        self.assertEqual(copied_p.stat().st_mtime_ns, 2_000_000_000, "the copy keeps the record's mtime")
        self.assertEqual(record_p.read_text(encoding="utf-8"), written, "the snapshot is not written")


class TestWorkflowSingleIdReferenceCompletion(unittest.TestCase):
    """Reference completion under one id, end to end through the entry point (#304, ADR 0037).

    One Release Bundle declares the build and the completion of a Dense Store.
    One `pixi run release <id>` builds and publishes the Observed-Only Store,
    completes it from that published Store into the id's staged path,
    validates it there, and publishes the completed Store at the same path.
    The replaced Store's arrays are deleted (#309). Its register record, its
    Validation Record and the Observed-View Check's verdict are archived under
    `replaced/<stamp>/`.

    The Observed-Only Store is asserted on through those archived records:
    that its build phase published it observed-only, with the VCF's
    observations, is the evidence that it was published first and then
    replaced. The fixture is #303's signal profile, whose completion
    `TestWorkflowTwoIdReferenceCompletion` already shows to be real and exact,
    so this class asserts only what one id changes.
    """

    PHASE_RECORDS = [
        "build", "validate", "register", "complete", "completion-validate",
        "completion-observed-view-check", "completion-register",
    ]

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
        cls.observed_before: dict[str, tuple[float, float, float, str]] | None = template["observed_before"]

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
        """`observed_only=True` associations by ALID: decoded z, se and eaf, none of them imputed."""
        view = observed_view(store_p, self.ANALYSIS_ID)
        self.assertNotIn("imputed", {status for *_, status in view.values()})
        return {alid: (z, se, eaf) for alid, (z, se, eaf, _) in view.items()}

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

    def test_the_observed_view_check_passed_before_the_swap_and_its_verdict_is_its_record(self) -> None:
        """The record holds the verdict the command printed, with the seed and `passed: true` (ADR 0037)."""
        rec_dir = paths.records_dir(self.STORE_ID, root=self.artifact_root)
        record = json.loads((rec_dir / "completion-observed-view-check.json").read_text(encoding="utf-8"))
        verdict = json.loads(record["stdout"])
        self.assertIs(verdict["passed"], True)
        self.assertEqual(verdict["failures"], [])
        self.assertEqual(verdict["seed"], 20261021)
        self.assertEqual(verdict["n_random"], 1000)
        self.assertEqual(verdict["source"]["release_id"], self.STORE_ID)
        self.assertEqual(verdict["target"]["release_id"], f"{self.STORE_ID}-completed")
        work_dir = paths.work_dir(self.STORE_ID, root=self.template_artifacts)
        self.assertEqual(record["argv"][-2:], ["--output", str(work_dir / "observed-view-check.json")])
        self.assertEqual(record["argv"][2], str(paths.store_path(self.STORE_ID, root=self.template_artifacts)))
        self.assertEqual(
            record["argv"][3], str(paths.partial_store_path(self.STORE_ID, root=self.template_artifacts))
        )
        self.assertEqual(
            json.loads((paths.work_dir(self.STORE_ID, root=self.artifact_root) / "observed-view-check.json").read_text()),
            verdict,
        )

    def test_the_completed_store_keeps_the_id_and_takes_its_own_release_id(self) -> None:
        """Completion inherits its source's Store identity, here the id itself (opengwasdb ADR 0011)."""
        manifest_doc = self.manifest(paths.store_path(self.STORE_ID, root=self.artifact_root))
        self.assertEqual(manifest_doc["store_id"], self.STORE_ID)
        self.assertEqual(manifest_doc["release_id"], f"{self.STORE_ID}-completed")

    def test_observed_only_returns_the_observed_only_stores_associations_unchanged(self) -> None:
        """By ALID, z, se and eaf are exactly those of the Store it replaced, read before completion began.

        The replaced Store is deleted with the swap (#309), so the template
        reads its observed view while the run is paused as `complete` starts.
        The comparison is exact, eaf NaN-equal: a tolerance would accept the
        one-quantum change completion must never make. Beside it, the
        Observed-Only Store's z and se sit within one quantum of the VCF
        literals, so neither side can be a Store with no observed cells.
        """
        import numpy as np
        from opengwasdb.encoding.plan import DEFAULT_Z_SCALE

        self.assertIsNotNone(self.observed_before, "the template never read the Observed-Only Store")
        before = {alid: (z, se, eaf) for alid, (z, se, eaf, _) in self.observed_before.items()}
        self.assertNotIn("imputed", {status for *_, status in self.observed_before.values()})
        completed = self.observed(paths.store_path(self.STORE_ID, root=self.artifact_root))
        expected_alids = sorted(SIGNAL_ALID[i] for i in range(len(SIGNAL_POSITIONS)) if i != SIGNAL_HELD_OUT_INDEX)
        self.assertEqual(sorted(before), expected_alids)
        self.assertEqual(sorted(completed), expected_alids)
        z_quantum = 1.0 / DEFAULT_Z_SCALE
        se_quantum = float(np.spacing(np.float16(SIGNAL_SE)))
        for alid in expected_alids:
            z, se, eaf = completed[alid]
            before_z, before_se, before_eaf = before[alid]
            self.assertEqual(z, before_z, f"z changed for {alid}")
            self.assertEqual(se, before_se, f"se changed for {alid}")
            self.assertTrue(
                eaf == before_eaf or (np.isnan(eaf) and np.isnan(before_eaf)),
                f"eaf changed for {alid}: {eaf!r} != {before_eaf!r}",
            )
            self.assertLessEqual(abs(before_z - SIGNAL_Z[SIGNAL_ALID.index(alid)]), z_quantum, alid)
            self.assertLessEqual(abs(before_se - SIGNAL_SE), se_quantum, alid)

    def test_the_replaced_store_is_deleted_and_its_records_are_archived(self) -> None:
        """No arrays under `replaced/<stamp>/`: the replaced release's two records and the check's verdict (#309)."""
        import yaml

        archive = self.archive()
        rec_dir = paths.records_dir(self.STORE_ID, root=self.artifact_root)
        self.assertEqual(sorted(p.name for p in archive.iterdir()), ["records", "validation.yaml"])
        self.assertEqual(
            sorted(p.name for p in (archive / "records").iterdir()),
            ["completion-observed-view-check.json", "register.json"],
        )
        self.assertEqual((archive / "records" / "register.json").read_bytes(), (rec_dir / "register.json").read_bytes())
        archived_check = archive / "records" / "completion-observed-view-check.json"
        self.assertEqual(archived_check.read_bytes(), (rec_dir / archived_check.name).read_bytes())
        verdict = json.loads(json.loads(archived_check.read_text())["stdout"])
        self.assertIs(verdict["passed"], True)
        self.assertEqual(verdict["failures"], [])
        self.assertEqual({name: verdict["totals"][name] for name in FAILING_COUNTERS}, dict.fromkeys(FAILING_COUNTERS, 0))
        archived_record = yaml.safe_load((archive / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(archived_record["observed"]["completion_state"], "observed_only")
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
    COMPLETION_RECORDS = ["complete", "completion-validate", "completion-observed-view-check", "completion-register"]

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
        # The swap deleted the Observed-Only Store and archived the records of
        # the release it replaced (#309): its register record and Validation Record.
        import yaml

        archives = list((paths.store_dir(self.STORE_ID, root=self.artifacts) / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)
        self.assertNotIn("store.opengwasdb", {p.name for p in archives[0].iterdir()})
        build_register = paths.records_dir(self.STORE_ID, root=self.artifacts) / "register.json"
        self.assertEqual(
            (archives[0] / "records" / "register.json").read_bytes(), self.template["build_phase_before"][build_register][1]
        )
        replaced = yaml.safe_load((archives[0] / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(replaced["observed"]["n_variants"], len(SIGNAL_POSITIONS) - 1, "the Observed-Only Store's record")

    def test_bundle_files_all_rewritten_later_still_plan_only_the_completion_phase(self) -> None:
        """A fresh checkout rewrites every bundle file; the identical analyses.tsv is no new membership (#339)."""
        stores, artifacts, store_id = self.observed_only_copy()
        amend_to_complete_own_store(stores, store_id, ld_panel=self.td / "ld-panel")
        later = max(p.stat().st_mtime_ns for p in paths.store_dir(store_id, root=artifacts).rglob("*")) + 10**9
        for name in ("release.yaml", "build.yaml", "analyses.tsv", "validation.yaml"):
            if (stores / store_id / name).is_file():
                os.utime(stores / store_id / name, ns=(later, later))

        dry = run_release([store_id, "--dry-run"], registry_root=stores, artifact_root=artifacts)

        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        records = [t for t in scheduled_targets(dry.stdout) if t != store_id]
        self.assertEqual(records, self.COMPLETION_RECORDS)
        self.assertNotIn("would refuse", dry.stdout)

    def test_an_unforced_run_rebuilding_its_build_phase_is_still_refused_unchanged(self) -> None:
        """Only completion-phase jobs are let through: a rescheduled build phase is still refused (#195)."""
        stores, artifacts, store_id = self.observed_only_copy()
        amend_to_complete_own_store(stores, store_id, ld_panel=self.td / "ld-panel")
        # The records are older than the manifest the build reads, as after an
        # interrupted build phase: unchanged membership, so only the published
        # guard stands between this run and a rebuild.
        day_ns = 86_400 * 10**9
        for p in paths.records_dir(store_id, root=artifacts).rglob("*"):
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


class TestWorkflowTheOneAmendmentOfAnAcceptedBundle(PublishedReleaseCopies):
    """A bundle that completes its own Store allows one amendment only (#305, ADR 0037).

    Gaining a `complete` block is allowed: `TestWorkflowCompletingAStoreBuiltBefore304`
    shows it runs the completion phase only. Once such a release's build phase
    has published, its bundle must still plan the Store-writing steps its
    records ran (`variant-reference`, `build`), with the membership its Store
    was built from; once its completion phase has published, the `complete`
    its record ran. Post steps are re-runnable in-place additions, so a new
    declaration, or a planner that has moved on since, is no change to refuse.
    Observed-only and two-id releases are not checked, as before #305.

    The cases copy #304's single-id and #303's two-id templates. One refusal
    goes through the entry point; the others ask `amendment.refuse_changed_bundles`.
    """

    def single_id_copy(self) -> tuple[Path, Path, str]:
        template = single_id_completion_template()
        stores, artifacts = self.published_copy(template["stores"], template["artifacts"])
        return stores, artifacts, template["store_id"]

    def edit_yaml(self, path: Path, edit) -> None:
        import yaml

        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        edit(document)
        path.write_text(yaml.safe_dump(document), encoding="utf-8")

    def record_older_planner(self, artifacts: Path, store_id: str, step: str) -> None:
        """Rewrite a step's record as the planner before `--format json` would have planned it.

        Every production release built before that planner change records its
        `validate` this way.
        """
        record_p = paths.record_path(store_id, step, root=artifacts)
        written = record_p.stat()
        record = json.loads(record_p.read_text(encoding="utf-8"))
        self.assertIn("--format", record["planned_argv"], step)
        record["planned_argv"] = [t for t in record["planned_argv"] if t not in ("--format", "json")]
        record_p.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.utime(record_p, ns=(written.st_atime_ns, written.st_mtime_ns))

    def refusal(self, stores: Path, artifacts: Path, store_id: str) -> str:
        """What `refuse_changed_bundles` refuses for this release, or "" when it lets it through."""
        from ogstores import amendment

        try:
            amendment.refuse_changed_bundles([store_id], registry_root=stores, artifact_root=artifacts)
        except amendment.AmendmentError as exc:
            return str(exc)
        return ""

    def test_a_stale_parent_manifest_is_rerun_when_only_the_child_is_named(self) -> None:
        """The `derived_from` walk: the child's dry run schedules the parent's build_manifest, and the unforced run is refused (#339)."""
        template = two_id_completion_template()
        stores, artifacts = self.published_copy(template["stores"], template["artifacts"])
        parent_analyses = stores / template["parent_id"] / "analyses.tsv"
        revise_registry_analyses(stores, template["parent_id"])
        os.utime(parent_analyses, ns=(10**9, 10**9))

        dry = run_release([template["child_id"], "--dry-run"], registry_root=stores, artifact_root=artifacts)

        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("analyses.exclusions", scheduled_targets(dry.stdout), dry.stdout)
        self.assertIn("already published", dry.stdout)
        res = run_release([template["child_id"]], registry_root=stores, artifact_root=artifacts)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("already published", res.stderr)

    def test_observed_only_and_two_id_releases_are_not_checked_even_after_planner_drift(self) -> None:
        template = two_id_completion_template()
        stores, artifacts = self.published_copy(template["stores"], template["artifacts"])
        for store_id in (template["parent_id"], template["child_id"]):
            self.record_older_planner(artifacts, store_id, "validate")

        res = run_release([template["child_id"]], registry_root=stores, artifact_root=artifacts)

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("nothing to be done", res.stdout)

    def test_a_changed_build_argv_is_refused_before_anything_is_written_naming_it(self) -> None:
        stores, artifacts, store_id = self.single_id_copy()
        self.edit_yaml(
            stores / store_id / "build.yaml", lambda doc: doc["build"]["options"].update({"source-assembly": "hg19"})
        )

        refusal = self.assert_refused_before_any_write(stores, artifacts, store_id)

        self.assertIn(
            f"{store_id}: build phase step 'build': `--source-assembly hg38` became `--source-assembly hg19`", refusal
        )

    def test_a_release_another_run_held_at_the_probe_is_checked_once_this_run_holds_it(self) -> None:
        """No run succeeds without checking every such release it touches, and no dry run hides one it could not.

        The probe says another run holds the release's lock, and that run is
        gone by the time this one takes the lock: the release is checked then,
        before any job. Patching the probe stands in for that race, which no
        timing reproduces.
        """
        from ogstores import release

        stores, artifacts, store_id = self.single_id_copy()
        self.edit_yaml(
            stores / store_id / "build.yaml", lambda doc: doc["build"]["options"].update({"source-assembly": "hg19"})
        )
        words = [store_id, "--cores", "1", "--config", f"registry_root={stores}", f"artifact_root={artifacts}"]

        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                out, err = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(release, "held_by_live_runs", return_value=frozenset({store_id})),
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    code = release.main([*words, "--dry-run"] if dry_run else words)
                if dry_run:
                    self.assertIn(
                        f"release-dry: {store_id} is held by another run, so its bundle is not checked here", out.getvalue()
                    )
                else:
                    self.assertEqual(code, 1, out.getvalue() + err.getvalue())
                    self.assertIn("`--source-assembly hg38` became `--source-assembly hg19`", err.getvalue())

    def test_a_new_post_step_or_a_post_step_planned_differently_is_not_refused(self) -> None:
        stores, artifacts, store_id = self.single_id_copy()
        self.edit_yaml(stores / store_id / "build.yaml", lambda doc: doc["post"].update({"rho": True}))
        for step in ("validate", "completion-validate"):
            self.record_older_planner(artifacts, store_id, step)

        self.assertEqual(self.refusal(stores, artifacts, store_id), "")

    def test_a_changed_membership_is_refused_naming_the_analyses(self) -> None:
        stores, artifacts, store_id = self.single_id_copy()
        analyses_p = stores / store_id / "analyses.tsv"
        header, row = analyses_p.read_text(encoding="utf-8").splitlines()
        fields = dict(zip(header.split("\t"), row.split("\t"), strict=True))
        added = {**fields, "analysis_id": "test_analysis_added", "trait_name": "Added Trait"}
        fields["sample_size"] = "20000"
        analyses_p.write_text(
            "\n".join([header, *("\t".join(r[c] for c in header.split("\t")) for r in (fields, added))]) + "\n",
            encoding="utf-8",
        )

        self.assertIn(
            f"{store_id}: membership: analysis 'test_analysis_added' added; "
            f"analysis 'test_analysis_{store_id}' changed (sample_size)",
            self.refusal(stores, artifacts, store_id),
        )

    def test_missing_or_unreadable_exclusion_evidence_is_refused_naming_it(self) -> None:
        """The published `work/analyses.exclusions.json` is the baseline's excluded Analyses, never "none" by default.

        Read as no exclusions, a missing sidecar would let a bundle that now
        excludes nothing pass for a Store that excluded Analyses.
        """
        stores, artifacts, store_id = self.single_id_copy()
        sidecar = paths.build_manifest_sidecar_path(store_id, root=artifacts)
        document = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(self.refusal(stores, artifacts, store_id), "")

        for case, text in (
            ("malformed JSON", json.dumps(document)[:-1]),
            ("not an object", json.dumps([document])),
            ("no dropped list", json.dumps({**document, "dropped": "test_analysis"})),
            ("an entry with no analysis_id", json.dumps({**document, "dropped": [{"row_index": 0}]})),
            ("deleted", None),
        ):
            with self.subTest(case=case):
                if text is None:
                    sidecar.unlink()
                else:
                    sidecar.write_text(text, encoding="utf-8")
                refusal = self.refusal(stores, artifacts, store_id)
                self.assertIn(f"{store_id}: membership cannot be compared", refusal)
                self.assertIn(str(sidecar), refusal)

    def test_a_register_record_that_is_not_an_object_is_refused_naming_it(self) -> None:
        """A register record that exists says its phase published; it is never read as unpublished.

        Read as unpublished, a `null` build register record left the build
        phase and the membership unchecked, so with no membership evidence
        either nothing was refused. A missing record still means the phase
        has not published.
        """
        stores, artifacts, store_id = self.single_id_copy()
        paths.build_manifest_path(store_id, root=artifacts).unlink()
        paths.build_manifest_sidecar_path(store_id, root=artifacts).unlink()

        for step in ("register", "completion-register"):
            record_p = paths.record_path(store_id, step, root=artifacts)
            published = record_p.read_text(encoding="utf-8")
            for case, text in (("null", "null"), ("a list", "[]"), ("malformed JSON", published[:-2])):
                with self.subTest(step=step, case=case):
                    record_p.write_text(text, encoding="utf-8")
                    refusal = self.refusal(stores, artifacts, store_id)
                    self.assertIn(f"{store_id}: ", refusal)
                    self.assertIn(str(record_p), refusal)
            record_p.write_text(published, encoding="utf-8")

    def test_a_changed_complete_block_is_refused_once_the_completion_has_published(self) -> None:
        stores, artifacts, store_id = self.single_id_copy()
        self.edit_yaml(
            stores / store_id / "build.yaml", lambda doc: doc["complete"]["options"].update({"min-cor": 0.5})
        )

        self.assertIn(
            f"{store_id}: completion phase step 'complete': `--min-cor 0.0` became `--min-cor 0.5`",
            self.refusal(stores, artifacts, store_id),
        )


class TestWorkflowForcedRunOfACompletedSingleIdRelease(unittest.TestCase):
    """`--config force=1` on a completed single-id release rebuilds both phases (#305, ADR 0037).

    Re-completing against a new reference panel is a forced run: the build
    phase rebuilds the Observed-Only Store and replaces the completed one, as
    #195 replaces any release, and the completion phase then completes the new
    Store and replaces it in turn. The completion's records are rebuilt with
    it, never left over from the completion they described.

    A subclass runs the same test on another layout's release: its template
    and the records its two phases write (#336).
    """

    PHASE_RECORDS = TestWorkflowSingleIdReferenceCompletion.PHASE_RECORDS

    @staticmethod
    def build_template() -> dict[str, Any]:
        return single_id_completion_template()

    def setUp(self) -> None:
        template = self.build_template()
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
        # A revised bundle, every file newer than the release's files.
        revise_registry_analyses(self.stores, self.store_id)
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
        for name in self.PHASE_RECORDS:
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


class TestWorkflowSingleIdFailingObservedViewCheck(unittest.TestCase):
    """A completed Store that fails the Observed-View Check is not published (#306, ADR 0037).

    The release is a copy of `single_id_completion_template` as its run stood
    when paused at the check (`check_started`, through `copy_snapshot`): the
    Observed-Only Store published and the completed Store staged and
    validated, its records naming the copy's paths. The copy's run
    has only the check and `completion-register` left, and is paused as the
    check starts. An observed Z of the staged Store is perturbed through
    opengwasdb's plane `patch` API. The check then fails, `completion-register`
    never runs, and the Observed-Only Store the build phase published is
    exactly as it was.

    A subclass runs the same test on another layout's release: its template,
    the cell it perturbs (`tamper`), the failure that names, and the
    Observed-Only Store's variant count (#333).
    """

    CHECK_STEP = "completion-observed-view-check"
    FAILURE = "z_mismatch"
    # The VCF's positions; the held-out one is in no Dense Store before completion.
    OBSERVED_ONLY_N_VARIANTS = len(SIGNAL_POSITIONS) - 1

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = cls.build_template()
        cls.STORE_ID: str = cls.template["store_id"]

    @staticmethod
    def build_template() -> dict[str, Any]:
        return single_id_completion_template()

    def tamper(self, staged_p: Path) -> None:
        """Perturb one observed Z of the staged completed Store."""
        import numpy as np
        from opengwasdb.encoding import DenseZPlane
        from opengwasdb.store.open import open_store

        found = query_store(staged_p).lookup([SIGNAL_ALID[0]], [f"test_analysis_{self.STORE_ID}"])
        rows, cols = np.array([int(found["variant_index"][0])]), np.array([int(found["analysis_index"][0])])
        opened = open_store(staged_p)
        plane = DenseZPlane.open(opened.arrays(mode="r+"), opened.manifest.encoding)
        plane.patch(rows, cols, plane.points(rows, cols) + 1.0)

    @staticmethod
    def hash_tree(root: Path) -> dict[str, str]:
        import hashlib

        return {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    def test_a_tampered_completed_store_is_refused_and_the_observed_only_store_survives(self) -> None:
        import yaml

        self.assertIsNotNone(self.template["check_started"], "the template's run never reached its check")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stores, artifacts = copy_snapshot(self.template["check_started"], built=self.template, into=root)
            pause = root / "paused"
            log = root / "run.log"
            env = {**os.environ, run.PAUSE_AT_ENV: f"{self.CHECK_STEP}-started", run.PAUSE_FILE_ENV: str(pause)}
            with log.open("w") as handle:
                proc = subprocess.Popen(
                    release_argv([self.STORE_ID], registry_root=stores, artifact_root=artifacts),
                    cwd=REPO_ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env, start_new_session=True,
                )
                self.addCleanup(lambda: proc.poll() is None and os.killpg(proc.pid, 9))
                deadline = time.monotonic() + 600
                while not pause.exists():
                    if proc.poll() is not None:
                        self.fail(f"the run ended before its check started:\n{log.read_text()}")
                    if time.monotonic() > deadline:
                        self.fail(f"the run never reached its check:\n{log.read_text()}")
                    time.sleep(0.2)

                store_p = paths.store_path(self.STORE_ID, root=artifacts)
                staged_p = paths.partial_store_path(self.STORE_ID, root=artifacts)
                self.assertEqual(
                    json.loads((store_p / "manifest.json").read_text())["completion_state"], "observed_only"
                )
                published_before = self.hash_tree(store_p)
                register_before = (paths.records_dir(self.STORE_ID, root=artifacts) / "register.json").read_bytes()
                validation_before = (stores / self.STORE_ID / "validation.yaml").read_bytes()

                self.tamper(staged_p)
                pause.unlink()
                returncode = proc.wait(timeout=600)

            output = log.read_text()
            self.assertNotEqual(returncode, 0, output)

            # The error names the failing counters and the verdict.
            verdict_p = paths.work_dir(self.STORE_ID, root=artifacts) / "observed-view-check.json"
            verdict = json.loads(verdict_p.read_text(encoding="utf-8"))
            self.assertIs(verdict["passed"], False)
            self.assertIn(self.FAILURE, verdict["failures"])
            self.assertIn(f"{self.FAILURE}=1", output)
            self.assertIn(str(verdict_p), output)

            # The check failed, so nothing registered and nothing was published.
            rec_dir = paths.records_dir(self.STORE_ID, root=artifacts)
            # Snakemake removes a failed job's record, so the verdict file is the evidence.
            self.assertFalse((rec_dir / f"{self.CHECK_STEP}.json").exists())
            self.assertFalse((rec_dir / "completion-register.json").exists())
            self.assertEqual(self.hash_tree(store_p), published_before)
            self.assertEqual((rec_dir / "register.json").read_bytes(), register_before)
            self.assertEqual((stores / self.STORE_ID / "validation.yaml").read_bytes(), validation_before)
            self.assertFalse((paths.store_dir(self.STORE_ID, root=artifacts) / "replaced").exists())
            self.assertFalse(paths.publication_marker(self.STORE_ID, root=artifacts).exists())
            self.assertEqual(
                yaml.safe_load((stores / self.STORE_ID / "validation.yaml").read_text())["observed"]["n_variants"],
                self.OBSERVED_ONLY_N_VARIANTS,
            )


class TestWorkflowSingleIdHybridFailingObservedViewCheck(TestWorkflowSingleIdFailingObservedViewCheck):
    """A Hybrid completed Store that fails the Observed-View Check is not published either (#333, ADR 0037).

    The release is `hybrid_single_id_completion_template`'s, and the cell
    perturbed is its crossover: the Overflow association completion moved into
    the Dense Component, which the Hybrid check compares with its source value.
    """

    FAILURE = "crossovers.z_mismatch"
    # The shared axis: the variant reference's ten positions, the held-out one
    # among them, and the two the Overflow holds.
    OBSERVED_ONLY_N_VARIANTS = len(SIGNAL_POSITIONS)

    @staticmethod
    def build_template() -> dict[str, Any]:
        return hybrid_single_id_completion_template()

    def tamper(self, staged_p: Path) -> None:
        """Perturb the crossover's Z in the staged Store's Dense Component."""
        import numpy as np
        from opengwasdb.encoding import DenseZPlane
        from opengwasdb.store.open import open_store

        # The held-out position is on the variant reference, which the
        # Observed-Only variant count counts on, and the crossover is observed.
        self.assertNotIn(SIGNAL_HELD_OUT_INDEX, HYBRID_OFF_REFERENCE_INDEXES)
        component = open_store(staged_p).dense_component()
        (crossover,) = set(HYBRID_OFF_REFERENCE_INDEXES) & set(HYBRID_PANEL_INDEXES)
        found = query_store(component.path).lookup([SIGNAL_ALID[crossover]], [f"test_analysis_{self.STORE_ID}"])
        self.assertEqual(list(found["association_status"]), ["observed"])
        rows, cols = np.array([int(found["variant_index"][0])]), np.array([int(found["analysis_index"][0])])
        plane = DenseZPlane.open(component.arrays(mode="r+"), component.manifest.encoding)
        plane.patch(rows, cols, plane.points(rows, cols) + 1.0)


class TestWorkflowSingleIdHybridReferenceCompletion(unittest.TestCase):
    """A Hybrid release completes its own Store under its id, end to end through the entry point (#333, ADR 0037).

    The release is `hybrid_single_id_completion_template`'s. Its Observed-Only
    Store holds two associations in its Overflow, and completion moves one of
    them into the Dense Component: a crossover. One run builds and publishes
    the Observed-Only Store, completes it, passes the Observed-View Check
    across both components, and publishes the completed Store at the same
    path. `TestWorkflowSingleIdReferenceCompletion` shows the rest of the
    single-id contract on a Dense release; this class asserts what a Hybrid
    one adds. Every test only reads the template.
    """

    BUILD_PHASE_RECORDS = ["build", "validate", "register"]
    COMPLETION_RECORDS = ["complete", "completion-validate", "completion-observed-view-check", "completion-register"]

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = hybrid_single_id_completion_template()
        cls.STORE_ID: str = cls.template["store_id"]
        cls.artifacts: Path = cls.template["artifacts"]
        cls.store_p: Path = paths.store_path(cls.STORE_ID, root=cls.artifacts)
        cls.rec_dir: Path = paths.records_dir(cls.STORE_ID, root=cls.artifacts)

    def archive(self) -> Path:
        archives = sorted((paths.store_dir(self.STORE_ID, root=self.artifacts) / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)
        return archives[0]

    def record(self, step: str) -> dict[str, Any]:
        return json.loads((self.rec_dir / f"{step}.json").read_text(encoding="utf-8"))

    def test_one_run_publishes_the_observed_only_store_then_the_completed_store_at_its_path(self) -> None:
        res = self.template["release_result"]
        self.assertEqual(res.returncode, 0, f"The entry point failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        for step in self.BUILD_PHASE_RECORDS + self.COMPLETION_RECORDS:
            self.assertTrue(self.record(step).get("success"), step)
        self.assertEqual(self.record("complete")["argv"][1], "complete-hybrid")
        self.assertEqual(self.record("register")["published_store"], str(self.store_p))
        self.assertIsNone(self.record("register")["replaced_archive"], "the build phase publishes first")
        self.assertEqual(self.record("completion-register")["replaced_archive"], str(self.archive()))
        for leftover in (
            paths.partial_store_path(self.STORE_ID, root=self.artifacts),
            paths.backup_store_path(self.STORE_ID, root=self.artifacts),
            paths.publication_marker(self.STORE_ID, root=self.artifacts),
        ):
            self.assertFalse(leftover.exists(), leftover)

    def test_the_completed_store_keeps_the_id_and_takes_its_own_release_id(self) -> None:
        manifest_doc = json.loads((self.store_p / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {key: manifest_doc[key] for key in ("primary_layout", "completion_state", "store_id", "release_id")},
            {
                "primary_layout": "hybrid",
                "completion_state": "reference_completed",
                "store_id": self.STORE_ID,
                "release_id": f"{self.STORE_ID}-completed",
            },
        )

    def test_the_check_compared_both_components_and_the_crossover_and_passed(self) -> None:
        """Two Overflow associations, one of them on the panel: one crossover, one left in the Overflow."""
        self.assertNotIn(SIGNAL_HELD_OUT_INDEX, HYBRID_OFF_REFERENCE_INDEXES, "both are observed in the VCF")
        verdict = json.loads(self.record("completion-observed-view-check")["stdout"])
        self.assertEqual(verdict["layout"], "hybrid")
        self.assertIs(verdict["passed"], True)
        self.assertEqual(verdict["failures"], [])
        self.assertEqual(verdict["crossovers"]["n"], 1)
        overflow = verdict["components"]["overflow"]
        self.assertEqual((overflow["n_parent_associations"], overflow["n_child_associations"]), (2, 1))

    def test_observed_only_returns_the_observed_only_stores_associations_unchanged(self) -> None:
        """By ALID, across both components, z, se and eaf are those read before completion began.

        The crossover's association moved from the Overflow to the Dense
        Component and must come back unchanged and observed.
        """
        import numpy as np

        before = self.template["observed_before"]
        self.assertIsNotNone(before, "the template never read the Observed-Only Store")
        after = observed_view(self.store_p, self.template["analysis_id"])
        expected_alids = sorted(SIGNAL_ALID[i] for i in range(len(SIGNAL_POSITIONS)) if i != SIGNAL_HELD_OUT_INDEX)
        self.assertEqual(sorted(before), expected_alids)
        self.assertEqual(sorted(after), expected_alids)
        for alid in expected_alids:
            (z, se, eaf, status), (before_z, before_se, before_eaf, before_status) = after[alid], before[alid]
            self.assertEqual((z, se), (before_z, before_se), alid)
            self.assertTrue(eaf == before_eaf or (np.isnan(eaf) and np.isnan(before_eaf)), alid)
            self.assertNotEqual(status, "imputed", alid)
            self.assertNotEqual(before_status, "imputed", alid)

    def test_the_replaced_store_is_deleted_and_its_records_are_archived(self) -> None:
        import yaml

        archive = self.archive()
        self.assertEqual(sorted(p.name for p in archive.iterdir()), ["records", "validation.yaml"])
        self.assertEqual(
            sorted(p.name for p in (archive / "records").iterdir()),
            ["completion-observed-view-check.json", "register.json"],
        )
        archived_record = yaml.safe_load((archive / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(archived_record["observed"]["completion_state"], "observed_only")


class TestWorkflowCompletedHybridStoreGetsItsSubsetAndOverview(unittest.TestCase):
    """The completion phase builds an Indexed Variant Subset and the overview on a completed Hybrid Store (#333).

    As OGS-00011's will. The release is `prepare_hybrid_subset_signal_release`'s,
    built by this class alone: the shared Hybrid template leaves both post
    steps out, since every process that builds it would pay for them.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.built = run_single_id_completion("OGS-00131", prepare_hybrid_subset_signal_release)
        cls.addClassCleanup(cls.built["temp_dir"].cleanup)

    def test_the_completion_phase_builds_the_subset_and_the_overview_on_the_completed_store(self) -> None:
        """`hm3` holds the listed positions the Store holds, the held-out one now imputed."""
        res = self.built["release_result"]
        self.assertEqual(res.returncode, 0, f"The entry point failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        store_id, artifacts = self.built["store_id"], self.built["artifacts"]
        rec_dir = paths.records_dir(store_id, root=artifacts)
        staged = str(paths.partial_store_path(store_id, root=artifacts))
        for step in ("indexed-subset-hm3", "overview", "completion-indexed-subset-hm3", "completion-overview"):
            record = json.loads((rec_dir / f"{step}.json").read_text(encoding="utf-8"))
            self.assertTrue(record["success"], step)
            self.assertEqual(record["argv"][2], staged, step)
        store_p = paths.store_path(store_id, root=artifacts)
        self.assertEqual(json.loads((store_p / "manifest.json").read_text())["completion_state"], "reference_completed")
        q = query_store(store_p)
        try:
            rows = q.analysis(self.built["analysis_id"], indexed_subset="hm3")
            table = q.variants_table()
        finally:
            q.close()
        self.assertEqual(
            {
                table[int(index)]["alid"]: str(status)
                for index, status in zip(rows["variant_index"], rows["association_status"], strict=True)
            },
            {
                SIGNAL_ALID[i]: "imputed" if i == SIGNAL_HELD_OUT_INDEX else "observed"
                for i in HYBRID_SUBSET_INDEXES
            },
        )
        self.assertTrue((store_p / "overview.html").is_file())


class TestWorkflowCompletingAPublishedHybridStore(PublishedReleaseCopies):
    """A published Observed-Only Hybrid Store is completed later under its id, with no rebuild (#333, #305).

    The release is a copy of `hybrid_single_id_completion_template` as its
    run stood when `complete` started (`complete_started`, through
    `copy_snapshot`): the build phase had published the Observed-Only Store
    and nothing of the completion phase had run. That build phase is what the
    bundle plans without its `complete` block (`tests/plan`), so with the
    block taken out the copy is an accepted, published Observed-Only release
    with nothing left to do. The bundle then gains a `complete-hybrid` block,
    the one amendment ADR 0037 allows, and an unforced run completes the
    Store. `TestWorkflowCompletingAStoreBuiltBefore304` shows the same for a
    Dense Store the workflow built before #304. A subclass runs the same test
    on another layout's release: its template, layout and completion command
    (#336).
    """

    LAYOUT = "hybrid"
    COMMAND = "complete-hybrid"
    COMPLETION_RECORDS = TestWorkflowSingleIdHybridReferenceCompletion.COMPLETION_RECORDS

    @staticmethod
    def build_template() -> dict[str, Any]:
        return hybrid_single_id_completion_template()

    def test_the_amended_bundle_runs_only_the_completion_phase(self) -> None:
        import yaml

        template = self.build_template()
        store_id = template["store_id"]
        self.assertIsNotNone(template["complete_started"], "the template's run never reached `complete`")
        stores, artifacts = copy_snapshot(template["complete_started"], built=template, into=self.td)
        build_yaml_p = stores / store_id / "build.yaml"
        declared = yaml.safe_load(build_yaml_p.read_text(encoding="utf-8"))
        observed_only = {key: value for key, value in declared.items() if key != "complete"}
        build_yaml_p.write_text(yaml.safe_dump({**observed_only, "completion_state": "observed_only"}), encoding="utf-8")

        published = run_release([store_id, "--dry-run"], registry_root=stores, artifact_root=artifacts)
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        self.assertEqual([t for t in scheduled_targets(published.stdout) if t != store_id], [])

        amend_to_complete_own_store(
            stores, store_id, ld_panel=Path(declared["complete"]["options"]["ld-panel"]), command=self.COMMAND
        )
        store_dir = paths.store_dir(store_id, root=artifacts)
        build_phase_before = {
            p: (p.stat().st_mtime_ns, p.read_bytes())
            for directory in ("records", "work") for p in sorted((store_dir / directory).iterdir())
        }
        dry = run_release([store_id, "--dry-run"], registry_root=stores, artifact_root=artifacts)
        res = run_release([store_id], registry_root=stores, artifact_root=artifacts)

        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual([t for t in scheduled_targets(dry.stdout) if t != store_id], self.COMPLETION_RECORDS)
        self.assertNotIn("would refuse", dry.stdout)
        self.assertEqual(res.returncode, 0, f"the completion failed:\n{res.stdout}\n{res.stderr}")
        for p, (mtime, content) in build_phase_before.items():
            self.assertEqual((p.stat().st_mtime_ns, p.read_bytes()), (mtime, content), f"{p} was rewritten")
        rec_dir = paths.records_dir(store_id, root=artifacts)
        for step in self.COMPLETION_RECORDS:
            self.assertTrue(json.loads((rec_dir / f"{step}.json").read_text(encoding="utf-8"))["success"], step)
        manifest_doc = json.loads((paths.store_path(store_id, root=artifacts) / "manifest.json").read_text())
        self.assertEqual(
            (manifest_doc["primary_layout"], manifest_doc["completion_state"]), (self.LAYOUT, "reference_completed")
        )


class TestWorkflowSingleIdRaggedReferenceCompletion(unittest.TestCase):
    """A Ragged release completes its own Store under its id, end to end through the entry point (#336, ADR 0037).

    The release is `ragged_single_id_completion_template`'s, shaped as
    OGS-00006's: a GWAS-SSF source, a Top-Hit Index built as a post step, and
    an Analysis with no Trait position. One run builds and publishes the
    Observed-Only Store, completes it against the panel's one block, passes
    the Ragged Observed-View Check, which rebuilds that block from the panel
    as the Analysis's retained region, and publishes the completed Store at
    the same path. `TestWorkflowSingleIdReferenceCompletion` shows the rest of
    the single-id contract on a Dense release; this class asserts what a
    Ragged one changes. Every test only reads the template.
    """

    BUILD_PHASE_RECORDS = ["build", "top-hits", "validate", "register"]
    COMPLETION_RECORDS = ["complete", "completion-validate", "completion-observed-view-check", "completion-register"]

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = ragged_single_id_completion_template()
        cls.STORE_ID: str = cls.template["store_id"]
        cls.artifacts: Path = cls.template["artifacts"]
        cls.store_p: Path = paths.store_path(cls.STORE_ID, root=cls.artifacts)
        cls.rec_dir: Path = paths.records_dir(cls.STORE_ID, root=cls.artifacts)

    def archive(self) -> Path:
        archives = sorted((paths.store_dir(self.STORE_ID, root=self.artifacts) / "replaced").iterdir())
        self.assertEqual(len(archives), 1, archives)
        return archives[0]

    def record(self, step: str) -> dict[str, Any]:
        return json.loads((self.rec_dir / f"{step}.json").read_text(encoding="utf-8"))

    def test_one_run_publishes_the_observed_only_store_then_the_completed_store_under_its_id(self) -> None:
        """`complete-ragged` builds the Top-Hit Index inline, so the completion phase has no step for it."""
        import yaml

        res = self.template["release_result"]
        self.assertEqual(res.returncode, 0, f"The entry point failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertEqual(
            sorted(p.stem for p in self.rec_dir.glob("*.json")), sorted(self.BUILD_PHASE_RECORDS + self.COMPLETION_RECORDS)
        )
        for step in self.BUILD_PHASE_RECORDS + self.COMPLETION_RECORDS:
            self.assertTrue(self.record(step).get("success"), step)
        self.assertEqual(self.record("complete")["argv"][1], "complete-ragged")
        self.assertEqual(self.record("register")["published_store"], str(self.store_p))
        self.assertIsNone(self.record("register")["replaced_archive"], "the build phase publishes first")
        self.assertEqual(self.record("completion-register")["replaced_archive"], str(self.archive()))
        for leftover in (
            paths.partial_store_path(self.STORE_ID, root=self.artifacts),
            paths.backup_store_path(self.STORE_ID, root=self.artifacts),
            paths.publication_marker(self.STORE_ID, root=self.artifacts),
        ):
            self.assertFalse(leftover.exists(), leftover)

        manifest_doc = json.loads((self.store_p / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {key: manifest_doc[key] for key in ("primary_layout", "completion_state", "store_id", "release_id")},
            {
                "primary_layout": "ragged",
                "completion_state": "reference_completed",
                "store_id": self.STORE_ID,
                "release_id": f"{self.STORE_ID}-completed",
            },
        )
        record = yaml.safe_load((self.template["stores"] / self.STORE_ID / "validation.yaml").read_text(encoding="utf-8"))
        self.assertIn(record["status"], ("passed", "passed_with_warnings"))
        self.assertEqual(record["observed"]["n_variants"], len(SIGNAL_POSITIONS), "the panel adds the held-out position")

    def test_the_check_rebuilt_the_retained_region_from_the_bundles_panel_and_passed(self) -> None:
        """The check took the `complete` block's panel and ancestry and found the one imputed cell in the one region."""
        import yaml

        declared = yaml.safe_load((self.template["stores"] / self.STORE_ID / "build.yaml").read_text(encoding="utf-8"))
        ld_panel = declared["complete"]["options"]["ld-panel"]
        argv = self.record("completion-observed-view-check")["argv"]
        output = argv.index("--output")
        self.assertEqual(argv[output - 4 : output], ["--ld-panel", ld_panel, "--ancestry", "EUR"])

        verdict = json.loads(self.record("completion-observed-view-check")["stdout"])
        self.assertIs(verdict["passed"], True)
        self.assertEqual(verdict["failures"], [])
        self.assertEqual((verdict["ld_panel"]["path"], verdict["ld_panel"]["ancestry"]), (ld_panel, "EUR"))
        self.assertIn("imputed_outside_retained", verdict["failing_counters"])
        self.assertEqual(
            {name: verdict["totals"][name] for name in verdict["failing_counters"]},
            dict.fromkeys(verdict["failing_counters"], 0),
        )
        (analysis,) = verdict["sample"]["analyses"]
        self.assertEqual(
            {key: analysis[key] for key in ("analysis_id", "admitted", "blocks", "observed_in_source", "imputed")},
            {
                "analysis_id": self.template["analysis_id"],
                "admitted": True,
                "blocks": [f"1/{SIGNAL_POSITIONS[0]}-{SIGNAL_POSITIONS[-1]}"],
                "observed_in_source": len(SIGNAL_POSITIONS) - 1,
                "imputed": 1,
            },
        )
        # The fixture keeps SE `float16` for the check's exact SE comparison (`create_signal_fixture_ssf`).
        manifest_doc = json.loads((self.store_p / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest_doc["encoding"]["se"]["kind"], "float16")

    def test_the_held_out_position_is_imputed_and_every_observed_association_is_unchanged(self) -> None:
        """By ALID, z, se and eaf are those read before completion began; the panel's extra position is imputed.

        The held-out position is read twice: from the Analysis, and by variant,
        through the `by_variant` index `complete-ragged` writes and validation
        requires of a Ragged Store.
        """
        import numpy as np

        before = self.template["observed_before"]
        self.assertIsNotNone(before, "the template never read the Observed-Only Store")
        after = observed_view(self.store_p, self.template["analysis_id"])
        expected_alids = sorted(SIGNAL_ALID[i] for i in range(len(SIGNAL_POSITIONS)) if i != SIGNAL_HELD_OUT_INDEX)
        self.assertEqual(sorted(before), expected_alids)
        self.assertEqual(sorted(after), expected_alids)
        for alid in expected_alids:
            (z, se, eaf, status), (before_z, before_se, before_eaf, before_status) = after[alid], before[alid]
            self.assertEqual((z, se), (before_z, before_se), alid)
            self.assertTrue(eaf == before_eaf or (np.isnan(eaf) and np.isnan(before_eaf)), alid)
            self.assertEqual((status, before_status), ("observed", "observed"), alid)
        self.assertTrue(np.isnan(after[SIGNAL_ALID[RAGGED_NO_EAF_INDEX]][2]), "that position reports no frequency")

        held_out = SIGNAL_ALID[SIGNAL_HELD_OUT_INDEX]
        q = query_store(self.store_p)
        try:
            rows = q.analysis(self.template["analysis_id"])
            table = q.variants_table()
            by_variant = q.phewas(held_out)
        finally:
            q.close()
        self.assertEqual(
            {
                table[int(index)]["alid"]: str(status)
                for index, status in zip(rows["variant_index"], rows["association_status"], strict=True)
            },
            {alid: "imputed" if alid == held_out else "observed" for alid in SIGNAL_ALID},
        )
        self.assertEqual([str(status) for status in by_variant["association_status"]], ["imputed"])

    def test_the_replaced_store_is_deleted_and_its_records_are_archived(self) -> None:
        import yaml

        archive = self.archive()
        self.assertEqual(sorted(p.name for p in archive.iterdir()), ["records", "validation.yaml"])
        self.assertEqual(
            sorted(p.name for p in (archive / "records").iterdir()),
            ["completion-observed-view-check.json", "register.json"],
        )
        archived_record = yaml.safe_load((archive / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(archived_record["observed"]["completion_state"], "observed_only")
        self.assertEqual(archived_record["observed"]["n_variants"], len(SIGNAL_POSITIONS) - 1)


class TestWorkflowSingleIdRaggedFailingObservedViewCheck(TestWorkflowSingleIdFailingObservedViewCheck):
    """A Ragged completed Store that fails the Observed-View Check is not published either (#336, ADR 0037).

    The release is `ragged_single_id_completion_template`'s, and the cell
    perturbed is an observed Z of its Analysis. The Ragged check names its
    counters with no part prefix, as the Dense check does.
    """

    OBSERVED_ONLY_N_VARIANTS = len(SIGNAL_POSITIONS) - 1

    @staticmethod
    def build_template() -> dict[str, Any]:
        return ragged_single_id_completion_template()

    def tamper(self, staged_p: Path) -> None:
        """Add one unit to an observed Z of the staged Store, in its stored codes.

        opengwasdb has no plane API that writes a Ragged cell, so the code is
        written in place: one unit of z is the manifest's fixed-point scale.
        The position chosen has z 0, so the code stays in range.
        """
        import numpy as np
        from opengwasdb.store import arrays as store_arrays

        self.assertEqual(SIGNAL_Z[0], 0.0)
        found = query_store(staged_p).lookup([SIGNAL_ALID[0]], [f"test_analysis_{self.STORE_ID}"])
        self.assertEqual(list(found["association_status"]), ["observed"])
        scale = json.loads((staged_p / "manifest.json").read_text(encoding="utf-8"))["encoding"]["z"]["scale"]
        ragged = store_arrays.open_group(staged_p / "data.zarr" / "ragged", mode="r+")
        analysis = int(found["analysis_index"][0])
        start, end = (int(offset) for offset in ragged["offsets"][analysis : analysis + 2])
        (position,) = np.flatnonzero(ragged["variant_index"][start:end] == int(found["variant_index"][0])) + start
        ragged["z"][position] = ragged["z"][position] + scale


class TestWorkflowCompletingAPublishedRaggedStore(TestWorkflowCompletingAPublishedHybridStore):
    """A published Observed-Only Ragged Store is completed later under its id, with no rebuild (#336, #305).

    The release is `ragged_single_id_completion_template`'s. Its build phase
    also builds a Top-Hit Index, a post step the amendment check leaves out of
    its comparison.
    """

    LAYOUT = "ragged"
    COMMAND = "complete-ragged"
    COMPLETION_RECORDS = TestWorkflowSingleIdRaggedReferenceCompletion.COMPLETION_RECORDS

    @staticmethod
    def build_template() -> dict[str, Any]:
        return ragged_single_id_completion_template()


class TestWorkflowForcedRunOfACompletedRaggedRelease(TestWorkflowForcedRunOfACompletedSingleIdRelease):
    """`--config force=1` on a completed Ragged release rebuilds, publishes and completes it in one run (#336)."""

    PHASE_RECORDS = (
        TestWorkflowSingleIdRaggedReferenceCompletion.BUILD_PHASE_RECORDS
        + TestWorkflowSingleIdRaggedReferenceCompletion.COMPLETION_RECORDS
    )

    @staticmethod
    def build_template() -> dict[str, Any]:
        return ragged_single_id_completion_template()


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
            "association_coverage": "gwas",
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
            "association_coverage": "gwas",
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


class TestEntryPointSnakemakeTemporaryFiles(unittest.TestCase):
    """The entry point's Snakemake runs with TMPDIR on the artifact root, never /tmp (#312)."""

    def test_snakemake_gets_a_tmpdir_under_the_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = str(root / ".tmp")
            seen: dict[str, Any] = {}

            class FakeProc:
                def wait(self) -> int:
                    return 0

            def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
                seen.update(kwargs["env"])
                return FakeProc()

            with mock.patch.object(release.subprocess, "Popen", fake_popen), \
                    mock.patch.object(release.run, "_supervised_command", lambda a, *r: a), \
                    mock.patch.object(release.os, "read", lambda fd, n: b""):
                release._run_snakemake(["snakemake"], (), root=root)
            for var in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(seen.get(var), expected, var)
            self.assertTrue((root / ".tmp").is_dir())

    def test_a_dry_run_names_an_existing_directory_on_the_artifact_root_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = release.snakemake_env(root, create=False)
            for var in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(env[var], str(root), var)
            self.assertEqual(sorted(root.rglob("*")), [], "a dry run wrote under the artifact root")
            (root / ".tmp").mkdir()
            self.assertEqual(release.snakemake_env(root, create=False)["TMPDIR"], str(root / ".tmp"))

    def test_clearing_snakemake_metadata_runs_with_the_artifact_root_tmpdir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seen: dict[str, Any] = {}

            def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                seen.update(kwargs["env"])
                return subprocess.CompletedProcess(argv, 0, "", "")

            inv = release.parse_invocation(["OGS-00001"])
            with mock.patch.object(release.subprocess, "run", fake_run):
                release._cleanup_metadata(inv, [root / "x"], root=root)
            self.assertEqual(seen.get("TMPDIR"), str(root / ".tmp"))


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


class TestWorkflowCarriedColumnMap(unittest.TestCase):
    """A carried Column Map is staged for the build and checked before it is read (#292).

    Two integrations of the DAG: the staging and the entry point's refusal, and
    a retried build whose staged copy changed. The resource-level cases are
    `test_column_map_resources.py`. Only the staging and the refusals run here:
    building through a map is OpenGWASDB's.
    """

    STORE_ID = "OGS-00096"
    MAP = b'column_map_version = 1\nname = "mvp-map"\n'
    FILE = "mvp-map.toml"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        td = Path(self.temp_dir.name)
        self.stores = td / "stores"
        self.stores.mkdir()
        self.artifacts = td / "artifacts"
        self.artifacts.mkdir()
        directory = create_dense_fixture_store(self.stores, self.STORE_ID, artifact_root=self.artifacts)
        recipe = yaml.safe_load((directory / "build.yaml").read_text())
        recipe["build"]["options"]["source-reader-capability"] = "opengwasdb.column-map"
        recipe["column_map"] = {
            "name": "mvp-map", "sha256": hashlib.sha256(self.MAP).hexdigest(), "file": self.FILE,
        }
        (directory / "build.yaml").write_text(yaml.safe_dump(recipe), encoding="utf-8")
        (directory / "column_maps").mkdir()
        self.carried = directory / "column_maps" / self.FILE
        self.carried.write_bytes(self.MAP)
        self.staged = paths.column_map_path(self.STORE_ID, self.FILE, root=self.artifacts)
        self.build_record = paths.record_path(self.STORE_ID, "build", root=self.artifacts)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_the_dry_run_schedules_staging_the_map_is_staged_and_the_entry_point_refuses_a_changed_one(self) -> None:
        loaded = bundle.load(self.STORE_ID, registry_root=self.stores)
        build = plan(loaded, artifact_root=self.artifacts)[0]
        self.assertEqual(build.argv[build.argv.index("--column-map") + 1], str(self.staged))
        dry = run_snakemake(
            [str(self.build_record)], registry_root=self.stores, artifact_root=self.artifacts, dry_run=True
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("rule column_map:", dry.stdout)

        staged = run_snakemake([str(self.staged)], registry_root=self.stores, artifact_root=self.artifacts)
        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertEqual(self.staged.read_bytes(), self.MAP)

        self.carried.write_bytes(self.MAP + b"# edited\n")
        res = run_release([self.STORE_ID], registry_root=self.stores, artifact_root=self.artifacts)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Column Map 'mvp-map'", res.stdout + res.stderr)
        self.assertFalse(paths.records_dir(self.STORE_ID, root=self.artifacts).exists())

    def test_a_retried_build_whose_staged_copy_changed_is_refused_by_name_before_it_runs(self) -> None:
        staged = run_snakemake([str(self.staged)], registry_root=self.stores, artifact_root=self.artifacts)
        self.assertEqual(staged.returncode, 0, staged.stderr)
        # Snakemake sees the staged copy as up to date: only the step's own check can catch this.
        self.staged.write_bytes(self.MAP + b"# edited after staging\n")

        res = run_snakemake([str(self.build_record)], registry_root=self.stores, artifact_root=self.artifacts)

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("the staged Column Map 'mvp-map'", res.stderr)
        self.assertFalse(self.build_record.exists())
        self.assertFalse(paths.store_path(self.STORE_ID, root=self.artifacts).exists())

# ---------------------------------------------------------------------------
# Association Coverage as a build input (#341, ADR 0040, opengwas/opengwasdb#298)
# ---------------------------------------------------------------------------

#: The coverage the Ragged fixture declares. `signals_only` is not any layout's
#: default, so a build that reported it cannot have got it from the layout: the
#: value is the release's own declaration or the test proves nothing.
RAGGED_FIXTURE_COVERAGE: str = "signals_only"


def create_ragged_fixture_store(
    stores: Path,
    store_id: str = "OGS-00130",
    *,
    artifact_root: Path,
    coverage: str = RAGGED_FIXTURE_COVERAGE,
) -> Path:
    """Write a small Ragged SSF Release Bundle and the sources its build reads.

    Two Analyses over a three-variant GWAS-SSF source each, under
    `<artifact-root>/<store_id>/source/`, which is where `plan()` points
    `build-ragged-ssf`'s `FILTERED_DIR`. Only the columns the builder and the
    shared Analysis schema require are written: this fixture exists to prove
    which Association Coverage reaches the builder and comes back out of the
    Store (ADR 0040), not to exercise a real source.
    """
    bundle_dir = stores / store_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "release.yaml").write_text(
        yaml.safe_dump({
            "store_id": store_id,
            "label": f"fixture-ragged-{coverage}",
            "association_coverage": coverage,
            "status": "accepted",
            "source_snapshot_id": "fixture-snapshot-v1",
            "created_at": "2026-10-01T00:00:00Z",
            "description": "Fixture Ragged Store Release",
            "generator": {"commands": ["fixtures/generate.py"]},
        }),
        encoding="utf-8",
    )
    (bundle_dir / "build.yaml").write_text(
        yaml.safe_dump({
            "store_id": store_id,
            "layout": "ragged",
            "completion_state": "observed_only",
            "build": {"command": "build-ragged-ssf", "options": {}},
            "post": {"top_hits": False, "overview": False, "validate": True},
        }),
        encoding="utf-8",
    )

    columns = [
        "analysis_index", "analysis_id", "analysis_label", "trait_chr", "trait_bp",
        "sample_size", "tissue", "context", "mhc", "source_file",
        "stored_effect_scale", "sample_size_kind", "sample_size_scope",
        "original_effect_scale", "original_sd_method", "ancestry_assignment_method",
    ]
    rows = [
        {
            "analysis_index": str(i),
            "analysis_id": f"FIXTURE_{i + 1}",
            "analysis_label": f"Fixture trait {i + 1}",
            "trait_chr": "1",
            "trait_bp": str(50_000 + i * 10_000),
            "sample_size": "1000",
            "tissue": "plasma",
            "context": "fixture",
            "mhc": "false",
            "source_file": f"FIXTURE_{i + 1}.tsv.gz",
            "stored_effect_scale": "sd",
            "sample_size_kind": "total",
            "sample_size_scope": "analysis_level",
            "original_effect_scale": "sd",
            "original_sd_method": "declared_standardised",
            "ancestry_assignment_method": "af_assigned",
        }
        for i in range(2)
    ]
    (bundle_dir / "analyses.tsv").write_text(
        "\n".join(["\t".join(columns), *("\t".join(row[c] for c in columns) for row in rows)]) + "\n",
        encoding="utf-8",
    )

    source = paths.source_dir(store_id, root=artifact_root)
    source.mkdir(parents=True)
    ssf_columns = [
        "chromosome", "base_pair_location", "rsid", "effect_allele", "other_allele",
        "beta", "standard_error", "effect_allele_frequency",
    ]
    for i in range(2):
        lines = ["\t".join(ssf_columns)]
        lines += [
            f"1\t{100_000 + j * 1_000}\trs{i}{j}\tA\tG\t0.1\t0.05\t0.3"
            for j in range(3)
        ]
        with gzip.open(source / f"FIXTURE_{i + 1}.tsv.gz", "wt", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return bundle_dir


class TestRaggedReleaseBuildsAtItsDeclaredCoverage(unittest.TestCase):
    """A Ragged release publishes a Store at the coverage its bundle declares (#341).

    End to end at fixture scale: the value in `release.yaml` is the value the
    builder is given, the value the Store's own manifest carries, and the value
    the Validation Record records.
    """

    STORE_ID: str = "OGS-00130"

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        root = Path(cls.temp_dir.name)
        cls.stores, cls.artifacts = root / "stores", root / "artifacts"
        cls.stores.mkdir()
        cls.artifacts.mkdir()
        create_ragged_fixture_store(cls.stores, cls.STORE_ID, artifact_root=cls.artifacts)
        cls.result = run_release([cls.STORE_ID], registry_root=cls.stores, artifact_root=cls.artifacts)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_the_release_is_built_published_and_recorded_at_its_declared_coverage(self) -> None:
        """One fixture build, all of it: argv, the published Store, and the record (#341).

        End to end at fixture scale: the value in `release.yaml` is the value
        the builder is given, the value the Store's own manifest carries, and
        the value the Validation Record records. One method, so the fixture is
        built once however the suite is sharded.
        """
        self.assertEqual(self.result.returncode, 0, self.result.stdout + self.result.stderr)
        store_p = paths.store_path(self.STORE_ID, root=self.artifacts)
        self.assertTrue(store_p.is_dir())

        # The builder got the declared value, from `release.yaml` and nowhere else.
        record = json.loads(
            paths.record_path(self.STORE_ID, "build", root=self.artifacts).read_text(encoding="utf-8")
        )
        for argv in (record["planned_argv"], record["argv"]):
            self.assertIn("--association-coverage", argv)
            self.assertEqual(argv[argv.index("--association-coverage") + 1], RAGGED_FIXTURE_COVERAGE)

        # And the published Store says it too: `info` reads the manifest, not the bundle.
        info = subprocess.run(
            [run.get_opengwasdb_executable(), "info", str(store_p), "--format", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(info.stdout)["association_coverage"], RAGGED_FIXTURE_COVERAGE)

        validation = yaml.safe_load((self.stores / self.STORE_ID / "validation.yaml").read_text(encoding="utf-8"))
        self.assertIn(validation["status"], ("passed", "passed_with_warnings"))
        self.assertEqual(validation["observed"]["association_coverage"], RAGGED_FIXTURE_COVERAGE)


# `dev` before #341: the workflow whose `plan()` names no `--association-coverage`,
# so the Records of a Store it published hold no such token. It stays reachable
# from `dev` and `main` however #341 is merged; CI fetches full history for this
# suite to have it.
PRE_341_REVISION: str = "e42066b"

_PRE_341_PUBLISHED: dict[str, Any] | None = None


def effective_snakemake_metadata(output: Path) -> Path:
    """Where the current code reads what Snakemake recorded about the job that wrote `output`.

    `release.snakemake_directory()` is the checkout root for an ordinary run and
    a private directory when the test runner gives the process one
    (`tests/run_all_tests.py`). A fixture that hands the current code an earlier
    run's metadata must put it where *this* run will look, or the dry run finds
    none and proves nothing about the rerun triggers.
    """
    encoded = base64.urlsafe_b64encode(str(output).encode()).decode()
    max_len = os.pathconf(REPO_ROOT, "PC_NAME_MAX") - 1
    chunks = [encoded[i:i + max_len] for i in range(0, len(encoded), max_len)]
    directory = release.snakemake_directory() / ".snakemake" / "metadata"
    return directory.joinpath(*(["@" + c for c in chunks[:-1]] + chunks[-1:]))


def _without_declared_coverage(argv: list[str], declared: str) -> list[str]:
    """`argv` with the one `--association-coverage <declared>` pair #341 composes removed.

    The plan a published Ragged Store's record was held to differs from this
    branch's plan by that pair alone, so the recorded argv is the plan minus it.
    """
    flag = plan_module.ASSOCIATION_COVERAGE_FLAG
    positions = [index for index, token in enumerate(argv) if token == f"--{flag}"]
    assert len(positions) == 1, f"expected one --{flag} in {argv}"
    index = positions[0]
    assert argv[index + 1] == declared, (argv, declared)
    return [*argv[:index], *argv[index + 2:]]


def pre_341_published_store() -> dict[str, Any]:
    """A Ragged Store built and published by the pre-#341 workflow, once per process.

    The Store is the fixture `create_ragged_fixture_store` writes, built by the
    workflow as it was at `PRE_341_REVISION`, run from a `git archive` of
    itself with its own `ogstores` ahead of this checkout's editable install.
    The archive's `.snakemake` is this checkout's, so the current code reads
    that run's Snakemake metadata, as an operator's checkout would after a pull.

    Two things the archived revision cannot do for itself are supplied at the
    edge, and nothing else about it changes:

    - the runtime pin is repinned to the one this environment provides, that
      code's own way to run another runtime (stores#218), as
      `pre_304_completion_template` does; and
    - `build-ragged-ssf` requires an Association Coverage at the pinned runtime
      (opengwas/opengwasdb#298), which code that predates #341 cannot name. A
      small `opengwasdb` on `PATH` ahead of the real one adds the flag to the
      two Ragged builders' argv, so the *builder* can run. The Store's records
      are still that run's own: `run.py` records the argv it planned, which
      names no such token, and which is the state under test. The shim sits in
      an install prefix whose `site-packages` is this environment's, so the
      runtime guard proves the real revision from the real metadata.
    """
    global _PRE_341_PUBLISHED
    if _PRE_341_PUBLISHED is not None:
        return _PRE_341_PUBLISHED

    present = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{PRE_341_REVISION}^{{commit}}"], capture_output=True
    )
    if present.returncode != 0:
        raise RuntimeError(
            f"revision {PRE_341_REVISION} is not in this clone, so no Store can be built with the "
            "workflow as it was before #341. A shallow clone lacks it: fetch the full history "
            "(`git fetch --unshallow`; CI checks this suite out with `fetch-depth: 0`)."
        )

    store_id = "OGS-00131"
    temp_dir = tempfile.TemporaryDirectory()
    root = Path(temp_dir.name)
    checkout = root / "pre-341"
    checkout.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", PRE_341_REVISION], capture_output=True, check=True
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(checkout, filter="data")

    from ogstores import runtime as runtime_module

    runtime_py = checkout / "src" / "ogstores" / "runtime.py"
    pin = re.compile(r'^PINNED_OPENGWASDB_REVISION: str = "[0-9a-f]{40}"$', re.MULTILINE)
    text = runtime_py.read_text(encoding="utf-8")
    if len(pin.findall(text)) != 1:
        raise RuntimeError(f"{runtime_py} no longer states its pin as this test expects")
    runtime_py.write_text(
        pin.sub(f'PINNED_OPENGWASDB_REVISION: str = "{runtime_module.PINNED_OPENGWASDB_REVISION}"', text),
        encoding="utf-8",
    )

    shim_root = root / "runtime-shim"
    (shim_root / "bin").mkdir(parents=True)
    python_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    (shim_root / "lib" / python_dir).mkdir(parents=True)
    (shim_root / "lib" / python_dir / "site-packages").symlink_to(
        Path(sys.prefix) / "lib" / python_dir / "site-packages", target_is_directory=True
    )
    real_opengwasdb = Path(run.get_opengwasdb_executable())
    shim = shim_root / "bin" / "opengwasdb"
    shim.write_text(
        f"""#!{sys.executable}
# Test fixture only: the pre-#341 code cannot declare a coverage, and the pinned
# runtime requires one for the Ragged builders (opengwas/opengwasdb#298).
import os
import sys

coverage = os.environ["OGSTORES_FIXTURE_COVERAGE"]
argv = sys.argv[1:]
if argv[:1] in (["build-ragged-ssf"], ["build-ragged-besd"]):
    argv = [argv[0], "--association-coverage", coverage, *argv[1:]]
os.execv({str(real_opengwasdb)!r}, [{str(real_opengwasdb)!r}, *argv])
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    # The archived run's Snakemake metadata must land where the current code
    # reads it, which is this run's working directory, not always the checkout.
    shared_metadata = release.snakemake_directory() / ".snakemake"
    shared_metadata.mkdir(parents=True, exist_ok=True)
    (checkout / ".snakemake").symlink_to(shared_metadata, target_is_directory=True)

    old_code = checkout / "src"
    old_env = {
        **os.environ,
        "PYTHONPATH": str(old_code),
        "PATH": f"{shim_root / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "OGSTORES_FIXTURE_COVERAGE": RAGGED_FIXTURE_COVERAGE,
    }
    imported = subprocess.run(
        [sys.executable, "-c", "import ogstores; print(ogstores.__file__)"],
        cwd=checkout, env=old_env, capture_output=True, text=True, check=True,
    ).stdout.strip()

    stores = root / "stores"
    artifacts = root / "artifacts"
    stores.mkdir()
    artifacts.mkdir()
    create_ragged_fixture_store(stores, store_id, artifact_root=artifacts)
    old_result = subprocess.run(
        [
            sys.executable, str(checkout / "workflow" / "release.py"), store_id, "--cores", "1",
            "--config", f"registry_root={stores}", f"artifact_root={artifacts}",
        ],
        cwd=checkout, env=old_env, capture_output=True, text=True,
    )
    store_dir = paths.store_dir(store_id, root=artifacts)
    published_before = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for directory in ("records", "work")
        for p in sorted((store_dir / directory).iterdir())
    }
    dry_result = run_release([store_id, "--dry-run"], registry_root=stores, artifact_root=artifacts)
    release_result = run_release([store_id], registry_root=stores, artifact_root=artifacts)
    after = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for directory in ("records", "work")
        for p in sorted((store_dir / directory).iterdir())
    }

    _PRE_341_PUBLISHED = {
        "temp_dir": temp_dir,
        "store_id": store_id,
        "imported": imported,
        "old_code": old_code,
        "old_result": old_result,
        "stores": stores,
        "artifacts": artifacts,
        "published_before": published_before,
        "published_after": after,
        "dry_result": dry_result,
        "release_result": release_result,
    }
    return _PRE_341_PUBLISHED


class TestWorkflowKeepingAStorePublishedBefore341(PublishedReleaseCopies):
    """A Ragged Store published before #341 stays up to date, with default rerun triggers.

    #341 puts `--association-coverage` into every Ragged build step, so the
    argv `plan()` produces for such a release changed. This proves the change
    cannot rebuild or refuse the Store it was not built with: Snakemake's
    up-to-date decision reads the rule's own code, the planned input list and
    the record's mtime, and no part of the argv, so the archived revision's
    run is left alone and the current one executes no job.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = pre_341_published_store()
        cls.STORE_ID: str = cls.template["store_id"]

    def test_the_current_workflow_neither_rebuilds_nor_refuses_it(self) -> None:
        """The whole published-before-#341 scenario in one method, so the fixture is built once.

        That the Store was built by the archived code, that its `build` record
        names no coverage, that the current run has Snakemake metadata to
        compare against, that the dry run and the run execute no job, that no
        record or work file is rewritten, and that the plan really did change.
        """
        # 1. The archived revision built and published it, and its own records say so.
        old = self.template["old_result"]
        self.assertEqual(old.returncode, 0, f"the pre-#341 build failed:\n{old.stdout}\n{old.stderr}")
        self.assertTrue(
            self.template["imported"].startswith(str(self.template["old_code"])),
            f"the old entry point imported {self.template['imported']}",
        )
        for name in ("build.json", "validate.json", "register.json"):
            self.assertTrue(
                (paths.records_dir(self.STORE_ID, root=self.template["artifacts"]) / name).is_file(), name
            )

        # 2. The state under test: a published Store whose argv predates the argument.
        record = json.loads(
            paths.record_path(self.STORE_ID, "build", root=self.template["artifacts"]).read_text(encoding="utf-8")
        )
        self.assertNotIn("--association-coverage", record["argv"])
        self.assertNotIn("--association-coverage", record["planned_argv"])

        # 3. What makes the guard mean something: this run has metadata to compare
        # against, so "Nothing to be done" is about triggers, not about missing
        # provenance (with none, Snakemake decides by mtime alone).
        for name in ("build.json", "validate.json"):
            record_p = paths.records_dir(self.STORE_ID, root=self.template["artifacts"]) / name
            self.assertTrue(
                effective_snakemake_metadata(record_p).is_file(),
                f"the archived run kept no Snakemake metadata for {name} where this run reads it",
            )

        # 4. The dry run schedules no job, and the real run refuses nothing and does nothing.
        dry = self.template["dry_result"]
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("Nothing to be done", dry.stdout)
        self.assertEqual(scheduled_targets(dry.stdout), [])
        self.assertNotIn("would refuse", dry.stdout)

        res = self.template["release_result"]
        self.assertEqual(res.returncode, 0, f"the run failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("nothing to be done", res.stdout + res.stderr)
        self.assertEqual(
            self.template["published_after"],
            self.template["published_before"],
            "the run rewrote a record or a work file",
        )

        # 5. The change is real: this branch plans the flag, and the plan is the
        # recorded argv with exactly those two tokens added. What keeps the Store
        # up to date is that no rerun trigger reads an argv token at all, not
        # that the argv is unchanged.
        loaded = bundle.load(self.STORE_ID, registry_root=self.template["stores"])
        planned = plan(loaded, artifact_root=self.template["artifacts"])[0]
        self.assertIn("--association-coverage", planned.argv)
        executed = register.normalize_executed_argv_for_staging(
            record["argv"], self.STORE_ID, artifact_root=self.template["artifacts"]
        )
        self.assertEqual(
            _without_declared_coverage(planned.argv, RAGGED_FIXTURE_COVERAGE),
            executed,
            "the plan and the record differ by more than the added flag",
        )


class TestWorkflowCompletingAStorePublishedBefore341(PublishedReleaseCopies):
    """A Ragged Store published before #341 may gain its own completion, and nothing else.

    Adding a single-id completion to a published Store is the one amendment ADR
    0037 allows, and the amendment check holds each Store-writing step to the
    argv its own record planned. #341 added a token to every Ragged build step,
    so a Store published before it plans an argv its record does not hold. ADR
    0040 records the one exception: a record that names no coverage matches a
    plan that differs by exactly the declaration-derived
    `--association-coverage <release.yaml value>` pair, because the flag only
    names what the old builder hard-coded and the Store's data does not change
    (the owner's pre-release decision on opengwas/opengwasdb#298). A record that
    names another coverage is refused, and so is every other difference.

    The single-id completion gate itself is #232's (#336) and is in this tree,
    so a Ragged release plans its own completion and the comparison is reached
    without a patch. Nothing is stubbed: the Store, its records and its derived
    build manifest are the archived run's.
    """

    COMPLETE_BLOCK: dict[str, Any] = {
        "command": "complete-ragged",
        "options": {"ld-panel": "/panel", "ancestry": "EUR"},
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = pre_341_published_store()
        cls.STORE_ID: str = cls.template["store_id"]

    def amended_copy(
        self,
        *,
        declared: str = RAGGED_FIXTURE_COVERAGE,
        recorded_coverage: str | None = None,
        build_options: dict[str, Any] | None = None,
    ) -> tuple[bundle.Bundle, Path]:
        """A copy of the published release whose bundle gained its `complete` block.

        `recorded_coverage` appends a coverage the *record* names, for the case
        that must still be refused; `build_options` replaces the recipe's build
        options, for the same reason.
        """
        stores, artifacts = self.published_copy(self.template["stores"], self.template["artifacts"])
        build_p = stores / self.STORE_ID / "build.yaml"
        document = yaml.safe_load(build_p.read_text(encoding="utf-8"))
        document["completion_state"] = "reference_completed"
        document["complete"] = dict(self.COMPLETE_BLOCK)
        if build_options is not None:
            document["build"]["options"] = build_options
        build_p.write_text(yaml.safe_dump(document), encoding="utf-8")

        release_p = stores / self.STORE_ID / "release.yaml"
        release_doc = yaml.safe_load(release_p.read_text(encoding="utf-8"))
        release_doc["association_coverage"] = declared
        release_p.write_text(yaml.safe_dump(release_doc), encoding="utf-8")

        if recorded_coverage is not None:
            record_p = paths.record_path(self.STORE_ID, "build", root=artifacts)
            record = json.loads(record_p.read_text(encoding="utf-8"))
            record["planned_argv"] = [*record["planned_argv"], "--association-coverage", recorded_coverage]
            run._write_record_atomically(record, record_p)
        return bundle.load(self.STORE_ID, registry_root=stores), artifacts

    def test_a_legacy_store_may_gain_its_completion_but_no_other_amendment(self) -> None:
        """The exception, its boundary, and everything it does not cover, in one method."""
        # 1. The Store published before #341 passes the amendment check: the
        #    plan differs from its record by the declared flag alone.
        loaded, artifacts = self.amended_copy()
        self.assertEqual(
            amendment.bundle_changes(loaded, artifacts),
            [],
            "a published Ragged Store may gain its completion without a rebuild",
        )
        self.assertIn("--association-coverage", plan(loaded, artifact_root=artifacts)[0].argv)

        # 2. A record that names another coverage is still refused, naming both.
        mismatched, mismatched_artifacts = self.amended_copy(recorded_coverage="cis")
        mismatch = amendment.bundle_changes(mismatched, mismatched_artifacts)
        self.assertEqual(len(mismatch), 1, mismatch)
        self.assertIn("association-coverage", mismatch[0])
        self.assertIn("cis", mismatch[0])
        self.assertIn(RAGGED_FIXTURE_COVERAGE, mismatch[0])

        # 3. Any other change to the Store-writing step is still an amendment.
        changed, changed_artifacts = self.amended_copy(build_options={"stored-effect-scale": "log_or"})
        other = amendment.bundle_changes(changed, changed_artifacts)
        self.assertEqual(len(other), 1, other)
        self.assertIn("stored-effect-scale", other[0])


if __name__ == "__main__":
    unittest.main()
