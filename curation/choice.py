#!/usr/bin/env python3
"""Choice stage: turn candidate shortlists into a proposal table (issue #167).

This is stage 3 of the Canonical Trait Mapping Table curation pipeline
(issue #161). It reads the shortlist TSV emitted by :mod:`curation.candidates`,
groups it by ``trait_label``, and asks a :class:`curation.chooser.Chooser` to
select one candidate per label. Each selection becomes one row in a *proposals*
table that a reviewer (or the later review/accept stage) can read.

What a proposal records
-----------------------
For every trait label with a non-empty shortlist the stage records:

``selected_ontology_id`` / ``selected_ontology_label``
    The chooser's choice, which is always a member of the shortlist.
``confidence``
    The selected term's probability under the chooser's own distribution.
``runner_up_id`` / ``runner_up_label`` / ``runner_up_confidence``
    The next-best candidate and its probability, blank when the shortlist has
    one candidate.
``runner_up_margin``
    ``confidence - runner_up_confidence``; ``1.0`` when there is no runner-up.
    A small margin flags a row worth a reviewer's closer look.
``probabilities``
    The full distribution as a JSON object, so the review can see how close
    the alternatives were.
``chooser_id`` / ``chooser_version`` / ``ontology_release``
    Provenance: which chooser proposed the term, which version of it, and which
    pinned ontology release the shortlist was resolved against.

A trait label with no shortlist rows contributes no proposal. A chooser that
selects a term outside its shortlist raises
:class:`~curation.chooser.SelectionNotInShortlistError`, and the CLI exits 1
without writing an output file: a fabricated term never reaches a proposal.

CLI
---
::

    python3 -m curation.choice \\
        --shortlists <shortlist.tsv> --output <proposals.tsv> \\
        --chooser stub --fixture <fixture.json>
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from curation.chooser import (
    CANDIDATE_COLUMNS,
    Candidate,
    ChoiceError,
    ChoiceResult,
    Chooser,
    validate_choice_result,
)
from curation.jev_chooser import (
    DEFAULT_JEV_MODEL,
    DEFAULT_MAX_INPUT_BYTES,
    DEFAULT_MAX_INPUT_TOKENS,
    MAX_JEV_OPTIONS,
    FixtureJevClient,
    HttpJevClient,
    JevChooser,
)
from curation.stub_chooser import StubChooser

PROPOSAL_COLUMNS: tuple[str, ...] = (
    "trait_label",
    "selected_ontology_id",
    "selected_ontology_label",
    "confidence",
    "runner_up_id",
    "runner_up_label",
    "runner_up_confidence",
    "runner_up_margin",
    "probabilities",
    "chooser_id",
    "chooser_version",
    "ontology_release",
)

# Every column a shortlist row must carry for the choice stage to read it.
REQUIRED_SHORTLIST_COLUMNS: tuple[str, ...] = ("trait_label",) + CANDIDATE_COLUMNS

# The single-candidate margin: there is no runner-up to lose to, so the choice
# is treated as maximally separated from the field.
SINGLE_CANDIDATE_MARGIN: float = 1.0

DEFAULT_CHOOSER: str = "stub"

_TSV_UNSAFE_RE = re.compile(r"[\t\r\n]+")


class ChoiceStageError(ChoiceError):
    """Base error for a shortlist the choice stage cannot read."""


class ShortlistFormatError(ChoiceStageError):
    """Raised when the shortlist TSV is missing or malformed."""


def _tsv_field(value: str) -> str:
    """Flatten a free-text value so it cannot break a TSV row's columns."""
    return _TSV_UNSAFE_RE.sub(" ", value)


def _format_number(value: float) -> str:
    """Render a probability or margin as a fixed-precision decimal."""
    return f"{value:.6f}"


def _serialize_probabilities(probabilities: Mapping[str, float]) -> str:
    """Serialize the distribution as a deterministic JSON object.

    Original float values are preserved exactly -- no rounding and no
    truncation -- so a chooser's native calibrated distribution reaches the
    proposal table unmodified. Keys are sorted so the same distribution always
    renders identically.
    """
    ordered = {
        ontology_id: probabilities[ontology_id]
        for ontology_id in sorted(probabilities)
    }
    return json.dumps(ordered, separators=(",", ":"))


@dataclass(frozen=True)
class Proposal:
    """One row of the proposals table: a chooser's choice for one trait label."""

    trait_label: str
    selected_ontology_id: str
    selected_ontology_label: str
    confidence: float
    runner_up_id: str
    runner_up_label: str
    runner_up_confidence: float | None
    runner_up_margin: float
    probabilities: dict[str, float]
    chooser_id: str
    chooser_version: str
    ontology_release: str

    def to_row(self) -> list[str]:
        return [
            _tsv_field(self.trait_label),
            _tsv_field(self.selected_ontology_id),
            _tsv_field(self.selected_ontology_label),
            _format_number(self.confidence),
            _tsv_field(self.runner_up_id),
            _tsv_field(self.runner_up_label),
            ""
            if self.runner_up_confidence is None
            else _format_number(self.runner_up_confidence),
            _format_number(self.runner_up_margin),
            _serialize_probabilities(self.probabilities),
            _tsv_field(self.chooser_id),
            _tsv_field(self.chooser_version),
            _tsv_field(self.ontology_release),
        ]


def read_shortlists(path: Path | str) -> "OrderedDict[str, list[Candidate]]":
    """Read a shortlist TSV into ``{trait_label: [Candidate, ...]}``.

    Rows are grouped by ``trait_label`` in first-seen order, and candidates
    keep their file order (which is the shortlist rank). A malformed row is an
    error rather than a silently dropped candidate.
    """
    shortlist_path = Path(path)
    if not shortlist_path.is_file():
        raise ShortlistFormatError(f"shortlist does not exist: {shortlist_path}")

    with open(shortlist_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        columns = list(header) if header is not None else []
        missing = [column for column in REQUIRED_SHORTLIST_COLUMNS if column not in columns]
        if missing:
            raise ShortlistFormatError(
                f"{shortlist_path} is missing shortlist column(s): "
                + ", ".join(missing)
            )

        grouped: "OrderedDict[str, list[Candidate]]" = OrderedDict()
        for row_index, fields in enumerate(reader):
            if len(fields) != len(columns):
                raise ShortlistFormatError(
                    f"{shortlist_path} data row {row_index} has {len(fields)} "
                    f"fields; header has {len(columns)}"
                )
            row = dict(zip(columns, fields))
            trait_label = row.get("trait_label", "")
            if not trait_label:
                raise ShortlistFormatError(
                    f"{shortlist_path} data row {row_index} has an empty trait_label"
                )
            grouped.setdefault(trait_label, []).append(Candidate.from_row(row))

    return grouped


def build_proposal(
    trait_label: str,
    candidates: Sequence[Candidate],
    result: ChoiceResult,
) -> Proposal:
    """Turn one validated choice result into a proposal row.

    The selected candidate is the winner; the runner-up is the highest
    probability among the remaining candidates (ties broken by shortlist
    order). The margin is the gap between them, or
    :data:`SINGLE_CANDIDATE_MARGIN` when the shortlist holds one candidate.
    """
    validate_choice_result(result, candidates)

    order = {candidate.ontology_id: index for index, candidate in enumerate(candidates)}
    by_id = {candidate.ontology_id: candidate for candidate in candidates}
    selected = by_id[result.selected_ontology_id]
    confidence = result.probabilities[selected.ontology_id]

    others = [
        candidate
        for candidate in candidates
        if candidate.ontology_id != selected.ontology_id
    ]
    if others:
        runner_up = max(
            others,
            key=lambda candidate: (
                result.probabilities[candidate.ontology_id],
                -order[candidate.ontology_id],
            ),
        )
        runner_up_confidence: float | None = result.probabilities[runner_up.ontology_id]
        runner_up_margin = confidence - runner_up_confidence
        runner_up_id = runner_up.ontology_id
        runner_up_label = runner_up.ontology_label
    else:
        runner_up_id = ""
        runner_up_label = ""
        runner_up_confidence = None
        runner_up_margin = SINGLE_CANDIDATE_MARGIN

    return Proposal(
        trait_label=trait_label,
        selected_ontology_id=selected.ontology_id,
        selected_ontology_label=selected.ontology_label,
        confidence=confidence,
        runner_up_id=runner_up_id,
        runner_up_label=runner_up_label,
        runner_up_confidence=runner_up_confidence,
        runner_up_margin=runner_up_margin,
        probabilities=dict(result.probabilities),
        chooser_id=result.chooser_id,
        chooser_version=result.chooser_version,
        ontology_release=selected.ontology_release,
    )


def build_proposals(
    shortlists: Mapping[str, Sequence[Candidate]],
    chooser: Chooser,
) -> list[Proposal]:
    """Run the chooser over every shortlist, skipping the no-proposal outcomes.

    A trait label whose shortlist is empty (or that the chooser declines with
    ``None``) contributes no row, so absence in the proposals table means "no
    proposal", never an arbitrary term.
    """
    proposals: list[Proposal] = []
    for trait_label, candidates in shortlists.items():
        result = chooser.choose(trait_label, list(candidates))
        if result is None:
            continue
        proposals.append(build_proposal(trait_label, candidates, result))
    return proposals


def format_proposals_tsv(proposals: Sequence[Proposal]) -> str:
    """Render the proposals table as a TSV; the header is always present."""
    lines = ["\t".join(PROPOSAL_COLUMNS)]
    lines.extend("\t".join(proposal.to_row()) for proposal in proposals)
    return "\n".join(lines) + "\n"


def build_chooser(
    name: str,
    fixture: Path | str | None,
    *,
    jev_endpoint: str | None = None,
    jev_model: str = DEFAULT_JEV_MODEL,
    jev_api_key: str | None = None,
    jev_fixture: Path | str | None = None,
    jev_max_options: int = MAX_JEV_OPTIONS,
    jev_max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    jev_max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
) -> Chooser:
    """Resolve a ``--chooser`` name and its options into a chooser.

    ``stub`` replays a recorded fixture. ``jev`` uses a hosted HTTP endpoint or,
    for hermetic offline runs, a recorded Jev fixture via ``jev_fixture``.
    """
    if name == "stub":
        if fixture is None:
            raise ChoiceError(
                "--fixture is required when --chooser is 'stub'"
            )
        return StubChooser.from_path(fixture)
    if name == "jev":
        if jev_fixture is not None:
            client = FixtureJevClient.from_path(jev_fixture)
        elif jev_endpoint:
            client = HttpJevClient(
                jev_endpoint, model=jev_model, api_key=jev_api_key
            )
        else:
            raise ChoiceError(
                "--jev-endpoint or --jev-fixture is required when "
                "--chooser is 'jev'"
            )
        return JevChooser(
            client,
            chooser_id="jev",
            chooser_version=jev_model,
            max_options=jev_max_options,
            max_input_bytes=jev_max_input_bytes,
            max_input_tokens=jev_max_input_tokens,
        )
    raise ChoiceError(f"unknown chooser: {name!r}")


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
        prog="choice",
        description=(
            "Turn candidate shortlists into a proposals table by running a "
            "chooser over each trait label."
        ),
    )
    parser.add_argument(
        "--shortlists",
        required=True,
        metavar="TSV",
        help="shortlist TSV emitted by curation.candidates",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="TSV",
        help="write the proposals TSV here instead of stdout",
    )
    parser.add_argument(
        "--chooser",
        default=DEFAULT_CHOOSER,
        metavar="NAME",
        help=f"chooser to run (default: {DEFAULT_CHOOSER})",
    )
    parser.add_argument(
        "--fixture",
        default=None,
        metavar="PATH",
        help="recorded choices for the stub chooser (JSON or TSV)",
    )
    parser.add_argument(
        "--jev-endpoint",
        default=os.environ.get("OPENGWASDB_JEV_ENDPOINT"),
        metavar="URL",
        help="hosted Jev decision endpoint (enables --chooser jev)",
    )
    parser.add_argument(
        "--jev-model",
        default=os.environ.get("OPENGWASDB_JEV_MODEL", DEFAULT_JEV_MODEL),
        metavar="MODEL",
        help=f"Jev model id to request (default: {DEFAULT_JEV_MODEL})",
    )
    parser.add_argument(
        "--jev-api-key",
        default=os.environ.get("OPENGWASDB_JEV_API_KEY"),
        metavar="KEY",
        help="bearer token for the hosted Jev endpoint",
    )
    parser.add_argument(
        "--jev-fixture",
        default=None,
        metavar="PATH",
        help="recorded Jev decisions for hermetic offline runs (JSON or TSV)",
    )
    parser.add_argument(
        "--jev-max-options",
        type=int,
        default=MAX_JEV_OPTIONS,
        metavar="N",
        help=(
            "maximum Jev enum options (hard cap "
            f"{MAX_JEV_OPTIONS}; default: {MAX_JEV_OPTIONS})"
        ),
    )
    parser.add_argument(
        "--jev-max-input-bytes",
        type=int,
        default=DEFAULT_MAX_INPUT_BYTES,
        metavar="N",
        help=(
            "Jev input byte budget for the candidate payload "
            f"(default: {DEFAULT_MAX_INPUT_BYTES})"
        ),
    )
    parser.add_argument(
        "--jev-max-input-tokens",
        type=int,
        default=DEFAULT_MAX_INPUT_TOKENS,
        metavar="N",
        help=(
            "Jev input token budget for the candidate payload "
            f"(default: {DEFAULT_MAX_INPUT_TOKENS})"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        shortlists = read_shortlists(args.shortlists)
        chooser = build_chooser(
            args.chooser,
            args.fixture,
            jev_endpoint=args.jev_endpoint,
            jev_model=args.jev_model,
            jev_api_key=args.jev_api_key,
            jev_fixture=args.jev_fixture,
            jev_max_options=args.jev_max_options,
            jev_max_input_bytes=args.jev_max_input_bytes,
            jev_max_input_tokens=args.jev_max_input_tokens,
        )
        proposals = build_proposals(shortlists, chooser)
    except ChoiceError as exc:
        print(f"choice: error: {exc}", file=sys.stderr)
        return 1

    text = format_proposals_tsv(proposals)
    if args.output:
        _write_text_atomically(text, Path(args.output))
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
