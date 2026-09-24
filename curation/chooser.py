#!/usr/bin/env python3
"""Chooser interface for the Canonical Trait Mapping Table (issue #167).

This is stage 3 of the curation pipeline (issue #161). Candidate generation
(:mod:`curation.candidates`) turns a queued Trait label into a shortlist of
plausible ontology terms. The *choice* stage takes that shortlist and asks a
:class:`Chooser` which term, if any, should be proposed for the label.

The interface is deliberately narrow. A chooser is a function from
``(trait_label, candidates)`` to a :class:`ChoiceResult` -- a single selected
ontology id plus a probability distribution over the shortlist -- or to the
explicit no-proposal outcome. It is not allowed to reach outside the shortlist:

* :meth:`Chooser.choose` returns ``None`` when the shortlist is empty, rather
  than letting a chooser pick from nothing.
* :func:`validate_choice_result` (called by :meth:`Chooser.choose`) raises
  :class:`SelectionNotInShortlistError` when the selected id is not one of the
  candidates. This is structural, not advisory: a chooser cannot invent a term
  that candidate generation did not retrieve, because the shortlist is the
  ceiling on what the whole pipeline can ever map.

Subclasses implement :meth:`Chooser.select`; callers call
:meth:`Chooser.choose`, which wraps it with the empty-shortlist and shortlist
membership rules so no chooser can skip them. The stub chooser
(:mod:`curation.stub_chooser`) replays a recorded fixture for hermetic,
network-free tests of the entire choice stage.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, Sequence

# The probability distribution is required to sum to 1 within this tolerance.
# The tolerance absorbs the rounding a real chooser's float arithmetic leaves
# behind without admitting a distribution that is meaningfully un-normalised.
PROBABILITY_TOLERANCE: float = 1e-6

# The shortlist columns a Candidate is built from. They are the columns
# emitted by curation.candidates.SHORTLIST_COLUMNS, minus the per-row
# trait_label (the chooser receives it separately) and shortlist_rank (the
# list order carries the rank).
CANDIDATE_COLUMNS: tuple[str, ...] = (
    "ontology_release",
    "ontology_id",
    "ontology_label",
    "definition",
    "parent_id",
    "parent_label",
    "channels",
    "channel_ranks",
    "is_obsolete",
)


class ChoiceError(ValueError):
    """Base error for a chooser or choice-stage request that cannot be served."""


class SelectionNotInShortlistError(ChoiceError):
    """Raised when a chooser selects an ontology id not in its shortlist.

    This is the hard structural rule of the choice stage: the shortlist is the
    only vocabulary a chooser may propose from. A chooser that returns any
    other id has invented a term, and the pipeline fails loudly rather than
    letting the fabrication reach a proposal.
    """


class InvalidProbabilityDistributionError(ChoiceError):
    """Raised when a chooser's probability distribution is malformed.

    A distribution must cover exactly the shortlist, assign a non-negative
    probability to every candidate, and sum to 1 within
    :data:`PROBABILITY_TOLERANCE`.
    """


class InconsistentChoiceError(ChoiceError):
    """Raised when a selection contradicts the chooser's own distribution.

    The selected term must carry the (possibly tied) maximum probability; a
    chooser that selects a low-probability term while claiming a distribution
    has produced an incoherent result.
    """


@dataclass(frozen=True)
class Candidate:
    """One ontology term in a trait label's shortlist, as a chooser sees it.

    The fields mirror the shortlist schema emitted by :mod:`curation.candidates`
    (issue #164) minus ``trait_label`` and ``shortlist_rank``: the chooser is
    handed the label and an ordered candidate list, so neither is repeated on
    every row. ``channel_ranks`` is the ordered tuple of ``(channel, rank)``
    pairs that retrieved the term; it is carried so a chooser can weigh the
    lexical evidence behind each candidate.
    """

    ontology_id: str
    ontology_label: str
    definition: str
    parent_id: str
    parent_label: str
    channels: tuple[str, ...]
    channel_ranks: tuple[tuple[str, int], ...]
    is_obsolete: bool
    ontology_release: str

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> "Candidate":
        """Build a candidate from one shortlist TSV row dictionary."""
        return cls(
            ontology_id=(row.get("ontology_id") or "").strip(),
            ontology_label=row.get("ontology_label") or "",
            definition=row.get("definition") or "",
            parent_id=row.get("parent_id") or "",
            parent_label=row.get("parent_label") or "",
            channels=_parse_channels(row.get("channels") or ""),
            channel_ranks=_parse_channel_ranks(row.get("channel_ranks") or ""),
            is_obsolete=_parse_bool(row.get("is_obsolete") or ""),
            ontology_release=(row.get("ontology_release") or "").strip(),
        )


def _parse_channels(value: str) -> tuple[str, ...]:
    """Parse the shortlist's comma-separated ``channels`` field."""
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _parse_channel_ranks(value: str) -> tuple[tuple[str, int], ...]:
    """Parse the shortlist's ``channel=rank,channel=rank`` attribution field."""
    ranks: list[tuple[str, int]] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        channel, _, raw_rank = part.partition("=")
        try:
            rank = int(raw_rank)
        except ValueError:
            # A malformed rank is a corrupt shortlist, not a chooser concern;
            # keep the channel but do not fabricate a rank for it.
            continue
        ranks.append((channel.strip(), rank))
    return tuple(ranks)


def _parse_bool(value: str) -> bool:
    """Parse the shortlist's ``true``/``false`` boolean field."""
    return value.strip().lower() in {"true", "1", "yes"}


@dataclass(frozen=True)
class ChoiceResult:
    """A chooser's proposal for one trait label.

    ``selected_ontology_id`` must be present in the shortlist the chooser was
    given. ``probabilities`` maps every candidate ontology id to a probability
    and sums to 1 within :data:`PROBABILITY_TOLERANCE`.
    """

    selected_ontology_id: str
    probabilities: dict[str, float]
    chooser_id: str
    chooser_version: str


def validate_choice_result(
    result: ChoiceResult,
    candidates: Sequence[Candidate],
) -> None:
    """Enforce the choice-stage invariants on one result.

    Raises :class:`SelectionNotInShortlistError` when the selection (or any
    probability key) is not a candidate, and
    :class:`InvalidProbabilityDistributionError` when the distribution does not
    cover exactly the shortlist, contains a negative probability, or does not
    sum to 1.
    """
    candidate_ids = {candidate.ontology_id for candidate in candidates}

    if result.selected_ontology_id not in candidate_ids:
        raise SelectionNotInShortlistError(
            f"chooser {result.chooser_id!r} selected "
            f"{result.selected_ontology_id!r}, which is not one of the "
            f"{len(candidate_ids)} shortlisted candidate(s)"
        )

    probabilities = result.probabilities
    if not probabilities:
        raise InvalidProbabilityDistributionError(
            "chooser returned no probability distribution"
        )

    invented = sorted(set(probabilities) - candidate_ids)
    if invented:
        raise SelectionNotInShortlistError(
            "chooser assigned probability to term(s) outside the shortlist: "
            + ", ".join(invented)
        )

    missing = sorted(candidate_ids - set(probabilities))
    if missing:
        raise InvalidProbabilityDistributionError(
            "chooser omitted a probability for shortlist term(s): "
            + ", ".join(missing)
        )

    for ontology_id, probability in probabilities.items():
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise InvalidProbabilityDistributionError(
                f"probability for {ontology_id!r} is not a number: {probability!r}"
            )
        if not math.isfinite(probability) or probability < 0.0:
            raise InvalidProbabilityDistributionError(
                f"probability for {ontology_id!r} must be finite and "
                f"non-negative, got {probability!r}"
            )

    total = math.fsum(probabilities.values())
    if not math.isclose(total, 1.0, abs_tol=PROBABILITY_TOLERANCE):
        raise InvalidProbabilityDistributionError(
            f"probabilities sum to {total!r}, not 1.0 "
            f"(tolerance {PROBABILITY_TOLERANCE})"
        )

    selected_probability = probabilities[result.selected_ontology_id]
    best_probability = max(probabilities.values())
    if selected_probability < best_probability - PROBABILITY_TOLERANCE:
        raise InconsistentChoiceError(
            f"chooser selected {result.selected_ontology_id!r} with probability "
            f"{selected_probability!r} but {best_probability!r} is the maximum"
        )


class Chooser(ABC):
    """Abstract choice stage: shortlist in, one proposal (or none) out.

    Subclasses implement :meth:`select`. Callers always call :meth:`choose`,
    which owns the two invariants no chooser may bypass:

    * an empty shortlist yields the explicit no-proposal outcome ``None``;
    * the returned selection must be one of the candidates, and its
      distribution must cover exactly them.
    """

    def choose(
        self,
        trait_label: str,
        candidates: list[Candidate],
    ) -> ChoiceResult | None:
        """Choose one candidate for ``trait_label``, or ``None`` for no proposal.

        An empty shortlist has nothing to choose from, so it returns ``None``
        rather than an arbitrary or invented selection.
        """
        if not candidates:
            return None
        result = self.select(trait_label, candidates)
        validate_choice_result(result, candidates)
        return result

    @abstractmethod
    def select(
        self,
        trait_label: str,
        candidates: list[Candidate],
    ) -> ChoiceResult:
        """Return the chooser's result for a non-empty shortlist.

        Implementations must not be called directly: :meth:`choose` enforces
        the empty-shortlist and shortlist-membership rules around them.
        """
        raise NotImplementedError
