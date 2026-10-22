#!/usr/bin/env python3
"""Offline provider-JSON resolution for the ukb-b batch (issue #219).

`POST /api/gwasinfo` needs a live, unexpired OpenGWAS JWT; the token this host
holds expired 2026-08-21. Every ukb-b Analysis, however, ships a provider JSON
sidecar (`<id>.json`) in the source mirror carrying the same study-level fields
the API served (`units`, `sample_size`, `ncase`, `ncontrol`, `population`,
`batch`, `author`, `consortium`). `read_provider_records()` turns those files
into `ProviderRecord`s, which the ukb-b Source Metadata Table adapter
(`providers.ukb_b.source_metadata_adapter`, #289) projects the canonical table
from (#282 review):

- it resolves **exactly** the declared Analysis IDs, and refuses a JSON whose
  own `id` or `batch` does not match (an exact-ID check, not a glob);
- it applies the same resolution contract as the live resolver did
  (`resources/generators/lib/metadata_resolvers/opengwas_api.R`, retired with
  ukb-b's R path in #289) -- `unit` maps to `stored_effect_scale`, a usable
  `ncase`/`ncontrol` pair means `case_control`, otherwise `sample_size` means
  `total`;
- it records each JSON's sha256 so the resolution is bound to frozen bytes.

Ancestry is taken from the provider's own `population` field as a
`source_fallback`: it is the source's label, not an AF-based assignment. The
ukb-b VCFs do carry `FORMAT/AF`, so the historical `source_trusted_no_af` value
is deliberately not reproduced, and `af_assigned` is not claimed until
`opengwasdb resolve-analyses` actually runs.

This module resolves and nothing else: the broad resolution-row projection the
retired generator wrote, and its hard-coded ancestry map, are gone (#282
review). The resolution record #219 derived that way, and its cross-check
against `opengwas-api-r2026-08-10` (shared ids 2,514 / evaluated 2,511 /
skipped 3), are history the receipts in `resources/inventories/` keep.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


PROVIDER_BATCH: str = "ukb-b"


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


def _text(value: object) -> str:
    """A provider JSON text field, read from the raw value.

    JSON ``null`` is absence, not the string ``"None"``: stringifying it first
    would put "None" in a bundle field and hide that the provider said nothing.
    A non-string value (a number, a list) is described as JSON, never flattened.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return json.dumps(value)
    return ""


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
            population=_text(document.get("population")),
            units="" if units is None else str(units),
            trait=_text(document.get("trait")), author=_text(document.get("author")),
            consortium=_text(document.get("consortium")), build=_text(document.get("build")),
            sha256=sha256, size=size, resolution_status="unresolved",
            resolution_notes="provider JSON had neither a usable ncase/ncontrol pair nor a usable sample_size",
            stored_effect_scale="", sample_size_kind="", sample_size="", n_cases="", n_controls="",
        )

    return ProviderRecord(
        analysis_id=analysis_id,
        provider_id=provider_id,
        batch=batch,
        population=_text(document.get("population")),
        units="" if units is None else str(units),
        trait=_text(document.get("trait")),
        author=_text(document.get("author")),
        consortium=_text(document.get("consortium")),
        build=_text(document.get("build")),
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
