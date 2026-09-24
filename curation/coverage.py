#!/usr/bin/env python3
"""Coverage reporting for a Canonical Trait Mapping Table curation round.

This is the reporting stage that closes the Canonical Trait Mapping Table
curation pipeline (issue #161, this module is issue #170). It answers one
question in the language of the problem -- *how much of the corpus is still
unmapped?* -- rather than the language of the machinery:

**Analyses resolved, not rows added.** A promoted Canonical Trait Mapping
Table row maps one *trait label*, but that label may appear on many Analyses,
in one Store Family or several. Promoting ``body mass index`` is one row and
resolves every Analysis carrying that label. The coverage report therefore
counts the **Analyses** a row unblocks and keeps that figure explicitly
distinct from the number of rows appended to the table. A round that adds ten
rows and resolves four thousand Analyses must never read as "ten fixed".

**Before and after, per Store Family.** The committed Release Manifest is the
"before" state: an Analysis whose ``trait_ontology_mapping_method`` is
``unmapped`` is a gap. "After" is what that same Manifest would resolve to if
it were regenerated against the table the round just produced -- the unmapped
Analyses whose normalised label is now mapped are subtracted, family by family.
The Store Family tier was retired (ADR 0028), so attribution follows
:func:`curation.gap_scan.derive_store_family`, the same precedence the work
queue uses.

**The work a round leaves behind.** Two numbers matter as much as the resolved
count:

``review_queue_size``
    Trait labels routed to a human curator because the chooser was not
    confident enough to auto-accept. These are *not* resolved yet and must not
    be reported as if they were.
``no_candidate_count``
    Trait labels for which no channel retrieved any candidate at all. There is
    no term to choose and no term to review, so they stay unmapped **by
    design**: the pipeline never forces an approximate term onto them. They are
    reported so the residual unmapped rate is not mistaken for a pipeline
    failure.

**Total round cost.** A cost-reporting chooser (the hosted Jev chooser) records
per-label spend; the report carries the total and whether it was tracked at
all, so an offline stub run is never mistaken for a free live run.

Formats
-------
The report renders as ``text`` (default), ``markdown``, or ``tsv``. All three
carry the same before/after unmapped rates per Store Family, the Analyses
resolved, the rows added, the review queue size, the no-candidate count, and
the total round cost.

CLI
---
::

    python3 -m curation.coverage \\
        --manifests families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv \\
        --mapping resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv \\
        [--mapping-before <before.tsv>] \\
        [--work-queue <queue.tsv>] [--shortlists <shortlist.tsv>] \\
        [--review-queue <review.tsv>] \\
        [--cost-report <cost.tsv>] [--cost-usd <total>] \\
        [--format text|markdown|tsv] [--output <report>]

The command is strictly read-only: it never writes a Release Manifest, a
bundle, or a store, and it does not write the Canonical Trait Mapping Table it
reports on.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from curation.gap_scan import (
    MAPPING_METHOD_COLUMN,
    TRAIT_LABEL_COLUMNS,
    UNMAPPED,
    GapScanError,
    derive_store_family,
    normalize_trait_label,
    read_analyses,
    resolve_manifest_path,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

#: Canonical Trait Mapping Table filename inside its Reference Resource dir.
MAPPING_FILENAME: str = "mapping.tsv"

#: Renderers keyed by ``--format`` value.
RENDERERS: tuple[str, ...] = ("text", "markdown", "tsv")
DEFAULT_FORMAT: str = "text"

#: Columns of the machine-readable TSV family table.
TSV_COLUMNS: tuple[str, ...] = (
    "store_family",
    "total_analyses",
    "unmapped_before",
    "unmapped_after",
    "analyses_resolved",
    "unmapped_rate_before",
    "unmapped_rate_after",
)


class CoverageError(ValueError):
    """Base error for a coverage request that cannot be served."""


class CoverageFormatError(CoverageError):
    """Raised when a report artifact (mapping, queue, cost) cannot be read."""


# ---------------------------------------------------------------------------
# Manifest scanning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyStats:
    """One Store Family's committed analyses and its unmapped label counts.

    ``unmapped_label_counts`` maps each normalised Trait label to the number of
    Analyses in this family that carry it and are currently unmapped. The
    per-label breakdown is what lets the report convert *rows added* into
    *Analyses resolved*: a promoted label's count here is exactly how many
    Analyses it unblocks in this family.
    """

    store_family: str
    total_analyses: int
    unmapped_label_counts: Mapping[str, int]

    @property
    def unmapped_before(self) -> int:
        """Analyses in this family whose mapping method is ``unmapped``."""
        return sum(self.unmapped_label_counts.values())


def scan_coverage_manifests(
    manifest_paths: Iterable[Path | str],
) -> dict[str, FamilyStats]:
    """Scan Release Manifests into per-Store-Family coverage statistics.

    Every data row contributes to the family's ``total_analyses`` denominator.
    A row contributes to ``unmapped_label_counts`` only when its
    ``trait_ontology_mapping_method`` is ``unmapped`` and its label is
    non-empty. A Manifest that predates the mapping-method column carries no
    Trait Ontology Mapping and is skipped with a warning, exactly as
    :mod:`curation.gap_scan` skips it -- it can contribute neither a total nor
    an unmapped count honestly.
    """
    families: dict[str, int] = {}
    label_counts: dict[str, dict[str, int]] = {}

    for manifest_path in manifest_paths:
        manifest = resolve_manifest_path(manifest_path)
        columns, rows = read_analyses(manifest)

        if MAPPING_METHOD_COLUMN not in columns:
            print(
                f"coverage: warning: {manifest} has no {MAPPING_METHOD_COLUMN} "
                "column; skipping it (it carries no Trait Ontology Mapping)",
                file=sys.stderr,
            )
            continue

        label_column = next((c for c in TRAIT_LABEL_COLUMNS if c in columns), None)
        if label_column is None:
            raise CoverageError(
                f"{manifest} has {MAPPING_METHOD_COLUMN} but none of "
                f"{', '.join(TRAIT_LABEL_COLUMNS)}; cannot identify a trait label"
            )

        family = derive_store_family(manifest)
        families[family] = families.get(family, 0) + len(rows)
        counts = label_counts.setdefault(family, {})
        for row in rows:
            if normalize_trait_label(row.get(MAPPING_METHOD_COLUMN)) != UNMAPPED:
                continue
            label = normalize_trait_label(row.get(label_column))
            if not label:
                # An unmapped row with no label has nothing to curate and
                # nothing to resolve; absence is not a zero-count label.
                continue
            counts[label] = counts.get(label, 0) + 1

    return {
        family: FamilyStats(
            store_family=family,
            total_analyses=total,
            unmapped_label_counts=dict(label_counts.get(family, {})),
        )
        for family, total in families.items()
    }


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyCoverage:
    """Before/after unmapped coverage for one Store Family, in Analyses."""

    store_family: str
    total_analyses: int
    unmapped_before: int
    analyses_resolved: int
    unmapped_after: int

    @property
    def unmapped_rate_before(self) -> float:
        """Fraction of this family's Analyses unmapped before the round."""
        return (
            self.unmapped_before / self.total_analyses
            if self.total_analyses
            else 0.0
        )

    @property
    def unmapped_rate_after(self) -> float:
        """Fraction of this family's Analyses unmapped after the round."""
        return (
            self.unmapped_after / self.total_analyses
            if self.total_analyses
            else 0.0
        )


@dataclass(frozen=True)
class CoverageReport:
    """A curation round's coverage: per-family rates and the global totals.

    ``analyses_resolved`` and ``rows_added`` are deliberately separate fields:
    one promoted row can resolve many Analyses, and collapsing the two would
    overstate or understate the round depending on which one was quoted.
    """

    families: tuple[FamilyCoverage, ...]
    rows_added: int
    analyses_resolved: int
    review_queue_size: int
    no_candidate_count: int
    total_cost_usd: float = 0.0
    cost_tracked: bool = False

    @property
    def total_analyses(self) -> int:
        return sum(family.total_analyses for family in self.families)

    @property
    def unmapped_before(self) -> int:
        return sum(family.unmapped_before for family in self.families)

    @property
    def unmapped_after(self) -> int:
        return sum(family.unmapped_after for family in self.families)

    @property
    def unmapped_rate_before(self) -> float:
        return (
            self.unmapped_before / self.total_analyses if self.total_analyses else 0.0
        )

    @property
    def unmapped_rate_after(self) -> float:
        return (
            self.unmapped_after / self.total_analyses if self.total_analyses else 0.0
        )

    def family(self, store_family: str) -> FamilyCoverage | None:
        for entry in self.families:
            if entry.store_family == store_family:
                return entry
        return None


def compute_coverage(
    family_stats: Mapping[str, FamilyStats],
    *,
    promoted_labels: Iterable[str],
    review_queue_size: int = 0,
    no_candidate_count: int = 0,
    cost_usd: float = 0.0,
    cost_tracked: bool = False,
) -> CoverageReport:
    """Compute the coverage report from per-family stats and the round's outcome.

    ``promoted_labels`` are the labels this round appended to the Canonical
    Trait Mapping Table (already-normalised or not; they are normalised here).
    Each family's ``analyses_resolved`` is the number of its unmapped Analyses
    whose label is in that set; its ``unmapped_after`` is what remains.
    ``rows_added`` is the number of distinct promoted labels -- the rows
    appended -- and is reported independently of ``analyses_resolved``.
    """
    promoted = {
        normalize_trait_label(label)
        for label in promoted_labels
        if normalize_trait_label(label)
    }

    families: list[FamilyCoverage] = []
    for store_family in sorted(family_stats):
        stats = family_stats[store_family]
        resolved = sum(
            count
            for label, count in stats.unmapped_label_counts.items()
            if label in promoted
        )
        families.append(
            FamilyCoverage(
                store_family=store_family,
                total_analyses=stats.total_analyses,
                unmapped_before=stats.unmapped_before,
                analyses_resolved=resolved,
                unmapped_after=stats.unmapped_before - resolved,
            )
        )

    return CoverageReport(
        families=tuple(families),
        rows_added=len(promoted),
        analyses_resolved=sum(family.analyses_resolved for family in families),
        review_queue_size=review_queue_size,
        no_candidate_count=no_candidate_count,
        total_cost_usd=float(cost_usd),
        cost_tracked=cost_tracked,
    )


# ---------------------------------------------------------------------------
# Reading the round's artifacts
# ---------------------------------------------------------------------------


def _read_tsv(path: Path | str) -> tuple[list[str], list[dict[str, str]]]:
    """Read a TSV into its header and row dictionaries; ragged rows raise."""
    tsv_path = Path(path)
    if not tsv_path.is_file():
        raise CoverageFormatError(f"file does not exist: {tsv_path}")
    with open(tsv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        columns = list(header) if header is not None else []
        rows: list[dict[str, str]] = []
        for row_index, fields in enumerate(reader):
            if len(fields) != len(columns):
                raise CoverageFormatError(
                    f"{tsv_path} data row {row_index} has {len(fields)} fields; "
                    f"header has {len(columns)}"
                )
            rows.append(dict(zip(columns, fields)))
    return columns, rows


def read_mapping_labels(path: Path | str) -> set[str]:
    """Read the normalised trait labels present in a Canonical Mapping Table."""
    columns, rows = _read_tsv(path)
    if "trait_label" not in columns:
        raise CoverageFormatError(f"{path} has no trait_label column")
    return {
        normalize_trait_label(row.get("trait_label"))
        for row in rows
        if normalize_trait_label(row.get("trait_label"))
    }


def promoted_labels_from_mapping(
    mapping_after: Path | str,
    mapping_before: Path | str | None = None,
) -> set[str]:
    """Derive the round's promoted labels from mapping tables.

    With a ``mapping_before`` table the promoted labels are the set difference
    ``after - before``; without one, every label in ``mapping_after`` is taken
    to be new (the first curation round, when the table starts empty).
    """
    after = read_mapping_labels(mapping_after)
    if mapping_before is None:
        return after
    return after - read_mapping_labels(mapping_before)


def read_review_queue_size(path: Path | str | None) -> int:
    """Count distinct Trait labels still awaiting a human curator.

    A review-queue row whose ``review_decision`` is empty is undecided; rows a
    curator has already accepted, amended, or rejected are not awaiting anyone.
    A queue without a decision column is taken to be entirely undecided.
    """
    if path is None:
        return 0
    queue_path = Path(path)
    if not queue_path.is_file():
        return 0
    columns, rows = _read_tsv(queue_path)
    if "trait_label" not in columns:
        raise CoverageFormatError(f"{queue_path} has no trait_label column")
    awaiting: set[str] = set()
    for row in rows:
        decision = (row.get("review_decision") or "").strip()
        if decision:
            continue
        label = normalize_trait_label(row.get("trait_label"))
        if label:
            awaiting.add(label)
    return len(awaiting)


def count_no_candidate_labels(
    work_queue_labels: Iterable[str],
    shortlisted_labels: Iterable[str],
) -> int:
    """Count labels with no candidate retrieved at all.

    These labels stay unmapped by design: there is no term to choose and no
    term to review, so the pipeline must not force an approximate term on them.
    """
    queue = {
        normalize_trait_label(label)
        for label in work_queue_labels
        if normalize_trait_label(label)
    }
    shortlisted = {
        normalize_trait_label(label)
        for label in shortlisted_labels
        if normalize_trait_label(label)
    }
    return len(queue - shortlisted)


def read_work_queue_labels(path: Path | str | None) -> list[str]:
    """Read the ``trait_label`` column of a gap-scan work queue."""
    if path is None:
        return []
    columns, rows = _read_tsv(path)
    if "trait_label" not in columns:
        raise CoverageFormatError(f"{path} has no trait_label column")
    return [row.get("trait_label", "") for row in rows]


def read_shortlist_labels(path: Path | str | None) -> list[str]:
    """Read the distinct ``trait_label`` values from a candidate shortlist."""
    if path is None:
        return []
    columns, rows = _read_tsv(path)
    if "trait_label" not in columns:
        raise CoverageFormatError(f"{path} has no trait_label column")
    return [row.get("trait_label", "") for row in rows]


def read_cost_report(path: Path | str | None) -> tuple[float, bool]:
    """Sum a cost report's ``cost_usd`` column into ``(total, tracked)``.

    A missing path is ``(0.0, False)``: an offline chooser reports no cost, and
    that is not the same as a live run that cost nothing. A negative or
    non-finite value is an error rather than silently skewing the total.
    """
    if path is None:
        return 0.0, False
    cost_path = Path(path)
    if not cost_path.is_file():
        raise CoverageFormatError(f"cost report does not exist: {cost_path}")
    columns, rows = _read_tsv(cost_path)
    if "cost_usd" not in columns:
        raise CoverageFormatError(f"{cost_path} has no cost_usd column")
    total = 0.0
    for row_index, row in enumerate(rows):
        raw = (row.get("cost_usd") or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError as exc:
            raise CoverageFormatError(
                f"{cost_path} data row {row_index} has a non-numeric cost_usd: "
                f"{raw!r}"
            ) from exc
        if not math.isfinite(value) or value < 0.0:
            raise CoverageFormatError(
                f"{cost_path} data row {row_index} has an invalid cost_usd: {raw!r}"
            )
        total += value
    return total, True


def chooser_cost(chooser: object) -> tuple[float, bool]:
    """Extract ``(total_usd, tracked)`` from a cost-reporting chooser.

    A chooser with no ``cost_records`` (the offline stub) is untracked. A
    chooser that exposes records but reported none is also untracked: nothing
    was billed, but nothing was observed either.
    """
    records = getattr(chooser, "cost_records", None)
    if not records:
        return 0.0, False
    total = 0.0
    for record in records:
        try:
            value = float(getattr(record, "cost_usd"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise CoverageFormatError(
                f"chooser cost record has no numeric cost_usd: {record!r}"
            ) from exc
        if not math.isfinite(value) or value < 0.0:
            raise CoverageFormatError(
                f"chooser cost record has an invalid cost_usd: {value!r}"
            )
        total += value
    return total, True


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def _format_cost(report: CoverageReport) -> str:
    if not report.cost_tracked:
        return "not tracked"
    return f"${report.total_cost_usd:.4f}"


def _summary_lines(report: CoverageReport) -> list[tuple[str, str]]:
    """The global metrics, as ``(label, value)`` pairs shared by all formats."""
    return [
        (
            "Analyses",
            f"{report.total_analyses} total; "
            f"{report.unmapped_before} unmapped before "
            f"({_format_percent(report.unmapped_rate_before)}); "
            f"{report.unmapped_after} unmapped after "
            f"({_format_percent(report.unmapped_rate_after)})",
        ),
        ("Analyses resolved", str(report.analyses_resolved)),
        ("Rows added", str(report.rows_added)),
        (
            "Review queue size",
            f"{report.review_queue_size} label(s) awaiting a human curator",
        ),
        (
            "No candidates retrieved",
            f"{report.no_candidate_count} label(s) left unmapped by design",
        ),
        ("Total round cost", _format_cost(report)),
    ]


def render_text(report: CoverageReport) -> str:
    """Render the report as aligned plain text."""
    lines: list[str] = []
    lines.append("Canonical Trait Mapping curation round coverage")
    lines.append("=" * 48)
    lines.append("")
    for label, value in _summary_lines(report):
        lines.append(f"{label}: {value}")
    lines.append("")
    lines.append("Per Store Family")
    lines.append("-" * 16)
    header = (
        f"{'Store Family':<20} {'Analyses':>9} {'Unmapped before':>16} "
        f"{'Unmapped after':>15} {'Resolved':>9} {'Rate before':>12} "
        f"{'Rate after':>11}"
    )
    lines.append(header)
    for family in report.families:
        lines.append(
            f"{family.store_family:<20} {family.total_analyses:>9} "
            f"{family.unmapped_before:>16} {family.unmapped_after:>15} "
            f"{family.analyses_resolved:>9} "
            f"{_format_percent(family.unmapped_rate_before):>12} "
            f"{_format_percent(family.unmapped_rate_after):>11}"
        )
    if not report.families:
        lines.append("(no Store Family carried an unmapped-capable Manifest)")
    return "\n".join(lines) + "\n"


def render_markdown(report: CoverageReport) -> str:
    """Render the report as Markdown."""
    lines: list[str] = []
    lines.append("# Canonical Trait Mapping curation round coverage")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    for label, value in _summary_lines(report):
        lines.append(f"| {label} | {value} |")
    lines.append("")
    lines.append("## Unmapped rate per Store Family")
    lines.append("")
    lines.append(
        "| Store Family | Analyses | Unmapped before | Unmapped after | "
        "Analyses resolved | Rate before | Rate after |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for family in report.families:
        lines.append(
            f"| {family.store_family} | {family.total_analyses} | "
            f"{family.unmapped_before} | {family.unmapped_after} | "
            f"{family.analyses_resolved} | "
            f"{_format_percent(family.unmapped_rate_before)} | "
            f"{_format_percent(family.unmapped_rate_after)} |"
        )
    if not report.families:
        lines.append("| _(none)_ | 0 | 0 | 0 | 0 | 0.00% | 0.00% |")
    lines.append("")
    lines.append(
        "> Analyses resolved counts the Analyses a promoted row unblocks; rows "
        "added counts the rows appended to the Canonical Trait Mapping Table. "
        "They are not the same number."
    )
    return "\n".join(lines) + "\n"


def render_tsv(report: CoverageReport) -> str:
    """Render the report as a machine-readable TSV.

    Comment lines prefixed with ``#`` carry the global metrics -- rows added,
    Analyses resolved, review queue size, no-candidate count, and total cost --
    so they are not lost when the family table has no rows. The table itself
    has one row per Store Family with its before/after unmapped rates.
    """
    lines: list[str] = []
    lines.append(f"# total_analyses: {report.total_analyses}")
    lines.append(f"# unmapped_before: {report.unmapped_before}")
    lines.append(f"# unmapped_after: {report.unmapped_after}")
    lines.append(f"# unmapped_rate_before: {report.unmapped_rate_before:.6f}")
    lines.append(f"# unmapped_rate_after: {report.unmapped_rate_after:.6f}")
    lines.append(f"# analyses_resolved: {report.analyses_resolved}")
    lines.append(f"# rows_added: {report.rows_added}")
    lines.append(f"# review_queue_size: {report.review_queue_size}")
    lines.append(f"# no_candidate_count: {report.no_candidate_count}")
    lines.append(f"# cost_tracked: {'true' if report.cost_tracked else 'false'}")
    lines.append(f"# total_cost_usd: {report.total_cost_usd:.6f}")
    lines.append("\t".join(TSV_COLUMNS))
    for family in report.families:
        lines.append(
            "\t".join(
                [
                    family.store_family,
                    str(family.total_analyses),
                    str(family.unmapped_before),
                    str(family.unmapped_after),
                    str(family.analyses_resolved),
                    f"{family.unmapped_rate_before:.6f}",
                    f"{family.unmapped_rate_after:.6f}",
                ]
            )
        )
    return "\n".join(lines) + "\n"


RENDERERS_MAP = {
    "text": render_text,
    "markdown": render_markdown,
    "tsv": render_tsv,
}


def render_report(report: CoverageReport, fmt: str = DEFAULT_FORMAT) -> str:
    """Render a coverage report in the requested format."""
    try:
        renderer = RENDERERS_MAP[fmt]
    except KeyError as exc:
        raise CoverageError(f"unknown report format: {fmt!r}") from exc
    return renderer(report)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_text_atomically(text: str, dest_path: Path) -> None:
    """Atomically write text via a temp file + os.replace, matching run.py/manifest.py."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(
        f".{dest_path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    )
    with open(temp_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, dest_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coverage",
        description=(
            "Report before/after unmapped rates per Store Family for a "
            "Canonical Trait Mapping Table curation round, in Analyses "
            "resolved and rows added."
        ),
    )
    parser.add_argument(
        "--manifests",
        nargs="+",
        required=True,
        metavar="MANIFEST",
        help="analyses.tsv or bundle directory to measure coverage over",
    )
    parser.add_argument(
        "--mapping",
        required=True,
        metavar="TSV",
        help="Canonical Trait Mapping Table after the round",
    )
    parser.add_argument(
        "--mapping-before",
        default=None,
        metavar="TSV",
        help=(
            "mapping table before the round; promoted labels are the set "
            "difference (default: every label in --mapping is new)"
        ),
    )
    parser.add_argument(
        "--work-queue",
        default=None,
        metavar="TSV",
        help="gap-scan work queue, used with --shortlists for the no-candidate count",
    )
    parser.add_argument(
        "--shortlists",
        default=None,
        metavar="TSV",
        help="candidate shortlist, used with --work-queue for the no-candidate count",
    )
    parser.add_argument(
        "--review-queue",
        default=None,
        metavar="TSV",
        help="review queue whose undecided labels are counted as awaiting review",
    )
    parser.add_argument(
        "--cost-report",
        default=None,
        metavar="TSV",
        help="cost report TSV with a cost_usd column, summed into the round cost",
    )
    parser.add_argument(
        "--cost-usd",
        type=float,
        default=None,
        metavar="FLOAT",
        help="total round cost in USD, when it is already known",
    )
    parser.add_argument(
        "--format",
        choices=sorted(RENDERERS_MAP),
        default=DEFAULT_FORMAT,
        help=f"report format (default: {DEFAULT_FORMAT})",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="write the report here instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cost_usd is not None and (
        not math.isfinite(args.cost_usd) or args.cost_usd < 0.0
    ):
        print(
            f"coverage: error: --cost-usd must be finite and non-negative, "
            f"got {args.cost_usd}",
            file=sys.stderr,
        )
        return 1

    try:
        family_stats = scan_coverage_manifests(args.manifests)
        promoted_labels = promoted_labels_from_mapping(
            args.mapping, args.mapping_before
        )
        review_queue_size = read_review_queue_size(args.review_queue)
        no_candidate_count = 0
        if args.work_queue is not None and args.shortlists is not None:
            no_candidate_count = count_no_candidate_labels(
                read_work_queue_labels(args.work_queue),
                read_shortlist_labels(args.shortlists),
            )
        cost_usd = 0.0
        cost_tracked = False
        if args.cost_report is not None:
            cost_usd, cost_tracked = read_cost_report(args.cost_report)
        if args.cost_usd is not None:
            cost_usd += args.cost_usd
            cost_tracked = True
        report = compute_coverage(
            family_stats,
            promoted_labels=promoted_labels,
            review_queue_size=review_queue_size,
            no_candidate_count=no_candidate_count,
            cost_usd=cost_usd,
            cost_tracked=cost_tracked,
        )
        text = render_report(report, args.format)
    except (CoverageError, GapScanError) as exc:
        print(f"coverage: error: {exc}", file=sys.stderr)
        return 1

    if args.output:
        _write_text_atomically(text, Path(args.output))
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
