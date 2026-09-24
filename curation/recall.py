#!/usr/bin/env python3
"""Report retrieval recall against the source-provided validation set (issue #165).

:mod:`curation.harvest` produces a ground-truth validation set of
``(trait_label -> ontology_id)`` pairs that the Source Collections supplied
themselves. This module scores retrieval against it: for each pair it generates
a candidate shortlist *blind* -- from the Trait label alone, never from the
known ontology id -- and checks whether the known term appears anywhere in the
shortlist. Recall is reported at several shortlist sizes, separately for the
analyte/measurement and disease strata and in aggregate.

The report must be read with one caveat, which it always states: no validation
stratum currently matches the target family (ukb-b) label distribution. The
ukb-b family's labels are free-text spanning disease, procedure, and
administrative concepts; the harvested strata cover only analyte/measurement and
disease terms. The recall measured here is therefore a lower-bound-style
indication for retrieval on those strata, not a direct estimate for ukb-b.

Inputs
------
The validation set is the TSV emitted by :mod:`curation.harvest`. Candidate
shortlists come from exactly one of:

``--index``
    A retrieval index built from the pinned ontology release; shortlists are
    generated blind from the validation trait labels.
``--shortlists``
    A candidate shortlist TSV already generated blind (the output of
    :mod:`curation.candidates`).

Scoring
-------
An obsolete validation pair is excluded from scoring (it is counted separately)
because a retrieval that avoids offering a retired term is not a failure.
A pair is a hit at shortlist size *N* when the known ontology id is among the
first *N* candidates, and a miss otherwise; the rank at which it was found is
retained on the miss so near-misses can be inspected.

CLI
---
::

    python3 -m curation.recall --validation <validation.tsv> \\
        (--index <index.json> | --shortlists <shortlist.tsv>) \\
        [--sizes 1,5,10,20] [--format text|markdown|tsv] [--output <report>]
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from curation.candidates import generate_shortlist, normalise_label
from curation.harvest import (
    STRATUM_ORDER,
    STRATUM_OTHER,
)
from curation.ontology import IndexFormatError, OntologyIndex, load_index

DEFAULT_SIZES: tuple[int, ...] = (1, 5, 10, 20)

# The caveat every report carries. It is deliberately plain and names the
# target family, the strata it does not match, and the label kinds it contains.
STRATUM_GAP_DISCLAIMER: str = (
    "No validation stratum currently matches the target family (ukb-b) label "
    "distribution. The ukb-b family's labels are free-text spanning disease, "
    "procedure, and administrative concepts, while the harvested validation "
    "strata cover only analyte/measurement and disease terms. The recall "
    "reported here is therefore an indication of retrieval on those strata, "
    "not a direct estimate of retrieval recall on ukb-b."
)

MISS_COLUMNS: tuple[str, ...] = (
    "record_type",
    "stratum",
    "shortlist_size",
    "total",
    "hits",
    "recall",
    "trait_label",
    "ontology_id",
    "ontology_label",
    "store_families",
    "found_rank",
)


class RecallError(ValueError):
    """Base error for a validation set or shortlist that cannot be scored."""


@dataclass(frozen=True)
class ValidationPair:
    """One ground-truth ``(trait_label -> ontology_id)`` pair to score."""

    trait_label: str
    ontology_id: str
    ontology_label: str
    stratum: str
    store_families: tuple[str, ...]
    is_obsolete: bool


@dataclass(frozen=True)
class Miss:
    """A validation pair not retrieved within a shortlist size."""

    trait_label: str
    ontology_id: str
    ontology_label: str
    stratum: str
    store_families: tuple[str, ...]
    #: 1-based rank of the known id in the largest shortlist, or ``None`` when
    #: it was never retrieved within that shortlist.
    found_rank: int | None


@dataclass
class RecallResult:
    """Retrieval recall, stratified and across shortlist sizes."""

    ontology_release: str
    sizes: tuple[int, ...]
    scored: int
    excluded_obsolete: int
    stratum_totals: dict[str, int] = field(default_factory=dict)
    #: stratum -> {shortlist_size: hits}
    stratum_hits: dict[str, dict[int, int]] = field(default_factory=dict)
    #: shortlist_size -> hits over every scored pair
    aggregate_hits: dict[int, int] = field(default_factory=dict)
    #: shortlist_size -> misses at that size
    misses: dict[int, tuple[Miss, ...]] = field(default_factory=dict)

    def recall_for(self, stratum: str, size: int) -> float:
        """Recall for one stratum at one shortlist size (0.0 for an empty stratum)."""
        total = self.stratum_totals.get(stratum, 0)
        if not total:
            return 0.0
        return self.stratum_hits.get(stratum, {}).get(size, 0) / total

    def aggregate_recall(self, size: int) -> float:
        """Recall over every scored pair at one shortlist size."""
        if not self.scored:
            return 0.0
        return self.aggregate_hits.get(size, 0) / self.scored

    def present_strata(self) -> list[str]:
        """The strata that actually have scored pairs, in canonical order."""
        known = [s for s in STRATUM_ORDER if self.stratum_totals.get(s)]
        extra = sorted(
            s for s in self.stratum_totals if s not in STRATUM_ORDER and self.stratum_totals[s]
        )
        return known + extra


# ---------------------------------------------------------------------------
# Reading inputs
# ---------------------------------------------------------------------------


def _read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        columns = list(header) if header is not None else []
        rows: list[dict[str, str]] = []
        for row_index, fields in enumerate(reader):
            if not fields:
                continue
            if len(fields) != len(columns):
                raise RecallError(
                    f"{path} data row {row_index} has {len(fields)} fields; "
                    f"header has {len(columns)}"
                )
            rows.append(dict(zip(columns, fields)))
    return columns, rows


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes"}


def read_validation(path: Path | str) -> list[ValidationPair]:
    """Read the harvested validation set TSV into pairs.

    Requires the harvest columns; a missing ``trait_label`` or ``ontology_id``
    column is an error rather than a silently unscorable set.
    """
    validation_path = Path(path)
    if not validation_path.is_file():
        raise RecallError(f"validation set does not exist: {validation_path}")

    columns, rows = _read_tsv(validation_path)
    for required in ("trait_label", "ontology_id"):
        if required not in columns:
            raise RecallError(
                f"{validation_path} has no {required} column; "
                "is this a harvest validation set?"
            )

    pairs: list[ValidationPair] = []
    for row in rows:
        trait_label = (row.get("trait_label") or "").strip()
        ontology_id = (row.get("ontology_id") or "").strip()
        if not trait_label or not ontology_id:
            continue
        pairs.append(
            ValidationPair(
                trait_label=trait_label,
                ontology_id=ontology_id,
                ontology_label=(row.get("ontology_label") or "").strip(),
                stratum=(row.get("stratum") or STRATUM_OTHER).strip() or STRATUM_OTHER,
                store_families=tuple(
                    part for part in (row.get("store_families") or "").split(",") if part
                ),
                is_obsolete=_parse_bool(row.get("is_obsolete")),
            )
        )
    return pairs


def read_shortlists(path: Path | str) -> dict[str, tuple[str, ...]]:
    """Read a candidate shortlist TSV into ``{trait_label: (ontology_id, ...)}``.

    Rows are grouped by trait label and ordered by ``shortlist_rank``. The
    ground-truth ontology id is not read from this file -- it only supplies the
    blind candidates retrieval produced.
    """
    shortlist_path = Path(path)
    if not shortlist_path.is_file():
        raise RecallError(f"shortlist does not exist: {shortlist_path}")

    columns, rows = _read_tsv(shortlist_path)
    for required in ("trait_label", "ontology_id", "shortlist_rank"):
        if required not in columns:
            raise RecallError(
                f"{shortlist_path} has no {required} column; "
                "is this a candidates shortlist?"
            )

    ranked: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        label = (row.get("trait_label") or "").strip()
        ontology_id = (row.get("ontology_id") or "").strip()
        if not label or not ontology_id:
            continue
        try:
            rank = int((row.get("shortlist_rank") or "0").strip())
        except ValueError as exc:
            raise RecallError(
                f"{shortlist_path} has a non-integer shortlist_rank: "
                f"{row.get('shortlist_rank')!r}"
            ) from exc
        ranked[label].append((rank, ontology_id))

    return {
        label: tuple(ontology_id for _, ontology_id in sorted(entries))
        for label, entries in ranked.items()
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _shortlist_lookup(
    pairs: Sequence[ValidationPair],
    index: OntologyIndex | None,
    shortlists: Mapping[str, Sequence[str]] | None,
    max_size: int,
) -> dict[str, tuple[str, ...]]:
    """Resolve the blind shortlist for every distinct validation label.

    Generation sees only the trait label and the ontology index; the pair's
    known ``ontology_id`` is never passed in, so the score cannot leak.
    """
    labels = list(dict.fromkeys(pair.trait_label for pair in pairs))
    generated: dict[str, tuple[str, ...]] = {}

    if shortlists is not None:
        for label in labels:
            ids = shortlists.get(label)
            if ids is None:
                ids = shortlists.get(normalise_label(label), ())
            generated[label] = tuple(ids)[:max_size]
        return generated

    if index is None:  # pragma: no cover - evaluate_recall guards this
        raise RecallError("an index is required to generate shortlists")
    cache: dict[str, tuple[str, ...]] = {}
    for label in labels:
        key = normalise_label(label)
        if key not in cache:
            cache[key] = tuple(
                candidate.ontology_id
                for candidate in generate_shortlist(label, index, max_size)
            )
        generated[label] = cache[key]
    return generated


def evaluate_recall(
    pairs: Iterable[ValidationPair],
    index: OntologyIndex | None = None,
    shortlists: Mapping[str, Sequence[str]] | None = None,
    sizes: Sequence[int] = DEFAULT_SIZES,
) -> RecallResult:
    """Score retrieval recall for the validation pairs.

    Exactly one of ``index`` or ``shortlists`` must be supplied. Obsolete pairs
    are excluded from scoring and counted; recall is computed per stratum and
    in aggregate at every size, and misses are enumerated per size.
    """
    if (index is None) == (shortlists is None):
        raise RecallError("supply exactly one of index or shortlists")

    ordered_sizes = tuple(sorted({int(size) for size in sizes}))
    if not ordered_sizes or ordered_sizes[0] < 1:
        raise RecallError(f"shortlist sizes must be positive integers, got {list(sizes)}")
    max_size = ordered_sizes[-1]

    all_pairs = list(pairs)
    scored_pairs = [pair for pair in all_pairs if not pair.is_obsolete]
    excluded_obsolete = len(all_pairs) - len(scored_pairs)

    lookup = _shortlist_lookup(scored_pairs, index, shortlists, max_size)

    release = (
        index.ontology_release
        if index is not None
        else ""
    )

    result = RecallResult(
        ontology_release=release,
        sizes=ordered_sizes,
        scored=len(scored_pairs),
        excluded_obsolete=excluded_obsolete,
    )
    result.aggregate_hits = {size: 0 for size in ordered_sizes}
    result.misses = {size: [] for size in ordered_sizes}

    for pair in scored_pairs:
        ids = lookup.get(pair.trait_label, ())
        found_rank: int | None = None
        for rank, ontology_id in enumerate(ids, start=1):
            if ontology_id == pair.ontology_id:
                found_rank = rank
                break

        result.stratum_totals[pair.stratum] = result.stratum_totals.get(pair.stratum, 0) + 1
        stratum_hits = result.stratum_hits.setdefault(pair.stratum, {size: 0 for size in ordered_sizes})
        for size in ordered_sizes:
            if found_rank is not None and found_rank <= size:
                result.aggregate_hits[size] = result.aggregate_hits.get(size, 0) + 1
                stratum_hits[size] += 1
            else:
                result.misses[size].append(
                    Miss(
                        trait_label=pair.trait_label,
                        ontology_id=pair.ontology_id,
                        ontology_label=pair.ontology_label,
                        stratum=pair.stratum,
                        store_families=pair.store_families,
                        found_rank=found_rank,
                    )
                )

    for size in ordered_sizes:
        result.misses[size] = tuple(
            sorted(result.misses[size], key=lambda miss: (miss.stratum, miss.trait_label, miss.ontology_id))
        )
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _size_header(sizes: Sequence[int]) -> list[str]:
    return [f"top-{size}" for size in sizes]


def render_text(result: RecallResult) -> str:
    """Render the report as plain text."""
    lines: list[str] = []
    lines.append("Retrieval recall for the source-provided validation set")
    lines.append("=" * 60)
    if result.ontology_release:
        lines.append(f"Ontology release: {result.ontology_release}")
    lines.append(f"Scored pairs: {result.scored}")
    lines.append(f"Excluded obsolete pairs: {result.excluded_obsolete}")
    lines.append("")
    lines.append("DISCLAIMER")
    lines.append(STRATUM_GAP_DISCLAIMER)
    lines.append("")
    lines.append("Recall by stratum")
    lines.append("-" * 60)
    header = ["stratum", "n", *_size_header(result.sizes)]
    rows = [
        [
            stratum,
            str(result.stratum_totals.get(stratum, 0)),
            *(_format_percent(result.recall_for(stratum, size)) for size in result.sizes),
        ]
        for stratum in result.present_strata()
    ]
    rows.append(
        [
            "aggregate",
            str(result.scored),
            *(_format_percent(result.aggregate_recall(size)) for size in result.sizes),
        ]
    )
    widths = [
        max(len(header[column]), *(len(row[column]) for row in rows))
        for column in range(len(header))
    ]

    def render_row(row: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)).rstrip()

    lines.append(render_row(header))
    lines.extend(render_row(row) for row in rows)
    lines.append("")
    lines.append(f"Misses at top-{result.sizes[-1]} ({len(result.misses.get(result.sizes[-1], ()))})")
    lines.append("-" * 60)
    for miss in result.misses.get(result.sizes[-1], ()):
        found = f"found at rank {miss.found_rank}" if miss.found_rank else "never retrieved"
        families = f" [{','.join(miss.store_families)}]" if miss.store_families else ""
        lines.append(
            f"  [{miss.stratum}] {miss.trait_label!r} -> {miss.ontology_id} "
            f"({miss.ontology_label}; {found}){families}"
        )
    return "\n".join(lines) + "\n"


def render_markdown(result: RecallResult) -> str:
    """Render the report as Markdown."""
    lines: list[str] = []
    lines.append("# Retrieval recall for the source-provided validation set")
    lines.append("")
    if result.ontology_release:
        lines.append(f"**Ontology release:** {result.ontology_release}")
    lines.append(f"**Scored pairs:** {result.scored}")
    lines.append(f"**Excluded obsolete pairs:** {result.excluded_obsolete}")
    lines.append("")
    lines.append(f"> **Disclaimer:** {STRATUM_GAP_DISCLAIMER}")
    lines.append("")
    lines.append("## Recall by stratum")
    lines.append("")
    lines.append("| " + " | ".join(["stratum", "n", *_size_header(result.sizes)]) + " |")
    lines.append("| " + " | ".join(["---"] * (2 + len(result.sizes))) + " |")
    for stratum in result.present_strata():
        cells = [
            stratum,
            str(result.stratum_totals.get(stratum, 0)),
            *(_format_percent(result.recall_for(stratum, size)) for size in result.sizes),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append(
        "| "
        + " | ".join(
            [
                "aggregate",
                str(result.scored),
                *(_format_percent(result.aggregate_recall(size)) for size in result.sizes),
            ]
        )
        + " |"
    )
    lines.append("")
    largest = result.sizes[-1]
    misses = result.misses.get(largest, ())
    lines.append(f"## Misses at top-{largest} ({len(misses)})")
    lines.append("")
    for miss in misses:
        found = f"found at rank {miss.found_rank}" if miss.found_rank else "never retrieved"
        lines.append(
            f"- `{miss.stratum}` **{miss.trait_label}** -> `{miss.ontology_id}` "
            f"({miss.ontology_label}; {found})"
        )
    return "\n".join(lines) + "\n"


def render_tsv(result: RecallResult) -> str:
    """Render the report as a machine-readable TSV.

    Comment lines prefixed with ``#`` carry the release, counts, and the
    mandatory disclaimer; the table then holds one ``recall`` row per stratum
    and size plus one ``miss`` row per enumerated miss.
    """
    lines: list[str] = []
    lines.append(f"# ontology_release: {result.ontology_release}")
    lines.append(f"# scored: {result.scored}")
    lines.append(f"# excluded_obsolete: {result.excluded_obsolete}")
    lines.append(f"# disclaimer: {STRATUM_GAP_DISCLAIMER}")
    lines.append("\t".join(MISS_COLUMNS))

    for stratum in [*result.present_strata(), "aggregate"]:
        for size in result.sizes:
            total = result.scored if stratum == "aggregate" else result.stratum_totals.get(stratum, 0)
            if stratum == "aggregate":
                hits = result.aggregate_hits.get(size, 0)
                recall = result.aggregate_recall(size)
            else:
                hits = result.stratum_hits.get(stratum, {}).get(size, 0)
                recall = result.recall_for(stratum, size)
            lines.append(
                "\t".join(
                    [
                        "recall",
                        stratum,
                        str(size),
                        str(total),
                        str(hits),
                        f"{recall:.6f}",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ]
                )
            )

    for size in result.sizes:
        for miss in result.misses.get(size, ()):
            lines.append(
                "\t".join(
                    [
                        "miss",
                        miss.stratum,
                        str(size),
                        str(result.stratum_totals.get(miss.stratum, 0)),
                        str(result.stratum_hits.get(miss.stratum, {}).get(size, 0)),
                        f"{result.recall_for(miss.stratum, size):.6f}",
                        miss.trait_label,
                        miss.ontology_id,
                        miss.ontology_label,
                        ",".join(miss.store_families),
                        "" if miss.found_rank is None else str(miss.found_rank),
                    ]
                )
            )
    return "\n".join(lines) + "\n"


RENDERERS = {
    "text": render_text,
    "markdown": render_markdown,
    "tsv": render_tsv,
}


def render_report(result: RecallResult, fmt: str = "text") -> str:
    """Render a result in the requested format."""
    try:
        renderer = RENDERERS[fmt]
    except KeyError as exc:
        raise RecallError(f"unknown report format: {fmt!r}") from exc
    return renderer(result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_sizes(value: str) -> tuple[int, ...]:
    """Parse a comma-separated list of positive shortlist sizes."""
    try:
        sizes = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --sizes value: {value!r}") from exc
    if not sizes or any(size < 1 for size in sizes):
        raise argparse.ArgumentTypeError("--sizes must be positive integers")
    return sizes


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
        prog="recall",
        description=(
            "Score retrieval recall against the source-provided validation set."
        ),
    )
    parser.add_argument(
        "--validation",
        required=True,
        metavar="TSV",
        help="harvest validation set TSV",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--index",
        default=None,
        metavar="JSON",
        help="retrieval index; shortlists are generated blind from the labels",
    )
    source.add_argument(
        "--shortlists",
        default=None,
        metavar="TSV",
        help="candidate shortlist TSV already generated blind",
    )
    parser.add_argument(
        "--sizes",
        type=parse_sizes,
        default=DEFAULT_SIZES,
        metavar="N,N,...",
        help=f"shortlist sizes to score (default: {','.join(map(str, DEFAULT_SIZES))})",
    )
    parser.add_argument(
        "--format",
        choices=sorted(RENDERERS),
        default="text",
        help="report format (default: text)",
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

    try:
        pairs = read_validation(args.validation)
        index: OntologyIndex | None = None
        shortlists: Mapping[str, Sequence[str]] | None = None
        if args.index:
            index = load_index(args.index)
        else:
            shortlists = read_shortlists(args.shortlists)
        result = evaluate_recall(pairs, index=index, shortlists=shortlists, sizes=args.sizes)
        text = render_report(result, args.format)
    except (RecallError, IndexFormatError) as exc:
        print(f"recall: error: {exc}", file=sys.stderr)
        return 1

    if args.output:
        _write_text_atomically(text, Path(args.output))
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
