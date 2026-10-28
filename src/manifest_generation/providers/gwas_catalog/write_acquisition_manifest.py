#!/usr/bin/env python3
"""Write the GWAS Catalog Acquisition Manifest from the EBI download passes (#283, ADR 0036).

The GWAS Catalog downloader (``download_ebi_gwas_catalog_eur_hybrid.py``)
writes one status manifest per acquisition pass: a base pass, a retry of its
transient failures, a rescue through EBI's harmonised index. Overlaying them is
acquisition's business, so it happens here, once, with the rule the
candidate-table freeze applied: passes are given
earliest first, a later pass is authoritative for every ``analysis_id`` it
covers, and a later pass may not turn a ready Analysis into a non-ready one.

The result is one canonical Acquisition Manifest (``acquisition_manifest``):
per Analysis, its harmonised data file (``location``, ``bytes``, ``sha256``,
``source_url``), its ``-meta.yaml`` (the sidecar columns), its readiness
``status`` and ``error``. The download manifests' study columns
(``publication_pmid``, ``trait``, ``study_design``, ``sample_size``) are intent,
not availability, and are left to the Source Metadata Table; ``seconds`` is
per-run timing and is dropped. A sidecar ``<out-stem>.meta.yaml`` records each
pass's path, sha256, size, row count, readiness counts and overrides, so the
manifest traces back to the passes it was written from.

Usage:
    python3 -m manifest_generation.providers.gwas_catalog.write_acquisition_manifest \\
        --config resources/generators/gwas-catalog-eur-hybrid/config-full.yaml --out <manifest.tsv>
    python3 -m manifest_generation.providers.gwas_catalog.write_acquisition_manifest \\
        --pass base=<manifest.tsv> --pass retry_transient=<manifest.tsv> --out <manifest.tsv>
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    load_acquisition_manifest,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import write_connector
from manifest_generation.source_inventory import (
    KNOWN_READINESS_STATUSES,
    READY_STATUSES,
    InventoryError,
    PreflightConfigError,
    atomic_write,
    counts,
    duplicate_values,
    read_tsv,
    sha256_file,
)

#: Download-manifest column -> canonical Acquisition Manifest column.
COLUMN_MAP: tuple[tuple[str, str], ...] = (
    ("analysis_id", "source_analysis_id"),
    ("status", "status"),
    ("data_file", "location"),
    ("data_bytes", "bytes"),
    ("sha256", "sha256"),
    ("data_url", "source_url"),
    ("yaml_file", "sidecar_location"),
    ("yaml_url", "sidecar_url"),
    ("yaml_bytes", "sidecar_bytes"),
    ("error", "error"),
)

#: The download-pass columns this writer reads. ``seconds`` is deliberately
#: not among them: it is per-run download timing, not a release fact, and
#: carrying it would make the frozen inventory depend on how fast the mirror was.
ACQUISITION_MANIFEST_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "publication_pmid",
    "trait",
    "study_design",
    "sample_size",
    "status",
    "data_url",
    "yaml_url",
    "data_file",
    "yaml_file",
    "data_bytes",
    "yaml_bytes",
    "sha256",
    "error",
)


@dataclass(frozen=True)
class AcquisitionPass:
    """One acquisition pass a freeze overlays, and the role it plays in the merge.

    A release declares its passes as an ordered sequence, earliest first. Order is
    the whole precedence rule: for an ``analysis_id`` two passes both cover, the
    later pass wins.
    """

    role: str
    path: Path




def read_acquisition_pass(path: Path) -> list[dict[str, str]]:
    """Read one acquisition status manifest, failing on duplicate or unknown rows.

    Duplicate ``analysis_id`` inside a single manifest is a defect in that
    manifest: the merge rule (a later manifest overrides an earlier one) cannot
    resolve two rows that claim the same identity in the same pass.
    """
    rows = read_tsv(path, ACQUISITION_MANIFEST_COLUMNS, "acquisition manifest")
    duplicates = duplicate_values(row["analysis_id"] for row in rows)
    if duplicates:
        raise InventoryError(
            f"acquisition manifest {path} has duplicate analysis_id: {', '.join(duplicates)}"
        )
    unknown = sorted({row["status"] for row in rows} - KNOWN_READINESS_STATUSES)
    if unknown:
        raise InventoryError(
            f"acquisition manifest {path} has unknown readiness status: {', '.join(unknown)}"
        )
    return rows


def merge_acquisition_manifests(
    passes: Sequence[tuple[str, Sequence[Mapping[str, str]]]],
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Overlay ordered acquisition passes; return ``(merged, overrides_by_role)``.

    ``passes`` is ``(role, rows)`` in precedence order, earliest first — the order
    the release config declares. Each later pass is the later observation of the
    same upstream file, so it wins for every ``analysis_id`` it covers. Every row
    a pass overrides is counted against that pass's role, so how much each pass
    moved the frozen snapshot stays visible in the provenance.
    """
    merged: dict[str, dict[str, str]] = {}
    overrides_by_role: dict[str, int] = {}
    for role, rows in passes:
        overridden = 0
        for row in rows:
            analysis_id = row["analysis_id"]
            if analysis_id in merged:
                overridden += 1
            merged[analysis_id] = dict(row)
        overrides_by_role[role] = overrides_by_role.get(role, 0) + overridden
    return [merged[key] for key in sorted(merged)], overrides_by_role


def reject_ready_regressions(
    passes: Sequence[tuple[str, Sequence[Mapping[str, str]]]],
    merged: Mapping[str, Mapping[str, str]],
) -> None:
    """Fail when a later pass turns a ready Analysis into a non-ready one.

    Overlaying later passes is safe because a later pass is the later observation
    of the same upstream file — but "later" is not a licence to discard a verified
    source file. A pass that regressed an already-ready row would silently drop a
    release member, so this is an error rather than a precedence rule (issue #151).
    """
    ready_status: dict[str, str] = {}
    last_writer: dict[str, str] = {}
    for role, rows in passes:
        for row in rows:
            analysis_id = row["analysis_id"]
            last_writer[analysis_id] = role
            if row["status"] in READY_STATUSES:
                ready_status.setdefault(analysis_id, row["status"])
    regressions = [
        (analysis_id, status, merged[analysis_id]["status"], last_writer[analysis_id])
        for analysis_id, status in sorted(ready_status.items())
        if merged[analysis_id]["status"] not in READY_STATUSES
    ]
    if not regressions:
        return
    detail = "; ".join(
        f"{analysis_id} ({status} -> {merged_status} by {role})"
        for analysis_id, status, merged_status, role in regressions[:10]
    )
    raise InventoryError(
        f"{len(regressions)} Analysis(es) were ready in an earlier acquisition pass but a later "
        f"pass recorded them as non-ready: {detail}"
    )



def parse_freeze_inputs(raw: Any, path: Path) -> tuple[AcquisitionPass, ...]:
    """Parse the ordered acquisition passes, where list order is the precedence rule.

    The config lists passes earliest first, so the order in the file is the merge
    order rather than a mapping's incidental key order. A duplicate role would make
    two passes indistinguishable in the provenance, so it is rejected here.
    """
    where = f"{path}:source.inventory.freeze_inputs"
    if not isinstance(raw, list) or not raw:
        raise PreflightConfigError(
            f"{where} must be a non-empty ordered list of {{role, path}} entries, earliest pass first"
        )
    passes: list[AcquisitionPass] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise PreflightConfigError(f"{where}[{index}] is not a mapping with 'role' and 'path'")
        role = str(entry.get("role") or "").strip()
        value = str(entry.get("path") or "").strip()
        if not role or not value:
            raise PreflightConfigError(
                f"{where}[{index}] needs a non-empty 'role' and 'path'"
            )
        if role in seen:
            raise PreflightConfigError(f"{where} declares role {role!r} twice")
        seen.add(role)
        passes.append(AcquisitionPass(role=role, path=Path(value)))
    return tuple(passes)


def parse_pass_arguments(values: Sequence[str], *, flag: str) -> tuple[AcquisitionPass, ...]:
    """Parse repeated ``ROLE=PATH`` command-line values into ordered acquisition passes.

    ``flag`` is the option the values came from, so an error names what the
    operator typed. Order is the precedence rule, exactly as in a config's
    ``freeze_inputs``; a role given twice is refused.
    """
    passes: list[AcquisitionPass] = []
    seen: set[str] = set()
    for value in values:
        role, separator, path_value = value.partition("=")
        role = role.strip()
        path_value = path_value.strip()
        if not separator or not role or not path_value:
            raise PreflightConfigError(f"{flag} expects ROLE=PATH, got {value!r}")
        if role in seen:
            raise PreflightConfigError(f"{flag} declares role {role!r} twice")
        seen.add(role)
        passes.append(AcquisitionPass(role=role, path=Path(path_value).expanduser()))
    return tuple(passes)


def write_from_passes(passes: Sequence[AcquisitionPass], out: Path) -> Path:
    """Overlay ``passes`` (earliest first) into one Acquisition Manifest; return its sidecar path."""
    if not passes:
        raise InventoryError("an Acquisition Manifest needs at least one download pass")
    read = [(entry.role, read_acquisition_pass(entry.path)) for entry in passes]
    merged, overrides = merge_acquisition_manifests(read)
    reject_ready_regressions(read, {row["analysis_id"]: row for row in merged})
    out = Path(out)
    write_connector(
        out,
        ACQUISITION_MANIFEST,
        [canonical for _, canonical in COLUMN_MAP],
        [{canonical: row[column] for column, canonical in COLUMN_MAP} for row in merged],
    )
    try:
        manifest = load_acquisition_manifest(out)
    except InventoryError:
        out.unlink()
        raise
    sidecar = pass_lineage_path(out)
    document: dict[str, Any] = {
        "acquisition_manifest": str(out),
        "sha256": manifest.sha256,
        "rows": len(manifest.entries),
        "readiness_counts": manifest.readiness_counts,
        "written_by": "manifest_generation.providers.gwas_catalog.write_acquisition_manifest",
        "overlay_rule": "passes earliest first; a later pass wins for every analysis_id it covers "
        "and may not regress a ready Analysis",
        "passes": [
            {
                "role": entry.role,
                "path": str(entry.path),
                "sha256": sha256_file(entry.path),
                "bytes": entry.path.stat().st_size,
                "rows": len(rows),
                "readiness_counts": counts(row["status"] for row in rows),
                "overrides": overrides[entry.role],
            }
            for entry, (_, rows) in zip(passes, read)
        ],
    }
    atomic_write(sidecar, yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100))
    return sidecar


def passes_from_config(path: Path) -> tuple[AcquisitionPass, ...]:
    """The ordered passes a candidate-workflow config declares in ``source.inventory.freeze_inputs``."""
    document: Mapping[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    try:
        raw = document["source"]["inventory"]["freeze_inputs"]
    except (KeyError, TypeError):
        raise InventoryError(f"{path} declares no source.inventory.freeze_inputs") from None
    return parse_freeze_inputs(raw, Path(path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path, help="a config whose source.inventory.freeze_inputs lists the passes")
    source.add_argument("--pass", dest="passes", action="append", metavar="ROLE=PATH",
                        help="one download pass; repeatable, earliest first")
    parser.add_argument("--out", required=True, type=Path, help="the Acquisition Manifest to write")
    args = parser.parse_args(argv)
    try:
        passes = passes_from_config(args.config) if args.config else parse_pass_arguments(args.passes, flag="--pass")
        sidecar = write_from_passes(passes, args.out)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Acquisition Manifest {args.out} written from {len(passes)} pass(es); provenance {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
