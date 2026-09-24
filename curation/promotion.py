#!/usr/bin/env python3
"""Promotion stage: confidence gate, review queue, and Reference Resource bump.

This is stage 4 -- the last stage -- of the Canonical Trait Mapping Table
curation pipeline (issue #161). It reads the proposals table emitted by
:mod:`curation.choice` and does three things:

1. **Gates on confidence and margin.** A proposal is *eligible for automatic
   acceptance* only when its ``confidence`` is at least
   ``--confidence-threshold`` *and* its ``runner_up_margin`` is at least
   ``--margin-threshold``. Either bound alone is not enough: a high-confidence
   winner over a near-tie is exactly the case a human should see.
2. **Promotes eligible rows** into the Canonical Trait Mapping Table
   (``resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv``)
   with the full provenance columns established in issue #162, and bumps the
   integer ``version`` in that resource's ``resource.yaml``. The first three
   columns are the resolver's lookup contract, so they are formatted exactly as
   :func:`curation.choice` records them. The table's header is fixed at those
   ten columns, so the chooser's exact version rides in the ``chooser_id`` cell
   as ``<chooser_id>:<chooser_version>`` (see
   :func:`format_chooser_provenance`).
3. **Queues everything else for review.** A sub-threshold proposal is written
   to a review queue TSV carrying the proposal, its full candidate shortlist
   and evidence, and empty decision columns a curator fills in
   (``review_decision``, ``override_ontology_id``, ``override_ontology_label``,
   ``curator_notes``, ``curator``, ``curated_at``). The shortlist is required
   whenever anything is queued -- ``--shortlists`` must be supplied -- so a
   review entry is never written with a missing shortlist or a blank candidate
   label.

Rejections are persistent. A rejection registry (or a previously reviewed queue
whose ``review_decision`` is ``reject``/``amend``) is read on every run, and any
``(trait_label, ontology_id)`` pair it records is suppressed: it is neither
promoted nor re-queued, on this or any later run.

Strict boundaries
-----------------
Promotion writes to exactly two places: the Reference Resource directory
(``mapping.tsv`` and ``resource.yaml``) and the review queue file. It never
modifies a Release Manifest, a Release Bundle, or a Store. It never writes the
rejection registry -- that is a curator-owned input, read but not rewritten.

CLI
---
::

    python3 -m curation.promotion \\
        --proposals <proposals.tsv> --review-queue <review.tsv> \\
        [--shortlists <shortlist.tsv>] [--rejections <rejections.tsv>] \\
        [--resource-dir <dir>] \\
        [--confidence-threshold 0.85] [--margin-threshold 0.20] \\
        [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from curation.choice import PROPOSAL_COLUMNS, read_shortlists
from curation.chooser import Candidate, ChoiceError

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

# The one Reference Resource promotion may write, and the files inside it.
DEFAULT_RESOURCE_DIR: Path = (
    REPO_ROOT / "resources" / "reference-resources" / "canonical-trait-mapping-efo"
)
MAPPING_FILENAME: str = "mapping.tsv"
RESOURCE_YAML_FILENAME: str = "resource.yaml"

# The Canonical Trait Mapping Table schema: the three resolver lookup columns
# first, then the provenance columns established in issue #162. The resolver
# reads the lookup columns by name and ignores the rest.
MAPPING_COLUMNS: tuple[str, ...] = (
    "trait_label",
    "trait_ontology_id",
    "trait_ontology_label",
    "ontology_release",
    "chooser_id",
    "confidence",
    "runner_up_margin",
    "review_status",
    "reviewer",
    "reviewed_at",
)

# A row promoted by this stage was accepted on the chooser's output alone.
AUTO_ACCEPTED: str = "auto_accepted"

# Review queue = the proposal columns, then why it was queued, then the full
# candidate evidence, then the decision columns a curator fills in.
REVIEW_QUEUE_COLUMNS: tuple[str, ...] = PROPOSAL_COLUMNS + (
    "review_reason",
    "candidates",
    "review_decision",
    "override_ontology_id",
    "override_ontology_label",
    "curator_notes",
    "curator",
    "curated_at",
)

# Decision columns are emitted empty for a curator to fill in.
REVIEW_DECISION_COLUMNS: tuple[str, ...] = (
    "review_decision",
    "override_ontology_id",
    "override_ontology_label",
    "curator_notes",
    "curator",
    "curated_at",
)

# Controlled review_reason values.
REASON_BELOW_CONFIDENCE: str = "below_confidence"
REASON_BELOW_MARGIN: str = "below_margin"
REASON_BELOW_BOTH: str = "below_confidence_and_margin"

# A rejection registry row names a rejected pair; a reviewed queue names it via
# the proposal columns plus its decision.
REJECTION_ID_COLUMNS: tuple[str, ...] = (
    "ontology_id",
    "rejected_ontology_id",
    "selected_ontology_id",
)
REJECTION_DECISIONS: frozenset[str] = frozenset({"reject", "amend"})

DEFAULT_CONFIDENCE_THRESHOLD: float = 0.85
DEFAULT_MARGIN_THRESHOLD: float = 0.20

_TSV_UNSAFE_RE = re.compile(r"[\t\r\n]+")
_VERSION_RE = re.compile(r"^(version:[ \t]*)(\d+)[ \t]*$", re.MULTILINE)


class PromotionError(ValueError):
    """Base error for a promotion request that cannot be served."""


class ProposalFormatError(PromotionError):
    """Raised when the proposals TSV is missing or malformed."""


class MappingFormatError(PromotionError):
    """Raised when the existing mapping table cannot be read."""


class ResourceYamlError(PromotionError):
    """Raised when resource.yaml has no integer ``version`` field to bump."""


class RejectionFormatError(PromotionError):
    """Raised when a rejection registry is missing a trait label or ontology id."""


class MissingShortlistEvidenceError(PromotionError):
    """Raised when a queued proposal has no full shortlist evidence to carry.

    A review queue entry exists so a human can judge the alternatives, so it
    must never be written with a missing shortlist or a blank candidate label.
    Promotion therefore requires the candidate shortlist whenever a proposal
    falls below either threshold.
    """


# Separator between a chooser's id and its version when they are recorded in
# the single ``chooser_id`` column of the Canonical Trait Mapping Table. The
# table's header is fixed at the ten issue-#162 columns, so the version is
# carried in the chooser_id cell (``stub:1``) rather than lost.
CHOOSER_PROVENANCE_SEPARATOR: str = ":"


def format_chooser_provenance(chooser_id: str, chooser_version: str) -> str:
    """Encode a chooser's id and exact version into the ``chooser_id`` column.

    The Canonical Trait Mapping Table's header is fixed at the ten columns
    settled in issue #162, so there is no separate ``chooser_version`` column
    to promote into. The exact version is preserved by recording
    ``<chooser_id>:<chooser_version>`` (for example ``stub:1``). An empty
    version leaves the id alone; an empty id leaves the version alone.
    """
    chooser_id = (chooser_id or "").strip()
    chooser_version = (chooser_version or "").strip()
    if chooser_id and chooser_version:
        return f"{chooser_id}{CHOOSER_PROVENANCE_SEPARATOR}{chooser_version}"
    return chooser_id or chooser_version


def _tsv_field(value: str) -> str:
    """Flatten a free-text value so it cannot break a TSV row's columns."""
    return _TSV_UNSAFE_RE.sub(" ", value)


def _format_number(value: float) -> str:
    """Render a confidence or margin as a fixed-precision decimal."""
    return f"{value:.6f}"


def _serialize_probabilities(probabilities: Mapping[str, float]) -> str:
    """Serialize a distribution as a deterministic JSON object (as choice does)."""
    rounded = {
        ontology_id: round(probability, 6)
        for ontology_id, probability in probabilities.items()
    }
    return json.dumps(rounded, sort_keys=True, separators=(",", ":"))


def normalize_trait_label(label: str | None) -> str:
    """Normalise a trait label exactly as the resolver's lookup does.

    ``trimws(tolower(x))`` in
    ``resources/generators/lib/metadata_resolvers/canonical_trait_table.R``:
    strip surrounding whitespace, then lowercase. Two labels differing only by
    case or surrounding whitespace are the same curation candidate.
    """
    return (label or "").strip().lower()


def _parse_float(value: str | None, field_name: str, source: Path) -> float:
    raw = (value or "").strip()
    if not raw:
        raise ProposalFormatError(f"{source}: {field_name} is empty")
    try:
        return float(raw)
    except ValueError as exc:
        raise ProposalFormatError(
            f"{source}: {field_name} is not a number: {value!r}"
        ) from exc


def _parse_optional_float(value: str | None, field_name: str, source: Path) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ProposalFormatError(
            f"{source}: {field_name} is not a number: {value!r}"
        ) from exc


def _parse_probabilities(raw: str | None, source: Path) -> dict[str, float]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProposalFormatError(
            f"{source}: probabilities is not valid JSON: {raw!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ProposalFormatError(
            f"{source}: probabilities must be a JSON object, got {type(parsed).__name__}"
        )
    probabilities: dict[str, float] = {}
    for ontology_id, probability in parsed.items():
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ProposalFormatError(
                f"{source}: probability for {ontology_id!r} is not a number: "
                f"{probability!r}"
            )
        probabilities[str(ontology_id)] = float(probability)
    return probabilities


@dataclass(frozen=True)
class ProposalRecord:
    """One proposal read from the choice stage's proposals table."""

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

    @classmethod
    def from_row(cls, row: Mapping[str, str], source: Path, row_index: int) -> "ProposalRecord":
        trait_label = (row.get("trait_label") or "").strip()
        if not trait_label:
            raise ProposalFormatError(f"{source} data row {row_index} has an empty trait_label")
        selected_ontology_id = (row.get("selected_ontology_id") or "").strip()
        if not selected_ontology_id:
            raise ProposalFormatError(
                f"{source} data row {row_index} has an empty selected_ontology_id"
            )
        return cls(
            trait_label=trait_label,
            selected_ontology_id=selected_ontology_id,
            selected_ontology_label=row.get("selected_ontology_label") or "",
            confidence=_parse_float(row.get("confidence"), "confidence", source),
            runner_up_id=(row.get("runner_up_id") or "").strip(),
            runner_up_label=row.get("runner_up_label") or "",
            runner_up_confidence=_parse_optional_float(
                row.get("runner_up_confidence"), "runner_up_confidence", source
            ),
            runner_up_margin=_parse_float(
                row.get("runner_up_margin"), "runner_up_margin", source
            ),
            probabilities=_parse_probabilities(row.get("probabilities"), source),
            chooser_id=(row.get("chooser_id") or "").strip(),
            chooser_version=(row.get("chooser_version") or "").strip(),
            ontology_release=(row.get("ontology_release") or "").strip(),
        )


def read_proposals(path: Path | str) -> list[ProposalRecord]:
    """Read the choice stage's proposals TSV into records.

    Every column in :data:`curation.choice.PROPOSAL_COLUMNS` is required, so a
    table that cannot be gated or promoted fails loudly rather than silently
    dropping a proposal. Extra columns are ignored.
    """
    proposals_path = Path(path)
    if not proposals_path.is_file():
        raise ProposalFormatError(f"proposals table does not exist: {proposals_path}")

    with open(proposals_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        columns = list(header) if header is not None else []
        missing = [column for column in PROPOSAL_COLUMNS if column not in columns]
        if missing:
            raise ProposalFormatError(
                f"{proposals_path} is missing proposal column(s): " + ", ".join(missing)
            )

        records: list[ProposalRecord] = []
        for row_index, fields in enumerate(reader):
            if len(fields) != len(columns):
                raise ProposalFormatError(
                    f"{proposals_path} data row {row_index} has {len(fields)} "
                    f"fields; header has {len(columns)}"
                )
            records.append(ProposalRecord.from_row(dict(zip(columns, fields)), proposals_path, row_index))
    return records


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def gate_reason(
    proposal: ProposalRecord,
    confidence_threshold: float,
    margin_threshold: float,
) -> str | None:
    """Return why a proposal is *not* eligible, or ``None`` when it is.

    Eligible means ``confidence >= confidence_threshold`` **and**
    ``runner_up_margin >= margin_threshold``. A value exactly on a bound is
    eligible: the threshold is inclusive.
    """
    below_confidence = proposal.confidence < confidence_threshold
    below_margin = proposal.runner_up_margin < margin_threshold
    if below_confidence and below_margin:
        return REASON_BELOW_BOTH
    if below_confidence:
        return REASON_BELOW_CONFIDENCE
    if below_margin:
        return REASON_BELOW_MARGIN
    return None


# ---------------------------------------------------------------------------
# Existing mapping table and resource.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappingTable:
    """An existing Canonical Trait Mapping Table, parsed for label lookup.

    ``canonical`` is true when the on-disk header is exactly
    :data:`MAPPING_COLUMNS`, in which case ``data_lines`` are the original raw
    rows and are re-emitted verbatim so promotion never rewrites a row it did
    not add.
    """

    columns: list[str]
    rows: list[dict[str, str]]
    data_lines: list[str]
    header_line: str
    canonical: bool


def load_mapping(path: Path | str) -> MappingTable:
    """Read an existing mapping table, or return the canonical empty one."""
    mapping_path = Path(path)
    if not mapping_path.is_file():
        return MappingTable(
            columns=list(MAPPING_COLUMNS),
            rows=[],
            data_lines=[],
            header_line="\t".join(MAPPING_COLUMNS),
            canonical=True,
        )

    text = mapping_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines:
        raise MappingFormatError(f"{mapping_path} is empty; expected a header row")
    header_line = lines[0]
    columns = header_line.split("\t")
    if "trait_label" not in columns:
        raise MappingFormatError(
            f"{mapping_path} header has no trait_label column: {header_line!r}"
        )

    rows: list[dict[str, str]] = []
    data_lines = lines[1:]
    for row_index, line in enumerate(data_lines):
        fields = line.split("\t")
        if len(fields) != len(columns):
            raise MappingFormatError(
                f"{mapping_path} data row {row_index} has {len(fields)} fields; "
                f"header has {len(columns)}"
            )
        rows.append(dict(zip(columns, fields)))

    return MappingTable(
        columns=columns,
        rows=rows,
        data_lines=data_lines,
        header_line=header_line,
        canonical=columns == list(MAPPING_COLUMNS),
    )


def existing_mapping_labels(table: MappingTable) -> set[str]:
    """The normalised trait labels already present in the mapping table."""
    return {
        normalize_trait_label(row.get("trait_label"))
        for row in table.rows
        if normalize_trait_label(row.get("trait_label"))
    }


def render_mapping(table: MappingTable, new_rows: Sequence[Sequence[str]]) -> str:
    """Render the mapping table with ``new_rows`` appended.

    Existing rows are preserved: when the header is already canonical the raw
    lines are re-emitted unchanged; a legacy or reordered header is widened to
    the canonical column order, with missing provenance left blank.
    """
    body: list[str] = []
    if table.canonical:
        body.extend(table.data_lines)
    else:
        for row in table.rows:
            body.append("\t".join(row.get(column, "") for column in MAPPING_COLUMNS))
    body.extend("\t".join(row) for row in new_rows)
    lines = ["\t".join(MAPPING_COLUMNS), *body]
    return "\n".join(lines) + "\n"


def bump_resource_version(text: str) -> tuple[str, int]:
    """Increment the integer ``version`` field in a resource.yaml document.

    A targeted line rewrite, not a YAML round-trip, so the rest of the file --
    including comments, quoting, and key order -- is untouched. Raises
    :class:`ResourceYamlError` when there is no top-level integer ``version``.
    """
    match = _VERSION_RE.search(text)
    if match is None:
        raise ResourceYamlError("resource.yaml has no top-level integer `version:` field")
    current = int(match.group(2))
    bumped = current + 1
    new_text = text[: match.start()] + match.group(1) + str(bumped) + text[match.end():]
    return new_text, bumped


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def read_rejections(path: Path | str | None) -> set[tuple[str, str]]:
    """Read a rejection registry into a set of normalised rejected pairs.

    Two shapes are accepted:

    * a rejection registry naming ``trait_label`` plus an ontology id column
      (``ontology_id``, ``rejected_ontology_id``, or ``selected_ontology_id``);
      every row is a rejection;
    * a previously reviewed review queue, where only rows whose
      ``review_decision`` is ``reject`` or ``amend`` are rejections (the
      original ``selected_ontology_id`` is the rejected term).
    """
    if path is None:
        return set()
    rejections_path = Path(path)
    if not rejections_path.is_file():
        return set()

    with open(rejections_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        columns = list(header) if header is not None else []
        if "trait_label" not in columns:
            raise RejectionFormatError(
                f"{rejections_path} has no trait_label column"
            )

        reviewed_queue = "review_decision" in columns
        id_column = next((c for c in REJECTION_ID_COLUMNS if c in columns), None)
        if id_column is None:
            raise RejectionFormatError(
                f"{rejections_path} has no ontology id column "
                f"(one of {', '.join(REJECTION_ID_COLUMNS)})"
            )

        rejected: set[tuple[str, str]] = set()
        for row_index, fields in enumerate(reader):
            if len(fields) != len(columns):
                raise RejectionFormatError(
                    f"{rejections_path} data row {row_index} has {len(fields)} "
                    f"fields; header has {len(columns)}"
                )
            row = dict(zip(columns, fields))
            if reviewed_queue:
                decision = (row.get("review_decision") or "").strip().lower()
                if decision not in REJECTION_DECISIONS:
                    continue
            label = normalize_trait_label(row.get("trait_label"))
            ontology_id = (row.get(id_column) or "").strip()
            if not label or not ontology_id:
                raise RejectionFormatError(
                    f"{rejections_path} data row {row_index} is missing a "
                    "trait_label or ontology id"
                )
            rejected.add((label, ontology_id))
    return rejected


# ---------------------------------------------------------------------------
# Review-queue evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateEvidence:
    """One candidate in a review-queue entry's shortlist, with its evidence."""

    ontology_id: str
    ontology_label: str
    probability: float
    definition: str = ""
    parent_id: str = ""
    parent_label: str = ""
    channels: tuple[str, ...] = ()
    channel_ranks: tuple[tuple[str, int], ...] = ()
    is_obsolete: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "ontology_id": self.ontology_id,
            "ontology_label": self.ontology_label,
            "probability": round(self.probability, 6),
            "definition": self.definition,
            "parent_id": self.parent_id,
            "parent_label": self.parent_label,
            "channels": list(self.channels),
            "channel_ranks": {channel: rank for channel, rank in self.channel_ranks},
            "is_obsolete": self.is_obsolete,
        }


def read_shortlist_evidence(path: Path | str | None) -> dict[str, list[Candidate]]:
    """Read the candidate shortlist TSV, keyed by normalised trait label."""
    if path is None:
        return {}
    grouped = read_shortlists(path)
    return {
        normalize_trait_label(label): list(candidates)
        for label, candidates in grouped.items()
    }


def build_candidate_evidence(
    proposal: ProposalRecord,
    shortlist: Sequence[Candidate] | None,
) -> tuple[CandidateEvidence, ...]:
    """Build the full candidate shortlist with evidence for a queued proposal.

    The shortlist is required: a review entry exists for a human to judge the
    alternatives, so it must carry every candidate's evidence (label,
    definition, parent term, channels, channel ranks, obsolete flag) and never
    a blank candidate label. The evidence covers the *whole* shortlist, not
    just the terms the proposal scored; an unscored candidate is carried with
    probability ``0.0``. Candidates are ordered by descending probability, then
    shortlist rank, then ontology id, so the queue is deterministic.

    Raises :class:`MissingShortlistEvidenceError` when no shortlist is
    available, when a proposal's distribution names a term outside the
    shortlist, or when a shortlisted candidate has no id or label.
    """
    if not shortlist:
        raise MissingShortlistEvidenceError(
            f"no candidate shortlist evidence for queued trait label "
            f"{proposal.trait_label!r}; promotion requires the shortlist "
            f"(--shortlists) whenever a proposal falls below a threshold"
        )

    by_id = {candidate.ontology_id: candidate for candidate in shortlist}
    order = {candidate.ontology_id: index for index, candidate in enumerate(shortlist)}

    outside = sorted(set(proposal.probabilities) - set(by_id))
    if outside:
        raise MissingShortlistEvidenceError(
            f"shortlist for {proposal.trait_label!r} is missing candidate(s) "
            f"named by the proposal distribution: {', '.join(outside)}"
        )

    ordered = sorted(
        shortlist,
        key=lambda candidate: (
            -proposal.probabilities.get(candidate.ontology_id, 0.0),
            order[candidate.ontology_id],
            candidate.ontology_id,
        ),
    )

    evidence: list[CandidateEvidence] = []
    for candidate in ordered:
        if not candidate.ontology_id or not candidate.ontology_label:
            raise MissingShortlistEvidenceError(
                f"shortlist for {proposal.trait_label!r} has a candidate with "
                f"no id or label (id={candidate.ontology_id!r}, "
                f"label={candidate.ontology_label!r})"
            )
        evidence.append(
            CandidateEvidence(
                ontology_id=candidate.ontology_id,
                ontology_label=candidate.ontology_label,
                probability=proposal.probabilities.get(candidate.ontology_id, 0.0),
                definition=candidate.definition,
                parent_id=candidate.parent_id,
                parent_label=candidate.parent_label,
                channels=candidate.channels,
                channel_ranks=candidate.channel_ranks,
                is_obsolete=candidate.is_obsolete,
            )
        )
    return tuple(evidence)


# ---------------------------------------------------------------------------
# Promotion plan and rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotedRow:
    """One auto-accepted row to append to the Canonical Trait Mapping Table."""

    trait_label: str
    trait_ontology_id: str
    trait_ontology_label: str
    ontology_release: str
    chooser_id: str
    confidence: float
    runner_up_margin: float
    review_status: str
    reviewer: str
    reviewed_at: str

    @classmethod
    def from_proposal(cls, proposal: ProposalRecord, reviewed_at: str) -> "PromotedRow":
        return cls(
            trait_label=proposal.trait_label,
            trait_ontology_id=proposal.selected_ontology_id,
            trait_ontology_label=proposal.selected_ontology_label,
            ontology_release=proposal.ontology_release,
            # The ten-column header has no chooser_version column, so the exact
            # version rides in the chooser_id cell as ``<id>:<version>``.
            chooser_id=format_chooser_provenance(
                proposal.chooser_id, proposal.chooser_version
            ),
            confidence=proposal.confidence,
            runner_up_margin=proposal.runner_up_margin,
            review_status=AUTO_ACCEPTED,
            reviewer="",
            reviewed_at=reviewed_at,
        )

    def to_row(self) -> list[str]:
        return [
            _tsv_field(self.trait_label),
            _tsv_field(self.trait_ontology_id),
            _tsv_field(self.trait_ontology_label),
            _tsv_field(self.ontology_release),
            _tsv_field(self.chooser_id),
            _format_number(self.confidence),
            _format_number(self.runner_up_margin),
            self.review_status,
            self.reviewer,
            self.reviewed_at,
        ]


@dataclass(frozen=True)
class ReviewEntry:
    """One sub-threshold proposal queued for a human curator."""

    proposal: ProposalRecord
    review_reason: str
    candidates: tuple[CandidateEvidence, ...]

    def to_row(self) -> list[str]:
        proposal = self.proposal
        return [
            _tsv_field(proposal.trait_label),
            _tsv_field(proposal.selected_ontology_id),
            _tsv_field(proposal.selected_ontology_label),
            _format_number(proposal.confidence),
            _tsv_field(proposal.runner_up_id),
            _tsv_field(proposal.runner_up_label),
            ""
            if proposal.runner_up_confidence is None
            else _format_number(proposal.runner_up_confidence),
            _format_number(proposal.runner_up_margin),
            _serialize_probabilities(proposal.probabilities),
            _tsv_field(proposal.chooser_id),
            _tsv_field(proposal.chooser_version),
            _tsv_field(proposal.ontology_release),
            self.review_reason,
            json.dumps(
                [candidate.to_json() for candidate in self.candidates],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "",  # review_decision
            "",  # override_ontology_id
            "",  # override_ontology_label
            "",  # curator_notes
            "",  # curator
            "",  # curated_at
        ]


@dataclass(frozen=True)
class PromotionPlan:
    """The result of gating a proposals table: promoted, queued, suppressed."""

    promoted: tuple[PromotedRow, ...] = ()
    queued: tuple[ReviewEntry, ...] = ()
    suppressed: tuple[ProposalRecord, ...] = ()


def build_promotion_plan(
    proposals: Sequence[ProposalRecord],
    *,
    confidence_threshold: float,
    margin_threshold: float,
    rejections: Iterable[tuple[str, str]] = (),
    existing_labels: Iterable[str] = (),
    shortlist_evidence: Mapping[str, Sequence[Candidate]] | None = None,
    reviewed_at: str,
) -> PromotionPlan:
    """Gate every proposal into promoted, queued, or suppressed.

    A rejected ``(trait_label, ontology_id)`` pair is suppressed outright. A
    proposal whose trait label is already in the mapping table is skipped
    (never duplicated). The first proposal for a trait label wins, so a
    repeated label cannot shadow itself. Eligible proposals are promoted; the
    rest are queued with their full candidate evidence.

    Queueing requires that evidence: a proposal below a threshold whose label
    has no shortlist raises :class:`MissingShortlistEvidenceError`, so a
    review entry is never built with a missing shortlist or blank labels.
    """
    rejected_pairs = set(rejections)
    already_mapped = set(existing_labels)
    evidence_by_label = shortlist_evidence or {}

    promoted: list[PromotedRow] = []
    queued: list[ReviewEntry] = []
    suppressed: list[ProposalRecord] = []
    seen_labels: set[str] = set()

    for proposal in proposals:
        normalized_label = normalize_trait_label(proposal.trait_label)
        pair = (normalized_label, proposal.selected_ontology_id)
        if pair in rejected_pairs:
            suppressed.append(proposal)
            continue
        if normalized_label in already_mapped or normalized_label in seen_labels:
            continue
        seen_labels.add(normalized_label)

        reason = gate_reason(proposal, confidence_threshold, margin_threshold)
        if reason is None:
            promoted.append(PromotedRow.from_proposal(proposal, reviewed_at))
        else:
            queued.append(
                ReviewEntry(
                    proposal=proposal,
                    review_reason=reason,
                    candidates=build_candidate_evidence(
                        proposal, evidence_by_label.get(normalized_label)
                    ),
                )
            )

    return PromotionPlan(
        promoted=tuple(promoted),
        queued=tuple(queued),
        suppressed=tuple(suppressed),
    )


def format_review_queue_tsv(entries: Sequence[ReviewEntry]) -> str:
    """Render the review queue TSV; the header is always present."""
    lines = ["\t".join(REVIEW_QUEUE_COLUMNS)]
    lines.extend("\t".join(entry.to_row()) for entry in entries)
    return "\n".join(lines) + "\n"


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


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionOutcome:
    """What a promotion run did: the plan and the resource version after it."""

    plan: PromotionPlan
    version: int
    mapping_written: bool


def run_promotion(
    *,
    proposals_path: Path | str,
    review_queue_path: Path | str,
    resource_dir: Path | str = DEFAULT_RESOURCE_DIR,
    shortlists_path: Path | str | None = None,
    rejections_path: Path | str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
    as_of: str | None = None,
) -> PromotionOutcome:
    """Gate the proposals and write the mapping table and review queue.

    Writes ``mapping.tsv`` and ``resource.yaml`` inside ``resource_dir`` only
    when there is at least one new row, and always writes the review queue.
    ``shortlists_path`` is required whenever a proposal falls below either
    threshold, because a review entry must carry its full candidate evidence;
    the error is raised while planning, before anything is written.
    """
    proposals = read_proposals(proposals_path)
    shortlist_evidence = read_shortlist_evidence(shortlists_path)
    rejections = read_rejections(rejections_path)

    resource_path = Path(resource_dir)
    mapping_path = resource_path / MAPPING_FILENAME
    resource_yaml_path = resource_path / RESOURCE_YAML_FILENAME

    table = load_mapping(mapping_path)
    reviewed_at = _resolve_reviewed_at(as_of)

    plan = build_promotion_plan(
        proposals,
        confidence_threshold=confidence_threshold,
        margin_threshold=margin_threshold,
        rejections=rejections,
        existing_labels=existing_mapping_labels(table),
        shortlist_evidence=shortlist_evidence,
        reviewed_at=reviewed_at,
    )

    version = _read_resource_version(resource_yaml_path)
    mapping_written = False
    if plan.promoted:
        if not resource_yaml_path.is_file():
            raise ResourceYamlError(
                f"cannot bump version: {resource_yaml_path} does not exist"
            )
        new_mapping = render_mapping(table, [row.to_row() for row in plan.promoted])
        _write_text_atomically(new_mapping, mapping_path)

        yaml_text = resource_yaml_path.read_text(encoding="utf-8")
        bumped_text, version = bump_resource_version(yaml_text)
        _write_text_atomically(bumped_text, resource_yaml_path)
        mapping_written = True

    _write_text_atomically(format_review_queue_tsv(plan.queued), Path(review_queue_path))

    return PromotionOutcome(plan=plan, version=version, mapping_written=mapping_written)


def _resolve_reviewed_at(as_of: str | None) -> str:
    """Return the ISO date to stamp on promoted rows."""
    if as_of is None:
        return date.today().isoformat()
    try:
        return date.fromisoformat(as_of).isoformat()
    except ValueError as exc:
        raise PromotionError(
            f"--as-of must be an ISO date (YYYY-MM-DD), got {as_of!r}"
        ) from exc


def _read_resource_version(path: Path) -> int:
    """Read the integer ``version`` from resource.yaml, or 0 when absent."""
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if match is None:
        raise ResourceYamlError(f"{path} has no top-level integer `version:` field")
    return int(match.group(2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promotion",
        description=(
            "Gate the choice stage's proposals, promote the confident ones to "
            "the Canonical Trait Mapping Table, and queue the rest for review."
        ),
    )
    parser.add_argument(
        "--proposals",
        required=True,
        metavar="TSV",
        help="proposals TSV emitted by curation.choice",
    )
    parser.add_argument(
        "--review-queue",
        required=True,
        metavar="TSV",
        help="write sub-threshold proposals here for curator review",
    )
    parser.add_argument(
        "--shortlists",
        default=None,
        metavar="TSV",
        help=(
            "candidate shortlist TSV; required whenever any proposal falls "
            "below a threshold, so the review queue carries full evidence"
        ),
    )
    parser.add_argument(
        "--rejections",
        default=None,
        metavar="TSV",
        help="rejection registry (or reviewed queue) of pairs never to re-propose",
    )
    parser.add_argument(
        "--resource-dir",
        default=str(DEFAULT_RESOURCE_DIR),
        metavar="DIR",
        help=(
            "Reference Resource directory holding mapping.tsv and resource.yaml "
            f"(default: {DEFAULT_RESOURCE_DIR})"
        ),
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_CONFIDENCE_THRESHOLD,
        metavar="FLOAT",
        help=f"minimum confidence to auto-accept (default: {DEFAULT_CONFIDENCE_THRESHOLD})",
    )
    parser.add_argument(
        "--margin-threshold",
        type=float,
        default=DEFAULT_MARGIN_THRESHOLD,
        metavar="FLOAT",
        help=f"minimum runner-up margin to auto-accept (default: {DEFAULT_MARGIN_THRESHOLD})",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        metavar="YYYY-MM-DD",
        help="ISO date to stamp on promoted rows (default: today)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not 0.0 <= args.confidence_threshold <= 1.0:
        print(
            f"promotion: error: --confidence-threshold must be between 0 and 1, "
            f"got {args.confidence_threshold}",
            file=sys.stderr,
        )
        return 1
    if args.margin_threshold < 0.0:
        print(
            f"promotion: error: --margin-threshold must be non-negative, "
            f"got {args.margin_threshold}",
            file=sys.stderr,
        )
        return 1

    try:
        outcome = run_promotion(
            proposals_path=args.proposals,
            review_queue_path=args.review_queue,
            resource_dir=args.resource_dir,
            shortlists_path=args.shortlists,
            rejections_path=args.rejections,
            confidence_threshold=args.confidence_threshold,
            margin_threshold=args.margin_threshold,
            as_of=args.as_of,
        )
    except (PromotionError, ChoiceError) as exc:
        print(f"promotion: error: {exc}", file=sys.stderr)
        return 1

    print(
        f"promotion: {len(outcome.plan.promoted)} promoted, "
        f"{len(outcome.plan.queued)} queued for review, "
        f"{len(outcome.plan.suppressed)} suppressed as rejected; "
        f"resource version {outcome.version}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
