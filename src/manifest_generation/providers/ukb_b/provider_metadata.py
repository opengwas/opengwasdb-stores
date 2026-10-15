#!/usr/bin/env python3
"""Offline provider-JSON metadata resolution for the ukb-b batch (issue #219).

`POST /api/gwasinfo` needs a live, unexpired OpenGWAS JWT; the token this host
holds expired 2026-08-21. Every ukb-b Analysis, however, ships a provider JSON
sidecar (`<id>.json`) in the source mirror carrying the same study-level fields
the API served (`units`, `sample_size`, `ncase`, `ncontrol`, `population`,
`batch`, `author`, `consortium`). This module turns those files into a
traceable resolution record:

- it resolves **exactly** the frozen inventory's Analysis IDs, and refuses a
  JSON whose own `id` or `batch` does not match (an exact-ID check, not a glob);
- it applies the same resolution contract as the live resolver
  (`resources/generators/lib/metadata_resolvers/opengwas_api.R`) -- `unit` maps
  to `stored_effect_scale`, a usable `ncase`/`ncontrol` pair means
  `case_control`, otherwise `sample_size` means `total`;
- it records each JSON's sha256 so the resolution is bound to frozen bytes;
- it cross-checks every previously-resolved Analysis against the recorded
  `opengwas-api-r2026-08-10` resolution and reports disagreements rather than
  hiding them.

Ancestry is taken from the provider's own `population` field as a
`source_fallback`: it is the source's label, not an AF-based assignment. The
ukb-b VCFs do carry `FORMAT/AF`, so the historical `source_trusted_no_af` value
is deliberately not reproduced, and `af_assigned` is not claimed until
`opengwasdb resolve-analyses` actually runs.

Usage:

    python3 -m manifest_generation.providers.ukb_b.provider_metadata derive \\
        --inventory resources/inventories/ukb-b-restored-2026-10-11.tsv \\
        --source-dir /data/opengwasdb/raw/gwas-vcf/ukb-b \\
        --out resources/inventories/ukb-b-provider-resolution-2026-10-11.tsv \\
        --receipt resources/inventories/ukb-b-provider-resolution-2026-10-11.receipt.json \\
        --recorded families/ukb-b/releases/dense-observed-vcf-c128-resolved/analyses.tsv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PROVIDER_BATCH: str = "ukb-b"
#: Provider JSON outputs columns the resolution record needs. The analysis TSV
#: is deliberately shape-compatible with the candidate emitter's resolution input.
RESOLUTION_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "provider_id",
    "provider_batch",
    "provider_population",
    "provider_units",
    "provider_author",
    "provider_consortium",
    "provider_build",
    "provider_json_sha256",
    "provider_json_bytes",
    "resolution_status",
    "resolution_notes",
    "source_label",
    "analysis_label",
    "trait_ontology_id",
    "trait_ontology_label",
    "trait_ontology_mapping_method",
    "source_ancestry_label",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "original_effect_scale",
    "original_sd",
    "original_sd_method",
    "stored_effect_scale",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "n_cases",
    "n_controls",
    "license",
    "consortium",
    "first_author",
    "inclusion_reason",
    "exclude_from_build",
)

#: The fields the cross-check compares against the recorded API resolution.
COMPARED_FIELDS: tuple[str, ...] = (
    "stored_effect_scale",
    "sample_size_kind",
    "sample_size",
    "n_cases",
    "n_controls",
)


class ProviderMetadataError(ValueError):
    """A provider JSON is missing, unreadable, or does not match the frozen id."""


@dataclass(frozen=True)
class ProviderRecord:
    analysis_id: str
    provider_id: str
    batch: str
    population: str
    units: str
    trait: str
    author: str
    consortium: str
    build: str
    sha256: str
    size: int
    resolution_status: str
    resolution_notes: str
    stored_effect_scale: str
    sample_size_kind: str
    sample_size: str
    n_cases: str
    n_controls: str


def _classify_stored_effect_scale(units: object) -> str | None:
    """Mirror the live resolver's `unit` -> scale vocabulary (returns None when unusable)."""
    if units is None:
        return None
    normalised = "".join(ch for ch in str(units).lower() if ch.isalpha())
    if not normalised or normalised == "na":
        return None
    if "loghazard" in normalised or "hazardratio" in normalised:
        return "log_hazard"
    if "logodds" in normalised or "logor" in normalised:
        return "log_or"
    if normalised.startswith("sd"):
        return "sd"
    return None


def _as_count(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _render_number(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def resolve_record(analysis_id: str, document: Mapping[str, object], *, sha256: str, size: int) -> ProviderRecord:
    """Apply the live resolver's contract to one provider JSON document."""
    provider_id = str(document.get("id", ""))
    if provider_id.lower() != analysis_id.lower():
        raise ProviderMetadataError(
            f"{analysis_id}: provider JSON id {provider_id!r} does not match the frozen analysis id"
        )
    batch = str(document.get("batch", ""))
    if batch.lower() != PROVIDER_BATCH:
        raise ProviderMetadataError(f"{analysis_id}: provider JSON batch {batch!r} is not {PROVIDER_BATCH!r}")

    units = document.get("units")
    scale = _classify_stored_effect_scale(units)
    n_cases = _as_count(document.get("ncase"))
    n_controls = _as_count(document.get("ncontrol"))
    sample_size = _as_count(document.get("sample_size"))
    is_case_control = n_cases is not None and n_controls is not None
    if scale is None:
        scale = "log_or" if is_case_control else "sd"

    if is_case_control:
        kind = "case_control"
        total = sample_size if sample_size is not None else n_cases + n_controls  # type: ignore[operator]
        status = "resolved"
        # When the provider also declares a total N, an ncase+ncontrol sum that
        # disagrees is a provenance fact worth surfacing, never silently reconciled.
        if sample_size is not None and sample_size != n_cases + n_controls:  # type: ignore[operator]
            notes = (
                f"provider sample_size {_render_number(sample_size)} != "
                f"ncase+ncontrol {_render_number(n_cases + n_controls)}"  # type: ignore[operator]
            )
        else:
            notes = ""
    elif sample_size is not None:
        kind, total = "total", sample_size
        n_cases = n_controls = None
        status, notes = "resolved", ""
    else:
        return ProviderRecord(
            analysis_id=analysis_id, provider_id=provider_id, batch=batch,
            population=str(document.get("population", "")), units="" if units is None else str(units),
            trait=str(document.get("trait", "")), author=str(document.get("author", "")),
            consortium=str(document.get("consortium", "")), build=str(document.get("build", "")),
            sha256=sha256, size=size, resolution_status="unresolved",
            resolution_notes="provider JSON had neither a usable ncase/ncontrol pair nor a usable sample_size",
            stored_effect_scale="", sample_size_kind="", sample_size="", n_cases="", n_controls="",
        )

    return ProviderRecord(
        analysis_id=analysis_id,
        provider_id=provider_id,
        batch=batch,
        population=str(document.get("population", "")),
        units="" if units is None else str(units),
        trait=str(document.get("trait", "")),
        author=str(document.get("author", "")),
        consortium=str(document.get("consortium", "")),
        build=str(document.get("build", "")),
        sha256=sha256,
        size=size,
        resolution_status=status,
        resolution_notes=notes,
        stored_effect_scale=scale,
        sample_size_kind=kind,
        sample_size=_render_number(total),
        n_cases=_render_number(n_cases),
        n_controls=_render_number(n_controls),
    )


def read_provider_records(
    analysis_ids: Iterable[str], source_dir: Path
) -> tuple[dict[str, ProviderRecord], list[str]]:
    """Resolve exactly the given analysis ids from their provider JSON sidecars."""
    records: dict[str, ProviderRecord] = {}
    problems: list[str] = []
    for analysis_id in analysis_ids:
        path = source_dir / f"{analysis_id}.json"
        if not path.is_file():
            problems.append(f"{analysis_id}: provider JSON not found at {path}")
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                problems.append(f"{analysis_id}: provider JSON is not an object")
                continue
            records[analysis_id] = resolve_record(
                analysis_id, document, sha256=sha256_file(path), size=path.stat().st_size
            )
        except (OSError, json.JSONDecodeError, ProviderMetadataError) as exc:
            problems.append(f"{analysis_id}: {exc}")
    return records, problems


def _resolve_ancestry(record: ProviderRecord) -> tuple[str, str]:
    """Provider `population` -> (source label, assigned code).

    The provider's own ancestry label is a source fallback, never an AF
    assignment: `population: European` maps to `EUR` with method
    `source_fallback`; an unrecognised label yields an empty code.
    """
    label = record.population.strip()
    mapping = {
        "european": "EUR",
        "african": "AFR",
        "east asian": "EAS",
        "south asian": "SAS",
        "admixed american": "AMR",
        "middle eastern": "MID",
        "north african": "NAF",
    }
    return label, mapping.get(label.lower(), "")


def build_resolution_rows(
    records: Mapping[str, ProviderRecord],
    *,
    inclusion_reason: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for analysis_id in sorted(records):
        record = records[analysis_id]
        label, assigned = _resolve_ancestry(record)
        row = {name: "" for name in RESOLUTION_COLUMNS}
        row.update(
            {
                "analysis_id": analysis_id,
                "provider_id": record.provider_id,
                "provider_batch": record.batch,
                "provider_population": record.population,
                "provider_units": record.units,
                "provider_author": record.author,
                "provider_consortium": record.consortium,
                "provider_build": record.build,
                "provider_json_sha256": record.sha256,
                "provider_json_bytes": str(record.size),
                "resolution_status": record.resolution_status,
                "resolution_notes": record.resolution_notes,
                "source_label": record.trait,
                "analysis_label": record.trait,
                "trait_ontology_mapping_method": "unmapped",
                "source_ancestry_label": label,
                "assigned_ancestry": assigned,
                "ancestry_assignment_method": "source_fallback" if assigned else "unassigned",
                "original_effect_scale": record.stored_effect_scale,
                "original_sd_method": (
                    "binary_trait"
                    if record.stored_effect_scale in {"log_or", "log_hazard"}
                    else "declared_standardised"
                    if record.stored_effect_scale == "sd"
                    else "unavailable"
                ),
                "stored_effect_scale": record.stored_effect_scale,
                "sample_size_kind": record.sample_size_kind,
                "sample_size_scope": "analysis_level",
                "sample_size": record.sample_size,
                "n_cases": record.n_cases,
                "n_controls": record.n_controls,
                "license": "UK Biobank terms of access",
                "consortium": record.consortium or "MRC-IEU",
                "first_author": record.author or "Ben Elsworth",
                "inclusion_reason": inclusion_reason,
            }
        )
        rows.append(row)
    return rows


def cross_check(
    records: Mapping[str, ProviderRecord],
    recorded: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    """Compare provider resolution against a recorded resolution for shared ids.

    A shared id is only *evaluated* when the recorded row carries at least one
    non-blank value to compare against. A recorded exclusion
    (``exclude_from_build: true``) or an all-blank recorded row is **skipped**, not
    counted as an agreement: the three historical API-missing rows have every
    compared field blank, so they are provider-only resolutions, not crosschecked
    matches. Per-field coverage is reported so a reader can see which fields were
    actually compared.
    """
    shared = sorted(set(records) & set(recorded))
    field_stats: dict[str, dict[str, int]] = {
        field: {"compared": 0, "missing": 0, "disagreements": 0} for field in COMPARED_FIELDS
    }
    disagreements: list[dict[str, str]] = []
    skipped: list[str] = []
    evaluated = 0
    for analysis_id in shared:
        row = recorded[analysis_id]
        record = records[analysis_id]
        provider_values = {
            "stored_effect_scale": record.stored_effect_scale,
            "sample_size_kind": record.sample_size_kind,
            "sample_size": record.sample_size,
            "n_cases": record.n_cases,
            "n_controls": record.n_controls,
        }
        if row.get("exclude_from_build", "").lower() == "true":
            skipped.append(analysis_id)
            continue
        present = 0
        for field in COMPARED_FIELDS:
            recorded_value = (row.get(field) or "").strip()
            if recorded_value == "":
                field_stats[field]["missing"] += 1
                continue
            present += 1
            field_stats[field]["compared"] += 1
            provider_value = provider_values[field]
            if not _numbers_equal(recorded_value, provider_value):
                field_stats[field]["disagreements"] += 1
                disagreements.append(
                    {"analysis_id": analysis_id, "field": field, "recorded": recorded_value, "provider": provider_value}
                )
        if present == 0:
            skipped.append(analysis_id)
        else:
            evaluated += 1
    return {
        "shared_ids": len(shared),
        "evaluated": evaluated,
        "skipped": len(skipped),
        "skipped_ids": skipped,
        "skipped_reason": "recorded exclusion (exclude_from_build=true) or every compared field blank",
        "per_field": field_stats,
        "disagreements": disagreements,
        "population_counts": _counts(record.population for record in records.values()),
        "batch_counts": _counts(record.batch.lower() for record in records.values()),
    }


def _numbers_equal(left: str, right: str) -> bool:
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return left == right


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def render_resolution_tsv(rows: Sequence[Mapping[str, str]]) -> str:
    lines = ["\t".join(RESOLUTION_COLUMNS)]
    for row in rows:
        lines.append("\t".join(row.get(name, "") for name in RESOLUTION_COLUMNS))
    return "\n".join(lines) + "\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _read_ids(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [row["analysis_id"] for row in csv.DictReader(handle, delimiter="\t")]


def _read_indexed(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["analysis_id"]: row for row in csv.DictReader(handle, delimiter="\t")}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    derive = sub.add_parser("derive", help="resolve provider JSON sidecars for a frozen inventory")
    derive.add_argument("--inventory", required=True)
    derive.add_argument("--source-dir", required=True)
    derive.add_argument("--out", required=True)
    derive.add_argument("--receipt", required=True)
    derive.add_argument("--recorded", help="a recorded resolution table to cross-check against")
    derive.add_argument(
        "--inclusion-reason",
        default="all ukb-b traits in the OpenGWAS IGD collection (data/ukb-b/manifest.tsv)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inventory_path = Path(args.inventory)
    analysis_ids = _read_ids(inventory_path)
    records, problems = read_provider_records(analysis_ids, Path(args.source_dir))
    if problems:
        raise SystemExit("provider metadata problems:\n" + "\n".join(f" - {p}" for p in problems))
    unresolved = sorted(a for a, r in records.items() if r.resolution_status != "resolved")
    rows = build_resolution_rows(records, inclusion_reason=args.inclusion_reason)
    out_path = Path(args.out)
    _atomic_write(out_path, render_resolution_tsv(rows))

    check: dict[str, object] = {}
    if args.recorded:
        check = cross_check(records, _read_indexed(Path(args.recorded)))

    receipt = {
        "inventory_path": str(inventory_path),
        "inventory_sha256": sha256_file(inventory_path),
        "source_dir": str(args.source_dir),
        "resolution_path": str(out_path),
        "resolution_sha256": sha256_file(out_path),
        "rows": len(rows),
        "unresolved": unresolved,
        "provider_batch_expected": PROVIDER_BATCH,
        "cross_check": check,
    }
    _atomic_write(Path(args.receipt), json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    print(
        f"Resolved {len(rows) - len(unresolved)}/{len(rows)} provider JSON records to {out_path}; "
        f"unresolved={unresolved}"
    )
    if check:
        print(
            f"Cross-check against {args.recorded}: {check['shared_ids']} shared ids, "
            f"{check['evaluated']} evaluated, {check['skipped']} skipped (no recorded values), "
            f"{len(check['disagreements'])} disagreement(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
