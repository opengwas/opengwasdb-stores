#!/usr/bin/env python3
"""Semantic embedding channel for candidate generation (issue #166).

Lexical channels (:mod:`curation.candidates`) can only reach a Trait label that
shares a string or a token with the correct ontology term. This module adds the
*semantic* channel: it embeds each ontology term's label, synonyms, and
definition into a nearest-neighbour index, embeds the queued label with the same
model, and returns the terms whose vectors are closest.

The channel is deliberately independent of the lexical channels and of the
choice stage:

* it is enabled per run (``--enable-embedding`` / ``--embedding-index``), never
  by default, so the lexical pipeline is unchanged unless asked;
* it degrades cleanly. A missing, stale, or mismatched index, or a model the
  run cannot reach, is reported and the run continues lexical-only -- a failure
  in the semantic channel must never fail candidate generation;
* the embedding model and the index build are pinned and recorded on the index
  artifact and on every shortlist row, alongside the ontology release, so a
  later proposal can state exactly which vectors and which ontology release it
  was resolved against.

Model and index
---------------
The pinned model identifier is :data:`PINNED_EMBEDDING_MODEL_ID`. The index is
a JSON document versioned by ``index_format_version`` so a stale artifact fails
loudly rather than being read with the wrong shape::

    {
      "index_format_version": 1,
      "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
      "ontology_release": "efo/v3.78.0",
      "built_at": "2024-01-01T00:00:00Z",
      "dimension": 384,
      "index_build_id": "blake2b:...",
      "terms": [
        {"ontology_id": "EFO:0004340", "vector": [0.01, ...]}
      ]
    }

``index_build_id`` is content-addressed: the same model, release, and vectors
always produce the same id, and a changed vector changes it. That is what makes
"the index build" pin-able on a shortlist row rather than just "an index".

Embedders
---------
:class:`Embedder` is the narrow interface. Two implementations ship here:

* :class:`HashingEmbedder` is a deterministic, stdlib-only, offline embedder.
  It is what makes the whole channel testable and usable without a network, and
  it is a genuine (if weak) semantic proxy, not a test double.
* :class:`HttpEmbedder` calls a hosted OpenAI-compatible ``/embeddings``
  endpoint. ``httpx`` is imported lazily so the pure-stdlib path, and the test
  suite, never require it.

CLI
---
Rebuild the embedding index from a retrieval index (issue #164)::

    python3 -m curation.embedding \\
        --ontology-index .cache/curation/efo-v3.78.0.index.json \\
        --output .cache/curation/efo-v3.78.0--local-hashing-v1.embedding.json \\
        [--model MODEL] [--endpoint URL] [--api-key KEY]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Protocol, Sequence

from curation.ontology import (
    IndexFormatError,
    OntologyIndex,
    OntologyTerm,
    load_index,
)

# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------

# The embedding model candidate generation embeds queries and ontology terms
# with. It is recorded on the index artifact and on every shortlist row. Bump
# only when the vectors are rebuilt with a different model; an index built with
# one model must never be queried with another.
PINNED_EMBEDDING_MODEL_ID: str = "sentence-transformers/all-MiniLM-L6-v2"

# The offline, deterministic embedder's model id. It is a real model (feature
# hashing over tokens) rather than a fixture stand-in, so a run can build and
# query a semantic index with no network at all.
HASHING_EMBEDDING_MODEL_ID: str = "local-hashing-v1"

# The embedding index schema version. A reader refuses an artifact whose version
# it does not know, so a rebuild is forced rather than a stale shape misread.
EMBEDDING_INDEX_FORMAT_VERSION: int = 1

# Rebuildable embedding artifacts live outside the tracked tree, next to the
# lexical index (see `.gitignore`).
DEFAULT_EMBEDDING_INDEX_DIR: Path = Path(".cache") / "curation"

# The channel returns at most this many nearest neighbours before the union's
# own channel limit is applied. Bounded so a common query cannot make the
# union unbounded.
DEFAULT_EMBEDDING_TOP_K: int = 50

# A neighbour is kept only when its cosine similarity is strictly greater than
# this value. The default keeps any positive similarity and drops the noise a
# cosine near zero carries; a caller can raise it to tighten precision.
DEFAULT_EMBEDDING_MIN_SCORE: float = 0.0

# Dimension of the offline hashing embedder's vectors.
DEFAULT_HASHING_DIMENSION: int = 256

# A JSON float round-trips to fewer bits than a Python float. Vectors are
# rounded here so the on-disk index and the in-memory index agree exactly and
# the content-addressed build id is stable across a write/read cycle.
_VECTOR_PRECISION: int = 9


class EmbeddingError(ValueError):
    """Base error for an embedding index, model, or query that cannot be served."""


class EmbeddingIndexError(EmbeddingError):
    """Raised when an embedding index artifact is missing or malformed."""


class EmbeddingIndexCorruptError(EmbeddingIndexError):
    """Raised when an index artifact's metadata disagrees with its vectors.

    The declared ``dimension`` and ``index_build_id`` are a checksum over the
    stored vectors. When either does not match what the vectors actually
    compute to, the artifact was truncated, hand-edited, or built by a
    different writer, and it must not be queried.
    """


class EmbeddingUnavailableError(EmbeddingError):
    """Raised when no embedder can serve the index's pinned model.

    This is the clean-degradation trigger: callers catch it and run
    lexical-only rather than failing candidate generation.
    """


# ---------------------------------------------------------------------------
# Embedders
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    """The narrow embedding interface candidate generation depends on."""

    @property
    def model_id(self) -> str:
        """The pinned model identifier these vectors belong to."""
        ...

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Embed each text, returning one vector per text in input order."""
        ...


class HashingEmbedder:
    """Deterministic, stdlib-only, offline embedder.

    Each token is hashed to a vector dimension with a stable cryptographic
    hash (not Python's per-process ``hash``), signed, and the vector is
    L2-normalised. It is a real feature-hashing model: related labels that share
    tokens score higher, and an exact label scores a cosine of 1.0 against its
    own indexed text. It exists so the semantic channel can be built, queried,
    and tested with no network and no third-party dependency.
    """

    def __init__(
        self,
        dimension: int = DEFAULT_HASHING_DIMENSION,
        model_id: str = HASHING_EMBEDDING_MODEL_ID,
    ) -> None:
        if dimension < 1:
            raise EmbeddingError(f"dimension must be at least 1, got {dimension}")
        self._dimension = dimension
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self._dimension
        for token in re.findall(r"[a-z0-9]+", (text or "").lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self._dimension
            sign = 1.0 if (value >> 8) & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(component * component for component in vector))
        if norm:
            return tuple(component / norm for component in vector)
        return tuple(vector)


class HttpEmbedder:
    """Hosted OpenAI-compatible ``/embeddings`` client.

    ``httpx`` is imported lazily so importing this module (and running the test
    suite) never requires it. Any transport or response error is wrapped as
    :class:`EmbeddingUnavailableError`, which the callers turn into clean
    degradation rather than a candidate-generation failure.
    """

    def __init__(
        self,
        endpoint: str,
        model_id: str = PINNED_EMBEDDING_MODEL_ID,
        api_key: str | None = None,
        timeout: float = 30.0,
        batch_size: int = 64,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx ships in curation env
            raise EmbeddingUnavailableError(
                "httpx is required for the hosted embedding client"
            ) from exc
        if not endpoint:
            raise EmbeddingError("an embedding endpoint is required")
        if batch_size < 1:
            raise EmbeddingError(f"batch_size must be at least 1, got {batch_size}")
        self._httpx = httpx
        self._endpoint = endpoint
        self._model_id = model_id
        self._api_key = api_key
        self._batch_size = batch_size
        self._timeout = timeout

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        try:
            with self._httpx.Client(timeout=self._timeout) as client:
                return self._embed_batches(client, texts)
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport/JSON/HTTP all degrade
            raise EmbeddingUnavailableError(
                f"hosted embedding request to {self._endpoint!r} failed: {exc}"
            ) from exc

    def _embed_batches(
        self, client: object, texts: Sequence[str]
    ) -> list[tuple[float, ...]]:
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            payload = {"model": self._model_id, "input": batch}
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
            response = client.post(self._endpoint, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json().get("data")
            if not isinstance(data, list) or len(data) != len(batch):
                raise EmbeddingUnavailableError(
                    "hosted embedding response did not return one vector per input"
                )
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            for item in ordered:
                vector = item.get("embedding")
                if not isinstance(vector, list):
                    raise EmbeddingUnavailableError(
                        "hosted embedding response carried no embedding vector"
                    )
                vectors.append(tuple(float(component) for component in vector))
        return vectors


def embedder_for_model(
    model_id: str,
    endpoint: str | None = None,
    api_key: str | None = None,
) -> Embedder:
    """Return an embedder for ``model_id``, or raise if none can serve it.

    The offline hashing model is always available. Any other model needs a
    hosted endpoint; without one this raises :class:`EmbeddingUnavailableError`
    so the caller can degrade to lexical-only.
    """
    if model_id == HASHING_EMBEDDING_MODEL_ID or model_id.startswith("local-hashing"):
        return HashingEmbedder(model_id=model_id)
    if endpoint:
        return HttpEmbedder(endpoint, model_id=model_id, api_key=api_key)
    raise EmbeddingUnavailableError(
        f"embedding model {model_id!r} needs an endpoint, but none is configured"
    )


# ---------------------------------------------------------------------------
# Indexing ontology terms
# ---------------------------------------------------------------------------


def term_embedding_text(term: OntologyTerm) -> str:
    """The text indexed for one term: label, synonyms, and definition.

    The channel indexes all three deliberately (acceptance criterion: "not
    labels alone"). A synonym or a definition can carry the semantic signal a
    bare label lacks, and they are what lets the channel reach a term the
    lexical channels cannot.
    """
    parts = [term.label, *term.synonyms]
    if term.definition:
        parts.append(term.definition)
    return "\n".join(part for part in parts if part)


@dataclass(frozen=True)
class EmbeddedTerm:
    """One ontology term's vector in the embedding index."""

    ontology_id: str
    vector: tuple[float, ...]

    def to_dict(self) -> dict[str, object]:
        return {"ontology_id": self.ontology_id, "vector": list(self.vector)}

    @classmethod
    def from_dict(cls, data: object) -> "EmbeddedTerm":
        if not isinstance(data, dict):
            raise EmbeddingIndexError("embedding index term is not a JSON object")
        ontology_id = data.get("ontology_id")
        raw_vector = data.get("vector")
        if not isinstance(ontology_id, str) or not ontology_id:
            raise EmbeddingIndexError("embedding index term carries no ontology_id")
        if not isinstance(raw_vector, list) or not raw_vector:
            raise EmbeddingIndexError(
                f"embedding index term {ontology_id!r} carries no vector"
            )
        try:
            vector = tuple(float(component) for component in raw_vector)
        except (TypeError, ValueError) as exc:
            raise EmbeddingIndexError(
                f"embedding index term {ontology_id!r} has a non-numeric vector"
            ) from exc
        return cls(ontology_id=ontology_id, vector=vector)


@dataclass(frozen=True)
class EmbeddingIndex:
    """A pinned model's vectors for one ontology release's terms."""

    model_id: str
    ontology_release: str
    terms: tuple[EmbeddedTerm, ...]
    built_at: str = ""

    @property
    def dimension(self) -> int:
        return len(self.terms[0].vector) if self.terms else 0

    @cached_property
    def build_id(self) -> str:
        """A content-addressed identifier for this exact set of vectors.

        Same model, release, and vectors always hash to the same id; a changed
        vector changes it. Recording it on a shortlist row therefore pins the
        index *build*, not merely "some index".
        """
        payload = {
            "index_format_version": EMBEDDING_INDEX_FORMAT_VERSION,
            "embedding_model": self.model_id,
            "ontology_release": self.ontology_release,
            "terms": [term.to_dict() for term in self.terms],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "blake2b:" + hashlib.blake2b(blob, digest_size=16).hexdigest()

    def by_id(self) -> dict[str, EmbeddedTerm]:
        return {term.ontology_id: term for term in self.terms}

    def to_dict(self) -> dict[str, object]:
        return {
            "index_format_version": EMBEDDING_INDEX_FORMAT_VERSION,
            "embedding_model": self.model_id,
            "ontology_release": self.ontology_release,
            "built_at": self.built_at,
            "dimension": self.dimension,
            "index_build_id": self.build_id,
            "terms": [term.to_dict() for term in self.terms],
        }

    @classmethod
    def from_dict(cls, data: object) -> "EmbeddingIndex":
        if not isinstance(data, dict):
            raise EmbeddingIndexError("embedding index artifact is not a JSON object")
        version = data.get("index_format_version")
        if version != EMBEDDING_INDEX_FORMAT_VERSION:
            raise EmbeddingIndexError(
                f"embedding index format version {version!r} is not the supported "
                f"{EMBEDDING_INDEX_FORMAT_VERSION}; rebuild the index"
            )
        model_id = data.get("embedding_model")
        if not isinstance(model_id, str) or not model_id:
            raise EmbeddingIndexError("embedding index artifact carries no embedding_model")
        release = data.get("ontology_release")
        if not isinstance(release, str) or not release:
            raise EmbeddingIndexError("embedding index artifact carries no ontology_release")
        raw_terms = data.get("terms")
        if not isinstance(raw_terms, list):
            raise EmbeddingIndexError("embedding index artifact carries no terms list")
        terms = tuple(EmbeddedTerm.from_dict(term) for term in raw_terms)
        dimensions = {len(term.vector) for term in terms}
        if len(dimensions) > 1:
            raise EmbeddingIndexError(
                f"embedding index vectors disagree on dimension: {sorted(dimensions)}"
            )
        index = cls(
            model_id=model_id,
            ontology_release=release,
            terms=terms,
            built_at=str(data.get("built_at", "")),
        )

        declared_dimension = data.get("dimension")
        if isinstance(declared_dimension, bool) or not isinstance(declared_dimension, int):
            raise EmbeddingIndexCorruptError(
                "embedding index artifact carries no integer dimension"
            )
        if declared_dimension != index.dimension:
            raise EmbeddingIndexCorruptError(
                f"embedding index declares dimension {declared_dimension}, but its "
                f"vectors have dimension {index.dimension}"
            )

        declared_build_id = data.get("index_build_id")
        if not isinstance(declared_build_id, str) or not declared_build_id:
            raise EmbeddingIndexCorruptError(
                "embedding index artifact carries no index_build_id"
            )
        if declared_build_id != index.build_id:
            raise EmbeddingIndexCorruptError(
                f"embedding index build id {declared_build_id!r} does not match the "
                f"content-address of its vectors ({index.build_id!r}); the artifact "
                "is corrupt or was edited"
            )
        return index


def _round_vector(vector: Sequence[float]) -> tuple[float, ...]:
    return tuple(round(float(component), _VECTOR_PRECISION) for component in vector)


def build_embedding_index(
    ontology_index: OntologyIndex,
    embedder: Embedder,
    built_at: str | None = None,
) -> EmbeddingIndex:
    """Embed every term's label, synonyms, and definition into an index.

    The returned index carries the embedder's ``model_id`` and the ontology
    release, so the two pins travel together.
    """
    terms = tuple(ontology_index)
    texts = [term_embedding_text(term) for term in terms]
    vectors = embedder.embed(texts)
    if len(vectors) != len(terms):
        raise EmbeddingIndexError(
            f"embedder {embedder.model_id!r} returned {len(vectors)} vectors for "
            f"{len(terms)} terms"
        )
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) > 1:
        raise EmbeddingIndexError(
            f"embedder {embedder.model_id!r} returned inconsistent vector "
            f"dimensions: {sorted(dimensions)}"
        )
    return EmbeddingIndex(
        model_id=embedder.model_id,
        ontology_release=ontology_index.ontology_release,
        terms=tuple(
            EmbeddedTerm(term.ontology_id, _round_vector(vector))
            for term, vector in zip(terms, vectors)
        ),
        built_at=built_at or _utc_now(),
    )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Persisting and loading the index
# ---------------------------------------------------------------------------


def default_embedding_index_path(
    ontology_release: str,
    model_id: str = PINNED_EMBEDDING_MODEL_ID,
) -> Path:
    """The default untracked artifact path for a release + model's index."""
    slug = re.sub(
        r"[^A-Za-z0-9._-]+", "-", f"{ontology_release}--{model_id}"
    ).strip("-")
    return DEFAULT_EMBEDDING_INDEX_DIR / f"{slug}.embedding.json"


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


def write_embedding_index(index: EmbeddingIndex, path: Path | str) -> None:
    """Serialize an embedding index to ``path`` atomically."""
    text = json.dumps(index.to_dict(), ensure_ascii=False, indent=2) + "\n"
    _write_text_atomically(text, Path(path))


def load_embedding_index(path: Path | str) -> EmbeddingIndex:
    """Load a previously built embedding index, failing loudly on a bad shape."""
    index_path = Path(path)
    if not index_path.is_file():
        raise EmbeddingIndexError(f"embedding index does not exist: {index_path}")
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EmbeddingIndexError(
            f"{index_path} is not valid JSON: {exc}"
        ) from exc
    return EmbeddingIndex.from_dict(data)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, 0.0 when either vector is empty or zero-length."""
    if len(left) != len(right) or not left:
        return 0.0
    dot = math.fsum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(math.fsum(a * a for a in left))
    right_norm = math.sqrt(math.fsum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def nearest_neighbours(
    query_vector: Sequence[float],
    index: EmbeddingIndex,
    top_k: int = DEFAULT_EMBEDDING_TOP_K,
    min_score: float = DEFAULT_EMBEDDING_MIN_SCORE,
) -> list[tuple[str, float]]:
    """Rank indexed terms by cosine similarity to ``query_vector``.

    Returns ``(ontology_id, score)`` pairs, highest score first, ties broken by
    ontology id so the ranking is deterministic. Only neighbours strictly above
    ``min_score`` are kept.
    """
    if top_k < 1:
        return []
    if index.dimension and len(query_vector) != index.dimension:
        raise EmbeddingIndexError(
            f"query vector has dimension {len(query_vector)}, but the index "
            f"expects {index.dimension}; query the index with the model that built it"
        )
    scored: list[tuple[float, str]] = []
    for term in index.terms:
        score = cosine_similarity(query_vector, term.vector)
        if score > min_score:
            scored.append((score, term.ontology_id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [(ontology_id, score) for score, ontology_id in scored[:top_k]]


@dataclass(frozen=True)
class SemanticRetriever:
    """An embedding index plus the embedder that queries it.

    The two pins must agree: the embedder's model is the index's model. A
    mismatch means the vectors are not commensurable, so the retriever refuses
    to be built rather than returning meaningless neighbours.
    """

    index: EmbeddingIndex
    embedder: Embedder
    top_k: int = DEFAULT_EMBEDDING_TOP_K
    min_score: float = DEFAULT_EMBEDDING_MIN_SCORE

    def __post_init__(self) -> None:
        if self.embedder.model_id != self.index.model_id:
            raise EmbeddingUnavailableError(
                f"embedder model {self.embedder.model_id!r} does not match "
                f"embedding index model {self.index.model_id!r}"
            )
        if self.top_k < 1:
            raise EmbeddingError(f"top_k must be at least 1, got {self.top_k}")

    @property
    def model_id(self) -> str:
        return self.index.model_id

    @property
    def build_id(self) -> str:
        return self.index.build_id

    def embed_query(self, text: str) -> tuple[float, ...]:
        vectors = self.embedder.embed([text])
        if len(vectors) != 1:
            raise EmbeddingUnavailableError(
                f"embedder {self.embedder.model_id!r} did not return one query vector"
            )
        return tuple(float(component) for component in vectors[0])

    def rank(self, text: str) -> list[tuple[str, float]]:
        """Rank the indexed terms for ``text`` by cosine similarity."""
        return nearest_neighbours(
            self.embed_query(text), self.index, self.top_k, self.min_score
        )


def resolve_retriever(
    embedding_index: Path | str | None,
    ontology_release: str,
    model_id: str = PINNED_EMBEDDING_MODEL_ID,
    endpoint: str | None = None,
    api_key: str | None = None,
    top_k: int = DEFAULT_EMBEDDING_TOP_K,
    min_score: float = DEFAULT_EMBEDDING_MIN_SCORE,
) -> SemanticRetriever:
    """Load an embedding index and the embedder that can query it.

    The artifact's own model id is authoritative: passing ``model_id`` only
    chooses the default artifact path when no explicit one is given. Raises
    :class:`EmbeddingError` for every way the channel can be unavailable, so a
    CLI can catch it once and continue lexical-only.
    """
    path = (
        Path(embedding_index)
        if embedding_index
        else default_embedding_index_path(ontology_release, model_id)
    )
    index = load_embedding_index(path)
    if index.ontology_release != ontology_release:
        raise EmbeddingUnavailableError(
            f"embedding index {path} was built for ontology release "
            f"{index.ontology_release!r}, not {ontology_release!r}"
        )
    embedder = embedder_for_model(index.model_id, endpoint=endpoint, api_key=api_key)
    return SemanticRetriever(
        index=index, embedder=embedder, top_k=top_k, min_score=min_score
    )


# ---------------------------------------------------------------------------
# CLI: rebuild the embedding index artifact
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embedding-index",
        description=(
            "Rebuild the untracked semantic embedding index from the pinned "
            "ontology retrieval index."
        ),
    )
    parser.add_argument(
        "--ontology-index",
        required=True,
        metavar="JSON",
        help="retrieval index built by curation.ontology",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="embedding index artifact path (default: the release+model path)",
    )
    parser.add_argument(
        "--model",
        default=PINNED_EMBEDDING_MODEL_ID,
        metavar="MODEL",
        help=(
            "embedding model identifier; the offline "
            f"{HASHING_EMBEDDING_MODEL_ID!r} needs no endpoint "
            f"(default: {PINNED_EMBEDDING_MODEL_ID})"
        ),
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("OPENGWASDB_EMBEDDING_ENDPOINT"),
        metavar="URL",
        help="hosted OpenAI-compatible /embeddings endpoint for a non-offline model",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENGWASDB_EMBEDDING_API_KEY"),
        metavar="KEY",
        help="bearer token for the hosted endpoint",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        ontology_index = load_index(args.ontology_index)
        embedder = embedder_for_model(
            args.model, endpoint=args.endpoint, api_key=args.api_key
        )
        embedding_index = build_embedding_index(ontology_index, embedder)
    except (EmbeddingError, IndexFormatError) as exc:
        print(f"embedding-index: error: {exc}", file=sys.stderr)
        return 1

    output = (
        Path(args.output)
        if args.output
        else default_embedding_index_path(ontology_index.ontology_release, embedder.model_id)
    )
    write_embedding_index(embedding_index, output)
    print(
        f"embedding-index: wrote {len(embedding_index.terms)} vectors "
        f"(dimension {embedding_index.dimension}, model {embedding_index.model_id}) "
        f"for {embedding_index.ontology_release} to {output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
