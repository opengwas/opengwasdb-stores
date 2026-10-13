"""Build the reviewed 15-Analysis MVP pilot selection and resolver manifest.

Metadata only: the mapping, the four store manifests and the per-Analysis
sidecars. No association body is opened and no metric is invented. Effect scale
is derived from real source provenance (dbGaP study design plus the
inverse-normal-transformed declaration) and is ``unknown``/pending otherwise --
explicitly **not** a silent ``sd`` default, because the public resolver defaults
a blank ``stored_effect_scale`` to ``sd``.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

#: The reviewed pilot buckets and their exact Analysis ids (metric-resolution
#: plan, section 4): two quantitative plus one case-control per bucket.
PILOT_IDS: Mapping[str, tuple[str, ...]] = {
    "EUR": ("GCST90475091", "GCST90475094", "GCST90475169"),
    "AFR": ("GCST90475090", "GCST90475093", "GCST90475184"),
    "AMR": ("GCST90475089", "GCST90475092", "GCST90475183"),
    "EAS": ("GCST90475346", "GCST90475350", "GCST90475203"),
    "META": ("GCST90479500", "GCST90479501", "GCST90479527"),
}

MANIFEST_COLUMNS: tuple[str, ...] = (
    "analysis_index",
    "analysis_id",
    "source_file",
    "source_reader_capability",
    "stored_effect_scale",
    "original_effect_scale",
    "effect_scale_provenance",
    "effect_scale_status",
    "original_sd_method",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "n_cases",
    "n_controls",
    "assigned_ancestry",
    "source_ancestry_label",
    "source_analysis_id",
    "source_genome_build",
    "checksum",
    "checksum_algorithm",
    "size_bytes",
    "source_file_path",
)

EVIDENCE_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "bucket",
    "mapping_ancestry",
    "study_design",
    "mapping_sample_size",
    "n_cases",
    "n_controls",
    "analyzed_variable",
    "source_file_path",
    "source_file_size",
    "source_sha256",
    "source_genome_build",
    "sidecar_gwas_id",
    "sidecar_data_file_name",
    "designed_for_scale",
    "effect_scale_provenance",
    "effect_scale_status",
    "metadata_content_sha256",
)

_CASE_CONTROL_RE = re.compile(r"case[-_ ]?control|binary", re.IGNORECASE)
_INV_NORM_RE = re.compile(r"inv[-_ ]?norm|inverse[-_ ]?normal|INT\b", re.IGNORECASE)

READER_CAPABILITY = "opengwasdb.gwas-ssf"


class SelectionError(ValueError):
    """The metadata cannot produce the reviewed pilot selection."""


@dataclass(frozen=True)
class SelectionRow:
    analysis_id: str
    bucket: str
    mapping: Mapping[str, str]
    manifest: Mapping[str, str]
    sidecar: Mapping[str, object]
    stored_effect_scale: str
    original_effect_scale: str
    original_sd_method: str
    effect_scale_provenance: str
    effect_scale_status: str

    @property
    def is_resolved(self) -> bool:
        return self.effect_scale_status == "resolved"


def read_tsv(path: Path | str) -> list[dict[str, str]]:
    tsv = Path(path)
    with tsv.open(newline="", encoding="utf-8") as handle:
        return [
            {key: (value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle, delimiter="\t")
        ]


def index_by_analysis_id(rows: Sequence[Mapping[str, str]], what: str) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        analysis_id = (row.get("analysis_id") or "").strip()
        if not analysis_id:
            raise SelectionError(f"{what} has a blank analysis_id")
        if analysis_id in indexed:
            raise SelectionError(f"{what} has duplicate analysis_id {analysis_id!r}")
        indexed[analysis_id] = dict(row)
    return indexed


def classify_effect_scale(
    mapping_row: Mapping[str, str], provenance: Mapping[str, str]
) -> tuple[str, str, str, str, str]:
    """Return ``(stored_effect_scale, original_sd_method, original_effect_scale,
    provenance, status)``.

    Two authoritative sources, in order:

    1. an explicit effect-provenance row may pass ``original_sd_method``
       through verbatim (with its declared scales);
    2. the dbGaP member's own ``dbgap_trait_type`` plus the exact
       ``inv-norm transformed`` phrase in ``analyzed_variable``:
       ``binary`` -> ``log_or``/``binary_trait`` (no SD), and
       ``quantitative`` + ``inv-norm transformed`` -> ``sd``/``declared_standardised``
       (SD = 1).

    Everything else is ``pending`` and is kept out of the resolver manifest.
    Study-design flags and loose label regexes are still never used as an
    effect model, and an empty ``original_sd_method`` is never emitted for a
    resolved row (the core would default it to ``estimated_from_source_maf``).
    """
    declared = (provenance.get("stored_effect_scale") or provenance.get("effect_scale") or "").strip().lower()
    explicit_method = (provenance.get("original_sd_method") or "").strip().lower()
    original = (provenance.get("original_effect_scale") or "").strip().lower()
    source = (provenance.get("effect_scale_provenance") or "").strip()
    if declared in ("sd", "log_or", "log_hazard"):
        method = explicit_method or ("binary_trait" if declared in ("log_or", "log_hazard") else "declared_standardised")
        if method not in ("binary_trait", "declared_standardised", "source_provided", "estimated_from_source_maf", "estimated_from_reference_maf", "estimated_from_beta_distribution", "unavailable"):
            raise SelectionError(f"invalid original_sd_method {method!r} in effect provenance")
        return declared, method, original or declared, source or "explicit_effect_provenance", "resolved"

    trait_type = (mapping_row.get("dbgap_trait_type") or "").strip().lower()
    variable = (mapping_row.get("analyzed_variable") or "").strip().lower()
    if trait_type == "binary":
        return "log_or", "binary_trait", "log_or", "dbgap_trait_type_binary", "resolved"
    if trait_type == "quantitative" and "inv-norm transformed" in variable:
        return (
            "sd",
            "declared_standardised",
            "sd",
            "dbgap_analyzed_variable_inv_norm_transformed",
            "resolved",
        )
    return "", "", "", source or "unknown_pending", "pending"


def _load_metadata_root_manifests(metadata_root: Path) -> dict[str, dict[str, str]]:
    manifest_dir = metadata_root / "manifests"
    indexed: dict[str, dict[str, str]] = {}
    for path in sorted(manifest_dir.glob("*.tsv")):
        for row in read_tsv(path):
            analysis_id = (row.get("analysis_id") or "").strip()
            if analysis_id:
                indexed[analysis_id] = row
    return indexed


def build_selection(
    *,
    metadata_root: Path | str,
    mapping_path: Path | str,
    metadata_content_sha256: str,
    candidate_table_path: Path | str | None = None,
    effect_provenance_path: Path | str | None = None,
) -> list[SelectionRow]:
    """Build the reviewed 15-row selection with per-row binding evidence."""
    root = Path(metadata_root)
    mapping_by_id = index_by_analysis_id(read_tsv(mapping_path), "mapping")
    manifest_by_id = _load_metadata_root_manifests(root)
    effect_by_id: dict[str, dict[str, str]] = {}
    if effect_provenance_path is not None:
        effect_by_id = index_by_analysis_id(
            read_tsv(effect_provenance_path), "effect provenance"
        )
    candidates: dict[str, dict[str, str]] = {}
    if candidate_table_path is not None:
        table_path = Path(candidate_table_path)
        if not table_path.is_file():
            raise SelectionError(f"candidate table not found: {table_path}")
        wanted = {analysis_id for ids in PILOT_IDS.values() for analysis_id in ids}
        for row in read_tsv(table_path):
            if (row.get("STUDY.ACCESSION") or "").strip() in wanted:
                candidates[row["STUDY.ACCESSION"].strip()] = row

    rows: list[SelectionRow] = []
    for bucket, ids in PILOT_IDS.items():
        for analysis_id in ids:
            if analysis_id not in mapping_by_id:
                raise SelectionError(f"pilot {analysis_id!r} is not in the mapping")
            mapping_row = mapping_by_id[analysis_id]
            if (mapping_row.get("ancestry") or "").strip() != bucket:
                raise SelectionError(
                    f"pilot {analysis_id!r} mapping ancestry "
                    f"{(mapping_row.get('ancestry') or '')!r} != bucket {bucket!r}"
                )
            manifest_row = manifest_by_id.get(analysis_id)
            if manifest_row is None:
                raise SelectionError(f"pilot {analysis_id!r} has no store-manifest row")
            sidecar_path = Path((manifest_row.get("yaml_file") or "").strip())
            if not sidecar_path.is_file():
                raise SelectionError(f"pilot {analysis_id!r} sidecar missing: {sidecar_path}")
            import yaml

            sidecar = yaml.safe_load(sidecar_path.read_text(encoding="utf-8")) or {}
            if str(sidecar.get("gwas_id") or "").strip() != analysis_id:
                raise SelectionError(f"pilot {analysis_id!r} sidecar gwas_id mismatch")
            scale, method, original, provenance, status = classify_effect_scale(
                mapping_row, effect_by_id.get(analysis_id, {})
            )
            rows.append(
                SelectionRow(
                    analysis_id=analysis_id,
                    bucket=bucket,
                    mapping=mapping_row,
                    manifest=manifest_row,
                    sidecar=sidecar,
                    stored_effect_scale=scale,
                    original_effect_scale=original,
                    original_sd_method=method,
                    effect_scale_provenance=provenance,
                    effect_scale_status=status,
                )
            )
            if candidate_table_path is not None and analysis_id not in candidates:
                raise SelectionError(f"pilot {analysis_id!r} is not in the candidate table")
    return rows


def _assigned_ancestry(row: SelectionRow) -> str:
    # META is cross-ancestry and canonicalises to the empty super-population; the
    # core-inferred ancestry is kept only as provenance.
    return "" if row.bucket == "META" else row.bucket


def render_evidence_tsv(rows: Sequence[SelectionRow], metadata_content_sha256: str) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(EVIDENCE_COLUMNS), delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        manifest = row.manifest
        writer.writerow(
            {
                "analysis_id": row.analysis_id,
                "bucket": row.bucket,
                "mapping_ancestry": row.mapping.get("ancestry", ""),
                "study_design": row.mapping.get("study_design", ""),
                "mapping_sample_size": row.mapping.get("sample_size", ""),
                "n_cases": row.mapping.get("n_cases", ""),
                "n_controls": row.mapping.get("n_controls", ""),
                "analyzed_variable": row.mapping.get("analyzed_variable", ""),
                "source_file_path": manifest.get("data_file", ""),
                "source_file_size": manifest.get("data_bytes", ""),
                "source_sha256": manifest.get("sha256", ""),
                "source_genome_build": manifest.get("genome_assembly", ""),
                "sidecar_gwas_id": str(row.sidecar.get("gwas_id") or ""),
                "sidecar_data_file_name": str(row.sidecar.get("data_file_name") or ""),
                "designed_for_scale": row.stored_effect_scale,
                "effect_scale_provenance": row.effect_scale_provenance,
                "effect_scale_status": row.effect_scale_status,
                "metadata_content_sha256": metadata_content_sha256,
            }
        )
    return output.getvalue()


def render_resolver_manifest(rows: Sequence[SelectionRow]) -> str:
    """The canonical resolver manifest, containing only resolved-effect rows."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(MANIFEST_COLUMNS), delimiter="\t", lineterminator="\n")
    writer.writeheader()
    index = 0
    for row in rows:
        if not row.is_resolved:
            continue
        manifest = row.manifest
        writer.writerow(
            {
                "analysis_index": index,
                "analysis_id": row.analysis_id,
                "source_file": manifest.get("data_file", ""),
                "source_reader_capability": READER_CAPABILITY,
                "stored_effect_scale": row.stored_effect_scale,
                "original_effect_scale": row.original_effect_scale,
                "effect_scale_provenance": row.effect_scale_provenance,
                "effect_scale_status": row.effect_scale_status,
                "original_sd_method": row.original_sd_method,
                "sample_size_kind": "case_control" if row.stored_effect_scale == "log_or" else "total",
                "sample_size_scope": "analysis_level",
                "sample_size": row.mapping.get("sample_size", ""),
                "n_cases": row.mapping.get("n_cases", ""),
                "n_controls": row.mapping.get("n_controls", ""),
                "assigned_ancestry": _assigned_ancestry(row),
                "source_ancestry_label": row.mapping.get("ancestry", ""),
                "source_analysis_id": row.analysis_id,
                "source_genome_build": manifest.get("genome_assembly", ""),
                "checksum": manifest.get("sha256", ""),
                "checksum_algorithm": "sha256" if manifest.get("sha256") else "",
                "size_bytes": manifest.get("data_bytes", ""),
                "source_file_path": manifest.get("data_file", ""),
            }
        )
        index += 1
    return output.getvalue()


def pending_rows(rows: Sequence[SelectionRow]) -> list[dict[str, str]]:
    return [
        {
            "analysis_id": row.analysis_id,
            "effect_scale_provenance": row.effect_scale_provenance,
            "effect_scale_status": row.effect_scale_status,
        }
        for row in rows
        if not row.is_resolved
    ]


def selection_summary(rows: Sequence[SelectionRow], metadata_content_sha256: str) -> dict:
    resolved = [row for row in rows if row.is_resolved]
    return {
        "total": len(rows),
        "resolved": len(resolved),
        "pending": len(rows) - len(resolved),
        "buckets": {
            bucket: [row.analysis_id for row in rows if row.bucket == bucket]
            for bucket in PILOT_IDS
        },
        "metadata_content_sha256": metadata_content_sha256,
        "pending_rows": pending_rows(rows),
    }


__all__ = [
    "EVIDENCE_COLUMNS",
    "MANIFEST_COLUMNS",
    "PILOT_IDS",
    "READER_CAPABILITY",
    "SelectionError",
    "SelectionRow",
    "build_selection",
    "classify_effect_scale",
    "index_by_analysis_id",
    "pending_rows",
    "read_tsv",
    "render_evidence_tsv",
    "render_resolver_manifest",
    "selection_summary",
]
