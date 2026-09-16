"""Materialise the derived build manifest: a bundle `analyses.tsv` minus excluded rows.

`analyses.tsv` is a release selection *and* an audit record. A row may carry
`exclude_from_build: true` because its statistics are retained for review but
must not be built -- OGS-00004 row 0 (`GCST003566`) is the worked case, where
`effect_allele_frequency` is reported against the other allele and building it
aborts opengwasdb's EAF consensus check 45 minutes in. opengwasdb classifies
`exclude_from_build` as a REGISTRY_ONLY column and strips it without acting on
it (its ADR 0034), so the registry has to enforce the exclusion.

This module is the enforcement point: it reads the bundle manifest, drops every
row whose `exclude_from_build` is `true`, re-densifies `analysis_index` over the
survivors when that column exists, and writes the result atomically. It records
which analyses were dropped and why (`inclusion_reason`) in a sidecar so the
audit row in the bundle is explained rather than silently discarded. It never
rewrites a decision into another column.

The Snakefile calls this; `plan()` points the build argv at the derived path it
produces, so no manifest translation logic lives in the Snakefile (ADR 0023) and
no excluded row reaches the builder (ADR 0025).

Fail loudly, never degrade silently: a malformed `exclude_from_build` value, a
row whose field count differs from the header, a manifest with no data rows, an
all-excluded manifest, or a missing `analysis_id` column raises rather than
building something that looks complete.

See docs/spec/store-release-workflow.md and ADR 0025.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXCLUDE_COLUMN: str = "exclude_from_build"
ANALYSIS_ID_COLUMN: str = "analysis_id"
ANALYSIS_INDEX_COLUMN: str = "analysis_index"
INCLUSION_REASON_COLUMN: str = "inclusion_reason"

_TRUE: str = "true"
_FALSE: str = "false"


class ManifestError(ValueError):
    """Base error for a bundle manifest that cannot be materialised."""


class MissingManifestColumnError(ManifestError):
    """Raised when a required column is absent from the bundle manifest."""


class MalformedExclusionError(ManifestError):
    """Raised when `exclude_from_build` holds a value other than blank/true/false."""


class MalformedRowError(ManifestError):
    """Raised when a data row's field count differs from the header's.

    A short row would otherwise have its missing fields invented as blanks and
    an overflowing row would have its extra fields silently discarded, producing
    a well-formed-looking build manifest that does not match the bundle.
    """


class EmptyBuildManifestError(ManifestError):
    """Raised when a manifest has no data rows, or every row is excluded."""


@dataclass(frozen=True)
class DroppedAnalysis:
    """One audit row excluded from the derived build manifest."""

    analysis_id: str
    row_index: int
    inclusion_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "row_index": self.row_index,
            "inclusion_reason": self.inclusion_reason,
        }


@dataclass(frozen=True)
class BuildManifestResult:
    """What `materialise_build_manifest` read, dropped, and wrote."""

    source_path: Path
    manifest_path: Path
    sidecar_path: Path
    columns: list[str]
    n_source_rows: int
    n_kept_rows: int
    n_dropped_rows: int
    redensified_analysis_index: bool
    dropped: list[DroppedAnalysis]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_manifest": str(self.source_path),
            "build_manifest": str(self.manifest_path),
            "columns": list(self.columns),
            "n_source_rows": self.n_source_rows,
            "n_kept_rows": self.n_kept_rows,
            "n_dropped_rows": self.n_dropped_rows,
            "redensified_analysis_index": self.redensified_analysis_index,
            "dropped": [d.to_dict() for d in self.dropped],
        }


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort fsync of a directory to persist directory metadata on POSIX."""
    try:
        fd = os.open(str(dir_path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _write_text_atomically(text: str, dest_path: Path) -> None:
    """Atomically write text to dest_path via a temp file + os.replace, like run.py."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(f".{dest_path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    with open(temp_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, dest_path)
    _fsync_dir(dest_path.parent)


def _classify_exclusion(raw: str | None, row_index: int, analysis_id: str) -> bool:
    """Return True if `raw` marks the row excluded, False if it is kept, else raise."""
    normalized = (raw or "").strip().lower()
    if normalized in ("", _FALSE):
        return False
    if normalized == _TRUE:
        return True
    label = f"analysis_id {analysis_id!r}" if analysis_id else "no analysis_id"
    raise MalformedExclusionError(
        f"{EXCLUDE_COLUMN} at data row {row_index} ({label}) is {raw!r}; "
        f"expected blank, 'true', or 'false' (case-insensitive)"
    )


def materialise_build_manifest(
    source_path: Path | str,
    manifest_path: Path | str,
    sidecar_path: Path | str,
) -> BuildManifestResult:
    """Write the filtered build manifest and its exclusion audit sidecar.

    Drops every row whose `exclude_from_build` is `true`, keeps all columns and
    row order, and re-densifies `analysis_index` to 0..n-1 over the survivors
    when that column exists. Refuses a ragged row (field count differing from the
    header) before writing anything. Atomic on both outputs.
    """
    source = Path(source_path)
    manifest = Path(manifest_path)
    sidecar = Path(sidecar_path)

    with open(source, "r", encoding="utf-8", newline="") as f:
        raw_reader = csv.reader(f, delimiter="\t")
        header = next(raw_reader, None)
        columns = list(header) if header is not None else []
        raw_rows = list(raw_reader)

    if ANALYSIS_ID_COLUMN not in columns:
        raise MissingManifestColumnError(
            f"{source} is missing required column {ANALYSIS_ID_COLUMN!r}; "
            f"present columns: {columns}"
        )
    if not raw_rows:
        raise EmptyBuildManifestError(f"{source} has a header but no data rows")

    # Refuse a ragged row before writing anything: a short row must not have its
    # missing fields invented as blanks, and an overflowing row must not have its
    # extra fields discarded. Either would yield a plausible manifest that does
    # not match the bundle. A genuinely blank trailing field is a well-formed row
    # whose field count still matches, so it is accepted.
    analysis_id_col = columns.index(ANALYSIS_ID_COLUMN)
    rows: list[dict[str, str]] = []
    for row_index, fields in enumerate(raw_rows):
        if len(fields) != len(columns):
            readable_id = (
                fields[analysis_id_col].strip()
                if analysis_id_col < len(fields)
                else ""
            )
            label = (
                f"analysis_id {readable_id!r}" if readable_id else "analysis_id not readable"
            )
            raise MalformedRowError(
                f"{source}: data row {row_index} ({label}) has {len(fields)} fields; "
                f"expected {len(columns)}"
            )
        rows.append(dict(zip(columns, fields)))

    has_index = ANALYSIS_INDEX_COLUMN in columns
    kept: list[dict[str, Any]] = []
    dropped: list[DroppedAnalysis] = []

    for row_index, row in enumerate(rows):
        analysis_id = (row.get(ANALYSIS_ID_COLUMN) or "").strip()
        if _classify_exclusion(row.get(EXCLUDE_COLUMN), row_index, analysis_id):
            dropped.append(
                DroppedAnalysis(
                    analysis_id=analysis_id,
                    row_index=row_index,
                    inclusion_reason=(row.get(INCLUSION_REASON_COLUMN) or ""),
                )
            )
            continue
        kept.append(row)

    if not kept:
        raise EmptyBuildManifestError(
            f"{source}: all {len(rows)} data rows are excluded from the build; "
            f"refusing to write an empty build manifest"
        )

    if has_index:
        for new_index, row in enumerate(kept):
            row[ANALYSIS_INDEX_COLUMN] = str(new_index)

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=columns,
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in kept:
        writer.writerow(row)

    result = BuildManifestResult(
        source_path=source,
        manifest_path=manifest,
        sidecar_path=sidecar,
        columns=columns,
        n_source_rows=len(rows),
        n_kept_rows=len(kept),
        n_dropped_rows=len(dropped),
        redensified_analysis_index=has_index,
        dropped=dropped,
    )

    _write_text_atomically(
        json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n", sidecar
    )
    _write_text_atomically(output.getvalue(), manifest)
    return result


__all__ = [
    "ANALYSIS_ID_COLUMN",
    "ANALYSIS_INDEX_COLUMN",
    "EXCLUDE_COLUMN",
    "INCLUSION_REASON_COLUMN",
    "BuildManifestResult",
    "DroppedAnalysis",
    "EmptyBuildManifestError",
    "MalformedExclusionError",
    "MalformedRowError",
    "ManifestError",
    "MissingManifestColumnError",
    "materialise_build_manifest",
]
