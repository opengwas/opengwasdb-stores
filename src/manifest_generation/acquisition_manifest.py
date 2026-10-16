"""The Acquisition Manifest: what acquisition produced (issue #283, ADR 0036).

Acquisition writes one row per Analysis it acquired a file for: the file's
location, size and sha256, where it came from, and an optional per-Analysis
metadata sidecar (a GWAS-SSF ``-meta.yaml``). It also writes a row for an
Analysis it tried and could not acquire, with a non-ready ``status`` and the
``error``: that is still an availability fact, recorded so freeze can tell an
explained absence from a silent one. The manifest never records intent; the
Source Metadata Table does.

One row per Analysis means one file per Analysis. A file several Analyses
share -- eQTLGen's BESD file set -- cannot be recorded yet and is refused as a
duplicate file; #291 extends the manifest when eQTLGen moves.

A file acquisition had to reformat before any reader could open it -- a
structural change a Column Map cannot express, such as splitting one file into
one per Analysis -- carries its lineage: the original's location and sha256 and
the tool that reformatted it, so a Release Bundle traces back to what the
provider published.

Loading is a gate: an unknown column or status, a ready file missing its
location, size or checksum, a partial lineage, or a duplicate Analysis or file
is refused, every problem named at once. The readiness vocabulary is
``source_inventory``'s (``READY_STATUSES``, ``KNOWN_READINESS_STATUSES``), so
"ready" keeps one spelling.

Write a manifest with ``connector_tsv.write_connector(path,
ACQUISITION_MANIFEST, columns, rows)``. ``docs/spec/source-connectors.md``
specifies every column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from manifest_generation.connector_tsv import (
    ConnectorSpec,
    FreezeError,
    raise_problems,
    read_connector,
)
from manifest_generation.source_inventory import (
    KNOWN_READINESS_STATUSES,
    READY_STATUSES,
    counts,
    duplicate_values,
    sha256_file,
)

#: Columns every manifest carries. ``source_analysis_id`` and ``status`` have a
#: value on every row; the file columns on every ready row.
REQUIRED_COLUMNS: tuple[str, ...] = ("source_analysis_id", "status", "location", "bytes", "sha256")

#: The file facts a ready row must record.
READY_FIELDS: tuple[str, ...] = ("location", "bytes", "sha256")

#: The lineage of a reformatted file: all three, or none.
LINEAGE_COLUMNS: tuple[str, ...] = ("original_location", "original_sha256", "reformat_tool")

OPTIONAL_COLUMNS: tuple[str, ...] = (
    "source_url",
    "sidecar_location",
    "sidecar_url",
    "sidecar_bytes",
    "error",
) + LINEAGE_COLUMNS

#: Every canonical column, in the order a writer emits them.
COLUMNS: tuple[str, ...] = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

# No leading zero, so a size survives the round trip to an integer and back.
_SIZE = re.compile(r"\A(?:0|[1-9]\d*)\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class AcquisitionManifestError(FreezeError):
    """An Acquisition Manifest is not usable as the record of what was acquired."""


#: The file contract the shared connector reader and writer apply.
ACQUISITION_MANIFEST = ConnectorSpec(
    what="Acquisition Manifest",
    columns=COLUMNS,
    required_columns=REQUIRED_COLUMNS,
    error=AcquisitionManifestError,
)


def pass_lineage_path(manifest_path: Path) -> Path:
    """Where an acquisition writer records the passes a manifest came from: ``<stem>.meta.yaml``."""
    manifest_path = Path(manifest_path)
    return manifest_path.with_name(f"{manifest_path.name.removesuffix('.tsv')}.meta.yaml")


@dataclass(frozen=True)
class Lineage:
    """Where a reformatted file came from, and what reformatted it."""

    original_location: str
    original_sha256: str
    reformat_tool: str

    def as_dict(self) -> dict[str, str]:
        return {
            "original_location": self.original_location,
            "original_sha256": self.original_sha256,
            "reformat_tool": self.reformat_tool,
        }


@dataclass(frozen=True)
class AcquisitionEntry:
    """One Analysis's acquisition outcome: its file, or why there is none.

    ``bytes`` and ``sidecar_bytes`` are ``None`` when acquisition recorded no
    size -- absence, never zero.
    """

    source_analysis_id: str
    status: str
    location: str
    bytes: int | None
    sha256: str
    source_url: str = ""
    sidecar_location: str = ""
    sidecar_url: str = ""
    sidecar_bytes: int | None = None
    error: str = ""
    lineage: Lineage | None = None

    @property
    def ready(self) -> bool:
        return self.status in READY_STATUSES

    @property
    def files(self) -> tuple[str, ...]:
        """Every local path this entry claims."""
        return tuple(path for path in (self.location, self.sidecar_location) if path)


@dataclass(frozen=True)
class AcquisitionManifest:
    """A loaded, validated Acquisition Manifest, its rows as written, and its identity."""

    path: Path
    sha256: str
    bytes: int
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]
    entries: tuple[AcquisitionEntry, ...]
    _by_id: Mapping[str, AcquisitionEntry] = field(repr=False, compare=False, default_factory=dict)

    def entry(self, analysis_id: str) -> AcquisitionEntry:
        return self._by_id[analysis_id]

    def __contains__(self, analysis_id: object) -> bool:
        return analysis_id in self._by_id

    @property
    def readiness_counts(self) -> dict[str, int]:
        return counts(entry.status for entry in self.entries)


def load_acquisition_manifest(path: Path) -> AcquisitionManifest:
    """Load and validate a canonical Acquisition Manifest, refusing it on any problem.

    Not to be confused with ``source_inventory.read_acquisition_pass``, which
    reads one GWAS Catalog download pass in the downloader's own columns.
    """
    path = Path(path)
    header, numbered = read_connector(path, ACQUISITION_MANIFEST)
    problems: list[str] = []
    entries: list[AcquisitionEntry] = []
    for line_number, row in numbered:
        row_problems = _row_problems(row)
        if row_problems:
            label = row.get("source_analysis_id") or "(no source_analysis_id)"
            problems.extend(f"line {line_number} ({label}): {problem}" for problem in row_problems)
            continue
        entries.append(_entry(row))
    repeated = duplicate_values(row["source_analysis_id"] for _, row in numbered if row["source_analysis_id"])
    if repeated:
        problems.append(f"duplicate source_analysis_id: {', '.join(repeated)}")
    repeated_files = duplicate_values(path for entry in entries for path in entry.files)
    if repeated_files:
        problems.append(f"duplicate file claimed by more than one entry: {', '.join(repeated_files)}")
    if problems:
        raise_problems(AcquisitionManifestError, f"Acquisition Manifest {path}", problems)
    return AcquisitionManifest(
        path=path,
        sha256=sha256_file(path),
        bytes=path.stat().st_size,
        columns=header,
        rows=tuple(row for _, row in numbered),
        entries=tuple(entries),
        _by_id={entry.source_analysis_id: entry for entry in entries},
    )


# ---------------------------------------------------------------------------
# Per-row rules
# ---------------------------------------------------------------------------


def _row_problems(row: Mapping[str, str]) -> list[str]:
    problems: list[str] = []

    def value(column: str) -> str:
        return row.get(column, "")

    for column in ("source_analysis_id", "status"):
        if not value(column):
            problems.append(f"{column} is empty")
    status = value("status")
    if status and status not in KNOWN_READINESS_STATUSES:
        problems.append(f"status {status!r} is not a known readiness status")
    if status in READY_STATUSES:
        absent = [column for column in READY_FIELDS if not value(column)]
        if absent:
            problems.append(f"a ready file needs {', '.join(absent)}")
    for column in ("bytes", "sidecar_bytes"):
        if value(column) and not _SIZE.match(value(column)):
            problems.append(f"{column} {value(column)!r} is not a size in bytes")
    for column in ("sha256", "original_sha256"):
        if value(column) and not _SHA256.match(value(column)):
            problems.append(f"{column} {value(column)!r} is not a lower-case sha256 digest")
    if any(value(column) for column in LINEAGE_COLUMNS):
        absent = [column for column in LINEAGE_COLUMNS if not value(column)]
        if absent:
            problems.append(f"lineage needs {', '.join(LINEAGE_COLUMNS)}; {', '.join(absent)} empty")
        if status not in READY_STATUSES:
            problems.append(f"lineage is recorded on a {status or 'status-less'!r} entry with no ready file")
    return problems


def _entry(row: Mapping[str, str]) -> AcquisitionEntry:
    lineage = (
        Lineage(*(row[column] for column in LINEAGE_COLUMNS))
        if row.get("original_location")
        else None
    )
    return AcquisitionEntry(
        source_analysis_id=row["source_analysis_id"],
        status=row["status"],
        location=row["location"],
        bytes=int(row["bytes"]) if row["bytes"] else None,
        sha256=row["sha256"],
        source_url=row.get("source_url", ""),
        sidecar_location=row.get("sidecar_location", ""),
        sidecar_url=row.get("sidecar_url", ""),
        sidecar_bytes=int(row["sidecar_bytes"]) if row.get("sidecar_bytes") else None,
        error=row.get("error", ""),
        lineage=lineage,
    )
