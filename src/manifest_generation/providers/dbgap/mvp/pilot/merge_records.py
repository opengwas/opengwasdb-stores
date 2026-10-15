"""Merge resolver records into the emitter's canonical resolution table (#221).

The full MVP resolution writes one JSON record per Analysis under a bucket's
``records/`` directory. This module turns those records plus the metadata-only
selection evidence into the canonical resolution rows that
``generate_candidate.py emit`` consumes, and refuses to produce a table when any
admitted Analysis is missing a record or did not resolve.

Nothing here opens an association body; the records are the resolver's output
and the rest is committed metadata.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
from pilot import selection, wrapper  # noqa: E402

_GC_PATH = _HERE.parent / "generate_candidate.py"
_spec = importlib.util.spec_from_file_location("dbgap_mvp_generate", _GC_PATH)
assert _spec and _spec.loader
generate_candidate = importlib.util.module_from_spec(_spec)
sys.modules["dbgap_mvp_generate"] = generate_candidate
_spec.loader.exec_module(generate_candidate)

#: Canonical resolution columns written for the emitter, in stable order.
RESOLUTION_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "source_ancestry_label",
    "stored_effect_scale",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "n_cases",
    "n_controls",
    "original_effect_scale",
    "original_sd_method",
    "original_sd",
    "source_file",
    "source_genome_build",
    "source_label",
    "source_analysis_id",
    "checksum",
    "checksum_algorithm",
    "size_bytes",
)


class MergeRecordsError(ValueError):
    """A record set cannot make a complete resolution table."""


@dataclass(frozen=True)
class MergeResult:
    rows: list[dict[str, str]]
    report: dict


def read_record(path: Path | str) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def record_resolution_row(record: Mapping, row: "selection.SelectionRow") -> dict[str, str]:
    """Map one resolver record + selection binding to canonical columns."""
    cfg = (record.get("fingerprints") or {}).get("resolution_config") or {}
    ancestry = record.get("ancestry") or {}
    assigned = str(ancestry.get("assigned_ancestry") or "").strip()
    # META is canonicalised to the empty super-population; an unassigned record
    # stays empty for every bucket. The value is never forced to the bucket.
    declared = "" if row.bucket == "META" else assigned
    # `ancestry_assignment_method` describes HOW the canonical assigned_ancestry
    # above was produced, not the raw core inference: a published value came from
    # source AF (af_assigned); a canonically-empty value is unassigned. The raw
    # core inference stays in the resolver record and is never rewritten here.
    method = "af_assigned" if declared else "unassigned"
    stored = str(cfg.get("stored_effect_scale") or row.stored_effect_scale or "").strip()
    sd_method = str(cfg.get("original_sd_method") or row.original_sd_method or "").strip()
    sample_size = cfg.get("sample_size")
    if sample_size in (None, ""):
        sample_size = row.mapping.get("sample_size", "")
    kind = "case_control" if stored in ("log_or", "log_hazard") else "total"
    case_control = kind == "case_control"
    sample_text = "" if sample_size is None else str(sample_size).strip()
    if case_control or kind == "total":
        # Integer-count kinds are emitted as integers even when the resolver
        # carried a float (e.g. 3547.0); the registry gate compares int(float()).
        try:
            sample_text = str(int(round(float(sample_text))))
        except ValueError:
            pass
    estimate = (record.get("phenotype_sd") or {}).get("estimate")
    fps = record.get("fingerprints") or {}
    source_file = (
        (record.get("diagnostics") or {}).get("source_file")
        or row.manifest.get("data_file", "")
    )
    return {
        "analysis_id": row.analysis_id,
        "assigned_ancestry": declared,
        "ancestry_assignment_method": method,
        # The source-declared ancestry label, from the exact acquisition mapping
        # field (never inferred from the core's AF assignment).
        "source_ancestry_label": row.mapping.get("ancestry", ""),
        "stored_effect_scale": stored,
        "sample_size_kind": kind,
        "sample_size_scope": "analysis_level",
        "sample_size": sample_text,
        "n_cases": row.mapping.get("n_cases", "") if case_control else "",
        "n_controls": row.mapping.get("n_controls", "") if case_control else "",
        "original_effect_scale": row.original_effect_scale or stored,
        "original_sd_method": sd_method,
        "original_sd": "" if estimate is None else str(estimate),
        "source_file": source_file,
        "source_genome_build": row.manifest.get("genome_assembly", ""),
        "source_label": row.mapping.get("analyzed_variable", ""),
        "source_analysis_id": row.analysis_id,
        "checksum": fps.get("source_recorded_sha256") or row.manifest.get("sha256", ""),
        "checksum_algorithm": "sha256",
        "size_bytes": fps.get("source_file_bytes") or row.manifest.get("data_bytes", ""),
    }


def merge_records(
    records_dir: Path | str, selection_rows: Sequence["selection.SelectionRow"]
) -> MergeResult:
    """Join resolver records to the admitted selection; fail closed on gaps."""
    directory = Path(records_dir)
    if not directory.is_dir():
        raise MergeRecordsError(f"records directory not found: {directory}")
    rows: list[dict[str, str]] = []
    missing: list[str] = []
    unresolved: list[tuple[str, str]] = []
    pending: list[str] = []
    for row in selection_rows:
        if not row.is_resolved:
            pending.append(row.analysis_id)
            continue
        record_path = directory / f"{row.analysis_id}.json"
        if not record_path.is_file():
            missing.append(row.analysis_id)
            continue
        record = read_record(record_path)
        status = str(record.get("status") or "")
        if status != "success":
            unresolved.append((row.analysis_id, status or "unknown"))
            continue
        rows.append(record_resolution_row(record, row))
    report = {
        "selection": len(selection_rows),
        "resolved_records": len(rows),
        "pending_provenance": len(pending),
        "pending_analysis_ids": pending,
        "missing_records": missing,
        "unresolved": [{"analysis_id": a, "status": s} for a, s in unresolved],
    }
    if missing or unresolved:
        raise MergeRecordsError(
            "resolution is incomplete: "
            f"{len(missing)} missing record(s) {missing[:5]}, "
            f"{len(unresolved)} non-success record(s) {unresolved[:5]}"
        )
    return MergeResult(rows=rows, report=report)


def render_resolution_tsv(rows: Sequence[Mapping[str, str]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output, fieldnames=list(RESOLUTION_COLUMNS), delimiter="\t", lineterminator="\n"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in RESOLUTION_COLUMNS})
    return output.getvalue()


def _bucket_from_scope(scope: str) -> str:
    if not scope.startswith("bucket:"):
        raise MergeRecordsError("--emit requires --scope bucket:<X> or an explicit --only-bucket")
    return scope.split(":", 1)[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True, help="pilot | bucket:<X> | all")
    parser.add_argument("--records-dir", type=Path, required=True)
    parser.add_argument("--metadata-root", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=generate_candidate.REPO_ROOT / "resources/generators/dbgap-mvp/config-candidate.yaml")
    parser.add_argument("--candidate-table", type=Path, default=None)
    parser.add_argument("--effect-provenance", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--emit", action="store_true", help="also emit the observed bundle(s)")
    parser.add_argument("--only-bucket", default=None, help="bucket to emit when --scope is not bucket:<X>")
    parser.add_argument("--created-at", default=None)
    args = parser.parse_args(argv)

    metadata_root = Path(args.metadata_root)
    content_sha, _ = wrapper.metadata_content.metadata_content_sha256(metadata_root)
    selection_rows = selection.build_selection(
        metadata_root=metadata_root,
        mapping_path=args.mapping,
        metadata_content_sha256=content_sha,
        candidate_table_path=args.candidate_table,
        effect_provenance_path=args.effect_provenance,
        scope=args.scope,
    )
    try:
        result = merge_records(args.records_dir, selection_rows)
    except MergeRecordsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    out_root = Path(args.out_dir)
    try:
        wrapper.safe_write.atomic_write_text(
            out_root / "merge" / "resolution.tsv",
            render_resolution_tsv(result.rows),
            out_root,
            overwrite=True,
        )
        wrapper.safe_write.atomic_write_text(
            out_root / "merge" / "merge-receipt.json",
            json.dumps(result.report, indent=2, sort_keys=True) + "\n",
            out_root,
            overwrite=True,
        )
    except wrapper.safe_write.SafeWriteError as exc:
        raise MergeRecordsError(str(exc)) from exc

    if args.emit:
        config = generate_candidate.load_config(args.config)
        bucket = args.only_bucket or _bucket_from_scope(args.scope)
        result_emit = generate_candidate.run_emit(
            config,
            args.mapping,
            out_root / "merge" / "resolution.tsv",
            out_root / "bundles",
            created_at=args.created_at or "1970-01-01T00:00:00+00:00",
            only_buckets=[bucket],
        )
        print(json.dumps({"merge": result.report, "emit": result_emit}, indent=2))
    else:
        print(json.dumps({"merge": result.report, "resolution": str(out_root / "merge" / "resolution.tsv")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
