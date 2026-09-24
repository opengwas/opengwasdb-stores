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
``normalised``
    Match after trimming, lowercasing, and stripping punctuation.
``token_overlap``
    Jaccard overlap between the trait label's tokens and the tokens of each
    term's label and synonyms.
``synonym``
    Match against the term's known synonyms, and against acronyms generated
    from multi-word labels and synonyms (``body mass index`` -> ``BMI``).
``embedding``
    Semantic nearest-neighbour match: the label is embedded with the pinned
    model and compared against an embedding index of each term's label,
    synonyms, and definition (:mod:`curation.embedding`). This channel is
    optional (``--enable-embedding`` / ``--embedding-index``); when it is
    disabled or unavailable the run is lexical-only and still succeeds.

Every channel is additive. A candidate records which channels retrieved it and
each channel's rank, because a candidate resting on several agreeing channels is
stronger evidence than one resting on a single weak channel.

Merge and shortlist
-------------------
Candidates are unioned and deduplicated by ontology id, then ranked with
Reciprocal Rank Fusion (RRF, ``k = 60``) so a term found by several channels
outranks one found at the same rank by a single channel. The union is truncated
to the configurable shortlist size. A label no channel matches yields an empty
shortlist -- the generator never fabricates a term.

The pinned ontology release travels on every shortlist row so a later proposal
can record exactly what it was resolved against. When the semantic channel is
in use, the pinned embedding model and the content-addressed embedding index
build travel on every row as well.

CLI
---
::

    python3 -m curation.candidates \\
        --work-queue <queue.tsv> --index <index.json> --output <shortlist.tsv> \\
        [--shortlist-size N] \\
        [--enable-embedding --embedding-index <embedding.json>]
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
    DEFAULT_EMBEDDING_MIN_SCORE,
    DEFAULT_EMBEDDING_TOP_K,
    PINNED_EMBEDDING_MODEL_ID,
    EmbeddingError,
    SemanticRetriever,
    resolve_retriever,
)
from curation.ontology import (
    IndexFormatError,
    OntologyIndex,
    load_index,
)

# Channel names. Order is meaningful: it is the canonical order in which a
# candidate's channels and per-channel ranks are recorded.
CHANNEL_EXACT = "exact"
CHANNEL_NORMALISED = "normalised"
CHANNEL_TOKEN_OVERLAP = "token_overlap"
CHANNEL_SYNONYM = "synonym"
CHANNEL_EMBEDDING = "embedding"
CHANNEL_ORDER: tuple[str, ...] = (
    CHANNEL_EXACT,
    CHANNEL_NORMALISED,
    CHANNEL_TOKEN_OVERLAP,
    CHANNEL_SYNONYM,
    CHANNEL_EMBEDDING,
)

DEFAULT_SHORTLIST_SIZE: int = 10

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

_PUNCTUATION_RE = re.compile(r"[\W_]+", re.UNICODE)

# A TSV field cannot contain a tab or a line break. Free-text fields (a term's
# definition in particular) are flattened rather than allowed to shift columns.
_TSV_UNSAFE_RE = re.compile(r"[\t\r\n]+")


def _tsv_field(value: str) -> str:
    """Flatten a free-text value so it cannot break a TSV row's columns."""
    return _TSV_UNSAFE_RE.sub(" ", value)


def normalise_label(label: str | None) -> str:
    """Trim, lowercase, strip punctuation, and collapse whitespace.

    Deliberately close to, but wider than, the canonical table's
    ``trimws(tolower(x))`` lookup key: this is a *retrieval* normalisation, so
    ``"Body-mass index (BMI)"`` and ``"body mass index bmi"`` collapse to the
    same string and the normalised channel can find terms the exact channel
    cannot.
    """
    text = (label or "").strip().lower()
    text = _PUNCTUATION_RE.sub(" ", text)
    return " ".join(text.split())


def tokenize(label: str | None) -> frozenset[str]:
    """Tokenize a label after normalisation."""
    normalised = normalise_label(label)
    return frozenset(normalised.split()) if normalised else frozenset()


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard similarity, 0.0 for two empty token sets."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def acronym(label: str | None) -> str:
    """First letter of each token of a multi-word label, lowercased.

    A single-token label has no distinct acronym and yields ``""`` -- returning
    the token itself would make the synonym channel match on ordinary shared
    words.
    """
    tokens = normalise_label(label).split()
    if len(tokens) < 2:
        return ""
    return "".join(token[0] for token in tokens)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def exact_channel(label: str, index: OntologyIndex) -> list[str]:
    """Ontology ids whose label is byte-for-byte the trait label, in id order."""
    matches = [term.ontology_id for term in index if term.label == label]
    return sorted(matches)


def normalised_channel(label: str, index: OntologyIndex) -> list[str]:
    """Ontology ids whose label normalises to the trait label's normal form."""
    key = normalise_label(label)
    if not key:
        return []
    matches = [
        term.ontology_id
        for term in index
        if normalise_label(term.label) == key
    ]
    return sorted(matches)


def token_overlap_channel(label: str, index: OntologyIndex) -> list[str]:
    """Ontology ids ranked by Jaccard token overlap with the trait label.

    A term's searchable tokens are its label's tokens unioned with its
    synonyms' tokens, so an abbreviation reaches the term it abbreviates here
    as well as through the synonym channel.
    """
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


def embedding_channel(
    label: str,
    retriever: SemanticRetriever | None,
) -> list[str]:
    """Ontology ids nearest to the trait label in the embedding index.

    This is the semantic channel. It is independent of the lexical channels:
    the query is embedded and compared to the indexed label/synonym/definition
    vectors, so a label sharing no token with the correct term can still reach
    it. When no retriever is supplied, or the retriever cannot serve the query,
    the channel contributes nothing rather than failing candidate generation --
    the clean-degradation contract of issue #166.
    """
    if retriever is None:
        return []
    text = (label or "").strip()
    if not text:
        return []
    try:
        ranked = retriever.rank(text)
    except EmbeddingError:
        # An embedder that is unreachable mid-run degrades to lexical-only.
        return []
    return [ontology_id for ontology_id, _ in ranked[:CHANNEL_LIMIT]]


def run_channels(
    label: str,
    index: OntologyIndex,
    embedding: SemanticRetriever | None = None,
) -> dict[str, list[str]]:
    """Run every channel for one label, returning ``{channel: [ontology_id]}``.

    The lexical channels always run. The semantic channel runs only when a
    retriever is supplied; its key is always present so a caller can attribute
    an empty result to a disabled channel rather than a missing key.
    """
    return {
        CHANNEL_EXACT: exact_channel(label, index),
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


def generate_shortlist(
    trait_label: str,
    index: OntologyIndex,
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE,
    embedding: SemanticRetriever | None = None,
) -> list[Candidate]:
    """Return the top ``shortlist_size`` candidates for one trait label.

    The channels are unioned and deduplicated by ontology id, ranked by RRF,
    and truncated. An unmatched label returns ``[]``. When an embedding
    retriever is supplied, its channel contributes candidates and its model and
    index build are recorded on every returned candidate; when it is ``None``
    (or fails), the shortlist is the lexical-only one.
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

    if not ranks:
        return []

    embedding_model = embedding.model_id if embedding is not None else ""
    embedding_index_build = embedding.build_id if embedding is not None else ""

    by_id = index.by_id()
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
    embedding: SemanticRetriever | None = None,
) -> list[Candidate]:
    """Generate shortlists for every label, concatenated in input order.

    A label with no match contributes no rows rather than a fabricated one; the
    empty shortlist is visible as the label's absence from the output.
    """
    rows: list[Candidate] = []
    for label in trait_labels:
        rows.extend(generate_shortlist(label, index, shortlist_size, embedding))
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
            "embedding index, and degrades to lexical-only when unavailable"
        ),
    )
    parser.add_argument(
        "--embedding-index",
        default=None,
        metavar="JSON",
        help=(
            "semantic embedding index artifact; supplying it also enables the "
            "channel (default: the release+model .cache/curation path)"
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
    return parser


def resolve_embedding(
    args: argparse.Namespace,
    ontology_release: str,
) -> SemanticRetriever | None:
    """Resolve the semantic retriever from parsed CLI args, or ``None``.

    The channel is enabled by ``--enable-embedding`` or by supplying
    ``--embedding-index``. Any unavailability is reported on stderr as a
    warning and returns ``None`` so the run continues lexical-only; a semantic
    channel problem must never fail candidate generation (issue #166).
    """
    if not (getattr(args, "enable_embedding", False) or args.embedding_index):
        return None
    try:
        return resolve_retriever(
            args.embedding_index,
            ontology_release,
            model_id=args.embedding_model,
            endpoint=args.embedding_endpoint,
            api_key=args.embedding_api_key,
            top_k=args.embedding_top_k,
            min_score=args.embedding_min_score,
        )
    except EmbeddingError as exc:
        print(
            f"candidates: warning: semantic channel disabled: {exc}",
            file=sys.stderr,
        )
        return None


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
    text = format_shortlist_tsv(candidates)

    if args.output:
        _write_text_atomically(text, Path(args.output))
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
