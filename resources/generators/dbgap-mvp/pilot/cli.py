#!/usr/bin/env python3
"""CLI for the bounded MVP metric-resolution pilot (#221).

``plan`` is the default and metadata-only. ``run`` executes the public resolver
once and refuses to do so without ``--execute``, the approved immutable metadata
SHA and a parent start receipt. No arbitrary command can be run: every argument
to the resolver is assembled here.
"""

from __future__ import annotations

import argparse
import json
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

DEFAULT_MAPPING = Path(
    "/data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z/mapping.tsv"
)
DEFAULT_CANDIDATE_TABLE = Path(
    "/home/gh13047/repo/opengwasdb-stores/resources/data/derived/store-candidates-analyses.tsv"
)
DEFAULT_METADATA_ROOT = Path(
    "/data/opengwasdb/work/epic232/220/metadata-refresh-parent-baf141c-20261013T123913Z"
)


def _write(path: Path, text: str, root: Path, *, overwrite: bool = False) -> None:
    try:
        wrapper.safe_write.atomic_write_text(path, text, root, overwrite=overwrite)
    except wrapper.safe_write.SafeWriteError as exc:
        raise wrapper.WrapperError(str(exc)) from exc


def _build(args: argparse.Namespace) -> tuple[list, str, Path]:
    root = Path(args.metadata_root)
    if not root.is_dir():
        raise wrapper.WrapperError(f"metadata root not found: {root}")
    content_sha, _ = wrapper.metadata_content.metadata_content_sha256(root)
    if args.approved_metadata_sha and content_sha != args.approved_metadata_sha:
        raise wrapper.WrapperError(
            f"metadata content sha {content_sha} != approved {args.approved_metadata_sha}"
        )
    rows = selection.build_selection(
        metadata_root=root,
        mapping_path=args.mapping,
        metadata_content_sha256=content_sha,
        candidate_table_path=args.candidate_table,
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


def _run(args: argparse.Namespace) -> int:
    if not args.execute:
        raise wrapper.WrapperError("run mode requires --execute (DRY PLAN is the default)")
    if not args.approved_metadata_sha:
        raise wrapper.WrapperError("run mode requires --approved-metadata-sha")
    if not args.start_receipt or not Path(args.start_receipt).is_file():
        raise wrapper.WrapperError("run mode requires an existing --start-receipt authorisation file")
    start_receipt = json.loads(Path(args.start_receipt).read_text(encoding="utf-8"))
    if str(start_receipt.get("approved_metadata_sha256")) != args.approved_metadata_sha:
        raise wrapper.WrapperError("start receipt metadata sha does not match --approved-metadata-sha")
    if start_receipt.get("authorized") is not True:
        raise wrapper.WrapperError("start receipt is not an explicit authorisation")

    resolver = args.resolver_executable or shutil.which("opengwasdb")
    if not resolver:
        raise wrapper.WrapperError("no opengwasdb executable found; pass --resolver-executable")

    rows, content_sha, root = _build(args)
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

    limits = wrapper.ResourceLimits(
        wall_limit_s=args.wall_limit_s,
        rss_limit_mb=args.rss_limit_mb,
        max_workers=wrapper.MAX_WORKERS,
    )
    argv = wrapper.build_resolver_argv(
        resolver_executable=resolver,
        manifest_path=manifest_path,
        records_dir=records_dir,
        limits=limits,
    )
    outcome = wrapper.run_bounded(argv, limits=limits)
    receipt = {
        "mode": "run",
        "executed": True,
        "resolver_executable": resolver,
        "resolver_argv": argv,
        "metadata_content_sha256": content_sha,
        "summary": summary,
        "outcome": {
            "ok": outcome.ok,
            "returncode": outcome.returncode,
            "wall_s": outcome.wall_s,
            "peak_rss_mb": outcome.peak_rss_mb,
            "timed_out": outcome.timed_out,
            "rss_breached": outcome.rss_breached,
            "escaped_pids": list(outcome.escaped_pids),
            "stdout_tail": outcome.stdout_tail,
            "stderr_tail": outcome.stderr_tail,
        },
        "rss_accounting": "aggregate process-tree VmRSS; shared pages double-counted (ceiling)",
        "source_evidence_scope": "capped_pilot_diagnostic",
    }
    _write(out_root / "receipt" / "resolution-receipt.json", json.dumps(receipt, indent=2, sort_keys=True) + "\n", out_root)
    if outcome.ok:
        _write(out_root / "COMMITTED", f"{content_sha}\n", out_root)
    print(json.dumps({"mode": "run", "ok": outcome.ok, "out_root": str(out_root), "outcome": receipt["outcome"]}, indent=2))
    return 0 if outcome.ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
        p.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
        p.add_argument("--candidate-table", type=Path, default=DEFAULT_CANDIDATE_TABLE)
        p.add_argument("--out-root", type=Path, required=True)
        p.add_argument("--approved-metadata-sha", default=None)
        p.add_argument("--protected-root", type=Path, action="append", default=[])
        p.add_argument("--registry-root", type=Path, action="append", default=[])
        p.add_argument("--execute", action="store_true")
        p.add_argument("--start-receipt", type=Path, default=None)
        p.add_argument("--resolver-executable", default=None)
        p.add_argument("--wall-limit-s", type=float, default=wrapper.WALL_LIMIT_S)
        p.add_argument("--rss-limit-mb", type=float, default=wrapper.RSS_LIMIT_MB)
    args = parser.parse_args(argv)
    try:
        return _plan(args) if args.mode == "plan" else _run(args)
    except (wrapper.WrapperError, selection.SelectionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
