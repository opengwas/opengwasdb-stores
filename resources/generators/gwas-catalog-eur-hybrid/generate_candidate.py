#!/usr/bin/env python3
"""Phase B candidate generator for the EBI GWAS Catalog eur-hybrid release (issue #153).

One operator entry point turns a frozen Source Inventory into a candidate Release
Bundle, without ever invoking Phase A or building a Store:

    pixi run generate-candidate OGS-00011 \
      --config resources/generators/gwas-catalog-eur-hybrid/config-full.yaml \
      --cores 64 \
      --resume

Stages, in order (``--stage``, default ``all``):

    preflight   prove the frozen inventory before any association row is read (#151)
    prepare     derive the canonical resolver manifest from the frozen exact paths
    resolve     invoke `opengwasdb resolve-analyses` (owns the pool and checkpoints)
    verify      account every resolver record; fail on missing/stale/duplicate/extra
    emit        apply release policy, render the bundle, bundle.check(), publish atomically

The per-stage form exists so coarse dependency wiring can drive the same code; the
operator normally runs ``all``. The registry owns membership, method tiers,
thresholds and exclusions; ``opengwasdb`` owns the statistics.

Usage:
    pixi run generate-candidate OGS-00011 --config <config> --cores 64 --resume
    pixi run generate-candidate OGS-00011 --config <config> --cores 64 --resume --stage emit
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
for extra in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from resources.generators.lib.candidate_workflow import (  # noqa: E402
    DEFAULT_RESOLVER_BIN,
    RECEIPT_FILENAME,
    STAGING_DIRNAME,
    CandidateConfiguration,
    CandidateError,
    CandidateFiles,
    account_records,
    apply_release_policy,
    build_candidate_tables,
    build_resolution_receipt,
    check_staged_candidate,
    cleanup_staging,
    derive_resolver_manifest,
    load_candidate_configuration,
    now_utc,
    publish_candidate,
    read_candidate_metadata,
    read_resolution_receipt,
    render_build_yaml,
    render_release_yaml,
    render_resolver_manifest,
    render_validation_yaml,
    resolver_argv,
    run_resolver,
    sha256_file,
    sha256_text,
    stage_candidate,
    validate_candidate_analyses,
    verify_records,
    write_resolution_receipt,
)
from resources.generators.lib.source_inventory import (  # noqa: E402
    InventoryError,
    PreflightConfigError,
    preflight,
    read_inventory,
    render_preflight_summary,
)
from ogstores.paths import require_valid_store_id  # noqa: E402

FAMILY_DIR = "resources/generators/gwas-catalog-eur-hybrid"
DEFAULT_CONFIG = f"{FAMILY_DIR}/config-full.yaml"
STAGES = ("preflight", "prepare", "resolve", "verify", "emit", "all")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("store_id", help="Candidate Store Release id, e.g. OGS-00011")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--cores", type=int, help="worker count (default: config runtime.cores)")
    parser.add_argument("--resume", action="store_true", help="reuse unchanged successful resolver records")
    parser.add_argument("--stage", choices=STAGES, default="all")
    parser.add_argument("--resolver", default=DEFAULT_RESOLVER_BIN, help=argparse.SUPPRESS)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--registry-root", help="registry stores/ directory (default: <repo>/stores)")
    parser.add_argument("--work-root", help="run records/logs root (default: config output.work_root)")
    parser.add_argument("--inventory", help="override the inventory path the config declares")
    parser.add_argument("--provenance", help="override the provenance sidecar path")
    parser.add_argument("--candidates", help="override the candidate metadata table path")
    parser.add_argument("--created-at", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


class Pipeline:
    """Shared state for one candidate run across the stages."""

    def __init__(
        self, args: argparse.Namespace, repo_root: Path, operator_command: str
    ) -> None:
        self.args = args
        self.repo_root = repo_root
        self.store_id = require_valid_store_id(args.store_id)
        self.config: CandidateConfiguration = load_candidate_configuration(
            self._config_path(), repo_root
        )
        self.registry_root = (
            Path(args.registry_root) if args.registry_root else repo_root / "stores"
        )
        self.work_root = Path(args.work_root) if args.work_root else self.config.base.work_root
        self.run_root = self.work_root / self.store_id
        self.records_dir = self.run_root / "resolver" / "records"
        self.resolver_manifest_path = self.run_root / "resolver" / "analyses.tsv"
        self.resolver_log_path = self.run_root / "resolver" / "resolve.log"
        self.receipt_path = self.run_root / "resolver" / RECEIPT_FILENAME
        self.preflight_report_path = (
            self.run_root / "preflight" / f"{self.config.base.inventory_snapshot_id}.json"
        )
        self.cores = args.cores if args.cores is not None else self.config.base.cores
        # The executed command log starts with the operator invocation; the
        # resolver argv is appended when it actually runs.
        self.commands: list[str] = [operator_command]

    def _config_path(self) -> Path:
        path = Path(self.args.config)
        return path if path.is_absolute() else self.repo_root / path

    @property
    def inventory_path(self) -> Path:
        return Path(self.args.inventory) if self.args.inventory else self.config.base.inventory_path

    @property
    def provenance_path(self) -> Path:
        return (
            Path(self.args.provenance)
            if self.args.provenance
            else self.config.base.inventory_provenance_path
        )

    @property
    def candidates_path(self) -> Path:
        return (
            Path(self.args.candidates)
            if self.args.candidates
            else self.config.base.candidates_path
        )


def stage_preflight(pipeline: Pipeline) -> dict:
    inventory_path = pipeline.inventory_path
    if not inventory_path.is_file():
        raise InventoryError(
            f"frozen source inventory not found: {inventory_path}; freeze it first with "
            "'pixi run inventory-freeze'"
        )
    rows = read_inventory(inventory_path)
    result = preflight(
        inventory_path=inventory_path,
        provenance_path=pipeline.provenance_path,
        rows=rows,
        config=replace(pipeline.config.base, work_root=pipeline.work_root),
        repo_root=pipeline.repo_root,
        cores=pipeline.cores,
    )
    report_path = pipeline.preflight_report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result.report, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(render_preflight_summary(result.report))
    print(f"Report: {report_path}")
    if not result.ok:
        raise CandidateError(
            "preflight failed: "
            + "; ".join(result.failures)
            + f". See {report_path}"
        )
    pipeline.preflight_report_path = report_path
    return result.report


def stage_prepare(pipeline: Pipeline) -> tuple[list, dict]:
    rows = read_inventory(pipeline.inventory_path)
    ready_ids = [row.analysis_id for row in rows if row.ready]
    metadata = read_candidate_metadata(pipeline.candidates_path, ready_ids)
    manifest = derive_resolver_manifest(rows, pipeline.config, metadata)
    pipeline.resolver_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline.resolver_manifest_path.write_text(
        render_resolver_manifest(manifest), encoding="utf-8"
    )
    print(
        f"Resolver manifest: {pipeline.resolver_manifest_path} "
        f"({len(manifest)} ready Analyses)"
    )
    return manifest, metadata


def stage_resolve(pipeline: Pipeline, manifest: list) -> None:
    argv = resolver_argv(
        resolver_bin=pipeline.args.resolver,
        manifest_path=pipeline.resolver_manifest_path,
        records_dir=pipeline.records_dir,
        config=pipeline.config,
        cores=pipeline.cores,
        resume=pipeline.args.resume,
    )
    pipeline.commands.append(" ".join(argv))
    print("Running: " + " ".join(argv))
    run = run_resolver(argv, cwd=pipeline.repo_root, log_path=pipeline.resolver_log_path)
    if run.returncode != 0:
        raise CandidateError(
            f"resolver exited {run.returncode}; see {run.log_path}"
        )
    print(f"Resolver log: {run.log_path}")

    # A successful exit is not enough: account the records, then bind them to the
    # contract they were resolved under. A later standalone verify/emit is only
    # trustworthy against this receipt.
    records, failures = account_records(manifest, pipeline.records_dir)
    if failures:
        raise CandidateError(
            f"resolver exited 0 but {len(failures)} record(s) are not accountable; refusing "
            "to bind a receipt:\n  - " + "\n  - ".join(failures)
        )
    index_path = pipeline.records_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    receipt = build_resolution_receipt(
        manifest_rows=manifest,
        config=pipeline.config,
        manifest_path=pipeline.resolver_manifest_path,
        records_dir=pipeline.records_dir,
        argv=argv,
        index=index,
    )
    write_resolution_receipt(pipeline.receipt_path, receipt)
    print(f"Resolution receipt: {pipeline.receipt_path} ({len(records)} records bound)")


def stage_verify(pipeline: Pipeline, manifest: list) -> tuple[list, dict]:
    records, failures = verify_records(
        manifest,
        pipeline.records_dir,
        config=pipeline.config,
        manifest_path=pipeline.resolver_manifest_path,
        receipt_path=pipeline.receipt_path,
    )
    if failures:
        raise CandidateError(
            f"{len(failures)} resolver accounting failure(s); refusing to finalise:\n  - "
            + "\n  - ".join(failures)
        )
    index_path = pipeline.records_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return records, index


def stage_emit(
    pipeline: Pipeline,
    manifest: list,
    metadata: dict,
    records: list,
    index: dict,
) -> Path:
    rows = read_inventory(pipeline.inventory_path)
    outcomes = apply_release_policy(rows, pipeline.config, metadata, {
        record["analysis_id"]: record for record in records
    })
    tables = build_candidate_tables(
        inventory_rows=rows,
        outcomes=outcomes,
        config=pipeline.config,
        index_summary=index,
    )
    schema_errors = validate_candidate_analyses(tables.analyses_tsv)
    if schema_errors:
        raise CandidateError(
            "analyses.tsv failed the pinned OpenGWASDB Analysis schema:\n  - "
            + "\n  - ".join(schema_errors)
        )

    inventory_sha256 = _sha256_file(pipeline.inventory_path)
    created_at = pipeline.args.created_at or now_utc()
    generator_version = "sha256:" + sha256_text(
        (pipeline.repo_root / FAMILY_DIR / "generate_candidate.py").read_bytes()
    )
    commands = list(dict.fromkeys(pipeline.commands))
    try:
        receipt = read_resolution_receipt(pipeline.receipt_path)
    except CandidateError:
        receipt = None
    if isinstance(receipt, dict):
        resolver_block = receipt.get("resolver")
        recorded_argv = resolver_block.get("argv") if isinstance(resolver_block, dict) else None
        if isinstance(recorded_argv, list) and recorded_argv:
            commands = list(dict.fromkeys([*commands, " ".join(str(t) for t in recorded_argv)]))
    receipt_sha256 = (
        sha256_file(pipeline.receipt_path) if pipeline.receipt_path.is_file() else None
    )

    files = CandidateFiles(
        release_yaml=render_release_yaml(
            store_id=pipeline.store_id,
            config=pipeline.config,
            tables=tables,
            commands=commands,
            created_at=created_at,
            inventory_sha256=inventory_sha256,
            preflight_report=pipeline.preflight_report_path,
            index_summary=index,
            generator_version=generator_version,
            resolver_receipt_path=pipeline.receipt_path,
            resolver_receipt_sha256=receipt_sha256,
        ),
        build_yaml=render_build_yaml(pipeline.store_id, pipeline.config),
        analyses_tsv=tables.analyses_tsv,
        validation_yaml=render_validation_yaml(
            tables=tables,
            index_summary=index,
            validated_at=created_at,
            validator_name="resources/generators/gwas-catalog-eur-hybrid/generate_candidate.py",
        ),
        source_readiness_tsv=tables.source_readiness_tsv,
        ancestry_tsv=tables.ancestry_tsv,
        sd_estimation_tsv=tables.sd_estimation_tsv,
        exclusions_tsv=tables.exclusions_tsv,
    )

    staging_store_dir = pipeline.registry_root / STAGING_DIRNAME / pipeline.store_id
    if staging_store_dir.exists():
        import shutil

        shutil.rmtree(staging_store_dir)
    stage_candidate(staging_store_dir, files)

    check_errors = check_staged_candidate(
        pipeline.store_id, pipeline.registry_root / STAGING_DIRNAME
    )
    if check_errors:
        raise CandidateError(
            "staged candidate failed bundle.check(); refusing publication:\n  - "
            + "\n  - ".join(check_errors)
        )

    published = publish_candidate(pipeline.registry_root, pipeline.store_id, staging_store_dir)
    cleanup_staging(pipeline.registry_root)
    print(
        f"Candidate {pipeline.store_id} published to {published} "
        f"({tables.included_rows} included, {tables.excluded_rows} excluded)"
    )
    if tables.exclusion_counts:
        print(
            "  exclusions: "
            + ", ".join(f"{k}={v}" for k, v in tables.exclusion_counts.items())
        )
    if tables.warnings:
        print("  review:")
        for warning in tables.warnings:
            print(f"    - {warning}")
    return published


def run(pipeline: Pipeline) -> Path | None:
    stage = pipeline.args.stage
    manifest: list = []
    metadata: dict = {}
    records: list = []
    index: dict = {}

    if stage in ("preflight", "all"):
        stage_preflight(pipeline)
    if stage in ("prepare", "all"):
        manifest, metadata = stage_prepare(pipeline)
    if stage in ("prepare", "resolve", "verify", "emit"):
        # A later stage run standalone rebuilds the manifest deterministically.
        if not manifest:
            manifest, metadata = stage_prepare(pipeline)
    if stage in ("resolve", "all"):
        stage_resolve(pipeline, manifest)
    if stage in ("verify", "emit", "all"):
        records, index = stage_verify(pipeline, manifest)
    if stage in ("emit", "all"):
        return stage_emit(pipeline, manifest, metadata, records, index)
    return None


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def operator_command(argv: Sequence[str]) -> str:
    """Render the invocation this process is carrying out, for the command log."""
    script = f"{FAMILY_DIR}/generate_candidate.py"
    return " ".join(["python3", script, *argv])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        pipeline = Pipeline(
            args, Path(args.repo_root).resolve(), operator_command(raw_argv)
        )
    except (PreflightConfigError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        run(pipeline)
    except (CandidateError, InventoryError, PreflightConfigError) as exc:
        cleanup_staging(pipeline.registry_root)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # Never leave a staging tree behind on an unexpected failure.
        cleanup_staging(pipeline.registry_root)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
