"""QC Panel Concordance Study Engine and Metrics (issue #152).

Evaluates whether the 10,000-variant `qc-panel-hg38` is scientifically
interchangeable with scanning the full 5.8M-variant ancestry reference
for AF-based ancestry assignment and effect-scale evidence in this Source Collection.

Invokes the shared one-pass resolver `opengwasdb.build.resolve.resolve_analysis`
(ADR 0044 / opengwasdb#207) under two modes:
  Method A: Full Reference Scan (extraction_panel=None)
  Method B: QC Panel Extraction (extraction_panel=qc_panel_alids)

Computes per-Analysis differences across ancestry assignment, dominant group,
proportions, margins, residuals, overlap counts, orientation outcomes, gate
reasons, and phenotype SD estimates.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

# Ensure opengwasdb from integration worktree / sibling is discoverable
for sibling_candidate in [
    Path("/home/gh13047/repo/opengwasdb-phase-b-integration"),
    Path("/home/gh13047/repo/opengwasdb-ticket207"),
    Path("/home/gh13047/repo/opengwasdb"),
]:
    if sibling_candidate.exists() and (sibling_candidate / "opengwasdb" / "build" / "resolve.py").exists():
        if str(sibling_candidate) not in sys.path:
            sys.path.insert(0, str(sibling_candidate))
        break

from opengwasdb.ancestry.mixture import AncestryAssignment, Gates  # type: ignore[import-untyped]
from opengwasdb.ancestry.reference import AncestryReference, load_reference  # type: ignore[import-untyped]
from opengwasdb.build.resolve import (  # type: ignore[import-untyped]
    DEFAULT_EVIDENCE_SAMPLE,
    AnalysisRequest,
    AnalysisResolution,
    SdReason,
    SdStatus,
    resolve_analysis,
)
from opengwasdb.model.enums import OriginalSdMethod, StoredEffectScale  # type: ignore[import-untyped]
from opengwasdb.readers.gwas_ssf import GwasSsfReader  # type: ignore[import-untyped]
from resources.generators.lib.source_inventory import (
    ReleaseConfiguration,
    SourceInventoryRow,
    load_release_configuration,
    read_inventory,
    sha256_file,
)


@dataclass(frozen=True)
class AnalysisConcordanceResult:
    """Detailed concordance comparison result for one Analysis."""

    analysis_id: str
    stratum: str
    study_design: str
    sample_size: float | None
    data_file: str
    data_bytes: int

    # Full Reference Resolution
    full_assigned_ancestry: str | None
    full_dominant_superpop: str | None
    full_dominant_proportion: float | None
    full_runner_up_margin: float | None
    full_residual: float | None
    full_af_overlap: int
    full_gate_reason: str
    full_orientation_outcome: str | None
    full_orientation_r: float | None
    full_sd_status: str
    full_sd_reason: str | None
    full_implied_sd: float | None
    full_seconds: float

    # QC Panel Resolution
    panel_assigned_ancestry: str | None
    panel_dominant_superpop: str | None
    panel_dominant_proportion: float | None
    panel_runner_up_margin: float | None
    panel_residual: float | None
    panel_af_overlap: int
    panel_gate_reason: str
    panel_orientation_outcome: str | None
    panel_orientation_r: float | None
    panel_sd_status: str
    panel_sd_reason: str | None
    panel_implied_sd: float | None
    panel_seconds: float

    # Comparative Metrics
    ancestry_match: bool
    dominant_match: bool
    gate_match: bool
    delta_proportion: float | None
    delta_margin: float | None
    delta_residual: float | None
    delta_implied_sd: float | None
    disagreement_category: str | None
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_comparison_tsv_row(self) -> dict[str, str]:
        return {
            "analysis_id": self.analysis_id,
            "stratum": self.stratum,
            "study_design": self.study_design,
            "data_bytes": str(self.data_bytes),
            "full_assigned": self.full_assigned_ancestry or "",
            "panel_assigned": self.panel_assigned_ancestry or "",
            "ancestry_match": str(self.ancestry_match).lower(),
            "full_dominant": self.full_dominant_superpop or "",
            "panel_dominant": self.panel_dominant_superpop or "",
            "dominant_match": str(self.dominant_match).lower(),
            "full_gate": self.full_gate_reason,
            "panel_gate": self.panel_gate_reason,
            "gate_match": str(self.gate_match).lower(),
            "full_overlap": str(self.full_af_overlap),
            "panel_overlap": str(self.panel_af_overlap),
            "full_residual": f"{self.full_residual:.6f}" if self.full_residual is not None else "",
            "panel_residual": f"{self.panel_residual:.6f}" if self.panel_residual is not None else "",
            "delta_residual": f"{self.delta_residual:.6f}" if self.delta_residual is not None else "",
            "full_sd_status": self.full_sd_status,
            "panel_sd_status": self.panel_sd_status,
            "full_implied_sd": f"{self.full_implied_sd:.4f}" if self.full_implied_sd is not None else "",
            "panel_implied_sd": f"{self.panel_implied_sd:.4f}" if self.panel_implied_sd is not None else "",
            "full_orientation_outcome": self.full_orientation_outcome or "",
            "panel_orientation_outcome": self.panel_orientation_outcome or "",
            "full_seconds": f"{self.full_seconds:.2f}",
            "panel_seconds": f"{self.panel_seconds:.2f}",
            "disagreement_category": self.disagreement_category or "",
            "error": self.error or "",
        }


COMPARISON_TSV_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "stratum",
    "study_design",
    "data_bytes",
    "full_assigned",
    "panel_assigned",
    "ancestry_match",
    "full_dominant",
    "panel_dominant",
    "dominant_match",
    "full_gate",
    "panel_gate",
    "gate_match",
    "full_overlap",
    "panel_overlap",
    "full_residual",
    "panel_residual",
    "delta_residual",
    "full_sd_status",
    "panel_sd_status",
    "full_implied_sd",
    "panel_implied_sd",
    "full_orientation_outcome",
    "panel_orientation_outcome",
    "full_seconds",
    "panel_seconds",
    "disagreement_category",
    "error",
)


def categorize_disagreement(
    full_ancestry: AncestryAssignment | None,
    panel_ancestry: AncestryAssignment | None,
    full_sd_implied: float | None,
    panel_sd_implied: float | None,
    error: str | None,
) -> str | None:
    """Categorize the scientific nature of any disagreement between Method A and B."""
    if error:
        return "execution_error"
    if full_ancestry is None or panel_ancestry is None:
        return "resolution_unavailable"

    # 1. Assigned ancestry flips
    if full_ancestry.assigned_ancestry != panel_ancestry.assigned_ancestry:
        # Critical: Full gated out, panel admitted
        if full_ancestry.gate_reason != "ok" and panel_ancestry.gate_reason == "ok":
            return "false_positive_panel_assignment"
        # Diagnostic: Panel overlap dropped below n_min while full had sufficient sites
        if full_ancestry.gate_reason == "ok" and panel_ancestry.gate_reason == "overlap":
            return "overlap_drop"
        # Major: Other gate exclusion by panel
        if full_ancestry.gate_reason == "ok" and panel_ancestry.gate_reason != "ok":
            return "panel_gate_exclusion"
        # Critical: Different non-null ancestries
        if full_ancestry.assigned_ancestry and panel_ancestry.assigned_ancestry:
            return "assigned_ancestry_flip"

    # 2. Dominant superpop mismatch
    if full_ancestry.dominant_superpop != panel_ancestry.dominant_superpop:
        return "dominant_superpop_flip"

    # 3. Orientation flip detection
    if full_ancestry.gate_reason == "eaf_orientation" and panel_ancestry.gate_reason != "eaf_orientation":
        return "orientation_flip_missed"

    # 4. Gate reason mismatch
    if full_ancestry.gate_reason != panel_ancestry.gate_reason:
        return "gate_reason_divergence"

    # 5. Large residual drift (> 0.02)
    if (
        full_ancestry.residual is not None
        and panel_ancestry.residual is not None
        and abs(panel_ancestry.residual - full_ancestry.residual) > 0.02
    ):
        return "residual_divergence"

    # 6. Phenotype SD divergence (> 5% relative)
    if full_sd_implied is not None and panel_sd_implied is not None and full_sd_implied > 0:
        if abs(panel_sd_implied - full_sd_implied) / full_sd_implied > 0.05:
            return "sd_divergence"

    return None


def compare_single_analysis(
    analysis_id: str,
    stratum: str,
    study_design: str,
    sample_size_str: str,
    data_file_path: str,
    data_bytes: int,
    original_sd_method: OriginalSdMethod,
    stored_effect_scale: StoredEffectScale,
    reference: AncestryReference,
    panel_alids: set[str],
    gates: Gates,
) -> AnalysisConcordanceResult:
    """Run one Analysis under both Full Reference and QC Panel and compare outputs."""
    sample_size = None
    if sample_size_str.strip():
        try:
            sample_size = float(sample_size_str.strip())
        except ValueError:
            pass

    req = AnalysisRequest(
        analysis_id=analysis_id,
        source_file=Path(data_file_path),
        sample_size=sample_size,
        original_sd_method=original_sd_method,
        stored_effect_scale=stored_effect_scale,
    )

    data_file = Path(data_file_path)
    if not data_file.is_file():
        return AnalysisConcordanceResult(
            analysis_id=analysis_id,
            stratum=stratum,
            study_design=study_design,
            sample_size=sample_size,
            data_file=data_file_path,
            data_bytes=data_bytes,
            full_assigned_ancestry=None,
            full_dominant_superpop=None,
            full_dominant_proportion=None,
            full_runner_up_margin=None,
            full_residual=None,
            full_af_overlap=0,
            full_gate_reason="missing_file",
            full_orientation_outcome=None,
            full_orientation_r=None,
            full_sd_status="unavailable",
            full_sd_reason="missing_file",
            full_implied_sd=None,
            full_seconds=0.0,
            panel_assigned_ancestry=None,
            panel_dominant_superpop=None,
            panel_dominant_proportion=None,
            panel_runner_up_margin=None,
            panel_residual=None,
            panel_af_overlap=0,
            panel_gate_reason="missing_file",
            panel_orientation_outcome=None,
            panel_orientation_r=None,
            panel_sd_status="unavailable",
            panel_sd_reason="missing_file",
            panel_implied_sd=None,
            panel_seconds=0.0,
            ancestry_match=True,
            dominant_match=True,
            gate_match=True,
            delta_proportion=None,
            delta_margin=None,
            delta_residual=None,
            delta_implied_sd=None,
            disagreement_category="execution_error",
            error=f"Source file not found: {data_file_path}",
        )

    # 1. Method A: Full Reference Scan
    t0 = time.perf_counter()
    reader_full = GwasSsfReader(data_file)
    res_full = resolve_analysis(
        req,
        reader=reader_full,
        reference=reference,
        extraction_panel=None,
        gates=gates,
    )
    t_full = time.perf_counter() - t0

    # 2. Method B: QC Panel Extraction
    t0 = time.perf_counter()
    reader_panel = GwasSsfReader(data_file)
    res_panel = resolve_analysis(
        req,
        reader=reader_panel,
        reference=reference,
        extraction_panel=panel_alids,
        gates=gates,
    )
    t_panel = time.perf_counter() - t0

    # Extract Full Metrics
    full_anc = res_full.ancestry
    full_sd = res_full.phenotype_sd
    full_assigned = full_anc.assigned_ancestry if full_anc else None
    full_dominant = full_anc.dominant_superpop if full_anc else None
    full_prop = full_anc.dominant_proportion if full_anc else None
    full_margin = full_anc.runner_up_margin if full_anc else None
    full_res = full_anc.residual if full_anc else None
    full_overlap = full_anc.af_overlap if full_anc else 0
    full_gate = full_anc.gate_reason if full_anc else ("error" if res_full.error else "unavailable")
    full_orient = full_anc.eaf_orientation if full_anc else None
    full_orient_r = full_anc.eaf_orientation_r if full_anc else None
    if full_orient_r is not None and math.isnan(full_orient_r):
        full_orient_r = None
    full_sd_st = full_sd.status.value if full_sd else "unavailable"
    full_sd_rs = full_sd.reason.value if (full_sd and full_sd.reason) else None
    full_impl_sd = full_sd.estimate.sd if (full_sd and full_sd.estimate) else None

    # Extract Panel Metrics
    panel_anc = res_panel.ancestry
    panel_sd = res_panel.phenotype_sd
    panel_assigned = panel_anc.assigned_ancestry if panel_anc else None
    panel_dominant = panel_anc.dominant_superpop if panel_anc else None
    panel_prop = panel_anc.dominant_proportion if panel_anc else None
    panel_margin = panel_anc.runner_up_margin if panel_anc else None
    panel_res = panel_anc.residual if panel_anc else None
    panel_overlap = panel_anc.af_overlap if panel_anc else 0
    panel_gate = panel_anc.gate_reason if panel_anc else ("error" if res_panel.error else "unavailable")
    panel_orient = panel_anc.eaf_orientation if panel_anc else None
    panel_orient_r = panel_anc.eaf_orientation_r if panel_anc else None
    if panel_orient_r is not None and math.isnan(panel_orient_r):
        panel_orient_r = None
    panel_sd_st = panel_sd.status.value if panel_sd else "unavailable"
    panel_sd_rs = panel_sd.reason.value if (panel_sd and panel_sd.reason) else None
    panel_impl_sd = panel_sd.estimate.sd if (panel_sd and panel_sd.estimate) else None

    # Compute Deltas
    anc_match = (full_assigned == panel_assigned)
    dom_match = (full_dominant == panel_dominant)
    gate_match = (full_gate == panel_gate)
    delta_prop = abs(panel_prop - full_prop) if (full_prop is not None and panel_prop is not None) else None
    delta_margin = abs(panel_margin - full_margin) if (full_margin is not None and panel_margin is not None) else None
    delta_res = abs(panel_res - full_res) if (full_res is not None and panel_res is not None) else None
    delta_sd = (
        abs(panel_impl_sd - full_impl_sd)
        if (full_impl_sd is not None and panel_impl_sd is not None)
        else None
    )

    combined_error = (res_full.error or res_panel.error) or None
    category = categorize_disagreement(
        full_ancestry=full_anc,
        panel_ancestry=panel_anc,
        full_sd_implied=full_impl_sd,
        panel_sd_implied=panel_impl_sd,
        error=combined_error,
    )

    return AnalysisConcordanceResult(
        analysis_id=analysis_id,
        stratum=stratum,
        study_design=study_design,
        sample_size=sample_size,
        data_file=data_file_path,
        data_bytes=data_bytes,
        full_assigned_ancestry=full_assigned,
        full_dominant_superpop=full_dominant,
        full_dominant_proportion=full_prop,
        full_runner_up_margin=full_margin,
        full_residual=full_res,
        full_af_overlap=full_overlap,
        full_gate_reason=full_gate,
        full_orientation_outcome=full_orient,
        full_orientation_r=full_orient_r,
        full_sd_status=full_sd_st,
        full_sd_reason=full_sd_rs,
        full_implied_sd=full_impl_sd,
        full_seconds=t_full,
        panel_assigned_ancestry=panel_assigned,
        panel_dominant_superpop=panel_dominant,
        panel_dominant_proportion=panel_prop,
        panel_runner_up_margin=panel_margin,
        panel_residual=panel_res,
        panel_af_overlap=panel_overlap,
        panel_gate_reason=panel_gate,
        panel_orientation_outcome=panel_orient,
        panel_orientation_r=panel_orient_r,
        panel_sd_status=panel_sd_st,
        panel_sd_reason=panel_sd_rs,
        panel_implied_sd=panel_impl_sd,
        panel_seconds=t_panel,
        ancestry_match=anc_match,
        dominant_match=dom_match,
        gate_match=gate_match,
        delta_proportion=delta_prop,
        delta_margin=delta_margin,
        delta_residual=delta_res,
        delta_implied_sd=delta_sd,
        disagreement_category=category,
        error=combined_error,
    )


# Worker process state for multiprocessing
_WORKER_REF: AncestryReference | None = None
_WORKER_PANEL: set[str] | None = None
_WORKER_GATES: Gates | None = None


def _init_worker(ref: AncestryReference, panel_alids: set[str], gates: Gates) -> None:
    global _WORKER_REF, _WORKER_PANEL, _WORKER_GATES
    _WORKER_REF = ref
    _WORKER_PANEL = panel_alids
    _WORKER_GATES = gates


def _worker_task(task_args: tuple[dict[str, str], str, str]) -> AnalysisConcordanceResult:
    row_dict, orig_method_str, stored_scale_str = task_args
    assert _WORKER_REF is not None and _WORKER_PANEL is not None and _WORKER_GATES is not None
    return compare_single_analysis(
        analysis_id=row_dict["analysis_id"],
        stratum=row_dict.get("stratum", "unassigned"),
        study_design=row_dict["study_design"],
        sample_size_str=row_dict.get("sample_size") or "",
        data_file_path=row_dict["data_file"],
        data_bytes=int(row_dict["data_bytes"]) if row_dict.get("data_bytes", "").strip() else 0,
        original_sd_method=OriginalSdMethod(orig_method_str),
        stored_effect_scale=StoredEffectScale(stored_scale_str),
        reference=_WORKER_REF,
        panel_alids=_WORKER_PANEL,
        gates=_WORKER_GATES,
    )


@dataclass(frozen=True)
class ConcordanceStudySummary:
    """Summary of the entire concordance study across the sample manifest."""

    created_at: str
    sample_manifest_path: str
    config_path: str
    total_analyses: int
    quantitative_count: int
    case_control_count: int
    ancestry_concordance_count: int
    ancestry_concordance_rate: float
    dominant_superpop_concordance_count: int
    dominant_superpop_concordance_rate: float
    gate_reason_concordance_count: int
    gate_reason_concordance_rate: float
    false_positive_eur_count: int
    orientation_sensitivity_rate: float
    total_disagreements: int
    disagreement_counts: dict[str, int]
    mean_full_seconds: float
    mean_panel_seconds: float
    speedup_ratio: float
    criteria_evaluation: dict[str, bool]
    recommendation: str
    results: list[AnalysisConcordanceResult]


def evaluate_concordance_study(
    manifest_path: Path,
    config_path: Path,
    cores: int = 1,
    max_analyses: int | None = None,
    dry_run: bool = False,
    qc_panel_path: Path | None = None,
) -> ConcordanceStudySummary:
    """Execute the concordance study on the sample manifest."""
    repo_root = Path(__file__).resolve().parents[3]
    config = load_release_configuration(config_path, repo_root)
    raw_doc = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}

    # 1. Read sample manifest
    with manifest_path.open("r", encoding="utf-8") as fh:
        raw_rows = list(csv.DictReader(fh, delimiter="\t"))

    if max_analyses is not None and max_analyses >= 0:
        raw_rows = raw_rows[:max_analyses]

    # 2. Load Reference Resource & QC Panel
    # Find ancestry mixture resource
    ancestry_res_id = config.ancestry_reference_resource_id
    ancestry_res = config.reference_resources.get(ancestry_res_id)
    if ancestry_res is None:
        ancestry_res = next(
            (r for r in config.reference_resources.values() if r.kind == "ancestry_mixture"),
            None,
        )

    fine_group_map = (
        dict(ancestry_res.auxiliary_paths).get("fine_group_map")
        if ancestry_res
        else None
    )
    if ancestry_res is None or not ancestry_res.location or not fine_group_map:
        raise ValueError("Config does not declare a valid ancestry_mixture reference resource with fine_group_map")

    anc_block = raw_doc.get("ancestry_assignment") or {}
    maf_floor = float(anc_block.get("maf_floor", 0.01))
    gates_block = anc_block.get("gates") or {}
    gates = Gates(
        tau=float(gates_block.get("tau", 0.50)),
        delta=float(gates_block.get("delta", 0.20)),
        n_min=int(gates_block.get("n_min", 5000)),
        residual_max=float(gates_block.get("residual_max", 0.06)),
        orientation_flip_r=float(gates_block.get("orientation_flip_r", -0.5)),
    )

    # Load 10k QC panel ALIDs
    panel_file = (
        qc_panel_path
        if qc_panel_path is not None
        else repo_root / "resources" / "reference-resources" / "qc-panel-hg38" / "qc_panel.tsv"
    )
    if not panel_file.is_file():
        raise ValueError(f"QC panel file missing at {panel_file}")
    with panel_file.open("r", encoding="utf-8") as fh:
        qc_alids = {row["alid"] for row in csv.DictReader(fh, delimiter="\t")}

    # Prepare worker tasks
    tasks = []
    for r in raw_rows:
        sd = r["study_design"]
        if sd not in config.method_tiers:
            raise ValueError(f"Config declares no method tier for study design {sd!r}")
        tier = config.method_tiers[sd]
        tasks.append((r, tier.original_sd_method, tier.stored_effect_scale))

    if dry_run or len(tasks) == 0:
        return ConcordanceStudySummary(
            created_at=datetime.now(timezone.utc).isoformat(),
            sample_manifest_path=str(manifest_path),
            config_path=str(config_path),
            total_analyses=len(tasks),
            quantitative_count=sum(1 for r in raw_rows if r.get("study_design") == "quantitative"),
            case_control_count=sum(1 for r in raw_rows if r.get("study_design") == "case-control"),
            ancestry_concordance_count=0,
            ancestry_concordance_rate=0.0,
            dominant_superpop_concordance_count=0,
            dominant_superpop_concordance_rate=0.0,
            gate_reason_concordance_count=0,
            gate_reason_concordance_rate=0.0,
            false_positive_eur_count=0,
            orientation_sensitivity_rate=1.0,
            total_disagreements=0,
            disagreement_counts={},
            mean_full_seconds=0.0,
            mean_panel_seconds=0.0,
            speedup_ratio=1.0,
            criteria_evaluation={
                "zero_false_positive_eur": True,
                "complete_orientation_sensitivity": True,
                "genome_wide_concordance_ge_98pct": True,
                "zero_execution_errors": True,
            },
            recommendation="DRY_RUN: Configuration, manifest, and reference resource contracts validated.",
            results=[],
        )

    ref = load_reference(
        freqs_path=ancestry_res.location,
        groups_path=fine_group_map,
        maf_floor=maf_floor,
    )

    results: list[AnalysisConcordanceResult] = []
    if cores > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(
            max_workers=cores,
            initializer=_init_worker,
            initargs=(ref, qc_alids, gates),
        ) as executor:
            futures = [executor.submit(_worker_task, t) for t in tasks]
            for future in as_completed(futures):
                results.append(future.result())
    else:
        _init_worker(ref, qc_alids, gates)
        for t in tasks:
            results.append(_worker_task(t))

    # Sort results by analysis_id
    results.sort(key=lambda x: x.analysis_id)

    # Aggregate Statistics
    total = len(results)
    quant_n = sum(1 for r in results if r.study_design == "quantitative")
    cc_n = sum(1 for r in results if r.study_design == "case-control")
    anc_match_n = sum(1 for r in results if r.ancestry_match)
    dom_match_n = sum(1 for r in results if r.dominant_match)
    gate_match_n = sum(1 for r in results if r.gate_match)

    disagreements = [r for r in results if r.disagreement_category is not None]
    dis_counts: dict[str, int] = {}
    for r in disagreements:
        cat = r.disagreement_category or "unknown"
        dis_counts[cat] = dis_counts.get(cat, 0) + 1

    false_pos_eur = sum(
        1
        for r in results
        if r.full_assigned_ancestry != "EUR" and r.panel_assigned_ancestry == "EUR"
    )

    # Orientation sensitivity check
    orientation_failures_full = [r for r in results if r.full_gate_reason == "eaf_orientation"]
    orientation_caught_panel = sum(
        1 for r in orientation_failures_full if r.panel_gate_reason == "eaf_orientation"
    )
    orient_sens = (
        (orientation_caught_panel / len(orientation_failures_full))
        if orientation_failures_full
        else 1.0
    )

    total_full_sec = sum(r.full_seconds for r in results)
    total_panel_sec = sum(r.panel_seconds for r in results)
    mean_full_sec = total_full_sec / max(1, total)
    mean_panel_sec = total_panel_sec / max(1, total)
    speedup = (total_full_sec / total_panel_sec) if total_panel_sec > 0 else 1.0

    # Decision Criteria
    # 1. Zero False Positives for EUR
    c1 = (false_pos_eur == 0)
    # 2. 100% Orientation sensitivity
    c2 = (orient_sens >= 1.0)
    # 3. >= 98% concordance on genome-wide (>500k variants / >10MB files)
    gw_results = [r for r in results if r.data_bytes > 10_000_000]
    gw_matches = sum(1 for r in gw_results if r.ancestry_match)
    c3 = (gw_matches / len(gw_results) >= 0.98) if gw_results else True
    # 4. No unhandled execution errors
    c4 = all(not r.error for r in results)

    all_criteria_pass = c1 and c2 and c3 and c4
    recommendation = (
        "ADOPT_QC_PANEL: qc-panel-hg38 meets all preregistered concordance and safety criteria."
        if all_criteria_pass
        else "RETAIN_FULL_REFERENCE: Concordance criteria failed; retain full ancestry reference."
    )

    return ConcordanceStudySummary(
        created_at=datetime.now(timezone.utc).isoformat(),
        sample_manifest_path=str(manifest_path),
        config_path=str(config_path),
        total_analyses=total,
        quantitative_count=quant_n,
        case_control_count=cc_n,
        ancestry_concordance_count=anc_match_n,
        ancestry_concordance_rate=(anc_match_n / max(1, total)),
        dominant_superpop_concordance_count=dom_match_n,
        dominant_superpop_concordance_rate=(dom_match_n / max(1, total)),
        gate_reason_concordance_count=gate_match_n,
        gate_reason_concordance_rate=(gate_match_n / max(1, total)),
        false_positive_eur_count=false_pos_eur,
        orientation_sensitivity_rate=orient_sens,
        total_disagreements=len(disagreements),
        disagreement_counts=dis_counts,
        mean_full_seconds=mean_full_sec,
        mean_panel_seconds=mean_panel_sec,
        speedup_ratio=speedup,
        criteria_evaluation={
            "zero_false_positive_eur": c1,
            "complete_orientation_sensitivity": c2,
            "genome_wide_concordance_ge_98pct": c3,
            "zero_execution_errors": c4,
        },
        recommendation=recommendation,
        results=results,
    )


def render_concordance_markdown_report(summary: ConcordanceStudySummary) -> str:
    """Render a comprehensive Markdown concordance study report."""
    lines: list[str] = [
        "# QC Panel Concordance Study Report: Full Reference vs. 10k Panel",
        "",
        f"**Date:** {summary.created_at}  ",
        f"**Sample Manifest:** `{summary.sample_manifest_path}`  ",
        f"**Configuration:** `{summary.config_path}`  ",
        f"**Total Analyses Evaluated:** {summary.total_analyses} ({summary.quantitative_count} Quantitative, {summary.case_control_count} Case-Control)  ",
        "",
        "## 1. Executive Summary",
        "",
        "| Metric | Full Reference (Method A) | QC Panel 10k (Method B) | Concordance |",
        "|---|---|---|---|",
        f"| Assigned Ancestry Identity | Reference | Panel | **{summary.ancestry_concordance_rate * 100:.1f}%** ({summary.ancestry_concordance_count}/{summary.total_analyses}) |",
        f"| Dominant Superpop Identity | Reference | Panel | **{summary.dominant_superpop_concordance_rate * 100:.1f}%** ({summary.dominant_superpop_concordance_count}/{summary.total_analyses}) |",
        f"| Gate Reason Identity | Reference | Panel | **{summary.gate_reason_concordance_rate * 100:.1f}%** ({summary.gate_reason_concordance_count}/{summary.total_analyses}) |",
        f"| False Positive EUR Calls | 0 (baseline) | {summary.false_positive_eur_count} | **{'PASS' if summary.false_positive_eur_count == 0 else 'FAIL'}** |",
        f"| Orientation Flip Sensitivity | 100% (baseline) | {summary.orientation_sensitivity_rate * 100:.1f}% | **{'PASS' if summary.orientation_sensitivity_rate >= 1.0 else 'FAIL'}** |",
        f"| Mean Scan Time per Analysis | {summary.mean_full_seconds:.2f} s | {summary.mean_panel_seconds:.2f} s | **{summary.speedup_ratio:.1f}× Speedup** |",
        "",
        "## 2. Decision Criteria Evaluation",
        "",
        "| Preregistered Criterion | Required Threshold | Observed Value | Status |",
        "|---|---|---|---|",
        f"| Zero False Positive EUR | == 0 | {summary.false_positive_eur_count} | {'✅ PASS' if summary.criteria_evaluation['zero_false_positive_eur'] else '❌ FAIL'} |",
        f"| Orientation Flip Sensitivity | 100.0% | {summary.orientation_sensitivity_rate * 100:.1f}% | {'✅ PASS' if summary.criteria_evaluation['complete_orientation_sensitivity'] else '❌ FAIL'} |",
        f"| Genome-Wide Concordance | $\\ge 98.0\\%$ | {'Pass' if summary.criteria_evaluation['genome_wide_concordance_ge_98pct'] else 'Fail'} | {'✅ PASS' if summary.criteria_evaluation['genome_wide_concordance_ge_98pct'] else '❌ FAIL'} |",
        f"| Zero Execution Errors | == 0 errors | {'0 errors' if summary.criteria_evaluation['zero_execution_errors'] else 'Errors present'} | {'✅ PASS' if summary.criteria_evaluation['zero_execution_errors'] else '❌ FAIL'} |",
        "",
        f"### Verdict & Recommendation: **{summary.recommendation}**",
        "",
        "## 3. Disagreement Breakdown & Dispositions",
        "",
        f"Total Disagreements: **{summary.total_disagreements}**",
        "",
    ]

    if summary.disagreement_counts:
        lines.extend([
            "| Disagreement Category | Count | Severity | Disposition |",
            "|---|---|---|---|",
        ])
        for cat, count in sorted(summary.disagreement_counts.items()):
            severity = "Critical" if "false" in cat or "flip" in cat else ("Diagnostic" if "overlap" in cat else "Minor")
            disp = "Documented sparse file behavior" if "overlap" in cat else "Evaluated against acceptance threshold"
            lines.append(f"| `{cat}` | {count} | {severity} | {disp} |")
        lines.append("")

        lines.extend([
            "### Detailed Disagreements Table",
            "",
            "| Analysis ID | Stratum | Design | Full Assigned | Panel Assigned | Full Gate | Panel Gate | Full Overlap | Panel Overlap | Category |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ])
        for r in summary.results:
            if r.disagreement_category is not None:
                lines.append(
                    f"| `{r.analysis_id}` | `{r.stratum}` | {r.study_design} | "
                    f"`{r.full_assigned_ancestry or 'None'}` | `{r.panel_assigned_ancestry or 'None'}` | "
                    f"`{r.full_gate_reason}` | `{r.panel_gate_reason}` | "
                    f"{r.full_af_overlap} | {r.panel_af_overlap} | `{r.disagreement_category}` |"
                )
        lines.append("")
    else:
        lines.append("No disagreements observed across the entire sample frame.")
        lines.append("")

    return "\n".join(lines)
