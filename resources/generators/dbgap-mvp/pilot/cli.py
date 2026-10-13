#!/usr/bin/env python3
"""CLI for the bounded MVP metric-resolution pilot (#221).

``plan`` is the default and metadata-only. ``run`` executes the public resolver
once and refuses to do so without ``--execute``, an approved immutable metadata
SHA and a fully-bound parent start receipt. Every resolver argument is assembled
here, so no arbitrary command can be run.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from pilot import selection, wrapper  # noqa: E402

DEFAULT_MAPPING = Path("/data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z/mapping.tsv")
DEFAULT_CANDIDATE_TABLE = Path("/home/gh13047/repo/opengwasdb-stores/resources/data/derived/store-candidates-analyses.tsv")
DEFAULT_METADATA_ROOT = Path("/data/opengwasdb/work/epic232/220/metadata-refresh-parent-baf141c-20261013T123913Z")
GUARD_SOURCE = _HERE / "guard" / "sitecustomize.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str, root: Path, *, overwrite: bool = False) -> None:
    try:
        wrapper.safe_write.atomic_write_text(path, text, root, overwrite=overwrite)
    except wrapper.safe_write.SafeWriteError as exc:
        raise wrapper.WrapperError(str(exc)) from exc


def _verify_runtime(args: argparse.Namespace) -> str:
    if args.core_commit != wrapper.PILOT_CORE_COMMIT:
        raise wrapper.WrapperError(
            f"--core-commit {args.core_commit!r} != pinned {wrapper.PILOT_CORE_COMMIT}"
        )
    if args.runtime_identity:
        identity = json.loads(Path(args.runtime_identity).read_text(encoding="utf-8"))
        if str(identity.get("core_commit")) != args.core_commit:
            raise wrapper.WrapperError("runtime identity core_commit does not match --core-commit")
        return str(identity.get("executable") or args.resolver_executable or "")
    # Production path: the resolver must be the opengwasdb console script of this
    # verified environment.
    resolver = args.resolver_executable or shutil.which("opengwasdb")
    if not resolver:
        raise wrapper.WrapperError("no opengwasdb executable found")
    expected = Path(sys.executable).parent / "opengwasdb"
    if Path(resolver).resolve() != expected.resolve():
        raise wrapper.WrapperError(
            f"resolver {resolver} is not the pinned opengwasdb console script {expected}"
        )
    return str(Path(resolver).resolve())


def _verify_inputs(args: argparse.Namespace, content_sha: str) -> None:
    root = Path(args.metadata_root)
    receipt_path = root / "parent-run-summary.json"
    if not receipt_path.is_file():
        raise wrapper.WrapperError(f"metadata receipt not found: {receipt_path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if str(receipt.get("output_metadata_content_sha256")) != content_sha:
        raise wrapper.WrapperError("recomputed metadata content sha != receipt content sha")
    if args.approved_metadata_sha and content_sha != args.approved_metadata_sha:
        raise wrapper.WrapperError("recomputed metadata content sha != approved sha")
    if not Path(args.mapping).is_file():
        raise wrapper.WrapperError(f"mapping not found: {args.mapping}")
    if args.candidate_table and not Path(args.candidate_table).is_file():
        raise wrapper.WrapperError(f"candidate table not found: {args.candidate_table}")
    # Reuse the qualified candidate validator for the frozen receipt.
    generator = _load_module("dbgap_mvp_generate", _HERE.parent / "generate_candidate.py")
    config = {
        "metadata": {
            "root": str(root),
            "code_sha": str(receipt.get("code_sha") or ""),
            "output_metadata_content_sha256": content_sha,
            "membership": receipt.get("membership"),
        }
    }
    generator.verify_metadata_snapshot(config)


def _build(args: argparse.Namespace):
    root = Path(args.metadata_root)
    if not root.is_dir():
        raise wrapper.WrapperError(f"metadata root not found: {root}")
    content_sha, _ = wrapper.metadata_content.metadata_content_sha256(root)
    _verify_inputs(args, content_sha)
    rows = selection.build_selection(
        metadata_root=root,
        mapping_path=args.mapping,
        metadata_content_sha256=content_sha,
        candidate_table_path=args.candidate_table,
        effect_provenance_path=args.effect_provenance,
        scope=args.scope,
    )
    return rows, content_sha, root


def _plan(args: argparse.Namespace) -> int:
    rows, content_sha, metadata_root = _build(args)
    out_root = wrapper.preflight_output_root(
        args.out_root,
        protected_roots=[metadata_root, *(args.protected_root or [])],
        registry_roots=args.registry_root or [],
    )
    summary = selection.selection_summary(rows, content_sha)
    _write(out_root / "selection" / "selection-evidence.tsv", selection.render_evidence_tsv(rows, content_sha), out_root)
    _write(out_root / "selection" / "pilot-analyses.tsv", selection.render_resolver_manifest(rows), out_root)
    _write(
        out_root / "selection" / "plan-receipt.json",
        json.dumps(
            {
                "mode": "plan",
                "executed": False,
                "metadata_root": str(args.metadata_root),
                "metadata_content_sha256": content_sha,
                "summary": summary,
                "note": "metadata-only plan; no resolver run, no association body opened",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        out_root,
    )
    print(json.dumps({"mode": "plan", "out_root": str(out_root), **summary}, indent=2))
    return 0


def _verify_start_receipt(receipt: dict, args: argparse.Namespace, resolver: str) -> None:
    required = (
        "authorized",
        "owner",
        "scope",
        "code_head",
        "core_commit",
        "resolver_executable",
        "analysis_ids",
        "reference_paths",
        "caps",
        "start_identity",
    )
    missing = [key for key in required if key not in receipt]
    if missing:
        raise wrapper.WrapperError(f"start receipt is missing required field(s): {', '.join(missing)}")
    if receipt["authorized"] is not True:
        raise wrapper.WrapperError("start receipt is not an explicit authorisation")
    if str(receipt["core_commit"]) != args.core_commit:
        raise wrapper.WrapperError("start receipt core_commit does not match --core-commit")
    if not receipt.get("analysis_ids"):
        raise wrapper.WrapperError("start receipt analysis_ids must be non-empty")
    if str(receipt["resolver_executable"]) != resolver:
        raise wrapper.WrapperError("start receipt resolver_executable does not match the verified runtime")


def _verify_records(records_dir: Path, resolved_ids: list[str]) -> dict:
    index_path = records_dir / "index.json"
    if not index_path.is_file():
        raise wrapper.WrapperError("resolver produced no index.json")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    missing = [analysis_id for analysis_id in resolved_ids if not (records_dir / f"{analysis_id}.json").is_file()]
    if missing:
        raise wrapper.WrapperError(f"resolver is missing {len(missing)} record(s): {missing[:5]}")
    statuses: dict[str, str] = {}
    for analysis_id in resolved_ids:
        record = json.loads((records_dir / f"{analysis_id}.json").read_text(encoding="utf-8"))
        status = str(record.get("status") or "")
        if status not in ("success", "controlled_failure", "skip"):
            raise wrapper.WrapperError(f"record {analysis_id} has unknown status {status!r}")
        statuses[analysis_id] = status
    return {"index": index, "statuses": statuses}


def _run(args: argparse.Namespace) -> int:
    if not args.execute:
        raise wrapper.WrapperError("run mode requires --execute (DRY PLAN is the default)")
    if not args.approved_metadata_sha:
        raise wrapper.WrapperError("run mode requires --approved-metadata-sha")
    if not args.start_receipt or not Path(args.start_receipt).is_file():
        raise wrapper.WrapperError("run mode requires an existing --start-receipt authorisation file")
    limits = wrapper.ResourceLimits(
        wall_limit_s=args.wall_limit_s,
        rss_limit_mb=args.rss_limit_mb,
        max_workers=wrapper.MAX_WORKERS,
    )
    wrapper.validate_limits(limits)  # before any mkdir
    resolver = _verify_runtime(args)
    start = json.loads(Path(args.start_receipt).read_text(encoding="utf-8"))
    _verify_start_receipt(start, args, resolver)
    if str(start.get("approved_metadata_sha256")) != args.approved_metadata_sha:
        raise wrapper.WrapperError("start receipt metadata sha does not match --approved-metadata-sha")

    rows, content_sha, root = _build(args)
    # Bind the receipt to the exact scope membership actually selected, so the
    # resolver cannot be pointed at an unapproved analysis set.
    expected = sorted(row.analysis_id for row in rows)
    if sorted(start.get("analysis_ids") or []) != expected:
        raise wrapper.WrapperError(
            f"start receipt analysis_ids ({len(start.get('analysis_ids') or [])}) "
            f"do not match the selected {args.scope or 'pilot'} scope ({len(expected)})"
        )
    out_root = wrapper.preflight_output_root(
        args.out_root,
        protected_roots=[root, *(args.protected_root or [])],
        registry_roots=args.registry_root or [],
    )
    summary = selection.selection_summary(rows, content_sha)
    _write(out_root / "selection" / "selection-evidence.tsv", selection.render_evidence_tsv(rows, content_sha), out_root)
    manifest_path = out_root / "selection" / "pilot-analyses.tsv"
    _write(manifest_path, selection.render_resolver_manifest(rows), out_root)
    records_dir = out_root / "records"
    records_dir.mkdir(parents=True, exist_ok=False)

    # Guard: read allowlist + write-root confinement injected into the resolver.
    guard_dir = out_root / "guard"
    guard_dir.mkdir()
    shutil.copyfile(GUARD_SOURCE, guard_dir / "sitecustomize.py")
    resolved_ids = [row.analysis_id for row in rows if row.is_resolved]
    allow_read = sorted(
        {str(Path(row.manifest.get("data_file", "")).resolve()) for row in rows if row.is_resolved}
        | {str(wrapper.ANCESTRY_REFERENCE.resolve()), str(wrapper.ANCESTRY_GROUPS.resolve()), str(wrapper.QC_PANEL.resolve())}
        | {str(manifest_path.resolve())}
    )
    guard_log = out_root / "receipt" / "guard-log.json"
    guard_config = {
        "allow_read": allow_read,
        "allow_write_root": str(out_root),
        "log": str(guard_log),
        "enforce_reads": True,
    }
    # The allowlist can exceed the per-string exec environment limit for large
    # scopes (e.g. 1,669 EUR bodies), so it is written to a file and only the
    # config path is passed via the environment.
    guard_config_path = out_root / "guard" / "guard-config.json"
    _write(guard_config_path, json.dumps(guard_config, sort_keys=True), out_root)

    argv = wrapper.build_resolver_argv(
        resolver_executable=resolver, manifest_path=manifest_path, records_dir=records_dir, limits=limits
    )
    outcome = wrapper.run_bounded(
        argv,
        limits=limits,
        stdout_path=out_root / "receipt" / "resolver.stdout.log",
        stderr_path=out_root / "receipt" / "resolver.stderr.log",
        extra_env={
            "OPENGWASDB_PILOT_GUARD_CONFIG": str(guard_config_path),
            "PYTHONPATH": str(guard_dir) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        },
    )
    receipt: dict = {
        "mode": "run",
        "executed": True,
        "resolver_executable": resolver,
        "resolver_argv": argv,
        "metadata_content_sha256": content_sha,
        "summary": summary,
        "outcome": outcome.as_dict(),
        "rss_accounting": "aggregate process-tree VmRSS; shared pages double-counted (ceiling)",
        "source_evidence_scope": "capped_pilot_diagnostic",
    }
    # Post-run verification: records complete and the frozen metadata unchanged.
    try:
        records = _verify_records(records_dir, resolved_ids)
        receipt["records"] = records
        after_sha, _ = wrapper.metadata_content.metadata_content_sha256(root)
        receipt["metadata_content_sha256_after"] = after_sha
        if after_sha != content_sha:
            raise wrapper.WrapperError("metadata content sha changed during the run")
        receipt["guard"] = json.loads(guard_log.read_text(encoding="utf-8")) if guard_log.is_file() else {}
        if int(receipt["guard"].get("denied_reads", 0)) or int(receipt["guard"].get("denied_writes", 0)):
            raise wrapper.WrapperError("guard recorded denied reads/writes")
    except wrapper.WrapperError as exc:
        receipt["verification_error"] = str(exc)
        outcome = None
    _write(out_root / "receipt" / "resolution-receipt.json", json.dumps(receipt, indent=2, sort_keys=True) + "\n", out_root)
    if outcome is not None and outcome.ok and "verification_error" not in receipt:
        _write(out_root / "COMMITTED", f"{content_sha}\n", out_root)
        print(json.dumps({"mode": "run", "ok": True, "out_root": str(out_root)}, indent=2))
        return 0
    print(json.dumps({"mode": "run", "ok": False, "out_root": str(out_root), "outcome": receipt.get("outcome"), "verification_error": receipt.get("verification_error")}, indent=2))
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
        p.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
        p.add_argument("--candidate-table", type=Path, default=DEFAULT_CANDIDATE_TABLE)
        p.add_argument("--effect-provenance", type=Path, default=None)
        p.add_argument(
            "--scope",
            default="pilot",
            help="resolution scope: pilot | bucket:<EUR|AFR|AMR|EAS|META> | all",
        )
        p.add_argument("--out-root", type=Path, required=True)
        p.add_argument("--approved-metadata-sha", default=None)
        p.add_argument("--protected-root", type=Path, action="append", default=[])
        p.add_argument("--registry-root", type=Path, action="append", default=[])
        p.add_argument("--execute", action="store_true")
        p.add_argument("--start-receipt", type=Path, default=None)
        p.add_argument("--resolver-executable", default=None)
        p.add_argument("--runtime-identity", type=Path, default=None)
        p.add_argument("--core-commit", default=wrapper.PILOT_CORE_COMMIT)
        p.add_argument("--wall-limit-s", type=float, default=wrapper.WALL_LIMIT_MAX_S)
        p.add_argument("--rss-limit-mb", type=float, default=wrapper.RSS_LIMIT_MAX_MB)
    args = parser.parse_args(argv)
    try:
        return _plan(args) if args.mode == "plan" else _run(args)
    except (wrapper.WrapperError, selection.SelectionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
