#!/usr/bin/env python3
"""Real-data end-to-end proof for the HM3 Indexed Variant Subset (stores #213).

Epic opengwas/opengwasdb-stores#214 makes a named ``hm3`` Indexed Variant Subset
the default derived artifact for every Dense-component Store Release. #208
promoted the maintained HapMap3 GRCh38 ALID list, #209 taught the release
planner to declare and run a subset Step, #211 migrated the existing bundles,
and #212 added the release-side benchmark report. This script proves those work
together end to end on real data, through the supported release pipeline
(``workflow/release.py`` -> Snakemake -> ``ogstores.run``) -- not a one-off
command.

The approach is a **fresh faithful reflink copy under a dedicated artifact
root**, read-only against the authoritative artifact root (stores#211's
no-primary-rebuild constraint):

* The authoritative OGS-00009 store is copied with ``cp --reflink=auto`` so the
  subset is built against the real statistics planes, byte-identical to the
  release, while ``/data/opengwasdb/stores/OGS-00009`` is never written.
* The pipeline stages every Step into ``store.opengwasdb.partial`` and the
  terminal ``register`` job publishes it, so the copy is staged there and no
  final store exists at the scratch root (no forced replacement, no archive).
* ``register`` compares planned argv (scratch root) with each Step record's
  executed argv. The two prerequisite records that describe the already-built
  store (``build.json``, ``top-hits.json``) are therefore **relocated** to the
  scratch root by a strict, disclosed artifact-root prefix substitution; the
  before/after sha256 of every relocated record is recorded in the proof
  artifact. Nothing else about those records changes.

The capability runtime for this historical proof is the core checkout
``/home/gh13047/repo/opengwasdb-worktrees/hm3-272`` at ``1c2c85d`` (a descendant
of ``4819a2ee``, opengwas/opengwasdb#264), installed as a VCS dependency so the
Step records and the installed package report its commit. The proof predates
stores#218's pin adoption and deliberately used a separate checkout; the
supported release entry point now runs the whole workflow on the tracked
current-format pin. The proof Store stays format 0.1.0 because the subset
builder is format-preserving.

Usage (phases are independent and resumable):

    python resources/scripts/hm3-pipeline-proof.py setup
    python resources/scripts/hm3-pipeline-proof.py plan
    python resources/scripts/hm3-pipeline-proof.py run
    python resources/scripts/hm3-pipeline-proof.py collect
    python resources/scripts/hm3-pipeline-proof.py benchmark
    python resources/scripts/hm3-pipeline-proof.py collect && ... # after benchmark

The JSON artifact and the generated Markdown report are generated, never
hand-edited. See docs/hm3-pipeline-proof.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

STORE_ID = "OGS-00009"
ISSUE = "opengwas/opengwasdb-stores#213"
EPIC = "opengwas/opengwasdb-stores#214"

AUTHORITATIVE_ROOT = Path("/data/opengwasdb/stores")
SCRATCH_ROOT = Path("/data/opengwasdb/work/213-hm3-proof")
CAPABILITY_CHECKOUT = Path("/home/gh13047/repo/opengwasdb-worktrees/hm3-272")
CAPABILITY_FLOOR = "4819a2ee"
CAP_ENV_BIN = SCRATCH_ROOT / "cap-env" / "bin"
RUNNER_PY = REPO_ROOT / ".pixi" / "envs" / "workflow" / "bin" / "python"

OUTPUT_JSON = REPO_ROOT / "docs" / "benchmark-output" / "opengwasdb_213_hm3_pipeline_proof.json"
OUTPUT_MD = REPO_ROOT / "docs" / "hm3-pipeline-proof.md"

#: Records that describe the already-built Store and must be relocated.
RELOCATED_RECORDS = ("build", "top-hits")
#: Records that the proof run must produce fresh, so they are not copied.
FRESH_RECORDS = ("overview", "validate", "register", "indexed-subset-hm3")

_SUBSET_ATTRS = Path("data.zarr/indexed_subsets/hm3/.zattrs")


class ProofError(RuntimeError):
    """A proof precondition or measurement failed; never swallowed."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_signature(path: Path) -> dict[str, Any]:
    """Cheap recursive identity for a built Store: counts, bytes, newest mtime."""
    files = 0
    total_size = 0
    total_blocks = 0
    newest_ns = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            st = os.lstat(os.path.join(dirpath, name))
            files += 1
            total_size += st.st_size
            total_blocks += st.st_blocks
            newest_ns = max(newest_ns, st.st_mtime_ns)
    return {
        "files": files,
        "apparent_bytes": total_size,
        "allocated_bytes": total_blocks * 512,
        "newest_mtime_ns": newest_ns,
    }


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_command(argv: list[str], *, log_path: Path, env: dict[str, str] | None = None,
                cwd: Path | None = None, timeout: float | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(argv)}\n\n")
        log.flush()
        proc = subprocess.run(
            argv, cwd=str(cwd or REPO_ROOT), env=env, stdout=log,
            stderr=subprocess.STDOUT, text=True, timeout=timeout,
        )
        log.write(f"\n[exit {proc.returncode}]\n")
    return proc.returncode


def git_revision(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ProofError(f"cannot read git revision of {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def stores_commit() -> dict[str, str]:
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    branch = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if head.returncode != 0:
        raise ProofError("cannot read the stores repository revision")
    return {"commit": head.stdout.strip(), "branch": branch.stdout.strip()}


def runner_env() -> dict[str, str]:
    """The capability `opengwasdb` first on PATH, then the stores runner env."""
    if not (CAP_ENV_BIN / "opengwasdb").is_file():
        raise ProofError(
            f"capability runtime not found at {CAP_ENV_BIN / 'opengwasdb'}; "
            "create it with the documented VCS install of the hm3-272 checkout"
        )
    env = dict(os.environ)
    env["PATH"] = f"{CAP_ENV_BIN}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return env


# --------------------------------------------------------------------------- #
# Phase: setup
# --------------------------------------------------------------------------- #


def phase_setup(args: argparse.Namespace) -> None:
    authoritative = AUTHORITATIVE_ROOT / STORE_ID
    store_source = authoritative / "store.opengwasdb"
    if not store_source.is_dir():
        raise ProofError(f"authoritative store not found: {store_source}")

    registry = SCRATCH_ROOT / "registry" / STORE_ID
    artifacts = SCRATCH_ROOT / "artifacts" / STORE_ID
    evidence = SCRATCH_ROOT / "evidence"

    if args.reset:
        # Never remove the capability environment: only the generated proof state.
        for stale in (SCRATCH_ROOT / "registry", SCRATCH_ROOT / "artifacts"):
            if stale.exists():
                shutil.rmtree(stale)
        for stale in (SCRATCH_ROOT / "evidence",):
            if stale.exists():
                shutil.rmtree(stale)
    evidence.mkdir(parents=True, exist_ok=True)

    # The authoritative identity, captured before any copy is made.
    before = {
        "root": str(authoritative),
        "store": tree_signature(store_source),
        "manifest_sha256": sha256_file(store_source / "manifest.json"),
        "analyses_sha256": sha256_file(store_source / "analyses.tsv"),
        "build_record_sha256": sha256_file(authoritative / "records" / "build.json"),
        "top_hits_record_sha256": sha256_file(authoritative / "records" / "top-hits.json"),
        "captured_at": utc_now(),
    }
    write_json(evidence / "authoritative-before.json", before)

    # A scratch registry copy, so register writes validation.yaml outside the repo.
    if registry.exists():
        shutil.rmtree(registry)
    registry.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / "stores" / STORE_ID, registry, symlinks=False)

    # The faithful artifact copy: the store is staged as `.partial`, no final store.
    if artifacts.exists():
        shutil.rmtree(artifacts)
    artifacts.mkdir(parents=True)

    def copy(src: Path, dst: Path) -> None:
        result = subprocess.run(["cp", "-a", "--reflink=auto", str(src), str(dst)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise ProofError(f"copy {src} -> {dst} failed: {result.stderr.strip()}")

    copy(store_source, artifacts / "store.opengwasdb.partial")
    copy(authoritative / "work", artifacts / "work")
    records = artifacts / "records"
    records.mkdir()
    for name in RELOCATED_RECORDS:
        copy(authoritative / "records" / f"{name}.json", records / f"{name}.json")

    # Relocate the prerequisite records: a strict artifact-root prefix substitution.
    old_root = str(AUTHORITATIVE_ROOT).encode()
    new_root = str(SCRATCH_ROOT / "artifacts").encode()
    relocation = []
    for name in RELOCATED_RECORDS:
        record_p = records / f"{name}.json"
        original = record_p.read_bytes()
        relocated = original.replace(old_root, new_root)
        replacements = original.count(old_root)
        if replacements == 0:
            raise ProofError(f"{record_p} contains no {old_root!r} prefix to relocate")
        if relocated == original:
            raise ProofError(f"{record_p} relocation changed nothing")
        record_p.write_bytes(relocated)
        relocation.append({
            "record": f"{name}.json",
            "replacements": replacements,
            "sha256_before": hashlib.sha256(original).hexdigest(),
            "sha256_after": hashlib.sha256(relocated).hexdigest(),
        })
    if not any(p.name == "store.opengwasdb" for p in artifacts.iterdir()):
        pass
    else:
        raise ProofError("a final store.opengwasdb must not exist at the scratch artifact root")

    # Freshness: bundle analyses.tsv < work/analyses.tsv < build record < top-hits
    # record, so Snakemake schedules only the new subset Step and its successors.
    bundle_analyses = registry / "analyses.tsv"
    manifest_tsv = artifacts / "work" / "analyses.tsv"
    exclusions = artifacts / "work" / "analyses.exclusions.json"
    base = time.time() + 10
    for offset, path in enumerate(
        [bundle_analyses, manifest_tsv, exclusions,
         records / "build.json", records / "top-hits.json"]
    ):
        ns = int((base + offset * 10) * 1e9)
        os.utime(path, ns=(ns, ns))
    if not exclusions.is_file():
        raise ProofError(f"missing second build_manifest output: {exclusions}")

    write_json(evidence / "setup.json", {
        "store_id": STORE_ID,
        "authoritative_store": str(store_source),
        "scratch_registry": str(registry),
        "scratch_artifacts": str(artifacts),
        "copy_kind": "reflink",
        "staged_as": "store.opengwasdb.partial",
        "final_store_present": False,
        "relocation": relocation,
        "relocation_rule": (
            "strict byte substitution of the authoritative artifact-root prefix "
            f"{AUTHORITATIVE_ROOT} -> {SCRATCH_ROOT / 'artifacts'} in the recorded "
            "argv/planned_argv/inputs/outputs/record_path of the build and top-hits "
            "records only; every other byte is unchanged"
        ),
        "authoritative_before": before,
        "set_up_at": utc_now(),
    })
    print(f"setup: staged {store_source} -> {artifacts / 'store.opengwasdb.partial'}")
    print(f"setup: relocated {len(relocation)} records; registry at {registry}")


# --------------------------------------------------------------------------- #
# Phase: plan
# --------------------------------------------------------------------------- #


def planned_steps() -> dict[str, Any]:
    from ogstores import bundle as bundle_module
    from ogstores.plan import plan

    registry = SCRATCH_ROOT / "registry"
    artifacts = SCRATCH_ROOT / "artifacts"
    loaded = bundle_module.load(STORE_ID, registry_root=registry)
    errors = loaded.check() if callable(getattr(loaded, "check", None)) else None
    steps = plan(loaded, artifact_root=artifacts)
    return {
        "bundle_errors": errors,
        "steps": [
            {
                "name": step.name,
                "planned_argv": [str(a) for a in step.argv],
                "inputs": [str(i) for i in step.inputs],
                "outputs": [str(o) for o in step.outputs],
            }
            for step in steps
        ],
    }


def parse_scheduled_steps(dry_run_output: str) -> dict[str, Any]:
    scheduled: list[str] = []
    for line in dry_run_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("wildcards:"):
            fields = dict(
                part.split("=", 1) for part in stripped[len("wildcards:"):].split(", ") if "=" in part
            )
            step = fields.get("step")
            if step:
                scheduled.append(step)
    rules: dict[str, int] = {}
    lines = dry_run_output.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "job          count":
            for row in lines[index + 2:]:
                stripped = row.strip()
                if not stripped or stripped.startswith("total"):
                    break
                parts = stripped.split()
                if len(parts) == 2 and parts[1].isdigit():
                    rules[parts[0]] = int(parts[1])
            break
    return {"steps": sorted(set(scheduled)), "rules": rules}


def phase_plan(args: argparse.Namespace) -> None:
    evidence = SCRATCH_ROOT / "evidence"
    registry = SCRATCH_ROOT / "registry"
    artifacts = SCRATCH_ROOT / "artifacts"

    steps = planned_steps()
    write_json(evidence / "plan.json", steps)

    argv = [
        str(RUNNER_PY), str(REPO_ROOT / "workflow" / "release.py"), STORE_ID,
        "--config", f"artifact_root={artifacts}", f"registry_root={registry}",
        "--dry-run", "--cores", "2",
    ]
    code = run_command(argv, log_path=evidence / "plan-dry-run.log", env=runner_env())
    output = (evidence / "plan-dry-run.log").read_text(encoding="utf-8")
    scheduled = parse_scheduled_steps(output)
    write_json(evidence / "plan-scheduled.json", {"exit_code": code, **scheduled})

    expected = ["indexed-subset-hm3", "overview", "validate"]
    if code != 0:
        raise ProofError(f"release --dry-run exited {code}; see plan-dry-run.log")
    for name in expected:
        if name not in scheduled["steps"]:
            raise ProofError(f"dry run did not schedule {name!r}: {scheduled}")
    if scheduled["rules"].get("register", 0) < 1:
        raise ProofError(f"dry run did not schedule register: {scheduled}")
    for name in ("build", "top-hits"):
        if name in scheduled["steps"]:
            raise ProofError(f"dry run unexpectedly scheduled {name!r}: {scheduled}")
    if scheduled["rules"].get("build_manifest", 0) > 0:
        raise ProofError(f"dry run unexpectedly scheduled build_manifest: {scheduled}")
    print(f"plan: scheduled steps = {scheduled['steps']}; rules = {scheduled['rules']}")


# --------------------------------------------------------------------------- #
# Phase: run
# --------------------------------------------------------------------------- #


def phase_run(args: argparse.Namespace) -> None:
    evidence = SCRATCH_ROOT / "evidence"
    registry = SCRATCH_ROOT / "registry"
    artifacts = SCRATCH_ROOT / "artifacts"
    argv = [
        str(RUNNER_PY), str(REPO_ROOT / "workflow" / "release.py"), STORE_ID,
        "--config", f"artifact_root={artifacts}", f"registry_root={registry}",
        "--cores", str(args.cores),
    ]
    started = time.monotonic()
    code = run_command(argv, log_path=evidence / "release-run.log", env=runner_env())
    elapsed = time.monotonic() - started
    write_json(evidence / "run.json", {
        "exit_code": code,
        "total_seconds": round(elapsed, 3),
        "command": argv,
        "finished_at": utc_now(),
    })
    if code != 0:
        raise ProofError(f"release run exited {code}; see release-run.log")
    print(f"run: pipeline exited 0 in {elapsed:,.1f} s")


# --------------------------------------------------------------------------- #
# Phase: benchmark
# --------------------------------------------------------------------------- #


def phase_benchmark(args: argparse.Namespace) -> None:
    evidence = SCRATCH_ROOT / "evidence"
    registry = SCRATCH_ROOT / "registry"
    artifacts = SCRATCH_ROOT / "artifacts"
    argv = [
        "pixi", "run", "-e", "dev", "python",
        str(REPO_ROOT / "resources" / "scripts" / "release-benchmark-report.py"),
        "--opengwasdb-checkout", str(CAPABILITY_CHECKOUT),
        "--registry-root", str(registry),
        "--artifact-root", str(artifacts),
        "--store", STORE_ID,
        "--hm3-only",
        "--reps", "5",
        "--output", str(evidence / "hm3-release-benchmark.json"),
        "--report", str(evidence / "hm3-release-benchmark.md"),
    ]
    code = run_command(argv, log_path=evidence / "benchmark.log", cwd=CAPABILITY_CHECKOUT)
    if code != 0:
        raise ProofError(f"release benchmark report exited {code}; see benchmark.log")
    print("benchmark: wrote hm3-release-benchmark.{json,md}")


# --------------------------------------------------------------------------- #
# Phase: collect
# --------------------------------------------------------------------------- #


def record_summary(path: Path) -> dict[str, Any]:
    document = read_json(path)
    return {
        "success": document.get("success"),
        "exit_code": document.get("exit_code"),
        "elapsed_seconds": document.get("elapsed_seconds"),
        "opengwasdb_rev": document.get("opengwasdb_rev"),
        "opengwasdb_version": document.get("opengwasdb_version"),
        "planned_argv": document.get("planned_argv"),
        "executed_argv": document.get("argv"),
        "stdout": (document.get("stdout") or "").strip(),
        "stderr": (document.get("stderr") or "").strip()[:2000],
    }


def phase_collect(args: argparse.Namespace) -> None:
    evidence = SCRATCH_ROOT / "evidence"
    registry = SCRATCH_ROOT / "registry" / STORE_ID
    artifacts = SCRATCH_ROOT / "artifacts" / STORE_ID
    proof_store = artifacts / "store.opengwasdb"

    setup = read_json(evidence / "setup.json")
    plan_doc = read_json(evidence / "plan.json")
    scheduled = read_json(evidence / "plan-scheduled.json")
    run_doc = read_json(evidence / "run.json")

    if not proof_store.is_dir():
        raise ProofError(f"proof store was not published at {proof_store}")

    manifest = read_json(proof_store / "manifest.json")
    subset_attrs = read_json(proof_store / _SUBSET_ATTRS)
    overview_html = (proof_store / "overview.html").read_text(encoding="utf-8", errors="replace")
    validation_yaml_path = registry / "validation.yaml"
    validation_yaml = validation_yaml_path.read_text(encoding="utf-8") if validation_yaml_path.is_file() else ""
    validation_document = yaml.safe_load(validation_yaml) if validation_yaml else {}
    observed = (validation_document or {}).get("observed") or {}
    bundle_sha256 = {
        name: sha256_file(registry / name)
        for name in ("release.yaml", "build.yaml", "analyses.tsv")
        if (registry / name).is_file()
    }

    records = {}
    for name in ("build", "top-hits", "indexed-subset-hm3", "overview", "validate", "register"):
        record_p = artifacts / "records" / f"{name}.json"
        records[name] = record_summary(record_p)

    validate_stdout = records["validate"]["stdout"]
    validate_verdict = None
    try:
        validate_verdict = json.loads(validate_stdout.splitlines()[-1]) if validate_stdout else None
    except json.JSONDecodeError:
        validate_verdict = None

    # Affirmative overview evidence: the tab and the hm3 row.
    overview_tab = "Indexed Variant Subsets" in overview_html
    overview_hm3 = "hm3" in overview_html

    # The authoritative root must be unchanged.
    authoritative = AUTHORITATIVE_ROOT / STORE_ID
    store_source = authoritative / "store.opengwasdb"
    after = {
        "store": tree_signature(store_source),
        "manifest_sha256": sha256_file(store_source / "manifest.json"),
        "analyses_sha256": sha256_file(store_source / "analyses.tsv"),
        "build_record_sha256": sha256_file(authoritative / "records" / "build.json"),
        "top_hits_record_sha256": sha256_file(authoritative / "records" / "top-hits.json"),
        "captured_at": utc_now(),
    }
    before = setup["authoritative_before"]
    primary_untouched = all(before[key] == after[key] for key in (
        "store", "manifest_sha256", "analyses_sha256",
        "build_record_sha256", "top_hits_record_sha256",
    ))
    write_json(evidence / "authoritative-after.json", after)

    benchmark = {}
    benchmark_json = evidence / "hm3-release-benchmark.json"
    if benchmark_json.is_file():
        document = read_json(benchmark_json)
        stores = document.get("stores") or []
        entry = next(
            (candidate for candidate in stores if candidate.get("store_id") == STORE_ID), None
        )
        benchmark = {
            "artifact": str(benchmark_json),
            "entry": entry,
            "core": document.get("core"),
            "measured_at": document.get("measured_at"),
            "summary": document.get("summary"),
        }

    hm3_timing = (benchmark.get("entry") or {}).get("hm3_extract", {})

    subset_record = records["indexed-subset-hm3"]
    build_seconds = subset_record.get("elapsed_seconds")

    runtime_rev = subset_record.get("opengwasdb_rev")
    if runtime_rev != git_revision(CAPABILITY_CHECKOUT):
        raise ProofError(
            f"step record opengwasdb_rev {runtime_rev!r} does not match the capability "
            f"checkout revision {git_revision(CAPABILITY_CHECKOUT)!r}"
        )

    artifact = {
        "issue": ISSUE,
        "epic": EPIC,
        "title": "Real-data HM3 Indexed Variant Subset pipeline proof",
        "generated_at": utc_now(),
        "generator": {
            "script": "resources/scripts/hm3-pipeline-proof.py",
            "script_sha256": sha256_file(Path(__file__)),
            "stores": stores_commit(),
        },
        "approach": {
            "kind": "fresh-faithful-reflink-copy",
            "staged_as": setup["staged_as"],
            "copy_kind": setup["copy_kind"],
            "dedicated_artifact_root": setup["scratch_artifacts"],
            "scratch_registry": setup["scratch_registry"],
            "authoritative_root_read_only": True,
            "primary_untouched": primary_untouched,
            "not_an_in_place_append": (
                "the pipeline stages every Step into store.opengwasdb.partial and register "
                "publishes it; an in-place run would force-replace and archive the "
                "authoritative store, so the proof runs against a reflink copy instead"
            ),
            "record_relocation": {
                "rule": setup["relocation_rule"],
                "records": setup["relocation"],
                "disclosure": (
                    "build.json and top-hits.json describe the already-built Store and had to "
                    "name the proof artifact root for register's argv-drift check. Only the "
                    "artifact-root prefix was substituted; the before/after sha256 are recorded."
                ),
            },
        },
        "runtime": {
            "checkout": str(CAPABILITY_CHECKOUT),
            "commit": git_revision(CAPABILITY_CHECKOUT),
            "capability_floor": CAPABILITY_FLOOR,
            "install": "opengwasdb @ git+file://...@<commit> (VCS install; direct_url records the commit)",
            "step_record_opengwasdb_rev": runtime_rev,
            "step_record_opengwasdb_version": subset_record.get("opengwasdb_version"),
        },
        "maintained_resource": {
            "resource_id": "hapmap3-hg38",
            "location": "/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt",
            "alid_sha256": "4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398",
            "row_count": 1206903,
        },
        "release": {
            "store_id": STORE_ID,
            "release_id": manifest.get("release_id", STORE_ID),
            "layout": manifest.get("primary_layout"),
            "completion_state": manifest.get("completion_state"),
            "format_version": manifest.get("format_version"),
            "n_analyses": manifest.get("n_analyses", observed.get("n_analyses")),
            "n_variants": manifest.get("n_variants", observed.get("n_variants")),
            "bundle_sha256": bundle_sha256,
            "authoritative_store": setup["authoritative_store"],
            "proof_store": str(proof_store),
            "bundle_errors": plan_doc.get("bundle_errors"),
            "indexed_subsets_declaration": (
                yaml.safe_load((registry / "build.yaml").read_text())["indexed_subsets"]
            ),
        },
        "plan": {
            "steps": [step["name"] for step in plan_doc["steps"]],
            "planned_argv": {step["name"]: step["planned_argv"] for step in plan_doc["steps"]},
            "dry_run_exit_code": scheduled["exit_code"],
            "dry_run_scheduled_steps": scheduled["steps"],
            "dry_run_scheduled_rules": scheduled["rules"],
        },
        "subset": {
            "name": subset_attrs.get("indexed_subset_name"),
            "namespace": str(_SUBSET_ATTRS.parent),
            "statistic_profile": subset_attrs.get("statistic_profile"),
            "reference_assembly": subset_attrs.get("reference_assembly"),
            "requested_count": subset_attrs.get("requested_count"),
            "resolved_count": subset_attrs.get("resolved_count"),
            "absent_count": subset_attrs.get("absent_count"),
            "n_subset_variants": subset_attrs.get("n_subset_variants"),
            "n_analyses": subset_attrs.get("n_analyses"),
            "input_sha256": subset_attrs.get("input_sha256"),
            "source_store_id": subset_attrs.get("source_store_id"),
            "source_release_id": subset_attrs.get("source_release_id"),
            "source_format_version": subset_attrs.get("source_format_version"),
            "builder_version": subset_attrs.get("builder_version"),
            "created_at": subset_attrs.get("created_at"),
            "physical_bytes": (lambda p: p.stat().st_blocks * 512 + sum(
                e.stat().st_blocks * 512 for e in p.rglob("*")))(proof_store / _SUBSET_ATTRS.parent),
        },
        "records": records,
        "pipeline": {
            "command": run_doc.get("command"),
            "exit_code": run_doc.get("exit_code"),
            "total_seconds": run_doc.get("total_seconds"),
        },
        "overview": {
            "present": overview_hm3,
            "has_indexed_subsets_tab": overview_tab,
            "sha256": sha256_file(proof_store / "overview.html"),
        },
        "validation": {
            "status": "passed" if (records["validate"]["success"] and validate_verdict
                                   and validate_verdict.get("ok")) else "failed",
            "record_success": records["validate"]["success"],
            "exit_code": records["validate"]["exit_code"],
            "verdict": validate_verdict,
            "exhaustive": (
                "standalone validate decodes every indexed cell; the build's own publication "
                "gate is sampled but the release validate Step is exhaustive"
            ),
            "validation_yaml_sha256": (
                sha256_file(validation_yaml_path) if validation_yaml_path.is_file() else None
            ),
        },
        "benchmark": {
            "report_script": "resources/scripts/release-benchmark-report.py",
            "registry_issue": "opengwas/opengwasdb-stores#212",
            "core_seam": "benchmarks._query_shapes.measure_hm3_extract",
            "core_issue": "opengwas/opengwasdb#272",
            "core_checkout": str(CAPABILITY_CHECKOUT),
            **benchmark,
        },
        "timings": {
            "headline_hm3_build_seconds": build_seconds,
            "headline_hm3_extract_median_ms": hm3_timing.get("median_ms"),
            "hm3_extract_p95_ms": hm3_timing.get("p95_ms"),
            "hm3_extract_first_read_ms": hm3_timing.get("first_read_ms"),
            "hm3_extract_result_count": hm3_timing.get("result_count"),
            "overview_seconds": records["overview"]["elapsed_seconds"],
            "validate_seconds": records["validate"]["elapsed_seconds"],
            "register_seconds": records["register"]["elapsed_seconds"],
            "pipeline_total_seconds": run_doc.get("total_seconds"),
        },
        "references": {
            "core_272": "https://github.com/opengwas/opengwasdb/pull/272",
            "prior_indexed_subset_evidence": (
                "opengwasdb docs/benchmark-output/opengwasdb_267_indexed_subset_benchmark.json "
                "(#267/#270; the 187.53 s post-#270 build is the comparison point)"
            ),
            "release_benchmark_report": "docs/benchmark-output/opengwasdb_hm3_release_benchmark.{json,md}",
        },
    }
    write_json(evidence / "proof.json", artifact)
    write_json(OUTPUT_JSON, artifact)
    write_markdown(artifact, OUTPUT_MD)
    print(f"collect: wrote {OUTPUT_JSON} and {OUTPUT_MD}")
    print(f"collect: primary_untouched={primary_untouched} validation={artifact['validation']['status']}")


def write_markdown(artifact: dict[str, Any], path: Path) -> None:
    subset = artifact["subset"]
    timings = artifact["timings"]
    validation = artifact["validation"]
    overview = artifact["overview"]
    release = artifact["release"]
    runtime = artifact["runtime"]
    rows = artifact["approach"]["record_relocation"]["records"]

    def fmt(value: Any) -> str:
        return "" if value is None else f"{value:,}" if isinstance(value, int) else str(value)

    lines = [
        "# Real-data HM3 pipeline proof (stores #213)",
        "",
        "Generated by `resources/scripts/hm3-pipeline-proof.py` (stores#213, epic#214). "
        "**Regenerate, do not hand-edit.**",
        "",
        f"- issue: `{artifact['issue']}` (epic `{artifact['epic']}`)",
        f"- generated at: `{artifact['generated_at']}`",
        f"- stores: `{artifact['generator']['stores']['commit']}` "
        f"({artifact['generator']['stores']['branch']})",
        f"- proof driver: `{artifact['generator']['script']}` "
        f"sha256 `{artifact['generator']['script_sha256']}`",
        f"- capability runtime: `{runtime['checkout']}` @ `{runtime['commit']}` "
        f"(floor `{runtime['capability_floor']}`)",
        f"- proof Store: `{release['proof_store']}` (format {release['format_version']})",
        f"- authoritative Store: `{release['authoritative_store']}` (read-only; "
        f"untouched={artifact['approach']['primary_untouched']})",
        "",
        "## Headline",
        "",
        f"- HM3 subset build (pipeline Step): **{fmt(timings['headline_hm3_build_seconds'])} s**",
        f"- HM3 extraction warm median: **{fmt(timings['headline_hm3_extract_median_ms'])} ms** "
        f"(p95 {fmt(timings['hm3_extract_p95_ms'])} ms, "
        f"first read {fmt(timings['hm3_extract_first_read_ms'])} ms, "
        f"results {fmt(timings['hm3_extract_result_count'])})",
        f"- validation: **{validation['status']}** (exit {validation['exit_code']})",
        f"- overview: **{'present' if overview['present'] else 'absent'}** "
        f"(Indexed Variant Subsets tab: {overview['has_indexed_subsets_tab']})",
        "",
        "## Subset counts and identity",
        "",
        "| field | value |",
        "|:---|:---|",
    ]
    for key in (
        "name", "namespace", "statistic_profile", "reference_assembly",
        "requested_count", "resolved_count", "absent_count", "n_subset_variants",
        "n_analyses", "input_sha256", "source_store_id", "source_release_id",
        "source_format_version", "builder_version", "created_at", "physical_bytes",
    ):
        lines.append(f"| {key} | `{fmt(subset.get(key))}` |")
    lines += [
        "",
        "## Pipeline plan and records",
        "",
        f"- planned steps: `{', '.join(artifact['plan']['steps'])}`",
        f"- dry-run scheduled steps: `{', '.join(artifact['plan']['dry_run_scheduled_steps'])}` "
        f"(rules `{artifact['plan']['dry_run_scheduled_rules']}`)",
        f"- pipeline exit code: {artifact['pipeline']['exit_code']} in "
        f"{fmt(artifact['pipeline']['total_seconds'])} s",
        "",
        "| step | success | exit | seconds | opengwasdb rev |",
        "|:---|:---|---:|---:|:---|",
    ]
    for name, record in artifact["records"].items():
        lines.append(
            f"| {name} | {record['success']} | {record['exit_code']} | "
            f"{fmt(record['elapsed_seconds'])} | `{record['opengwasdb_rev']}` |"
        )
    lines += [
        "",
        "`build` and `top-hits` were not re-run: their records are the relocated, "
        "historical evidence of the original Store build (the `f8125322` runtime), so "
        "their `seconds` are not part of this proof run's wall time. The proof run's "
        "own steps are `indexed-subset-hm3`, `overview`, `validate` and `register`, all "
        "on the capability runtime `1c2c85d`.",
        "",
        "## Record relocation (disclosed)",
        "",
        "`build.json` and `top-hits.json` describe the already-built authoritative Store. "
        "`register` compares each Step record's executed argv with the plan, so those two "
        "records were relocated to the proof artifact root by a strict prefix substitution. "
        "Everything else is byte-identical.",
        "",
        "| record | replacements | sha256 before | sha256 after |",
        "|:---|---:|:---|:---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['record']} | {row['replacements']} | `{row['sha256_before']}` | "
            f"`{row['sha256_after']}` |"
        )
    lines += [
        "",
        "## Benchmark (core opengwasdb#272 seam, release report stores#212)",
        "",
        f"- core seam: `{artifact['benchmark']['core_seam']}`",
        f"- checkout: `{artifact['benchmark'].get('core_checkout')}` "
        f"@ `{(artifact['benchmark'].get('core') or {}).get('revision')}`",
        f"- report artifact: `{artifact['benchmark'].get('artifact')}`",
        "",
        "> HM3 is an optional derived artifact. Its absence is operational evidence, not a "
        "Store validation failure; a present, valid subset whose extraction raises propagates.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=["setup", "plan", "run", "benchmark", "collect", "all"])
    parser.add_argument("--cores", default="2")
    parser.add_argument("--reset", action="store_true",
                        help="remove the scratch root before setup")
    args = parser.parse_args(argv)

    try:
        if args.phase in ("setup", "all"):
            phase_setup(args)
        if args.phase in ("plan", "all"):
            phase_plan(args)
        if args.phase in ("run", "all"):
            phase_run(args)
        if args.phase in ("benchmark", "all"):
            phase_benchmark(args)
        if args.phase in ("collect", "all"):
            phase_collect(args)
    except ProofError as exc:
        print(f"proof: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
