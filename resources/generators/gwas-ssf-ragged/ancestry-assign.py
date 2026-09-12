#!/usr/bin/env python3
"""Run AF-based ancestry assignment over a release bundle (issues #23, #25).

For each Analysis with usable source allele frequencies, builds a canonical
`{alid: af}` map from its retained filtered source rows and calls
`opengwasdb.ancestry.assign_ancestry` against the release's configured
ancestry-mixture Reference Resource, writing one row per attempted Analysis to
`sidecars/ancestry.tsv`. Analyses without usable source AF are left untouched
at whatever `ancestry_assignment_method` the generator already gave them
(`source_trusted_no_af`, per issue #11's settled policy) and still get an
explicit skipped sidecar row.

Mirrors the workflow's phase shape (issue #103): reads
`build.yaml`/`analyses.tsv`, writes a small TSV sidecar, and merges its own
`checks.ancestry` into `validation.yaml` via the shared
`resources/lib/release_yaml.py::merge_validation_yaml` helper, so no stage
clobbers another's checks.

Usage:
  pixi run python resources/generators/gwas-ssf-ragged/ancestry-assign.py \
    --release-dir=families/<family>/releases/<release>
"""

from __future__ import annotations

import argparse
import csv
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from resources.lib.release_yaml import (  # noqa: E402
    get,
    merge_validation_yaml,
    read_release_yaml,
    repo_root,
    require_text,
    resolve_path,
)

from opengwasdb.ancestry import Gates, assign_ancestry, load_reference  # noqa: E402
from opengwasdb.readers import is_palindromic  # noqa: E402
from opengwasdb.variants.normalise import VariantNormalisationError, orient_to_canonical  # noqa: E402

# Best-effort mapping from this registry's free-text source ancestry labels
# (see resources/data/derived/store-candidates-analyses.tsv `ancestry_group`)
# to the ancestry-mixture reference's super-population codes, used only to
# flag a source/assigned disagreement. Deliberately conservative: ambiguous
# labels (Multiple/Mixed, NR/Unknown, Other, Asian (unspecified)) are left
# unmapped so no mismatch is fabricated from a label that isn't precise
# enough to compare.
SOURCE_LABEL_TO_SUPERPOP = {
    "African": "AFR",
    "East Asian": "EAS",
    "European": "EUR",
    "South Asian": "SAS",
    "South East Asian": "EAS",
    "Greater Middle Eastern": "MID",
    "Hispanic or Latin American": "AMR",
    "Native American": "AMR",
}

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True)
    return parser.parse_args()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def read_filtered_ssf(path: Path) -> list[dict[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8") as fh:  # type: ignore[operator]
        return list(csv.DictReader(fh, delimiter="\t"))


def has_usable_source_af(rows: list[dict[str, str]]) -> bool:
    for row in rows:
        try:
            af = float(row.get("effect_allele_frequency", ""))
        except (TypeError, ValueError):
            continue
        if 0.0 < af < 1.0:
            return True
    return False


def build_study_af(rows: list[dict[str, str]]) -> dict[str, float]:
    """Canonical `{alid: af}` map, dropping unusable/palindromic/malformed rows."""
    out: dict[str, float] = {}
    for row in rows:
        try:
            af = float(row.get("effect_allele_frequency", ""))
        except (TypeError, ValueError):
            continue
        if not (0.0 < af < 1.0):
            continue
        effect, other = row.get("effect_allele", ""), row.get("other_allele", "")
        if is_palindromic(effect, other):
            continue
        try:
            orientation = orient_to_canonical(
                row["chromosome"], row["base_pair_location"], effect, other
            )
        except (VariantNormalisationError, KeyError, ValueError):
            continue
        alid = orientation.variant.alid
        out[alid] = (1.0 - af) if orientation.flipped else af
    return out


def find_reference_resource(build: dict, resource_id: str) -> dict:
    for resource in build.get("reference_resources") or []:
        if isinstance(resource, dict) and resource.get("resource_id") == resource_id:
            return resource
    raise SystemExit(f"No reference_resources entry with resource_id={resource_id!r} in build.yaml")


def sidecar_row(
    row: dict[str, str],
    resource_id: str,
    superpops: list[str],
    *,
    gate_reason: str,
    dominant_superpop: str | None = None,
    dominant_proportion: float | None = None,
    runner_up_margin: float | None = None,
    af_overlap: int | None = None,
    residual: float | None = None,
    composition: dict[str, float] | None = None,
    assigned_ancestry: str = "",
    notes: str = "",
) -> dict[str, object]:
    source_label = row.get("source_ancestry_label", "")
    expected_superpop = SOURCE_LABEL_TO_SUPERPOP.get(source_label)
    mismatch = ""
    if expected_superpop is not None and dominant_superpop is not None:
        mismatch = "true" if expected_superpop != dominant_superpop else "false"

    out: dict[str, object] = {
        "analysis_id": row["analysis_id"],
        "source_analysis_id": row.get("source_analysis_id", row["analysis_id"]),
        "source_ancestry_label": source_label,
        "assigned_ancestry": assigned_ancestry,
        "ancestry_assignment_method": (
            "af_assigned" if gate_reason == "ok"
            else row.get("ancestry_assignment_method", "") if gate_reason == "no_usable_source_af"
            else "unassigned"
        ),
        "ancestry_reference_id": resource_id if gate_reason != "no_usable_source_af" else "",
        "af_overlap": af_overlap if af_overlap is not None else "",
        "dominant_superpop": dominant_superpop or "",
        "dominant_proportion": f"{dominant_proportion:.6g}" if dominant_proportion is not None else "",
        "runner_up_margin": f"{runner_up_margin:.6g}" if runner_up_margin is not None else "",
        "nnls_residual": f"{residual:.6g}" if residual is not None and residual == residual else "",
        "gate_reason": gate_reason,
        "source_assigned_mismatch": mismatch,
        "ancestry_notes": notes,
    }
    for sp in superpops:
        out[f"ancestry_prop_{sp}"] = f"{composition.get(sp, 0.0):.6g}" if composition else ""
    return out


def main() -> None:
    args = parse_args()
    release_dir = Path(args.release_dir).resolve()
    root = repo_root(release_dir)
    build = read_release_yaml(release_dir / "build.yaml")

    ancestry_cfg = get(build, "ancestry_assignment", default=None)
    if not isinstance(ancestry_cfg, dict) or not ancestry_cfg.get("enabled"):
        raise SystemExit("build.yaml has no enabled ancestry_assignment config; nothing to do")

    resource_id = require_text(ancestry_cfg, "reference_resource_id")
    resource = find_reference_resource(build, resource_id)
    gates_cfg = ancestry_cfg.get("gates") if isinstance(ancestry_cfg.get("gates"), dict) else {}
    gates = Gates(
        tau=float(gates_cfg.get("tau", 0.50)),
        delta=float(gates_cfg.get("delta", 0.20)),
        n_min=int(gates_cfg.get("n_min", 5000)),
        residual_max=float(gates_cfg.get("residual_max", 0.06)),
    )

    maf_floor = float(ancestry_cfg.get("maf_floor", 0.01))
    reference = load_reference(str(resource["location"]), str(resource["fine_group_map"]), maf_floor=maf_floor)
    superpops = list(reference.superpops)

    filtered_dir = resolve_path(root, require_text(build, "artifacts", "filtered_dir"))
    analyses_path = release_dir / "analyses.tsv"
    analyses = read_tsv(analyses_path)

    sidecar_rows: list[dict[str, object]] = []
    updates: dict[str, tuple[str, str]] = {}  # analysis_id -> (assigned_ancestry, method)

    for row in analyses:
        filtered_file = row.get("filtered_file", "")
        ssf_path = filtered_dir / filtered_file if filtered_file else None
        ssf_rows = read_filtered_ssf(ssf_path) if ssf_path and ssf_path.exists() else []

        if not has_usable_source_af(ssf_rows):
            sidecar_rows.append(sidecar_row(
                row, resource_id, superpops, gate_reason="no_usable_source_af",
                notes="No usable source allele frequencies; left at source-trusted ancestry.",
            ))
            continue

        study_af = build_study_af(ssf_rows)
        result = assign_ancestry(study_af, reference, gates)
        assigned = result.assigned_ancestry or ""
        sidecar_rows.append(sidecar_row(
            row, resource_id, superpops,
            gate_reason=result.gate_reason,
            dominant_superpop=result.dominant_superpop,
            dominant_proportion=result.dominant_proportion,
            runner_up_margin=result.runner_up_margin,
            af_overlap=result.af_overlap,
            residual=result.residual,
            composition=result.superpop_composition,
            assigned_ancestry=assigned,
            notes=(
                f"dominant={result.dominant_superpop} proportion={result.dominant_proportion:.3f} "
                f"margin={result.runner_up_margin:.3f} overlap={result.af_overlap} "
                f"residual={result.residual:.4f}"
            ),
        ))
        if result.gate_reason == "ok":
            updates[row["analysis_id"]] = (assigned, "af_assigned")
        else:
            updates[row["analysis_id"]] = ("", "unassigned")

    # Column order follows docs/release-metadata-schema.md's Ancestry sidecar
    # table, with the dynamic ancestry_prop_* family inserted after gate_reason.
    sidecar_columns = (
        ["analysis_id", "source_analysis_id", "source_ancestry_label", "assigned_ancestry",
         "ancestry_assignment_method", "ancestry_reference_id", "af_overlap",
         "dominant_superpop", "dominant_proportion", "runner_up_margin", "nnls_residual", "gate_reason"]
        + [f"ancestry_prop_{sp}" for sp in superpops]
        + ["source_assigned_mismatch", "ancestry_notes"]
    )
    sidecar_dir = release_dir / "sidecars"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    with (sidecar_dir / "ancestry.tsv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=sidecar_columns, lineterminator="\n")
        writer.writeheader()
        for row in sidecar_rows:
            writer.writerow(row)

    if updates:
        fieldnames = list(analyses[0].keys())
        for row in analyses:
            if row["analysis_id"] in updates:
                assigned, method = updates[row["analysis_id"]]
                row["assigned_ancestry"] = assigned
                row["ancestry_assignment_method"] = method
        with analyses_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, delimiter="\t", fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(analyses)

    attempted = [r for r in sidecar_rows if r["gate_reason"] != "no_usable_source_af"]
    n_failed_gate = sum(1 for r in attempted if r["gate_reason"] != "ok")
    checks_warnings: list[str] = []
    for r in attempted:
        if r["gate_reason"] != "ok":
            checks_warnings.append(f"{r['analysis_id']}: ancestry gate failed ({r['gate_reason']})")
    for r in sidecar_rows:
        if r["source_assigned_mismatch"] == "true":
            checks_warnings.append(
                f"{r['analysis_id']}: source_ancestry_label={r['source_ancestry_label']!r} "
                f"disagrees with AF-based dominant_superpop={r['dominant_superpop']!r}"
            )

    if not attempted:
        ancestry_check = "not_run"
    elif n_failed_gate == 0 and not checks_warnings:
        ancestry_check = "passed"
    else:
        ancestry_check = "passed_with_warnings"

    merge_validation_yaml(
        release_dir / "validation.yaml",
        validator_name="resources/generators/gwas-ssf-ragged/ancestry-assign.py",
        updated_checks={"ancestry": ancestry_check},
        updated_reports={},
        new_warnings=checks_warnings,
    )

    print(
        f"Ancestry assignment: {len(sidecar_rows)} analyses "
        f"({len(sidecar_rows) - len(attempted)} skipped, {len(attempted)} attempted, "
        f"{len(attempted) - n_failed_gate} af_assigned); checks.ancestry={ancestry_check}"
    )


if __name__ == "__main__":
    main()
