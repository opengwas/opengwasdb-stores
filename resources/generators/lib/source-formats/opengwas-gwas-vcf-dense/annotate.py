#!/usr/bin/env python3
"""One-pass AF-based ancestry and phenotype-SD annotation for dense releases."""
from __future__ import annotations

import argparse
import csv
import gzip
import math
import sys
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np
from opengwasdb.ancestry import (
    AncestryReference,
    Gates,
    assign_ancestry,
    load_reference,
)
from opengwasdb.build.phenotype_sd import estimate_phenotype_sd
from opengwasdb.model.enums import OriginalSdMethod, StoredEffectScale
from opengwasdb.readers import (
    GWAS_VCF_CAPABILITY,
    GwasVcfReader,
    load_liftover,
    resolve_reader,
    write_regions_file,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from resources.generators.lib.release_yaml import (  # noqa: E402
    get,
    merge_validation_yaml,
    read_release_yaml,
    resolve_path,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--only-analysis-id", default="")
    parser.add_argument("--source-dir", default="", help="Override source_file by analysis_id.vcf.gz")
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def write_tsv(
    path: Path, rows: Sequence[Mapping[str, object]], columns: list[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def find_resource(build: dict, resource_id: str) -> dict:
    for resource in build.get("reference_resources") or []:
        if resource.get("resource_id") == resource_id:
            return resource
    raise SystemExit(f"No reference resource {resource_id!r} in build.yaml")


def _snp_from_alid(alid: str) -> tuple[str, str, str] | None:
    fields = alid.split(":")
    if len(fields) != 4 or len(fields[2]) != 1 or len(fields[3]) != 1:
        return None
    return fields[0], fields[2].upper(), fields[3].upper()


def make_reference_subset(source: Path, destination: Path, chromosome: str, max_sites: int) -> None:
    """Stream a deterministic SNP subset; OpenGWASDB remains the reference parser."""
    opener = gzip.open if str(source).endswith(".gz") else open
    with opener(source, "rt", newline="", encoding="utf-8") as src, destination.open(
        "w", newline="", encoding="utf-8"
    ) as dst:
        reader = csv.reader(src, delimiter="\t")
        writer = csv.writer(dst, delimiter="\t", lineterminator="\n")
        header = next(reader)
        writer.writerow(header)
        alid_index = header.index("alid")
        kept = 0
        for row in reader:
            parsed = _snp_from_alid(row[alid_index])
            if parsed is None:
                continue
            chrom, a1, a2 = parsed
            if chrom != chromosome or {a1, a2} in ({"A", "T"}, {"C", "G"}):
                continue
            writer.writerow(row)
            kept += 1
            if kept >= max_sites:
                break
    if kept < 2:
        raise SystemExit(f"Reference subset contains only {kept} usable sites")


def finite_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def overall_check(statuses: list[str]) -> str:
    if not statuses:
        return "not_run"
    if "failed" in statuses:
        return "failed"
    if "warning" in statuses or "skipped" in statuses:
        return "passed_with_warnings"
    return "passed"


@dataclass(frozen=True)
class AnnotationContext:
    reference: AncestryReference
    gates: Gates
    source_dir: str
    default_capability: str
    chromosome: str
    regions_file: str
    chain_file: str
    effect_cfg: dict[str, Any]
    resource_id: str
    maf_floor: float
    estimator_version: str


@dataclass(frozen=True)
class AnnotationResult:
    row: dict[str, str]
    ancestry_row: dict[str, object]
    sd_row: dict[str, object]
    ancestry_status: str
    sd_status: str
    warnings: list[str]


@cache
def cached_liftover(chain_file: str) -> object:
    return load_liftover(chain_file=chain_file)


def annotate_one(row: dict[str, str], context: AnnotationContext) -> AnnotationResult:
    """Annotate one Analysis in one AF/SE extraction pass."""
    row = dict(row)
    source_path = (
        Path(context.source_dir) / f"{row['analysis_id']}.vcf.gz"
        if context.source_dir else Path(row["source_file"])
    )
    if not source_path.exists():
        raise FileNotFoundError(f"Source artifact does not exist: {source_path}")
    capability = row.get("source_reader_capability") or context.default_capability
    scale = StoredEffectScale(row["stored_effect_scale"])
    liftover = cached_liftover(context.chain_file) if context.chain_file else None
    if capability == GWAS_VCF_CAPABILITY:
        reader = GwasVcfReader(
            source_path,
            stored_effect_scale=scale,
            liftover=liftover,
            regions_file=Path(context.regions_file) if context.regions_file else None,
            region=context.chromosome if liftover is not None else None,
        )
    else:
        if liftover is not None:
            raise ValueError(
                f"Source Reader Capability {capability!r} cannot use the GWAS-VCF "
                "annotation liftover path"
            )
        reader = resolve_reader(capability, source_path, scale)
    metrics = reader.extract_at_sites(context.reference.index.keys())
    study_af = {alid: metric.af for alid, metric in metrics.items()}
    assignment = assign_ancestry(study_af, context.reference, context.gates)
    ancestry_status = "passed" if assignment.gate_reason == "ok" else "warning"
    assigned = assignment.assigned_ancestry or ""
    row["assigned_ancestry"] = assigned
    row["ancestry_assignment_method"] = "af_assigned" if assigned else "unassigned"
    warnings = []
    if not assigned:
        warnings.append(f"{row['analysis_id']}: ancestry gate failed ({assignment.gate_reason})")
    ancestry_row: dict[str, object] = {
        "analysis_id": row["analysis_id"],
        "source_analysis_id": row.get("source_analysis_id", row["analysis_id"]),
        "source_ancestry_label": row.get("source_ancestry_label", ""),
        "assigned_ancestry": assigned,
        "ancestry_assignment_method": row["ancestry_assignment_method"],
        "ancestry_reference_id": context.resource_id,
        "af_overlap": assignment.af_overlap,
        "dominant_superpop": assignment.dominant_superpop or "",
        "dominant_proportion": assignment.dominant_proportion,
        "runner_up_margin": assignment.runner_up_margin,
        "nnls_residual": assignment.residual,
        "gate_reason": assignment.gate_reason,
        "source_assigned_mismatch": "",
        "ancestry_notes": f"one-pass {capability} AF/SE extraction",
    }
    for superpop in context.reference.superpops:
        column = f"ancestry_prop_{superpop}"
        proportion = assignment.superpop_composition.get(superpop, 0.0)
        ancestry_row[column] = proportion
        # The sidecar records the assignment evidence; the Release Manifest
        # must carry the same composition so the Store builder can preserve it
        # as Analytical Metadata.
        row[column] = str(proportion)

    scale_value = row.get("stored_effect_scale", "")
    sample_size = finite_float(row.get("sample_size", ""))
    if scale_value in {"log_or", "log_hazard"}:
        sd_status, skip_reason, estimate = "skipped", "non_quantitative_effect_scale", None
    elif sample_size is None or not metrics:
        sd_status, skip_reason, estimate = "skipped", "no_usable_site_metrics", None
    else:
        af = np.asarray([metric.af for metric in metrics.values()], dtype=float)
        se = np.asarray([metric.se for metric in metrics.values()], dtype=float)
        estimate = estimate_phenotype_sd(
            OriginalSdMethod.ESTIMATED_FROM_SOURCE_MAF, sample_size, se=se, af=af
        )
        min_sites = int(context.effect_cfg.get("min_overlap_variants", 20))
        if estimate.method is OriginalSdMethod.UNAVAILABLE or len(metrics) < min_sites:
            sd_status, skip_reason = "skipped", "low_overlap"
        elif row.get("original_sd_method") == "declared_standardised":
            tolerance = float(context.effect_cfg.get("sd_tolerance", 0.15))
            delta = abs(estimate.sd - 1.0)
            sd_status = (
                "passed" if delta <= tolerance
                else "warning" if delta <= tolerance * 2
                else "failed"
            )
            skip_reason = ""
        else:
            sd_status, skip_reason = "passed", ""
            row["original_sd"] = str(estimate.sd)
            row["original_sd_method"] = estimate.method.value
    if sd_status in {"warning", "failed"}:
        warnings.append(f"{row['analysis_id']}: empirical effect-scale status={sd_status}")
    sd_row = {
        "analysis_id": row["analysis_id"],
        "source_analysis_id": row.get("source_analysis_id", row["analysis_id"]),
        "status": sd_status,
        "skip_reason": skip_reason,
        "af_source": "source" if estimate else "",
        "ancestry_reference_id": "",
        "original_sd": row.get("original_sd", ""),
        "original_sd_method": row.get("original_sd_method", ""),
        "n_variants_considered": context.reference.n_variants,
        "n_variants_overlapping": len(metrics),
        "n_variants_excluded_ambiguous": "",
        "n_variants_excluded_mismatch": "",
        "n_variants_excluded_missing_af": context.reference.n_variants - len(metrics),
        "n_variants_excluded_maf": 0,
        "n_variants_retained": len(metrics),
        "maf_min": context.maf_floor,
        "maf_max": 0.5,
        "implied_sd_median": estimate.sd if estimate else "",
        "sd_dispersion": estimate.dispersion if estimate else "",
        "sd_notes": (estimate.notes or "one-pass source-AF estimate") if estimate else skip_reason,
        "estimator_version": context.estimator_version,
    }
    print(
        f"{row['analysis_id']}: ancestry={ancestry_status} effect_scale={sd_status} "
        f"overlap={len(metrics)}",
        flush=True,
    )
    return AnnotationResult(row, ancestry_row, sd_row, ancestry_status, sd_status, warnings)


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    release_dir = Path(args.release_dir).resolve()
    build = read_release_yaml(release_dir / "build.yaml")
    ancestry_cfg = get(build, "ancestry_assignment", default={})
    effect_cfg = get(build, "effect_scale_validation", default={})
    if not isinstance(ancestry_cfg, dict) or not ancestry_cfg.get("enabled"):
        raise SystemExit("build.yaml has no enabled ancestry_assignment config")
    if not isinstance(effect_cfg, dict) or not effect_cfg.get("enabled"):
        raise SystemExit("build.yaml has no enabled effect_scale_validation config")

    resource_id = str(ancestry_cfg["reference_resource_id"])
    resource = find_resource(build, resource_id)
    chromosome = str(ancestry_cfg.get("sampling_chromosome", "1"))
    max_sites = int(ancestry_cfg.get("max_reference_sites", 20000))
    maf_floor = float(ancestry_cfg.get("maf_floor", 0.01))
    root = Path(__file__).resolve().parents[5]
    reference_path = resolve_path(root, str(resource["location"]))
    groups_path = resolve_path(root, str(resource["fine_group_map"]))

    raw_gates_cfg = ancestry_cfg.get("gates")
    gates_cfg: dict[str, Any] = raw_gates_cfg if isinstance(raw_gates_cfg, dict) else {}
    gates = Gates(
        tau=float(gates_cfg.get("tau", 0.50)), delta=float(gates_cfg.get("delta", 0.20)),
        n_min=int(gates_cfg.get("n_min", 5000)), residual_max=float(gates_cfg.get("residual_max", 0.06)),
    )
    chain = get(build, "normalisation", "liftover_chain", default=None)
    chain_file = str(resolve_path(root, str(chain))) if chain else ""

    analyses_path = release_dir / "analyses.tsv"
    analyses = read_tsv(analyses_path)
    requested = {x for x in args.only_analysis_id.split(",") if x}
    selected = [row for row in analyses if not requested or row["analysis_id"] in requested]
    if not selected:
        raise SystemExit("No analyses selected")

    try:
        estimator_version = f"opengwasdb:{version('opengwasdb')}"
    except PackageNotFoundError:
        estimator_version = "opengwasdb:unknown"

    with tempfile.TemporaryDirectory() as tmp:
        subset_path = Path(tmp) / "ancestry-reference-subset.tsv"
        make_reference_subset(reference_path, subset_path, chromosome, max_sites)
        reference = load_reference(subset_path, groups_path, maf_floor=maf_floor)
        regions_file = (
            "" if chain_file
            else str(write_regions_file(reference.index.keys(), Path(tmp) / "regions.tsv"))
        )
        context = AnnotationContext(
            reference=reference,
            gates=gates,
            source_dir=str(Path(args.source_dir).resolve()) if args.source_dir else "",
            default_capability=str(
                get(build, "source", "source_reader_capability", default=GWAS_VCF_CAPABILITY)
            ),
            chromosome=chromosome,
            regions_file=regions_file,
            chain_file=chain_file,
            effect_cfg=effect_cfg,
            resource_id=resource_id,
            maf_floor=maf_floor,
            estimator_version=estimator_version,
        )
        if args.workers == 1:
            results = [annotate_one(row, context) for row in selected]
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                results = list(executor.map(annotate_one, selected, [context] * len(selected)))

    updated_by_id = {result.row["analysis_id"]: result.row for result in results}
    analyses = [updated_by_id.get(row["analysis_id"], row) for row in analyses]
    ancestry_rows = [result.ancestry_row for result in results]
    sd_rows = [result.sd_row for result in results]
    ancestry_statuses = [result.ancestry_status for result in results]
    sd_statuses = [result.sd_status for result in results]
    warnings = [warning for result in results for warning in result.warnings]

    write_tsv(analyses_path, analyses, list(analyses[0].keys()))
    ancestry_columns = list(ancestry_rows[0].keys())
    write_tsv(release_dir / "sidecars" / "ancestry.tsv", ancestry_rows, ancestry_columns)
    sd_columns = list(sd_rows[0].keys())
    write_tsv(release_dir / "sidecars" / "sd_estimation.tsv", sd_rows, sd_columns)
    merge_validation_yaml(
        release_dir / "validation.yaml",
        validator_name="resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/annotate.py",
        updated_checks={
            "ancestry": overall_check(ancestry_statuses),
            "effect_scale": overall_check(sd_statuses),
            "sd_estimation": overall_check(sd_statuses),
        },
        new_warnings=warnings,
        updated_reports={"ancestry": "sidecars/ancestry.tsv", "sd_estimation": "sidecars/sd_estimation.tsv"},
    )
    print(
        f"Annotated {len(selected)} analyses in one AF/SE extraction pass each; "
        f"ancestry={overall_check(ancestry_statuses)} effect_scale={overall_check(sd_statuses)}"
    )


if __name__ == "__main__":
    main()
