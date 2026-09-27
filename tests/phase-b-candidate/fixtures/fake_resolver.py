#!/usr/bin/env python3
"""Hermetic stand-in for `opengwasdb resolve-analyses` (issue #153 tests).

It reproduces only the resolver's *contract* that the registry depends on:
one atomic ``{analysis_id}.json`` record per manifest row plus a deterministic
``index.json`` in manifest order, with fingerprint inputs that let a caller
verify the record was produced against the manifest it is finalising, and
``--resume`` semantics that skip a record whose fingerprint still matches.

The statistical outcomes are supplied by a JSON file through
``FAKE_RESOLVER_OUTCOMES`` (keyed by ``analysis_id``); ``FAKE_RESOLVER_FAIL_AFTER``
makes it exit non-zero after writing that many records, to test interruption.
It computes no ancestry, alignment or SD -- that stays OpenGWASDB's, and the
real resolver is exercised upstream.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

RECORD_SCHEMA_VERSION = 1


def _opengwasdb_version() -> str:
    try:
        return importlib.metadata.version("opengwasdb")
    except Exception:
        return "0.0.0"


#: The distribution version the registry's resolution contract cross-checks, so a
#: stand-in resolver must report the same one the installed environment has.
TOOL_VERSION = _opengwasdb_version()
DEFAULT_OUTCOME = {
    "status": "success",
    "assigned_ancestry": "EUR",
    "gate_reason": "ok",
    # The pinned upstream EafOrientationOutcome vocabulary is
    # passed|failed|unverified; a genuine assignment records `passed`.
    "eaf_orientation": "passed",
    "sd_status": "estimated",
    "sd": 1.0,
    "sd_dispersion": 0.05,
}


def fingerprint_digest(fingerprints: dict) -> str:
    clean = {k: v for k, v in fingerprints.items() if k != "fingerprint_digest"}
    payload = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: list[str]) -> dict:
    manifest = argv[0]
    records_dir = argv[1]
    options: dict[str, object] = {"resume": False, "n_workers": 1}
    i = 2
    flags = {
        "--ancestry-reference": "ancestry_reference",
        "--ancestry-groups": "ancestry_groups",
        "--extraction-panel": "extraction_panel",
        "--variant-reference": "variant_reference",
        "--af-reference": "af_reference",
        "--default-source-reader-capability": "capability",
        "--maf-floor": "maf_floor",
        "--tau": "tau",
        "--delta": "delta",
        "--n-min": "n_min",
        "--residual-max": "residual_max",
        "--evidence-sample": "evidence_sample",
        "--n-workers": "n_workers",
        "--reference-version": "reference_version",
    }
    while i < len(argv):
        token = argv[i]
        if token == "--resume":
            options["resume"] = True
            i += 1
            continue
        if token == "--largest-first" or token == "--manifest-order":
            options["largest_first"] = token == "--largest-first"
            i += 1
            continue
        if token in flags:
            value = argv[i + 1]
            key = flags[token]
            if key == "af_reference":
                options.setdefault("af_reference", [])
                options["af_reference"].append(value)  # type: ignore[union-attr]
            else:
                options[key] = value
            i += 2
            continue
        raise SystemExit(f"fake resolver: unknown argument {token!r}")
    options["manifest"] = manifest
    options["records_dir"] = records_dir
    return options


def compute_fingerprints(row: dict, options: dict) -> dict:
    source_path = Path(row["source_file"])
    file_bytes = source_path.stat().st_size if source_path.is_file() else None
    file_mtime_ns = source_path.stat().st_mtime_ns if source_path.is_file() else None
    size_bytes = int(row["size_bytes"]) if row.get("size_bytes") else None
    sample_size = float(row["sample_size"]) if row.get("sample_size") else None
    fingerprints = {
        "source_file": row["source_file"],
        "source_recorded_sha256": row.get("checksum") or None,
        "source_recorded_bytes": size_bytes,
        "source_file_bytes": file_bytes,
        "source_file_mtime_ns": file_mtime_ns,
        "opengwasdb_version": TOOL_VERSION,
        "opengwasdb_git_hash": "testgithash",
        "ancestry_reference_id": Path(str(options.get("ancestry_reference", "reference"))).name,
        "ancestry_reference_sha256": "a" * 64,
        "ancestry_groups_sha256": "b" * 64,
        "extraction_panel_sha256": None,
        "extraction_panel_variants": None,
        "af_references": [],
        "resolution_config": {
            "original_sd_method": row.get("original_sd_method", ""),
            "stored_effect_scale": row.get("stored_effect_scale", ""),
            "sample_size": sample_size,
            "source_reader_capability": row.get("source_reader_capability", ""),
            "maf_floor": float(options.get("maf_floor", 0.01)),
            "evidence_sample": int(options.get("evidence_sample", 20000)),
            "gates": {
                "tau": float(options.get("tau", 0.5)),
                "delta": float(options.get("delta", 0.2)),
                "n_min": int(options.get("n_min", 5000)),
                "residual_max": float(options.get("residual_max", 0.06)),
                "orientation_flip_r": float(options.get("orientation_flip_r", -0.5)),
                "sum_to_one_penalty": 1.0,
            },
        },
    }
    if row.get("imputation_score_column"):
        fingerprints["resolution_config"].update({
            "info_score_threshold": float(row["info_score_threshold"]),
            "imputation_score_column": row["imputation_score_column"],
            "imputation_score_kind": row["imputation_score_kind"],
            "imputation_score_provenance": row["imputation_score_provenance"],
        })
    fingerprints["fingerprint_digest"] = fingerprint_digest(fingerprints)
    return fingerprints


def ancestry_payload(outcome: dict) -> dict:
    assigned = outcome.get("assigned_ancestry")
    return {
        "assigned_ancestry": assigned,
        "dominant_superpop": outcome.get("dominant_superpop", assigned),
        "dominant_proportion": 0.9 if assigned else 0.2,
        "runner_up_margin": 0.7 if assigned else 0.1,
        "af_overlap": outcome.get("af_overlap", 100000),
        "residual": outcome.get("residual", 0.01),
        "gate_reason": outcome.get("gate_reason", "ok"),
        "eaf_orientation": outcome.get("eaf_orientation", "passed"),
        "eaf_orientation_r": outcome.get("eaf_orientation_r", 0.99),
        "superpop_composition": {assigned: 0.9} if assigned else {},
        "fine_composition": {},
    }


def phenotype_sd_payload(outcome: dict) -> dict:
    sd_status = outcome.get("sd_status", "estimated")
    reason = outcome.get("sd_reason")
    estimate = None
    if sd_status == "estimated":
        estimate = {
            "sd": outcome.get("sd", 1.0),
            "dispersion": outcome.get("sd_dispersion", 0.05),
            "method": "estimated_from_source_maf",
            "notes": outcome.get("sd_notes", ""),
        }
    return {
        "status": sd_status,
        "reason": reason,
        "estimate": estimate,
        "reference_id": outcome.get("reference_id", ""),
        "n_evidence_considered": 2000,
        "n_estimate_inputs": 1800 if estimate else 0,
        "evidence_sampled": False,
    }


def info_score_state(row: dict, outcome: dict) -> str:
    """The state a real resolver would report for this row (stores #175).

    A declared score defaults to the state its threshold implies (zero is
    `disabled`, positive is `filtered`); an undeclared row has no policy and is
    `legacy_absent`. Tests override it through the outcome to simulate an
    unavailable or unusable record.
    """
    if "info_score_state" in outcome:
        return outcome["info_score_state"]
    if not row.get("imputation_score_column"):
        return "legacy_absent"
    return "disabled" if float(row.get("info_score_threshold") or 0) == 0 else "filtered"


def scan_diagnostics(row: dict, options: dict, outcome: dict) -> dict:
    """The additive diagnostics a v1 resolver record carries (stores #175)."""
    rows_read = outcome.get("rows_read", 2000)
    ancestry_n = outcome.get("ancestry_rows_read", rows_read)
    ancestry_matched = outcome.get("ancestry_reference_rows_matched", ancestry_n)
    build_eligible = outcome.get("build_eligible_rows", rows_read)
    diagnostics = {
        "source_file": row["source_file"],
        "rows_read": rows_read,
        "ancestry_sites": ancestry_n,
        "stop_reason": outcome.get("stop_reason", "eof"),
        "ancestry_rows_read": ancestry_n,
        "ancestry_stop_reason": "eof",
        "ancestry_reference_rows_matched": ancestry_matched,
        "variant_reference_rows_matched": outcome.get(
            "variant_reference_rows_matched", rows_read
        ),
        "canonical_rows_observed": outcome.get("canonical_rows_observed", rows_read),
        "canonical_rows_retained": outcome.get("canonical_rows_retained", rows_read),
        "info_rows_below_threshold": outcome.get("info_rows_below_threshold", 0),
        "info_rows_missing": outcome.get("info_rows_missing", 0),
        "info_rows_malformed": outcome.get("info_rows_malformed", 0),
        "info_rows_nonfinite": outcome.get("info_rows_nonfinite", 0),
        "info_rows_out_of_range": outcome.get("info_rows_out_of_range", 0),
        "info_rows_usable": outcome.get("info_rows_usable", 2000),
        "info_score_state": info_score_state(row, outcome),
        "build_eligible_rows": build_eligible,
    }
    if options.get("variant_reference"):
        # The partition defaults to the pre-INFO matched count so a fixture can
        # still project off-reference overflow by varying that one number.
        on = outcome.get(
            "build_eligible_rows_on_variant_reference",
            min(build_eligible, diagnostics["variant_reference_rows_matched"]),
        )
        diagnostics["build_eligible_rows_on_variant_reference"] = on
        diagnostics["build_eligible_rows_off_variant_reference"] = outcome.get(
            "build_eligible_rows_off_variant_reference", build_eligible - on
        )
    else:
        diagnostics["build_eligible_rows_on_variant_reference"] = None
        diagnostics["build_eligible_rows_off_variant_reference"] = None
    return diagnostics


def record_for(row: dict, options: dict, outcome: dict) -> dict:
    fingerprints = compute_fingerprints(row, options)
    declared = bool(row.get("imputation_score_column"))
    status = outcome.get("status", "success")
    # A declared score with no usable value is a controlled failure in core; the
    # stand-in reproduces that so the registry's exclusion path is exercised.
    if status == "success" and declared and outcome.get("info_rows_usable", 2000) == 0:
        status = "controlled_failure"
        outcome = {
            **outcome,
            "error": outcome.get("error") or (
                f"Analysis {row['analysis_id']}: declared imputation score has no "
                "usable scores in scanned canonical rows"
            ),
        }
    if status != "success":
        return {
            "record_schema_version": RECORD_SCHEMA_VERSION,
            "analysis_id": row["analysis_id"],
            "status": "controlled_failure",
            "fingerprints": fingerprints,
            "diagnostics": scan_diagnostics(row, options, outcome),
            "ancestry": None,
            "phenotype_sd": None,
            "error": outcome.get("error", "simulated source failure"),
            "warnings": [],
            "metrics": {"elapsed_seconds": 0.0, "peak_memory_bytes": 0},
        }
    return {
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "analysis_id": row["analysis_id"],
        "status": "success",
        "fingerprints": fingerprints,
        "diagnostics": scan_diagnostics(row, options, outcome),
        "ancestry": ancestry_payload(outcome),
        "phenotype_sd": phenotype_sd_payload(outcome),
        "error": None,
        "warnings": [],
        "metrics": {"elapsed_seconds": 0.0, "peak_memory_bytes": 0},
    }


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".tmp_{path.name}"
    with temporary.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "resolve-analyses":
        argv = argv[1:]
    options = parse_args(argv)
    manifest_path = Path(str(options["manifest"]))
    records_dir = Path(str(options["records_dir"]))
    records_dir.mkdir(parents=True, exist_ok=True)
    with manifest_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))

    outcomes_path = os.environ.get("FAKE_RESOLVER_OUTCOMES")
    outcomes: dict = {}
    if outcomes_path:
        outcomes = json.loads(Path(outcomes_path).read_text(encoding="utf-8"))
    fail_after = os.environ.get("FAKE_RESOLVER_FAIL_AFTER")
    fail_after_n = int(fail_after) if fail_after else None

    analyses = []
    n_success = 0
    n_failed = 0
    n_resumed = 0
    failed_ids: list[str] = []
    written = 0
    for index, row in enumerate(rows):
        analysis_id = row["analysis_id"]
        outcome = {**DEFAULT_OUTCOME, **outcomes.get(analysis_id, {})}
        record = record_for(row, options, outcome)
        record_path = records_dir / f"{analysis_id}.json"
        digest = record["fingerprints"]["fingerprint_digest"]
        resumed = False
        if options["resume"] and record_path.is_file():
            try:
                existing = json.loads(record_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
            if (
                existing.get("status") == "success"
                and existing.get("fingerprints", {}).get("fingerprint_digest") == digest
            ):
                resumed = True
                record = existing
        if not resumed:
            if fail_after_n is not None and written >= fail_after_n:
                print(
                    f"fake resolver: simulated interruption after {written} record(s)",
                    file=sys.stderr,
                )
                return 1
            atomic_write_json(record_path, record)
            written += 1
        else:
            n_resumed += 1

        status = record["status"]
        if status == "success":
            n_success += 1
        else:
            n_failed += 1
            failed_ids.append(analysis_id)
        ancestry = record.get("ancestry") or {}
        estimate = (record.get("phenotype_sd") or {}).get("estimate") or {}
        analyses.append(
            {
                "manifest_index": index,
                "analysis_id": analysis_id,
                "record_file": f"{analysis_id}.json",
                "status": status,
                "assigned_ancestry": ancestry.get("assigned_ancestry"),
                "gate_reason": ancestry.get("gate_reason"),
                "original_sd": estimate.get("sd"),
                "original_sd_method": record["fingerprints"]["resolution_config"]["original_sd_method"],
                "error": record.get("error"),
            }
        )

    index_data = {
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "manifest_path": str(manifest_path),
        "records_dir": str(records_dir),
        "opengwasdb_version": TOOL_VERSION,
        "opengwasdb_git_hash": "testgithash",
        "n_total": len(rows),
        "n_success": n_success,
        "n_resumed": n_resumed,
        "n_failed": n_failed,
        "failed_analyses": failed_ids,
        "analyses": analyses,
    }
    atomic_write_json(records_dir / "index.json", index_data)
    print(json.dumps({"n_total": len(rows), "n_success": n_success, "n_failed": n_failed, "n_resumed": n_resumed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
