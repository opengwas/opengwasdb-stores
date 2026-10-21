"""The shared region filter: a release's region policy applied to one source file (#300).

A region-filtered release (Chen 2023, Sun 2018) keeps only the regions its
release config's region policy names (``release_config.region_policy``), and
acquisition applies it: it downloads each file, filters it, records the
filtered file and its lineage in the Acquisition Manifest, and only then
deletes the original (``manifest_generation.region_filtered_acquisition``,
ADR 0036 clarified by #300). The policy is the R ``gwas-ssf-ragged``
generator's, reproduced row for row:

- **cis** (``cis_plus_signals`` only): every row within ``cis_flank_bp`` of the
  Analysis's target span, from the Source Metadata Table;
- **significant trans**: every row within ``trans_flank_bp`` of a row outside
  the cis window with ``p_value <= significant_p``;
- **suggestive leads**: of the remaining rows with ``significant_p < p_value
  <= suggestive_p``, the strongest first, each kept only when no lead already
  kept on its chromosome is within ``suggestive_lead_min_distance_bp``;
- **the QC panel**: every row at a QC-panel position, whatever its p-value, so
  Analysis Resolution can still measure ancestry and phenotype SD. It is part
  of every region policy, never optional.

A row with no position or no p-value is in no region and is dropped, as the R
filter dropped it. The filter keeps whole source rows, byte for byte, with the
source's header: it selects, it never reformats, so the filtered file is read
exactly as the original would have been.

The file is read twice -- once for the rows that can be signals, once to keep
rows -- so memory holds only the signals, never the file. Filtering also
reports its plan, as R's ``sparse_regions.tsv`` recorded it (:class:`Region`).
"""

from __future__ import annotations

import bisect
import csv
import gzip
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from manifest_generation.source_inventory import sha256_file

#: The region policies a release may declare (Association Coverage).
ASSOCIATION_COVERAGES: tuple[str, ...] = ("signals_only", "cis_plus_signals")

#: The source columns the filter refuses a file without, as the R filter did.
REQUIRED_SOURCE_COLUMNS: tuple[str, ...] = (
    "chromosome", "base_pair_location", "effect_allele", "other_allele",
    "beta", "standard_error", "p_value",
)

#: The values that mean "no value" in a source cell (``data.table::fread``'s).
_MISSING: frozenset[bytes] = frozenset({b"", b"NA"})


class RegionFilterError(ValueError):
    """A region policy or a source file cannot be filtered as declared."""


@dataclass(frozen=True)
class RegionPolicy:
    """A release's region policy (``release_config.region_policy``)."""

    region_policy_id: str
    association_coverage: str
    trans_flank_bp: int
    significant_p: float
    suggestive_p: float
    suggestive_lead_min_distance_bp: int
    #: Only for ``cis_plus_signals``.
    cis_flank_bp: int | None = None


@dataclass(frozen=True)
class QcPanel:
    """The QC panel every region policy keeps: its identity and its positions."""

    resource_id: str
    sha256: str
    #: ``(chromosome, position)``, the chromosome without a ``chr`` prefix.
    positions: frozenset[tuple[str, int]]


def load_qc_panel(path: Path, resource_id: str) -> QcPanel:
    """Read a ``qc_panel`` Reference Resource's table (``chromosome``, ``position``)."""
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if not {"chromosome", "position"} <= set(reader.fieldnames or ()):
            raise RegionFilterError(f"QC panel {path} needs chromosome and position columns")
        positions = frozenset(
            (clean_chromosome(entry["chromosome"]), int(entry["position"])) for entry in reader
        )
    if not positions:
        raise RegionFilterError(f"QC panel {path} lists no positions")
    return QcPanel(resource_id=resource_id, sha256=sha256_file(path), positions=positions)


@dataclass(frozen=True)
class TargetSpan:
    """An Analysis's target gene span on the source build (the Source Metadata Table's).

    ``gene_id`` and ``symbol`` name the gene in the plan's cis region; the span
    alone decides what is kept.
    """

    chromosome: str
    start: int
    end: int
    gene_id: str = ""
    symbol: str = ""


#: ``Region.kind`` values, in the order a plan lists them (R's ``region_kind``).
REGION_KINDS: tuple[str, ...] = ("cis", "significant_trans", "suggestive_trans", "qc_panel")


@dataclass(frozen=True)
class Region:
    """One region a filtered file kept, as R's ``sparse_regions.tsv`` recorded it.

    A cis window or a merged significant-trans range, each with the rows kept
    in it; a suggestive lead, a one-base region named by its ``variant_id``
    (else its ``rsid``); or the QC-panel rows kept on one chromosome, spanning
    the first to the last. Ends are included.
    """

    kind: str
    chromosome: str
    start: int
    end: int
    n_variants_retained: int
    lead_variant_id: str = ""


@dataclass(frozen=True)
class FilterSummary:
    """What filtering one file kept, in the R filter's ``filter_summary`` terms, and its plan."""

    input_rows: int
    retained_rows: int
    cis_rows: int
    significant_trans_regions: int
    significant_trans_rows: int
    suggestive_leads: int
    qc_panel_rows: int
    #: The plan in R's order: cis, then trans ranges by chromosome (as text) and
    #: start, then leads and QC-panel chromosomes in file order.
    regions: tuple[Region, ...] = ()

    def counts(self) -> dict[str, int]:
        """The counts alone, as ``filter_summary.tsv`` records them."""
        return {name: value for name, value in self.__dict__.items() if name != "regions"}


@dataclass(frozen=True)
class _Signal:
    """A row whose p-value can make it a significant hit or a suggestive lead."""

    index: int
    chromosome: str
    position: int
    p_value: float


def filter_file(
    source: Path,
    destination: Path,
    policy: RegionPolicy,
    qc_panel: QcPanel,
    *,
    target: TargetSpan | None = None,
) -> FilterSummary:
    """Filter ``source`` to ``policy`` into ``destination`` (gzip), atomically."""
    source, destination = Path(source), Path(destination)
    header, columns = _read_header(source)
    signals = [
        _Signal(index, chromosome, position, p_value)
        for index, _line, chromosome, position, p_value in _rows(source, columns)
        if p_value <= policy.suggestive_p
    ]
    cis = _cis_intervals(policy, target)
    trans = merge_intervals(
        (signal.chromosome, max(1, signal.position - policy.trans_flank_bp),
         signal.position + policy.trans_flank_bp)
        for signal in signals
        if signal.p_value <= policy.significant_p
        and not _contains(cis, signal.chromosome, signal.position)
    )
    leads = pick_leads(
        (
            signal for signal in signals
            if signal.p_value > policy.significant_p
            and not _contains(cis, signal.chromosome, signal.position)
            and not _contains(trans, signal.chromosome, signal.position)
        ),
        policy.suggestive_lead_min_distance_bp,
    )

    input_rows = retained = cis_rows = trans_rows = qc_rows = 0
    in_interval: dict[tuple[str, int, int], int] = {}
    lead_regions: list[Region] = []
    panel_spans: dict[str, list[int]] = {}
    partial = destination.with_name(destination.name + ".partial")
    try:
        # mtime 0 and no file name: the same rows always give the same bytes.
        with partial.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as out:
                out.write(header)
                for index, line, chromosome, position, _p_value in _rows(source, columns):
                    input_rows += 1
                    in_cis = _contains(cis, chromosome, position)
                    in_trans = _contains(trans, chromosome, position)
                    in_panel = (chromosome, position) in qc_panel.positions
                    if in_cis or in_trans or in_panel or index in leads:
                        out.write(line if line.endswith(b"\n") else line + b"\n")
                        retained += 1
                        cis_rows += in_cis
                        trans_rows += in_trans
                        qc_rows += in_panel
                        for kind, intervals, inside in (("cis", cis, in_cis), ("significant_trans", trans, in_trans)):
                            if inside:
                                key = (kind, chromosome, _locate(intervals, chromosome, position))
                                in_interval[key] = in_interval.get(key, 0) + 1
                        if index in leads:
                            lead_regions.append(Region(
                                "suggestive_trans", chromosome, position, position, 1,
                                _lead_variant_id(line, columns),
                            ))
                        if in_panel:
                            span = panel_spans.setdefault(chromosome, [position, position, 0])
                            span[0], span[1], span[2] = min(span[0], position), max(span[1], position), span[2] + 1
            raw.flush()
            os.fsync(raw.fileno())
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return FilterSummary(
        input_rows=input_rows,
        retained_rows=retained,
        cis_rows=cis_rows,
        significant_trans_regions=sum(len(spans) for spans in trans.values()),
        significant_trans_rows=trans_rows,
        suggestive_leads=len(leads),
        qc_panel_rows=qc_rows,
        regions=tuple(
            Region(kind, chromosome, start, end, in_interval.get((kind, chromosome, at), 0))
            for kind, intervals in (("cis", cis), ("significant_trans", trans))
            for chromosome in sorted(intervals)
            for at, (start, end) in enumerate(intervals[chromosome])
        )
        + tuple(lead_regions)
        + tuple(
            Region("qc_panel", chromosome, start, end, count)
            for chromosome, (start, end, count) in panel_spans.items()
        ),
    )


def pick_leads(candidates, min_distance_bp: int) -> frozenset[int]:
    """The row indices of the suggestive leads among ``candidates``, as R's ``pick_leads``.

    Strongest first: R's ``setorder`` is a stable sort, so rows with equal
    p-values keep file order and the earlier row is considered first. A row
    is a lead when every lead already kept on its chromosome is more than
    ``min_distance_bp`` away.
    """
    kept: dict[str, list[int]] = {}
    leads: set[int] = set()
    for signal in sorted(candidates, key=lambda candidate: (candidate.p_value, candidate.index)):
        positions = kept.setdefault(signal.chromosome, [])
        if all(abs(previous - signal.position) > min_distance_bp for previous in positions):
            positions.append(signal.position)
            leads.add(signal.index)
    return frozenset(leads)


# ---------------------------------------------------------------------------
# Reading a source file
# ---------------------------------------------------------------------------


def _open(path: Path):
    with path.open("rb") as fh:
        gzipped = fh.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rb") if gzipped else path.open("rb")


def _read_header(path: Path) -> tuple[bytes, dict[str, int]]:
    with _open(path) as fh:
        header = fh.readline()
    names = header.rstrip(b"\r\n").decode("utf-8").split("\t")
    missing = [column for column in REQUIRED_SOURCE_COLUMNS if column not in names]
    if missing:
        raise RegionFilterError(f"{path}: source file is missing required column(s) {', '.join(missing)}")
    return header, {name: position for position, name in enumerate(names)}


def _rows(path: Path, columns: dict[str, int]) -> Iterator[tuple[int, bytes, str, int, float]]:
    """``(index, line, chromosome, position, p_value)`` for every row with a position and a p-value."""
    width = len(columns)
    chromosome_at = columns["chromosome"]
    position_at = columns["base_pair_location"]
    p_at = columns["p_value"]
    with _open(path) as fh:
        fh.readline()
        for index, line in enumerate(fh):
            fields = line.rstrip(b"\r\n").split(b"\t")
            if len(fields) != width:
                raise RegionFilterError(
                    f"{path}: data row {index + 1} has {len(fields)} fields; the header has {width}"
                )
            position, p_value = fields[position_at], fields[p_at]
            if position in _MISSING or p_value in _MISSING:
                continue
            try:
                p = float(p_value)
                bp = int(position)
            except ValueError as exc:
                raise RegionFilterError(f"{path}: data row {index + 1}: {exc}") from None
            if math.isnan(p):
                continue
            yield index, line, clean_chromosome(fields[chromosome_at].decode("utf-8")), bp, p


def clean_chromosome(value: str) -> str:
    """A chromosome without its ``chr`` prefix, as the R filter compared them."""
    return value[3:] if value[:3].lower() == "chr" else value


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------

#: Per chromosome, merged ``(start, end)`` intervals sorted by start, ends included.
Intervals = dict[str, list[tuple[int, int]]]


def _cis_intervals(policy: RegionPolicy, target: TargetSpan | None) -> Intervals:
    if policy.association_coverage != "cis_plus_signals" or target is None:
        return {}
    flank = int(policy.cis_flank_bp or 0)
    return {target.chromosome: [(max(1, target.start - flank), target.end + flank)]}


def merge_intervals(spans) -> Intervals:
    """``(chromosome, start, end)`` spans merged per chromosome, as R's ``merge_intervals_dt``.

    Sorted by chromosome, start and end; a span starting at or before the
    current end extends it, so overlapping and touching spans merge and a
    one-base gap does not.
    """
    merged: Intervals = {}
    for chromosome, start, end in sorted(spans):
        current = merged.setdefault(chromosome, [])
        if current and start <= current[-1][1]:
            current[-1] = (current[-1][0], max(current[-1][1], end))
        else:
            current.append((start, end))
    return merged


def _locate(intervals: Intervals, chromosome: str, position: int) -> int | None:
    """The index of the interval on ``chromosome`` that holds ``position``, if one does."""
    spans = intervals.get(chromosome)
    if not spans:
        return None
    at = bisect.bisect_right(spans, (position, math.inf)) - 1
    return at if at >= 0 and spans[at][0] <= position <= spans[at][1] else None


def _contains(intervals: Intervals, chromosome: str, position: int) -> bool:
    return _locate(intervals, chromosome, position) is not None


def _lead_variant_id(line: bytes, columns: dict[str, int]) -> str:
    """A lead's ``variant_id``, else its ``rsid``, as R named it; ``NA`` is no value."""
    fields = line.rstrip(b"\r\n").split(b"\t")
    for column in ("variant_id", "rsid"):
        if column in columns and fields[columns[column]] not in _MISSING:
            return fields[columns[column]].decode("utf-8")
    return ""
