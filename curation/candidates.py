#!/usr/bin/env python3
"""Multi-channel lexical candidate generation for Trait Ontology Mapping.

This is stage 2 of the Canonical Trait Mapping Table curation pipeline
(issue #161): turn each queued, unmapped Trait label into a shortlist of
plausible ontology terms, using no model at all. The work queue is the TSV
emitted by :mod:`curation.gap_scan` (``trait_label``, ``occurrence_count``,
``store_families``); the ontology terms come from a rebuildable retrieval index
built from a pinned release by :mod:`curation.ontology`.

Channels
--------
Retrieval runs independent lexical channels and unions their results:

``exact``
    Identical string match against an ontology term label.
``icd10``
    Cross-reference match: when a label carries a ukb-b ICD-10 code
    (``diagnoses - main icd10: c20 ...``), the code is looked up in the
    ontology's ICD-10 xref map. This is high-precision evidence -- a code is a
    direct assertion of the diagnosis class, not a string guess -- so the
    channel sits next to ``exact`` in :data:`CHANNEL_ORDER`. A label without a
    code contributes nothing.
``normalised``
    Match after trimming, lowercasing, and stripping punctuation.
``token_overlap``
    Jaccard overlap between the trait label's tokens and the tokens of each
    term's label and synonyms.
``synonym``
    Match against the term's known synonyms, and against acronyms generated
    from multi-word labels and synonyms (``body mass index`` -> ``BMI``).
``embedding``
    Semantic nearest-neighbour match: a query vector for the label is compared
    against a vector store of each term's label, synonyms, and definition
    (:mod:`curation.embedding`). The query vector comes from a precomputed
    trait store when one covers the label and otherwise from the pinned model
    over HTTP. This channel is optional (``--enable-embedding`` /
    ``--ontology-embeddings`` / ``--trait-embeddings``); when it is disabled or
    unavailable the run is lexical-only and still succeeds.

Every channel is additive. A candidate records which channels retrieved it and
each channel's rank, because a candidate resting on several agreeing channels is
stronger evidence than one resting on a single weak channel.

Candidate-space restriction
---------------------------
Before ranking, the union is restricted to phenotype-bearing terms. Two
pinned rules drop the rest (issue #185):

* ontology prefixes ``BTO``, ``PO``, ``CHEBI``, ``NCBITaxon``, ``UBERON``,
  ``CL``, ``CLO``, ``PR`` -- the cell lines, chemicals, taxa, anatomy, and
  proteins no ukb-b trait maps to;
* terms whose label or parent lineage ends with (or equals) ``cell line``,
  ``cell type``, or ``immortalized cell line``, so EFO's own cell-line and
  cell-type subtree is dropped without banning the whole EFO prefix.

Verifying the suffix rule against the pinned EFO v3.94.0 corpus: exact-label
lineage matching alone catches only ~24 EFO cell-type children and misses the
~1787-term EFO cell-line/cell-type family (whose lineage passes through
``cancer cell line`` and ``cultured cell``), whereas the suffix rule catches
the family while avoiding false positives such as the OBA ``...germ cell
type...`` quantitative measurements, which must be kept. The exclusion set is
a parameter with a pinned default so a future corpus can be checked against
OGS-00011 before the list is changed (the orchestrator performs that check,
not this module).

Merge and shortlist
-------------------
Candidates are unioned and deduplicated by ontology id, then the excluded
terms are dropped, then the remainder is ranked with Reciprocal Rank Fusion
(RRF, ``k = 60``) so a term found by several channels outranks one found at
the same rank by a single channel. The union is truncated to the configurable
shortlist size. A label no channel matches yields an empty shortlist -- the
generator never fabricates a term.

The pinned ontology release travels on every shortlist row so a later proposal
can record exactly what it was resolved against. When the semantic channel is
in use, the pinned embedding model and the content-addressed ontology store
build travel on every row as well.

CLI
---
::

    python3 -m curation.candidates \\
        --work-queue <queue.tsv> --index <index.json> --output <shortlist.tsv> \\
        [--shortlist-size N] \\
        [--ontology-embeddings <ontology-store-dir>] \\
        [--trait-embeddings <trait-store-dir>]
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from curation.embedding import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    DEFAULT_EMBEDDING_MIN_SCORE,
    DEFAULT_EMBEDDING_TOP_K,
    PINNED_EMBEDDING_MODEL_ID,
    EmbeddingChannel,
    EmbeddingError,
    SemanticRetriever,
    as_embedding_channel,
    resolve_retriever,
)
from curation.ontology import (
    IndexFormatError,
    OntologyIndex,
    acronym,
    icd10_code_from_xref,
    jaccard,
    load_index,
    normalise_icd10_code,
    normalise_label,
    tokenize,
)
from curation.ukb import icd10_code_from_label

# Channel names. Order is meaningful: it is the canonical order in which a
# candidate's channels and per-channel ranks are recorded.
CHANNEL_EXACT = "exact"
CHANNEL_NORMALISED = "normalised"
CHANNEL_TOKEN_OVERLAP = "token_overlap"
CHANNEL_SYNONYM = "synonym"
CHANNEL_EMBEDDING = "embedding"
CHANNEL_ICD10 = "icd10"
CHANNEL_ORDER: tuple[str, ...] = (
    CHANNEL_EXACT,
    # ICD-10 cross-references are high-precision evidence (a code is a direct
    # assertion of the diagnosis class), so the channel runs with ``exact``
    # rather than with the string-similarity channels.
    CHANNEL_ICD10,
    CHANNEL_NORMALISED,
    CHANNEL_TOKEN_OVERLAP,
    CHANNEL_SYNONYM,
    CHANNEL_EMBEDDING,
)

#: The default shortlist size (issue #185). 100 candidates comfortably fit the
#: chooser's 60k-token / 1 MiB input budget with 200-character definitions (see
#: ``tests/curation/test_jev_chooser.py``), and the larger surface offers the
#: chooser the correct term more often than the old default of 10.
DEFAULT_SHORTLIST_SIZE: int = 100

# Reciprocal Rank Fusion constant. 60 is the value from the original RRF work
# and is deliberately insensitive to the tail of a channel's ranking.
RRF_K: int = 60

# A channel's ranked list is bounded so a common token cannot make the union
# unbounded. The final shortlist is truncated separately to its own size.
CHANNEL_LIMIT: int = 200

SHORTLIST_COLUMNS: tuple[str, ...] = (
    "trait_label",
    "ontology_release",
    "shortlist_rank",
    "ontology_id",
    "ontology_label",
    "definition",
    "parent_id",
    "parent_label",
    "channels",
    "channel_ranks",
    "is_obsolete",
    # The semantic channel's pins: empty when the channel was disabled or
    # unavailable, so a row always states whether it was resolved with the
    # embedding channel and, if so, with which model and index build.
    "embedding_model",
    "embedding_index_build",
)


class CandidateGenerationError(ValueError):
    """Base error for a work queue or request candidate generation cannot serve."""


class WorkQueueError(CandidateGenerationError):
    """Raised when the work queue TSV is missing or malformed."""


# ---------------------------------------------------------------------------
# Lexical primitives
# ---------------------------------------------------------------------------
# ``normalise_label``, ``tokenize``, ``jaccard``, and ``acronym`` live in
# :mod:`curation.ontology` next to the prebuilt lookups built from them and are
# re-exported here because the channels below are their public home.

# A TSV field cannot contain a tab or a line break. Free-text fields (a term's
# definition in particular) are flattened rather than allowed to shift columns.
_TSV_UNSAFE_RE = re.compile(r"[\t\r\n]+")


def _tsv_field(value: str) -> str:
    """Flatten a free-text value so it cannot break a TSV row's columns."""
    return _TSV_UNSAFE_RE.sub(" ", value)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def exact_channel(label: str, index: OntologyIndex) -> list[str]:
    """Ontology ids whose label is byte-for-byte the trait label, in id order."""
    return index.lexical_lookups.exact_ids(label)


def icd10_channel(label: str, index: OntologyIndex) -> list[str]:
    """Ontology ids whose ICD-10 xref matches the label's code, in id order.

    A label that carries a ukb-b ICD-10 code (``diagnoses - main icd10:
    c20 ...``) looks the normalised code up in the index's prebuilt ICD-10
    xref map; the map merges the ICD10/ICD10CM/ICD10WHO provenances and skips
    obsolete terms. When the exact code has no term, the 3-character chapter
    prefix is tried (``C349`` -> ``C34``), so a code whose category EFO models
    only at chapter level still retrieves. A label without a code contributes
    nothing.
    """
    code = icd10_code_from_label(label)
    if not code:
        return []
    lookup = index.icd10_lookup
    key = normalise_icd10_code(code)
    matched = list(lookup.get(key, ()))
    if not matched:
        matched = list(lookup.get(key[:3], ()))
    return matched[:CHANNEL_LIMIT]


def normalised_channel(label: str, index: OntologyIndex) -> list[str]:
    """Ontology ids whose label normalises to the trait label's normal form."""
    return index.lexical_lookups.normalised_ids(normalise_label(label))


def token_overlap_channel(label: str, index: OntologyIndex) -> list[str]:
    """Ontology ids ranked by Jaccard token overlap with the trait label.

    A term's searchable tokens are its label's tokens unioned with its
    synonyms' tokens, so an abbreviation reaches the term it abbreviates here
    as well as through the synonym channel. Only terms sharing at least one
    token are scored, via the prebuilt token postings.
    """
    return index.lexical_lookups.token_overlap_ids(tokenize(label), CHANNEL_LIMIT)


def synonym_channel(label: str, index: OntologyIndex) -> list[str]:
    """Ontology ids whose synonyms or generated acronyms match the trait label.

    A match is either an exact/normalised equality against a declared synonym,
    or an equality against an acronym generated from a multi-word label or
    synonym.
    """
    raw = (label or "").strip()
    normalised = normalise_label(label)
    if not raw and not normalised:
        return []
    return index.lexical_lookups.synonym_ids(raw, normalised)


# The brute-force scans the prebuilt lookups replace. They are retained here,
# not as dead code but as the reference the equivalence test in
# ``tests/curation/test_candidates.py`` checks the fast channels against over a
# fixture index and randomised labels.

def _brute_exact_channel(label: str, index: OntologyIndex) -> list[str]:
    """Reference implementation: scan every term for an identical label."""
    matches = [term.ontology_id for term in index if term.label == label]
    return sorted(matches)


def _brute_normalised_channel(label: str, index: OntologyIndex) -> list[str]:
    """Reference implementation: scan every term for a normalised match."""
    key = normalise_label(label)
    if not key:
        return []
    matches = [
        term.ontology_id
        for term in index
        if normalise_label(term.label) == key
    ]
    return sorted(matches)


def _brute_token_overlap_channel(label: str, index: OntologyIndex) -> list[str]:
    """Reference implementation: score every term by Jaccard overlap."""
    query = tokenize(label)
    if not query:
        return []
    scored: list[tuple[float, str]] = []
    for term in index:
        term_tokens = tokenize(term.label)
        for synonym in term.synonyms:
            term_tokens |= tokenize(synonym)
        score = jaccard(query, term_tokens)
        if score > 0.0:
            scored.append((score, term.ontology_id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [ontology_id for _, ontology_id in scored[:CHANNEL_LIMIT]]


def _brute_synonym_channel(label: str, index: OntologyIndex) -> list[str]:
    """Reference implementation: scan every term's synonyms and acronyms."""
    raw = (label or "").strip()
    normalised = normalise_label(label)
    if not raw and not normalised:
        return []

    matches: list[str] = []
    for term in index:
        synonyms = term.synonyms
        if raw and raw in synonyms:
            matches.append(term.ontology_id)
            continue
        if normalised and any(normalise_label(s) == normalised for s in synonyms):
            matches.append(term.ontology_id)
            continue
        if normalised:
            generated = {acronym(term.label)}
            generated.update(acronym(s) for s in synonyms)
            if normalised in {a for a in generated if a}:
                matches.append(term.ontology_id)
    return sorted(matches)


def _brute_icd10_channel(label: str, index: OntologyIndex) -> list[str]:
    """Reference implementation: scan every term's ICD-10 xrefs."""
    code = icd10_code_from_label(label)
    if not code:
        return []
    key = normalise_icd10_code(code)
    prefix = key[:3]

    matches: list[str] = []
    for term in index:
        if term.is_obsolete:
            continue
        for xref in term.xrefs:
            if icd10_code_from_xref(xref) == key:
                matches.append(term.ontology_id)
                break
    if not matches:
        for term in index:
            if term.is_obsolete:
                continue
            for xref in term.xrefs:
                if icd10_code_from_xref(xref) == prefix:
                    matches.append(term.ontology_id)
                    break
    return sorted(matches)[:CHANNEL_LIMIT]


def embedding_channel(
    label: str,
    embedding: SemanticRetriever | EmbeddingChannel | None,
) -> list[str]:
    """Ontology ids nearest to the trait label in the embedding index.

    This is the semantic channel. It is independent of the lexical channels:
    the query is embedded and compared to the indexed label/synonym/definition
    vectors, so a label sharing no token with the correct term can still reach
    it. When no retriever is supplied, or the retriever cannot serve the query,
    the channel contributes nothing rather than failing candidate generation --
    the clean-degradation contract of issue #166.

    A run-scoped :class:`~curation.embedding.EmbeddingChannel` is used when
    supplied; it carries the circuit breaker, so a connection failure on one
    label disables the channel for the rest of the run.
    """
    if embedding is None:
        return []
    if isinstance(embedding, EmbeddingChannel):
        return embedding.retrieve(label)
    text = (label or "").strip()
    if not text:
        return []
    try:
        ranked = embedding.rank(text)
    except EmbeddingError:
        # An embedder that is unreachable mid-run degrades to lexical-only.
        return []
    return [ontology_id for ontology_id, _ in ranked[:CHANNEL_LIMIT]]


def run_channels(
    label: str,
    index: OntologyIndex,
    embedding: SemanticRetriever | EmbeddingChannel | None = None,
) -> dict[str, list[str]]:
    """Run every channel for one label, returning ``{channel: [ontology_id]}``.

    The lexical channels always run. The semantic channel runs only when a
    retriever is supplied; its key is always present so a caller can attribute
    an empty result to a disabled channel rather than a missing key.
    """
    return {
        CHANNEL_EXACT: exact_channel(label, index),
        CHANNEL_ICD10: icd10_channel(label, index),
        CHANNEL_NORMALISED: normalised_channel(label, index),
        CHANNEL_TOKEN_OVERLAP: token_overlap_channel(label, index),
        CHANNEL_SYNONYM: synonym_channel(label, index),
        CHANNEL_EMBEDDING: embedding_channel(label, embedding),
    }


# ---------------------------------------------------------------------------
# Candidates and shortlists
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One ontology term in a trait label's shortlist, with its attribution."""

    trait_label: str
    ontology_release: str
    rank: int
    ontology_id: str
    ontology_label: str
    definition: str
    parent_id: str
    parent_label: str
    channels: tuple[str, ...]
    channel_ranks: tuple[tuple[str, int], ...]
    is_obsolete: bool
    #: The semantic channel's pins. Both are empty when the channel was
    #: disabled or unavailable for this run.
    embedding_model: str = ""
    embedding_index_build: str = ""

    def to_row(self) -> list[str]:
        return [
            _tsv_field(self.trait_label),
            _tsv_field(self.ontology_release),
            str(self.rank),
            _tsv_field(self.ontology_id),
            _tsv_field(self.ontology_label),
            _tsv_field(self.definition),
            _tsv_field(self.parent_id),
            _tsv_field(self.parent_label),
            ",".join(self.channels),
            ",".join(f"{channel}={rank}" for channel, rank in self.channel_ranks),
            "true" if self.is_obsolete else "false",
            _tsv_field(self.embedding_model),
            _tsv_field(self.embedding_index_build),
        ]


def _rrf_score(channel_ranks: Mapping[str, int]) -> float:
    """Reciprocal Rank Fusion score for a candidate's per-channel ranks."""
    return sum(1.0 / (RRF_K + rank) for rank in channel_ranks.values())


# ---------------------------------------------------------------------------
# Candidate-space restriction (issue #185)
# ---------------------------------------------------------------------------
# A ukb-b trait never maps to a cell line, a chemical, a taxon, an anatomical
# structure, a cell, or a protein, so such terms are dropped before ranking.

#: Ontology prefixes whose terms are never phenotype-bearing for a ukb-b trait.
#: Pinned: changing the list is checked against OGS-00011 by the orchestrator.
EXCLUDED_ONTOLOGY_PREFIXES: frozenset[str] = frozenset(
    {"BTO", "PO", "CHEBI", "NCBITaxon", "UBERON", "CL", "CLO", "PR"}
)

#: Lineage labels whose presence (equal, or a word-boundary suffix) marks a
#: term as a cell line or cell type, catching EFO's own cell-line/cell-type
#: subtree without banning the whole EFO prefix. Verified against the pinned
#: EFO v3.94.0 corpus when pinned (see the module docstring).
EXCLUDED_LINEAGE_MARKERS: frozenset[str] = frozenset(
    {"cell line", "cell type", "immortalized cell line"}
)


def _lineage_labels(ontology_id: str, by_id: Mapping[str, object]) -> list[str]:
    """The term's own label followed by its parents' labels, up the chain.

    The walk follows ``parent_id`` links and stops at a missing parent, an
    already-seen id (a cycle), or a fixed depth ceiling, so a malformed or
    cyclic lineage still yields a bounded list.
    """
    labels: list[str] = []
    seen: set[str] = set()
    current = ontology_id
    while current and current not in seen and len(seen) < 64:
        seen.add(current)
        term = by_id.get(current)
        if term is None:
            break
        labels.append(term.label)
        current = term.parent_id
    return labels


def _term_is_excluded(
    ontology_id: str,
    by_id: Mapping[str, object],
    excluded_prefixes: frozenset[str] = EXCLUDED_ONTOLOGY_PREFIXES,
    lineage_markers: frozenset[str] = EXCLUDED_LINEAGE_MARKERS,
) -> bool:
    """Whether a live term must never be offered for a ukb-b trait.

    Two independent rules: the ontology prefix is banned outright, and the
    term's own label or any lineage label equals a marker or ends with
    ``" " + marker`` (so ``cancer cell line`` is caught by ``cell line`` while
    the OBA ``...germ cell type...`` measurement labels are not).
    """
    prefix = ontology_id.partition(":")[0]
    if prefix in excluded_prefixes:
        return True
    if not lineage_markers:
        return False
    normalised_markers = frozenset(normalise_label(m) for m in lineage_markers)
    for label in _lineage_labels(ontology_id, by_id):
        normalised = normalise_label(label)
        if not normalised:
            continue
        if normalised in normalised_markers:
            return True
        if any(normalised.endswith(" " + marker) for marker in normalised_markers):
            return True
    return False


def _excluded_candidate_ids(
    ranks: Mapping[str, object],
    by_id: Mapping[str, object],
    excluded_prefixes: frozenset[str] = EXCLUDED_ONTOLOGY_PREFIXES,
    lineage_markers: frozenset[str] = EXCLUDED_LINEAGE_MARKERS,
) -> frozenset[str]:
    """The ids in ``ranks`` the restriction drops, so callers can count them.

    The shortlist rows keep their pinned schema (provenance changes are
    evaluated with the chooser-context pass); the excluded count is observed
    through this set and the predicate it is built from.
    """
    return frozenset(
        ontology_id
        for ontology_id in ranks
        if _term_is_excluded(
            ontology_id, by_id, excluded_prefixes, lineage_markers
        )
    )


def generate_shortlist(
    trait_label: str,
    index: OntologyIndex,
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE,
    embedding: SemanticRetriever | EmbeddingChannel | None = None,
    *,
    excluded_prefixes: frozenset[str] = EXCLUDED_ONTOLOGY_PREFIXES,
    lineage_markers: frozenset[str] = EXCLUDED_LINEAGE_MARKERS,
) -> list[Candidate]:
    """Return the top ``shortlist_size`` candidates for one trait label.

    A bare retriever is wrapped in a one-shot channel; a run spanning many
    labels should pass a shared :class:`~curation.embedding.EmbeddingChannel`
    (as :func:`generate_shortlists` does) so the circuit breaker persists.
    The candidate-space restriction's two lists are parameters so a corpus
    can be re-checked against OGS-00011 before either is repinned.
    """
    return _generate_shortlist(
        trait_label,
        index,
        shortlist_size,
        as_embedding_channel(embedding),
        excluded_prefixes=excluded_prefixes,
        lineage_markers=lineage_markers,
    )


def _live_successor(ontology_id: str, by_id: Mapping[str, object]) -> str | None:
    """The live term an obsolete one was replaced by, following a short chain."""
    seen: set[str] = set()
    current = ontology_id
    while current not in seen and len(seen) < 8:
        seen.add(current)
        term = by_id.get(current)
        if term is None:
            return None
        if not term.is_obsolete:
            return current
        current = term.replaced_by
        if not current:
            return None
    return None


def _fold_obsolete_terms(
    ranks: dict[str, dict[str, int]], by_id: Mapping[str, object]
) -> dict[str, dict[str, int]]:
    """Credit an obsolete term's channel ranks to its live replacement.

    An obsolete term must never be offered for mapping: it is replaced by its
    ``replaced_by`` successor (keeping the better rank per channel) or dropped
    when it has none in the index.
    """
    folded: dict[str, dict[str, int]] = {}
    for ontology_id, channel_ranks in ranks.items():
        live = _live_successor(ontology_id, by_id)
        if live is None:
            continue
        merged = folded.setdefault(live, {})
        for channel, rank in channel_ranks.items():
            merged[channel] = min(rank, merged.get(channel, rank))
    return folded


def _generate_shortlist(
    trait_label: str,
    index: OntologyIndex,
    shortlist_size: int,
    embedding: EmbeddingChannel | None,
    *,
    excluded_prefixes: frozenset[str] = EXCLUDED_ONTOLOGY_PREFIXES,
    lineage_markers: frozenset[str] = EXCLUDED_LINEAGE_MARKERS,
) -> list[Candidate]:
    """Return the top ``shortlist_size`` candidates for one trait label.

    The channels are unioned and deduplicated by ontology id, obsolete terms
    are folded to their live successors, non-phenotype terms are dropped by
    the candidate-space restriction, the remainder is ranked by RRF, and the
    shortlist is truncated. An unmatched label returns ``[]``. When an
    embedding channel is supplied, its channel contributes candidates and,
    only if semantic retrieval actually ran for this label, its model and
    index build are recorded on the returned candidates; a disabled, tripped,
    or failed channel leaves the provenance empty and the shortlist
    lexical-only.
    """
    if shortlist_size < 1:
        raise CandidateGenerationError(
            f"shortlist_size must be at least 1, got {shortlist_size}"
        )

    channels = run_channels(trait_label, index, embedding)

    # ontology_id -> {channel: rank}, first rank wins if a channel ever repeats.
    ranks: dict[str, dict[str, int]] = {}
    for channel in CHANNEL_ORDER:
        for rank, ontology_id in enumerate(channels[channel], start=1):
            ranks.setdefault(ontology_id, {}).setdefault(channel, rank)

    by_id = index.by_id()
    ranks = _fold_obsolete_terms(ranks, by_id)
    if not ranks:
        return []

    excluded = _excluded_candidate_ids(
        ranks, by_id, excluded_prefixes, lineage_markers
    )
    if excluded:
        ranks = {
            ontology_id: channel_ranks
            for ontology_id, channel_ranks in ranks.items()
            if ontology_id not in excluded
        }
        if not ranks:
            return []

    # Provenance is claimed only when the semantic channel actually embedded
    # this label; a degraded or disabled channel must not look like a success.
    embedding_ran = embedding is not None and embedding.last_retrieval_ok
    embedding_model = embedding.model_id if embedding_ran else ""
    embedding_index_build = embedding.build_id if embedding_ran else ""

    ranked: list[tuple[float, str, dict[str, int]]] = [
        (_rrf_score(channel_ranks), ontology_id, channel_ranks)
        for ontology_id, channel_ranks in ranks.items()
    ]
    # Highest RRF first; ties broken by ontology id so output is deterministic.
    ranked.sort(key=lambda item: (-item[0], item[1]))

    shortlist: list[Candidate] = []
    for rank, (_, ontology_id, channel_ranks) in enumerate(ranked[:shortlist_size], start=1):
        term = by_id.get(ontology_id)
        if term is None:  # pragma: no cover - channels only ever yield indexed ids
            continue
        ordered_channels = tuple(
            channel for channel in CHANNEL_ORDER if channel in channel_ranks
        )
        shortlist.append(
            Candidate(
                trait_label=trait_label,
                ontology_release=index.ontology_release,
                rank=rank,
                ontology_id=term.ontology_id,
                ontology_label=term.label,
                definition=term.definition,
                parent_id=term.parent_id,
                parent_label=term.parent_label,
                channels=ordered_channels,
                channel_ranks=tuple(
                    (channel, channel_ranks[channel]) for channel in ordered_channels
                ),
                is_obsolete=term.is_obsolete,
                embedding_model=embedding_model,
                embedding_index_build=embedding_index_build,
            )
        )
    return shortlist


def generate_shortlists(
    trait_labels: Iterable[str],
    index: OntologyIndex,
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE,
    embedding: SemanticRetriever | EmbeddingChannel | None = None,
    *,
    excluded_prefixes: frozenset[str] = EXCLUDED_ONTOLOGY_PREFIXES,
    lineage_markers: frozenset[str] = EXCLUDED_LINEAGE_MARKERS,
) -> list[Candidate]:
    """Generate shortlists for every label, concatenated in input order.

    A label with no match contributes no rows rather than a fabricated one; the
    empty shortlist is visible as the label's absence from the output.

    A bare retriever is coerced to one run-scoped channel here, so a
    connection failure on an early label disables the semantic channel for
    every later label instead of retrying it per label. The candidate-space
    restriction's two lists pass through unchanged.
    """
    channel = as_embedding_channel(embedding)
    rows: list[Candidate] = []
    for label in trait_labels:
        rows.extend(
            _generate_shortlist(
                label,
                index,
                shortlist_size,
                channel,
                excluded_prefixes=excluded_prefixes,
                lineage_markers=lineage_markers,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Work queue and TSV rendering
# ---------------------------------------------------------------------------


def read_work_queue(path: Path | str) -> list[dict[str, str]]:
    """Read the gap scan's work queue TSV into row dictionaries.

    Requires a ``trait_label`` column; a ragged row is an error rather than a
    silently dropped work item.
    """
    queue_path = Path(path)
    if not queue_path.is_file():
        raise WorkQueueError(f"work queue does not exist: {queue_path}")

    with open(queue_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        columns = list(header) if header is not None else []
        if "trait_label" not in columns:
            raise WorkQueueError(
                f"{queue_path} has no trait_label column; is this a gap-scan queue?"
            )
        rows: list[dict[str, str]] = []
        for row_index, fields in enumerate(reader):
            if len(fields) != len(columns):
                raise WorkQueueError(
                    f"{queue_path} data row {row_index} has {len(fields)} fields; "
                    f"header has {len(columns)}"
                )
            rows.append(dict(zip(columns, fields)))
    return rows


def format_shortlist_tsv(candidates: Sequence[Candidate]) -> str:
    """Render the shortlist as a TSV; the header is always present."""
    lines = ["\t".join(SHORTLIST_COLUMNS)]
    lines.extend("\t".join(candidate.to_row()) for candidate in candidates)
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
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="candidates",
        description=(
            "Generate multi-channel lexical ontology shortlists for the "
            "unmapped Trait work queue."
        ),
    )
    parser.add_argument(
        "--work-queue",
        required=True,
        metavar="TSV",
        help="gap-scan work queue TSV (trait_label, occurrence_count, store_families)",
    )
    parser.add_argument(
        "--index",
        required=True,
        metavar="JSON",
        help="retrieval index built from the pinned ontology release",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="TSV",
        help="write the shortlist TSV here instead of stdout",
    )
    parser.add_argument(
        "--shortlist-size",
        type=int,
        default=DEFAULT_SHORTLIST_SIZE,
        metavar="N",
        help=f"maximum candidates per trait label (default: {DEFAULT_SHORTLIST_SIZE})",
    )
    parser.add_argument(
        "--enable-embedding",
        action="store_true",
        help=(
            "add the semantic embedding channel; requires or defaults to an "
            "ontology embedding store, and degrades to lexical-only when "
            "unavailable"
        ),
    )
    parser.add_argument(
        "--ontology-embeddings",
        default=None,
        metavar="DIR",
        help=(
            "ontology embedding store directory; supplying it also enables the "
            "channel (default: the release+model .cache/curation path)"
        ),
    )
    parser.add_argument(
        "--embedding-index",
        default=None,
        metavar="DIR",
        help=(
            "deprecated alias for --ontology-embeddings (kept for the curation "
            "round driver)"
        ),
    )
    parser.add_argument(
        "--trait-embeddings",
        default=None,
        metavar="DIR",
        help=(
            "precomputed trait embedding store directory; when supplied the "
            "semantic channel needs no network for the labels it covers"
        ),
    )
    parser.add_argument(
        "--embedding-model",
        default=PINNED_EMBEDDING_MODEL_ID,
        metavar="MODEL",
        help=(
            "default embedding model/artifact to resolve when no explicit "
            f"index is given (default: {PINNED_EMBEDDING_MODEL_ID})"
        ),
    )
    parser.add_argument(
        "--embedding-endpoint",
        default=os.environ.get("OPENGWASDB_EMBEDDING_ENDPOINT"),
        metavar="URL",
        help="hosted OpenAI-compatible /embeddings endpoint for the model",
    )
    parser.add_argument(
        "--embedding-api-key",
        default=os.environ.get("OPENGWASDB_EMBEDDING_API_KEY"),
        metavar="KEY",
        help="bearer token for the hosted embedding endpoint",
    )
    parser.add_argument(
        "--embedding-top-k",
        type=int,
        default=DEFAULT_EMBEDDING_TOP_K,
        metavar="N",
        help=f"neighbours the semantic channel returns (default: {DEFAULT_EMBEDDING_TOP_K})",
    )
    parser.add_argument(
        "--embedding-min-score",
        type=float,
        default=DEFAULT_EMBEDDING_MIN_SCORE,
        metavar="SCORE",
        help=(
            "minimum cosine similarity a semantic neighbour must exceed "
            f"(default: {DEFAULT_EMBEDDING_MIN_SCORE})"
        ),
    )
    parser.add_argument(
        "--embedding-batch-size",
        type=int,
        default=DEFAULT_EMBEDDING_BATCH_SIZE,
        metavar="N",
        help=(
            "texts per hosted embedding request when a label is embedded on "
            f"the fly (default: {DEFAULT_EMBEDDING_BATCH_SIZE})"
        ),
    )
    return parser


def resolve_embedding(
    args: argparse.Namespace,
    ontology_release: str,
) -> EmbeddingChannel | None:
    """Resolve the run-scoped semantic channel from parsed CLI args, or ``None``.

    The channel is enabled by ``--enable-embedding`` or by supplying an
    ontology or trait embedding store. Any unavailability is reported on
    stderr as a warning and returns ``None`` so the run continues
    lexical-only; a semantic channel problem must never fail candidate
    generation (issue #166).
    """
    ontology_path = getattr(args, "ontology_embeddings", None) or getattr(
        args, "embedding_index", None
    )
    trait_path = getattr(args, "trait_embeddings", None)
    if not (
        getattr(args, "enable_embedding", False) or ontology_path or trait_path
    ):
        return None
    try:
        retriever = resolve_retriever(
            ontology_path,
            ontology_release,
            model_id=args.embedding_model,
            endpoint=args.embedding_endpoint,
            api_key=args.embedding_api_key,
            top_k=args.embedding_top_k,
            min_score=args.embedding_min_score,
            trait_embeddings=trait_path,
            batch_size=getattr(
                args, "embedding_batch_size", DEFAULT_EMBEDDING_BATCH_SIZE
            ),
        )
    except EmbeddingError as exc:
        print(
            f"candidates: warning: semantic channel disabled: {exc}",
            file=sys.stderr,
        )
        return None
    return EmbeddingChannel(retriever)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.shortlist_size < 1:
        print(
            f"candidates: error: --shortlist-size must be at least 1, "
            f"got {args.shortlist_size}",
            file=sys.stderr,
        )
        return 1

    try:
        index = load_index(args.index)
        queue = read_work_queue(args.work_queue)
    except (IndexFormatError, WorkQueueError) as exc:
        print(f"candidates: error: {exc}", file=sys.stderr)
        return 1

    embedding = resolve_embedding(args, index.ontology_release)
    candidates = generate_shortlists(
        (row["trait_label"] for row in queue),
        index,
        args.shortlist_size,
        embedding,
    )
    if embedding is not None and embedding.tripped:
        print(
            f"candidates: warning: semantic channel disabled after "
            f"endpoint failure: {embedding.failure}",
            file=sys.stderr,
        )
    if embedding is not None and embedding.query_vector_misses:
        print(
            f"candidates: warning: {embedding.query_vector_misses} label(s) had "
            "no precomputed trait vector and no endpoint; treated as lexical-only",
            file=sys.stderr,
        )
    text = format_shortlist_tsv(candidates)

    if args.output:
        _write_text_atomically(text, Path(args.output))
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
