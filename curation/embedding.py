#!/usr/bin/env python3
"""Semantic embedding channel and the on-disk vector store (issues #166, #161).

Lexical channels (:mod:`curation.candidates`) can only reach a Trait label that
shares a string or a token with the correct ontology term. This module adds the
*semantic* channel: it embeds each ontology term's label, synonyms, and
definition into a nearest-neighbour store, embeds (or looks up a precomputed)
query vector for the queued label with the same model, and returns the terms
whose vectors are closest.

The channel is deliberately independent of the lexical channels and of the
choice stage:

* it is enabled per run (``--enable-embedding`` / ``--ontology-embeddings``),
  never by default, so the lexical pipeline is unchanged unless asked;
* it degrades cleanly. A missing, stale, or mismatched store, or a model the
  run cannot reach, is reported and the run continues lexical-only -- a failure
  in the semantic channel must never fail candidate generation;
* the embedding model and the store build are pinned and recorded on the store
  artifact and on every shortlist row, alongside the ontology release, so a
  later proposal can state exactly which vectors and which ontology release it
  was resolved against.

Vector store
------------
An embedding store is a *directory* rather than a JSON document, because the
vectors have to be loaded into numpy and queried with a matrix multiply::

    <dir>/
      vectors.npy   float32 (count, dimension), each row L2-normalised
      ids.tsv       one item id per row, in the same order as vectors.npy
      meta.yaml     format_version, model_id, ontology_release, text_recipe,
                    text_recipe_version, dimension, count, built_at, build_id

``build_id`` is content-addressed over the model, release, text recipe, ids and
vector bytes, so the same inputs always produce the same id and a changed
vector changes it. That is what makes "the store build" pin-able on a shortlist
row rather than just "a store". The loader refuses a store whose declared
count, dimension, or build id disagrees with its files, and
:func:`resolve_retriever` refuses an ontology store and a trait store whose
model, release, or dimension disagree -- vectors from two different models are
not commensurable.

Embedders
---------
:class:`Embedder` is the narrow interface. Three implementations ship here:

* :class:`HttpEmbedder` calls a hosted HuggingFace
  text-embeddings-inference server over its OpenAI-compatible
  ``POST <base>/v1/embeddings`` endpoint. This is the production path, with
  exponential-backoff retries on 429/5xx/transport errors.
* :class:`DictionaryEmbedder` replays explicitly registered dense vectors
  offline. It is how precomputed embeddings (from any model, including a
  hosted one) are exercised hermetically, with no network and no model
  execution.
* :class:`HashingEmbedder` is a deterministic, stdlib-only, *offline stub* built
  for tests and for exercising the plumbing without a network. It is a feature
  hash over tokens, **not** a semantic model: it must never be described as one
  or offered as the production embedding channel.

CLI
---
Embed the pinned ontology index into a resumable store::

    python3 -m curation.embedding embed-ontology \\
        --ontology-index .cache/curation/efo-v3.94.0.index.json \\
        --output .cache/curation/efo-v3.94.0--BioLORD-2023.ontology-embeddings \\
        --endpoint "$OPENGWASDB_EMBEDDING_ENDPOINT"

Embed a work queue's trait labels with the same model as an ontology store::

    python3 -m curation.embedding embed-traits \\
        --work-queue .cache/curation/queue.tsv \\
        --output .cache/curation/traits.ontology-embeddings \\
        --model-of .cache/curation/efo-v3.94.0--BioLORD-2023.ontology-embeddings \\
        --endpoint "$OPENGWASDB_EMBEDDING_ENDPOINT"

Each run writes every finished chunk under ``<output>/chunks/`` before it
assembles the store, so an interrupted run resumes and only requests the
chunks it is missing.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

import numpy as np
import yaml

from curation.ontology import (
    IndexFormatError,
    OntologyIndex,
    OntologyTerm,
    load_index,
    normalise_label,
)

# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------

# The embedding model candidate generation embeds queries and ontology terms
# with. It is recorded on the store artifact and on every shortlist row. Bump
# only when the vectors are rebuilt with a different model; a store built with
# one model must never be queried with another.
PINNED_EMBEDDING_MODEL_ID: str = "FremyCompany/BioLORD-2023"

# The offline, deterministic stub embedder's model id. It exists so the
# plumbing and tests can run with no network; it is a feature hash, not a
# semantic model, and must not be used for a real curation run.
HASHING_EMBEDDING_MODEL_ID: str = "local-hashing-v1"

# The explicit-dictionary embedder's model id. It replays precomputed dense
# vectors offline, so a run or a test can exercise genuine nearest-neighbour
# retrieval without a network.
DICTIONARY_EMBEDDING_MODEL_ID: str = "local-dictionary-v1"

# The store schema version. A reader refuses an artifact whose version it does
# not know, so a rebuild is forced rather than a stale shape misread.
EMBEDDING_STORE_FORMAT_VERSION: int = 2

# Rebuildable embedding artifacts live outside the tracked tree, next to the
# lexical index (see `.gitignore`).
DEFAULT_EMBEDDING_INDEX_DIR: Path = Path(".cache") / "curation"

# The channel returns at most this many nearest neighbours before the union's
# own channel limit is applied.
DEFAULT_EMBEDDING_TOP_K: int = 50

# A neighbour is kept only when its cosine similarity is strictly greater than
# this value.
DEFAULT_EMBEDDING_MIN_SCORE: float = 0.0

# Dimension of the offline hashing stub's vectors.
DEFAULT_HASHING_DIMENSION: int = 256

# Texts per ``POST /v1/embeddings`` request.
DEFAULT_EMBEDDING_BATCH_SIZE: int = 128

# Texts per resumable chunk file. A finished chunk is written before the next
# one is requested, so a run resumes at chunk granularity.
DEFAULT_EMBEDDING_CHUNK_SIZE: int = 1000

# Retry policy for the hosted endpoint.
DEFAULT_EMBEDDING_MAX_RETRIES: int = 5
DEFAULT_EMBEDDING_BACKOFF_BASE: float = 0.5

# The text recipes that pin *what* was embedded, independently of the model.
ONTOLOGY_TEXT_RECIPE: str = "ontology-label-synonyms-definition"
ONTOLOGY_TEXT_RECIPE_VERSION: str = "1"
TRAIT_TEXT_RECIPE: str = "trait-label"
TRAIT_TEXT_RECIPE_VERSION: str = "1"

_META_FILENAME = "meta.yaml"
_VECTORS_FILENAME = "vectors.npy"
_IDS_FILENAME = "ids.tsv"
_CHUNKS_DIRNAME = "chunks"


class EmbeddingError(ValueError):
    """Base error for an embedding store, model, or query that cannot be served."""


class EmbeddingStoreError(EmbeddingError):
    """Raised when an embedding store directory is missing or malformed."""


class EmbeddingStoreCorruptError(EmbeddingStoreError):
    """Raised when a store's metadata disagrees with its vectors.

    The declared ``count``, ``dimension``, and ``build_id`` are a checksum over
    the stored bytes. When any disagrees with the files the artifact was
    truncated, hand-edited, or built by a different writer, and must not be
    queried.
    """


class EmbeddingUnavailableError(EmbeddingError):
    """Raised when no embedder can serve the store's pinned model.

    This is the clean-degradation trigger: callers catch it and run
    lexical-only rather than failing candidate generation.
    """


class EmbeddingQueryUnavailable(EmbeddingError):
    """Raised when one query has neither a precomputed vector nor an endpoint.

    Unlike :class:`EmbeddingUnavailableError` this is a per-label condition: the
    run continues, that label is lexical-only, and the channel counts it rather
    than tripping its circuit breaker.
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
    """Deterministic, stdlib-only, offline **stub** embedder.

    Each token is hashed to a vector dimension with a stable cryptographic hash
    (not Python's per-process ``hash``), signed, and the vector is
    L2-normalised. It exists so the embedding plumbing and its tests can run
    with no network and no third-party model. It is a feature hash over tokens
    and **not** a semantic model: use :class:`HttpEmbedder` with the pinned
    model for a real run.
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


class DictionaryEmbedder:
    """Offline embedder backed by an explicit ``text -> vector`` dictionary.

    It returns the dense vector registered for the exact text. It is how
    precomputed embeddings -- from any model, including a hosted one -- are
    replayed offline: register the texts a run will embed and it performs
    genuine nearest-neighbour retrieval with no network and no model execution.
    A text that is not registered maps to ``default`` (a zero vector unless one
    is supplied), so it retrieves nothing rather than fabricating similarity.
    """

    def __init__(
        self,
        model_id: str = DICTIONARY_EMBEDDING_MODEL_ID,
        vectors: Mapping[str, Sequence[float]] | None = None,
        default: Sequence[float] | None = None,
    ) -> None:
        self._model_id = model_id
        self._vectors: dict[str, tuple[float, ...]] = {
            text: tuple(float(component) for component in vector)
            for text, vector in (vectors or {}).items()
        }
        dimensions = {len(vector) for vector in self._vectors.values()}
        if len(dimensions) > 1:
            raise EmbeddingError(
                f"dictionary vectors disagree on dimension: {sorted(dimensions)}"
            )
        self._dimension = next(iter(dimensions), 0)
        if default is None:
            self._default = tuple(0.0 for _ in range(self._dimension))
        else:
            default_vector = tuple(float(component) for component in default)
            if self._dimension and len(default_vector) != self._dimension:
                raise EmbeddingError(
                    f"default vector has dimension {len(default_vector)}, but the "
                    f"registered vectors have dimension {self._dimension}"
                )
            self._dimension = len(default_vector)
            self._default = default_vector

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def register(self, text: str, vector: Sequence[float]) -> None:
        """Register (or replace) the dense vector for ``text``."""
        registered = tuple(float(component) for component in vector)
        if self._dimension and len(registered) != self._dimension:
            raise EmbeddingError(
                f"vector for {text!r} has dimension {len(registered)}, but the "
                f"dictionary uses dimension {self._dimension}"
            )
        if not self._dimension:
            self._dimension = len(registered)
            self._default = tuple(0.0 for _ in registered)
        self._vectors[text] = registered

    def registered_texts(self) -> tuple[str, ...]:
        """The texts this embedder has a vector for, in registration order."""
        return tuple(self._vectors)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._vectors.get(text, self._default) for text in texts]


def _status_code_of(exc: BaseException) -> int | None:
    """Return an HTTP status code carried by ``exc``, if any."""
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _is_transport_error(exc: BaseException) -> bool:
    """Whether ``exc`` looks like a transport/connection error worth retrying."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships in the curation env
        return False
    return isinstance(exc, httpx.TransportError)


class HttpEmbedder:
    """Hosted OpenAI-compatible ``/embeddings`` client.

    Calls a HuggingFace text-embeddings-inference server over
    ``POST <base>/v1/embeddings`` with ``{"model": ..., "input": [texts]}`` and
    reads ``{"data": [{"index", "embedding"}]}``. Requests are batched and
    retried with exponential backoff on HTTP 429, any 5xx, and transport
    errors. ``httpx`` is imported lazily so importing this module never
    requires it; any transport or response error is wrapped as
    :class:`EmbeddingUnavailableError`, which the callers turn into clean
    degradation.
    """

    def __init__(
        self,
        endpoint: str,
        model_id: str = PINNED_EMBEDDING_MODEL_ID,
        api_key: str | None = None,
        timeout: float = 30.0,
        batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
        max_retries: int = DEFAULT_EMBEDDING_MAX_RETRIES,
        backoff_base: float = DEFAULT_EMBEDDING_BACKOFF_BASE,
        sleep: Callable[[float], None] | None = None,
        client_factory: object | None = None,
    ) -> None:
        if not endpoint:
            raise EmbeddingError("an embedding endpoint is required")
        if batch_size < 1:
            raise EmbeddingError(f"batch_size must be at least 1, got {batch_size}")
        if max_retries < 0:
            raise EmbeddingError(
                f"max_retries must be non-negative, got {max_retries}"
            )
        if client_factory is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - httpx ships in curation env
                raise EmbeddingUnavailableError(
                    "httpx is required for the hosted embedding client"
                ) from exc
            client_factory = httpx.Client
        self._client_factory = client_factory
        self._endpoint = endpoint
        self._model_id = model_id
        self._api_key = api_key
        self._batch_size = batch_size
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep or time.sleep

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        try:
            with self._client_factory(timeout=self._timeout) as client:
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
            response = self._post_with_retries(client, batch)
            vectors.extend(self._parse_response(response, batch))
        return vectors

    def _post_with_retries(self, client: object, batch: Sequence[str]):
        request = {"model": self._model_id, "input": list(batch)}
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        attempt = 0
        while True:
            try:
                response = client.post(self._endpoint, json=request, headers=headers)
                response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001 - decide by status/type below
                if not self._is_retryable(exc) or attempt >= self._max_retries:
                    raise
                self._sleep(self._backoff_base * (2**attempt))
                attempt += 1

    def _is_retryable(self, exc: BaseException) -> bool:
        status = _status_code_of(exc)
        if status is not None:
            return status == 429 or 500 <= status < 600
        return _is_transport_error(exc)

    def _parse_response(
        self, response: object, batch: Sequence[str]
    ) -> list[tuple[float, ...]]:
        payload = response.json()
        if not isinstance(payload, dict):
            raise EmbeddingUnavailableError(
                "hosted embedding response was not a JSON object"
            )
        # An endpoint that reports the model it served must have served the one
        # we asked for; vectors from a different model are not commensurable
        # with the store and must not be used.
        returned_model = payload.get("model")
        if (
            isinstance(returned_model, str)
            and returned_model
            and returned_model != self._model_id
        ):
            raise EmbeddingUnavailableError(
                f"hosted embedding endpoint returned vectors for model "
                f"{returned_model!r}, not the requested {self._model_id!r}"
            )
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingUnavailableError(
                "hosted embedding response did not return one vector per input"
            )
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors: list[tuple[float, ...]] = []
        for item in ordered:
            if not isinstance(item, dict):
                raise EmbeddingUnavailableError(
                    "hosted embedding response item was not a JSON object"
                )
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
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    max_retries: int = DEFAULT_EMBEDDING_MAX_RETRIES,
    sleep: Callable[[float], None] | None = None,
) -> Embedder:
    """Return an embedder for ``model_id``, or raise if none can serve it.

    The offline hashing stub is always available. Any other model needs a
    hosted endpoint; without one this raises :class:`EmbeddingUnavailableError`
    so the caller can degrade to lexical-only.
    """
    if model_id == HASHING_EMBEDDING_MODEL_ID or model_id.startswith("local-hashing"):
        return HashingEmbedder(model_id=model_id)
    if endpoint:
        return HttpEmbedder(
            endpoint,
            model_id=model_id,
            api_key=api_key,
            batch_size=batch_size,
            max_retries=max_retries,
            sleep=sleep,
        )
    raise EmbeddingUnavailableError(
        f"embedding model {model_id!r} needs an endpoint, but none is configured"
    )


# ---------------------------------------------------------------------------
# Indexing text
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


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _l2_normalise(vectors: np.ndarray) -> np.ndarray:
    """Return ``vectors`` as float32 with each row L2-normalised."""
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim != 2:
        raise EmbeddingError(
            f"embedding vectors must be two-dimensional, got shape {array.shape}"
        )
    if array.shape[0] == 0:
        return array
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return (array / norms).astype(np.float32)


# ---------------------------------------------------------------------------
# The vector store
# ---------------------------------------------------------------------------


def _load_store_vectors(path: Path) -> np.ndarray:
    """Load a store's vectors, wrapping any load failure as corruption.

    A truncated or non-``.npy`` payload makes numpy raise a bare ``ValueError``
    (or ``OSError``); treating it as ``EmbeddingStoreCorruptError`` lets a
    caller distinguish a corrupt store from a missing one and refuse a pinned
    round instead of degrading it.
    """
    try:
        loaded = np.load(path, mmap_mode="r", allow_pickle=False)
        return np.asarray(loaded, dtype=np.float32)
    except EmbeddingStoreCorruptError:
        raise
    except Exception as exc:  # noqa: BLE001 - any load failure is corruption
        raise EmbeddingStoreCorruptError(
            f"embedding store vectors {path} could not be loaded: {exc}"
        ) from exc


@dataclass(frozen=True, eq=False)
class EmbeddingStore:
    """A pinned model's vectors for a set of item ids.

    ``ids`` and ``vectors`` are row-aligned: ``vectors[i]`` is the vector for
    ``ids[i]``. Every row is float32 and L2-normalised, so nearest-neighbour
    search is one matrix multiply. An ontology store's ids are ontology ids; a
    trait store's ids are normalised trait labels.
    """

    model_id: str
    ontology_release: str
    ids: tuple[str, ...]
    vectors: np.ndarray
    text_recipe: str
    text_recipe_version: str
    built_at: str = ""
    format_version: int = EMBEDDING_STORE_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not self.model_id:
            raise EmbeddingStoreError("embedding store carries no model id")
        if not self.ontology_release:
            raise EmbeddingStoreError("embedding store carries no ontology release")
        vectors = np.asarray(self.vectors, dtype=np.float32)
        if vectors.ndim != 2:
            raise EmbeddingStoreError(
                f"embedding vectors must be two-dimensional, got shape {vectors.shape}"
            )
        if vectors.shape[0] != len(self.ids):
            raise EmbeddingStoreError(
                f"embedding store has {len(self.ids)} ids but "
                f"{vectors.shape[0]} vectors"
            )
        object.__setattr__(self, "vectors", vectors)

    @property
    def count(self) -> int:
        return len(self.ids)

    @property
    def dimension(self) -> int:
        return int(self.vectors.shape[1]) if self.vectors.ndim == 2 else 0

    @cached_property
    def build_id(self) -> str:
        """A content-addressed identifier for this exact set of vectors.

        Same model, release, text recipe, ids, and vector bytes always hash to
        the same id; a changed vector changes it. Recording it on a shortlist
        row therefore pins the store *build*, not merely "some store".
        """
        digest = hashlib.blake2b(digest_size=16)
        for part in (
            str(self.format_version),
            self.model_id,
            self.ontology_release,
            self.text_recipe,
            self.text_recipe_version,
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x00")
        for item_id in self.ids:
            digest.update(item_id.encode("utf-8"))
            digest.update(b"\x00")
        digest.update(np.ascontiguousarray(self.vectors, dtype=np.float32).tobytes())
        return "blake2b:" + digest.hexdigest()

    @cached_property
    def _row_by_id(self) -> dict[str, int]:
        return {item_id: row for row, item_id in enumerate(self.ids)}

    def row_for(self, item_id: str) -> int | None:
        """The vector row for ``item_id``, or ``None`` when it is absent."""
        return self._row_by_id.get(item_id)

    def nearest(
        self,
        query_vectors: np.ndarray,
        top_k: int = DEFAULT_EMBEDDING_TOP_K,
        min_score: float = DEFAULT_EMBEDDING_MIN_SCORE,
    ) -> list[list[tuple[str, float]]]:
        """Rank the store for each query vector, batched.

        Returns one ``[(item_id, score), ...]`` list per query, highest score
        first and ties broken by id. Only neighbours strictly above
        ``min_score`` are kept.
        """
        queries = np.asarray(query_vectors, dtype=np.float32)
        if queries.ndim == 1:
            queries = queries[None, :]
        if queries.ndim != 2:
            raise EmbeddingStoreError(
                f"query vectors must be two-dimensional, got shape {queries.shape}"
            )
        if self.dimension and queries.shape[1] != self.dimension:
            raise EmbeddingStoreError(
                f"query vectors have dimension {queries.shape[1]}, but the store "
                f"expects {self.dimension}; query the store with the model that "
                "built it"
            )
        results: list[list[tuple[str, float]]] = []
        if top_k < 1 or self.count == 0:
            return [[] for _ in range(queries.shape[0])]

        scores = queries @ self.vectors.T  # (Q, N)
        k = min(top_k, self.count)
        top = np.argpartition(-scores, k - 1, axis=1)[:, :k]
        for row_index in range(queries.shape[0]):
            pairs = [
                (self.ids[column], float(scores[row_index, column]))
                for column in top[row_index]
                if scores[row_index, column] > min_score
            ]
            pairs.sort(key=lambda pair: (-pair[1], pair[0]))
            results.append(pairs)
        return results

    def to_meta(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "model_id": self.model_id,
            "ontology_release": self.ontology_release,
            "text_recipe": self.text_recipe,
            "text_recipe_version": self.text_recipe_version,
            "dimension": self.dimension,
            "count": self.count,
            "built_at": self.built_at,
            "build_id": self.build_id,
        }

    def save(self, directory: Path | str) -> None:
        """Write the store directory, replacing any existing files atomically."""
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        _atomic_numpy_save(target / _VECTORS_FILENAME, self.vectors)
        _write_text_atomically(
            "".join(f"{item_id}\n" for item_id in self.ids),
            target / _IDS_FILENAME,
        )
        _write_text_atomically(
            yaml.safe_dump(self.to_meta(), sort_keys=False),
            target / _META_FILENAME,
        )

    @classmethod
    def load(cls, directory: Path | str) -> "EmbeddingStore":
        """Load a store directory, failing loudly on a bad or corrupt shape."""
        source = Path(directory)
        if not source.is_dir():
            raise EmbeddingStoreError(f"embedding store does not exist: {source}")
        meta_path = source / _META_FILENAME
        vectors_path = source / _VECTORS_FILENAME
        ids_path = source / _IDS_FILENAME
        for path in (meta_path, vectors_path, ids_path):
            if not path.is_file():
                raise EmbeddingStoreError(
                    f"embedding store {source} is missing {path.name}"
                )
        try:
            meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise EmbeddingStoreError(
                f"{meta_path} is not valid YAML: {exc}"
            ) from exc
        if not isinstance(meta, dict):
            raise EmbeddingStoreError(f"{meta_path} is not a mapping")

        version = meta.get("format_version")
        if version != EMBEDDING_STORE_FORMAT_VERSION:
            raise EmbeddingStoreError(
                f"embedding store format version {version!r} is not the supported "
                f"{EMBEDDING_STORE_FORMAT_VERSION}; rebuild the store"
            )
        model_id = meta.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            raise EmbeddingStoreError("embedding store carries no model_id")
        release = meta.get("ontology_release")
        if not isinstance(release, str) or not release:
            raise EmbeddingStoreError("embedding store carries no ontology_release")
        recipe = meta.get("text_recipe")
        if not isinstance(recipe, str) or not recipe:
            raise EmbeddingStoreError("embedding store carries no text_recipe")
        recipe_version = meta.get("text_recipe_version")
        if not isinstance(recipe_version, str) or not recipe_version:
            raise EmbeddingStoreError("embedding store carries no text_recipe_version")
        declared_count = meta.get("count")
        declared_dimension = meta.get("dimension")
        if (
            isinstance(declared_count, bool)
            or not isinstance(declared_count, int)
            or isinstance(declared_dimension, bool)
            or not isinstance(declared_dimension, int)
        ):
            raise EmbeddingStoreCorruptError(
                "embedding store carries no integer count/dimension"
            )

        ids = tuple(
            line for line in ids_path.read_text(encoding="utf-8").splitlines() if line
        )
        if len(ids) != declared_count:
            raise EmbeddingStoreCorruptError(
                f"embedding store declares {declared_count} ids but ids.tsv has "
                f"{len(ids)}"
            )
        vectors = _load_store_vectors(vectors_path)
        if vectors.ndim != 2:
            raise EmbeddingStoreCorruptError(
                f"embedding store vectors are not two-dimensional: {vectors.shape}"
            )
        if vectors.shape != (declared_count, declared_dimension):
            raise EmbeddingStoreCorruptError(
                f"embedding store declares {declared_count}x{declared_dimension} "
                f"vectors but vectors.npy is {vectors.shape[0]}x{vectors.shape[1]}"
            )

        store = cls(
            model_id=model_id,
            ontology_release=release,
            ids=ids,
            vectors=vectors,
            text_recipe=recipe,
            text_recipe_version=recipe_version,
            built_at=str(meta.get("built_at", "")),
        )
        declared_build_id = meta.get("build_id")
        if not isinstance(declared_build_id, str) or not declared_build_id:
            raise EmbeddingStoreCorruptError(
                "embedding store carries no build_id"
            )
        if declared_build_id != store.build_id:
            raise EmbeddingStoreCorruptError(
                f"embedding store build id {declared_build_id!r} does not match "
                f"the content-address of its vectors ({store.build_id!r}); the "
                "store is corrupt or was edited"
            )
        return store

    @classmethod
    def load_from_directory(cls, directory: Path | str) -> "EmbeddingStore":
        """Alias for :meth:`load`."""
        return cls.load(directory)


@dataclass(frozen=True)
class EmbeddingStoreMeta:
    """An embedding store's ``meta.yaml`` without its vector payload.

    A curation round pins the model, ontology release, and content-addressed
    ``build_id`` of every store it consumed. Reading the meta file lets
    ``round-init`` record those pins and every later stage check them without
    loading (or hashing) gigabytes of vectors. The full :meth:`EmbeddingStore.load`
    still re-checks the build id against the vector bytes when the store is used.
    """

    model_id: str
    ontology_release: str
    build_id: str
    dimension: int
    count: int
    text_recipe: str = ""
    text_recipe_version: str = ""
    built_at: str = ""


def read_embedding_store_meta(directory: Path | str) -> EmbeddingStoreMeta:
    """Read an embedding store's ``meta.yaml`` without loading its vectors."""
    source = Path(directory)
    meta_path = source / _META_FILENAME
    if not meta_path.is_file():
        raise EmbeddingStoreError(f"embedding store does not exist: {source}")
    try:
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EmbeddingStoreError(f"{meta_path} is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise EmbeddingStoreError(f"{meta_path} is not a mapping")
    version = meta.get("format_version")
    if version != EMBEDDING_STORE_FORMAT_VERSION:
        raise EmbeddingStoreError(
            f"embedding store format version {version!r} is not the supported "
            f"{EMBEDDING_STORE_FORMAT_VERSION}; rebuild the store"
        )
    model_id = meta.get("model_id")
    release = meta.get("ontology_release")
    build_id = meta.get("build_id")
    for field_name, value in (
        ("model_id", model_id),
        ("ontology_release", release),
        ("build_id", build_id),
    ):
        if not isinstance(value, str) or not value:
            raise EmbeddingStoreError(
                f"embedding store {source} carries no {field_name}"
            )
    dimension = meta.get("dimension")
    count = meta.get("count")
    if (
        isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
    ):
        raise EmbeddingStoreError(
            f"embedding store {source} carries no integer count/dimension"
        )
    return EmbeddingStoreMeta(
        model_id=model_id,
        ontology_release=release,
        build_id=build_id,
        dimension=dimension,
        count=count,
        text_recipe=str(meta.get("text_recipe", "")),
        text_recipe_version=str(meta.get("text_recipe_version", "")),
        built_at=str(meta.get("built_at", "")),
    )


def nearest_neighbours(
    query_vector: Sequence[float],
    store: EmbeddingStore,
    top_k: int = DEFAULT_EMBEDDING_TOP_K,
    min_score: float = DEFAULT_EMBEDDING_MIN_SCORE,
) -> list[tuple[str, float]]:
    """Rank a single query vector against a store (see :meth:`EmbeddingStore.nearest`)."""
    ranked = store.nearest(np.asarray(query_vector, dtype=np.float32), top_k, min_score)
    return ranked[0] if ranked else []


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, 0.0 when either vector is empty or zero-length."""
    if len(left) != len(right) or not left:
        return 0.0
    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    left_norm = float(np.linalg.norm(left_array))
    right_norm = float(np.linalg.norm(right_array))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return float(np.dot(left_array, right_array) / (left_norm * right_norm))


# ---------------------------------------------------------------------------
# Building and persisting: resumable chunks
# ---------------------------------------------------------------------------


def _chunk_path(chunks_dir: Path, chunk_index: int) -> Path:
    return chunks_dir / f"{chunk_index:05d}.npz"


def _load_chunk(
    path: Path,
    expected_ids: tuple[str, ...],
    model_id: str,
    text_recipe: str,
    text_recipe_version: str,
) -> np.ndarray | None:
    """Return a cached chunk's vectors, or ``None`` when it must be re-embedded."""
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            stored_ids = tuple(str(item) for item in data["ids"].tolist())
            if stored_ids != expected_ids:
                return None
            if str(data["model_id"].tolist()) != model_id:
                return None
            if str(data["text_recipe"].tolist()) != text_recipe:
                return None
            if str(data["text_recipe_version"].tolist()) != text_recipe_version:
                return None
            return np.asarray(data["vectors"], dtype=np.float32)
    except (OSError, ValueError, KeyError, EOFError):
        return None


def _save_chunk(
    path: Path,
    ids: tuple[str, ...],
    vectors: np.ndarray,
    model_id: str,
    text_recipe: str,
    text_recipe_version: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(
        f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}.npz"
    )
    np.savez(
        temp_path,
        ids=np.array(ids),
        vectors=np.asarray(vectors, dtype=np.float32),
        model_id=np.array(model_id),
        text_recipe=np.array(text_recipe),
        text_recipe_version=np.array(text_recipe_version),
    )
    os.replace(temp_path, path)


def embed_in_chunks(
    ids: Sequence[str],
    texts: Sequence[str],
    embedder: Embedder,
    chunks_dir: Path | str,
    chunk_size: int = DEFAULT_EMBEDDING_CHUNK_SIZE,
    text_recipe: str = "",
    text_recipe_version: str = "",
    on_chunk: Callable[[int, int], None] | None = None,
) -> np.ndarray:
    """Embed ``texts`` in resumable chunks and return the concatenated vectors.

    Each finished chunk is saved under ``chunks_dir`` keyed by its position, its
    ids, its embedder's model id, and its text recipe. A rerun skips any chunk
    whose file matches all of those and only requests the rest, so an
    interrupted run resumes rather than re-embedding from the start.
    """
    if len(ids) != len(texts):
        raise EmbeddingError(
            f"got {len(ids)} ids for {len(texts)} texts"
        )
    if chunk_size < 1:
        raise EmbeddingError(f"chunk_size must be at least 1, got {chunk_size}")
    directory = Path(chunks_dir)
    directory.mkdir(parents=True, exist_ok=True)
    chunks = list(range(0, len(ids), chunk_size))
    arrays: list[np.ndarray] = []
    dimension: int | None = None
    for position, start in enumerate(chunks):
        end = min(start + chunk_size, len(ids))
        chunk_ids = tuple(ids[start:end])
        chunk_texts = list(texts[start:end])
        cache_path = _chunk_path(directory, position)
        vectors = _load_chunk(
            cache_path,
            chunk_ids,
            embedder.model_id,
            text_recipe,
            text_recipe_version,
        )
        if vectors is None:
            vectors = np.asarray(embedder.embed(chunk_texts), dtype=np.float32)
            if vectors.ndim != 2 or vectors.shape[0] != len(chunk_ids):
                raise EmbeddingError(
                    f"embedder {embedder.model_id!r} returned {vectors.shape[0]} "
                    f"vectors for {len(chunk_ids)} texts"
                )
            _save_chunk(
                cache_path,
                chunk_ids,
                vectors,
                embedder.model_id,
                text_recipe,
                text_recipe_version,
            )
        if dimension is None:
            dimension = int(vectors.shape[1]) if vectors.ndim == 2 else 0
        elif vectors.shape[1] != dimension:
            raise EmbeddingError(
                f"embedder {embedder.model_id!r} returned inconsistent vector "
                f"dimensions: {dimension} vs {vectors.shape[1]}"
            )
        arrays.append(vectors)
        if on_chunk is not None:
            on_chunk(position + 1, len(chunks))
    if not arrays:
        return np.zeros((0, 0), dtype=np.float32)
    return np.concatenate(arrays, axis=0).astype(np.float32)


def build_embedding_store(
    ids: Sequence[str],
    texts: Sequence[str],
    embedder: Embedder,
    ontology_release: str,
    text_recipe: str,
    text_recipe_version: str,
    chunks_dir: Path | str,
    chunk_size: int = DEFAULT_EMBEDDING_CHUNK_SIZE,
    built_at: str | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
) -> EmbeddingStore:
    """Embed ``texts`` into an L2-normalised store, resuming from chunks.

    The assembled store is written only by :meth:`EmbeddingStore.save`; the
    chunk files under ``chunks_dir`` are the resume state and may be kept or
    removed after a successful assembly.
    """
    vectors = embed_in_chunks(
        ids,
        texts,
        embedder,
        chunks_dir,
        chunk_size=chunk_size,
        text_recipe=text_recipe,
        text_recipe_version=text_recipe_version,
        on_chunk=on_chunk,
    )
    return EmbeddingStore(
        model_id=embedder.model_id,
        ontology_release=ontology_release,
        ids=tuple(ids),
        vectors=_l2_normalise(vectors),
        text_recipe=text_recipe,
        text_recipe_version=text_recipe_version,
        built_at=built_at or _utc_now(),
    )


def build_ontology_embedding_store(
    ontology_index: OntologyIndex,
    embedder: Embedder,
    chunks_dir: Path | str,
    chunk_size: int = DEFAULT_EMBEDDING_CHUNK_SIZE,
    built_at: str | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
) -> EmbeddingStore:
    """Embed every term's label, synonyms, and definition into a store."""
    terms = tuple(ontology_index)
    return build_embedding_store(
        ids=[term.ontology_id for term in terms],
        texts=[term_embedding_text(term) for term in terms],
        embedder=embedder,
        ontology_release=ontology_index.ontology_release,
        text_recipe=ONTOLOGY_TEXT_RECIPE,
        text_recipe_version=ONTOLOGY_TEXT_RECIPE_VERSION,
        chunks_dir=chunks_dir,
        chunk_size=chunk_size,
        built_at=built_at,
        on_chunk=on_chunk,
    )


def build_trait_embedding_store(
    labels: Sequence[str],
    embedder: Embedder,
    ontology_release: str,
    chunks_dir: Path | str,
    chunk_size: int = DEFAULT_EMBEDDING_CHUNK_SIZE,
    built_at: str | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
) -> EmbeddingStore:
    """Embed trait labels into a store keyed by the normalised label.

    Duplicate normalised labels collapse to one row (the first raw label seen),
    so a lookup by :func:`curation.ontology.normalise_label` is unambiguous.
    Rows are ordered by the normalised key for determinism.
    """
    representatives: dict[str, str] = {}
    for label in labels:
        key = normalise_label(label)
        if not key:
            continue
        representative = (label or "").strip()
        representatives.setdefault(key, representative or key)
    ids = sorted(representatives)
    texts = [representatives[key] for key in ids]
    return build_embedding_store(
        ids=ids,
        texts=texts,
        embedder=embedder,
        ontology_release=ontology_release,
        text_recipe=TRAIT_TEXT_RECIPE,
        text_recipe_version=TRAIT_TEXT_RECIPE_VERSION,
        chunks_dir=chunks_dir,
        chunk_size=chunk_size,
        built_at=built_at,
        on_chunk=on_chunk,
    )


def _atomic_numpy_save(path: Path, array: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(
        f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}.npy"
    )
    np.save(temp_path, np.ascontiguousarray(array, dtype=np.float32))
    os.replace(temp_path, path)


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
# Retrieval
# ---------------------------------------------------------------------------


class SemanticRetriever:
    """An ontology vector store plus how to produce query vectors for it.

    A query vector comes from a precomputed trait store when one is supplied
    and carries the label, and otherwise from ``embedder`` (the hosted model).
    With a trait store the semantic channel needs no network at all for the
    labels it covers; a label it does not cover raises
    :class:`EmbeddingQueryUnavailable`, unless an endpoint is configured to
    embed it on the fly.
    """

    def __init__(
        self,
        store: EmbeddingStore,
        embedder: Embedder | None = None,
        trait_store: EmbeddingStore | None = None,
        top_k: int = DEFAULT_EMBEDDING_TOP_K,
        min_score: float = DEFAULT_EMBEDDING_MIN_SCORE,
    ) -> None:
        if top_k < 1:
            raise EmbeddingError(f"top_k must be at least 1, got {top_k}")
        if embedder is not None and embedder.model_id != store.model_id:
            raise EmbeddingUnavailableError(
                f"embedder model {embedder.model_id!r} does not match embedding "
                f"store model {store.model_id!r}"
            )
        if trait_store is not None:
            if trait_store.model_id != store.model_id:
                raise EmbeddingUnavailableError(
                    f"trait store model {trait_store.model_id!r} does not match "
                    f"ontology store model {store.model_id!r}"
                )
            if trait_store.ontology_release != store.ontology_release:
                raise EmbeddingUnavailableError(
                    f"trait store release {trait_store.ontology_release!r} does not "
                    f"match ontology store release {store.ontology_release!r}"
                )
            if trait_store.dimension != store.dimension:
                raise EmbeddingUnavailableError(
                    f"trait store dimension {trait_store.dimension} does not match "
                    f"ontology store dimension {store.dimension}"
                )
        self._store = store
        self._embedder = embedder
        self._trait_store = trait_store
        self._top_k = top_k
        self._min_score = min_score

    @property
    def store(self) -> EmbeddingStore:
        return self._store

    @property
    def trait_store(self) -> EmbeddingStore | None:
        return self._trait_store

    @property
    def model_id(self) -> str:
        return self._store.model_id

    @property
    def build_id(self) -> str:
        return self._store.build_id

    @property
    def top_k(self) -> int:
        return self._top_k

    @property
    def min_score(self) -> float:
        return self._min_score

    def query_vector(self, text: str) -> np.ndarray:
        """The vector to query ``text`` with.

        Prefers a precomputed trait vector; falls back to the embedder; raises
        :class:`EmbeddingQueryUnavailable` when neither can serve the label.
        """
        if self._trait_store is not None:
            row = self._trait_store.row_for(normalise_label(text))
            if row is not None:
                return self._trait_store.vectors[row]
        if self._embedder is not None:
            vectors = self._embedder.embed([text])
            if len(vectors) != 1:
                raise EmbeddingUnavailableError(
                    f"embedder {self._embedder.model_id!r} did not return one "
                    "query vector"
                )
            vector = np.asarray(vectors[0], dtype=np.float32)
            if vector.ndim != 1 or vector.shape[0] != self._store.dimension:
                raise EmbeddingUnavailableError(
                    f"embedder {self._embedder.model_id!r} returned a query vector "
                    f"of shape {vector.shape}, but the store expects dimension "
                    f"{self._store.dimension}"
                )
            # Store vectors are L2-normalised, so the dot product in
            # ``nearest`` is only a cosine when the query is normalised too.
            norm = float(np.linalg.norm(vector))
            if norm != 0.0:
                vector = (vector / norm).astype(np.float32)
            return vector
        raise EmbeddingQueryUnavailable(
            f"no precomputed vector for {text!r} and no embedding endpoint is "
            "configured"
        )

    def embed_query(self, text: str) -> tuple[float, ...]:
        return tuple(float(component) for component in self.query_vector(text))

    def rank(self, text: str) -> list[tuple[str, float]]:
        """Rank the ontology store for ``text`` by cosine similarity."""
        vector = self.query_vector(text)
        ranked = self._store.nearest(vector, self._top_k, self._min_score)
        return ranked[0] if ranked else []


class EmbeddingChannel:
    """Run-scoped gate around a :class:`SemanticRetriever`.

    The channel owns the per-run state a bare retriever cannot:

    * **Provenance.** :attr:`last_retrieval_ok` records whether the most recent
      call produced a query vector. A disabled channel, or one that failed,
      reports ``False`` so the caller does not stamp the shortlist row with a
      model and store build that were never used.
    * **A circuit breaker.** A connection/endpoint failure
      (:class:`EmbeddingUnavailableError`) is almost always run-wide, so the
      first one trips the channel and every later label is served lexical-only
      without another embed attempt or timeout. A label with no precomputed
      vector and no endpoint (:class:`EmbeddingQueryUnavailable`) is counted
      instead, because the run can still serve other labels.
    """

    def __init__(self, retriever: SemanticRetriever | None = None) -> None:
        self._retriever = retriever
        self._tripped = False
        self._failure = ""
        self._last_retrieval_ok = False
        self._query_vector_misses = 0

    @property
    def retriever(self) -> SemanticRetriever | None:
        return self._retriever

    @property
    def available(self) -> bool:
        """True while the channel can still attempt a retrieval."""
        return self._retriever is not None and not self._tripped

    @property
    def tripped(self) -> bool:
        """True once an endpoint failure has disabled the channel for the run."""
        return self._tripped

    @property
    def failure(self) -> str:
        """The endpoint failure that tripped the breaker, or ``""``."""
        return self._failure

    @property
    def last_retrieval_ok(self) -> bool:
        """Whether the most recent :meth:`retrieve` produced a query vector."""
        return self._last_retrieval_ok

    @property
    def query_vector_misses(self) -> int:
        """Labels with no precomputed vector and no endpoint to embed on the fly."""
        return self._query_vector_misses

    @property
    def model_id(self) -> str:
        return self._retriever.model_id if self._retriever is not None else ""

    @property
    def build_id(self) -> str:
        return self._retriever.build_id if self._retriever is not None else ""

    def trip(self, reason: str) -> None:
        """Disable the channel for the rest of the run."""
        self._tripped = True
        self._failure = reason

    def retrieve(self, label: str) -> list[str]:
        """Return neighbour ids, or ``[]`` when disabled or failed."""
        self._last_retrieval_ok = False
        text = (label or "").strip()
        if not self.available or not text:
            return []
        try:
            ranked = self._retriever.rank(text)
        except EmbeddingQueryUnavailable:
            self._query_vector_misses += 1
            return []
        except EmbeddingUnavailableError as exc:
            self.trip(str(exc))
            return []
        except EmbeddingError:
            return []
        self._last_retrieval_ok = True
        return [ontology_id for ontology_id, _ in ranked]


def as_embedding_channel(
    embedding: SemanticRetriever | "EmbeddingChannel" | None,
) -> EmbeddingChannel | None:
    """Coerce a retriever (or channel) into a run-scoped channel."""
    if embedding is None or isinstance(embedding, EmbeddingChannel):
        return embedding
    return EmbeddingChannel(embedding)


def default_ontology_embeddings_path(
    ontology_release: str,
    model_id: str = PINNED_EMBEDDING_MODEL_ID,
) -> Path:
    """The default untracked store path for a release + model's ontology vectors."""
    return DEFAULT_EMBEDDING_INDEX_DIR / f"{_artifact_slug(ontology_release, model_id)}.ontology-embeddings"


def default_trait_embeddings_path(
    ontology_release: str,
    model_id: str = PINNED_EMBEDDING_MODEL_ID,
) -> Path:
    """The default untracked store path for a release + model's trait vectors."""
    return DEFAULT_EMBEDDING_INDEX_DIR / f"{_artifact_slug(ontology_release, model_id)}.trait-embeddings"


def _artifact_slug(ontology_release: str, model_id: str) -> str:
    return re.sub(
        r"[^A-Za-z0-9._-]+", "-", f"{ontology_release}--{model_id}"
    ).strip("-")


def resolve_retriever(
    ontology_embeddings: Path | str | None,
    ontology_release: str,
    model_id: str = PINNED_EMBEDDING_MODEL_ID,
    endpoint: str | None = None,
    api_key: str | None = None,
    top_k: int = DEFAULT_EMBEDDING_TOP_K,
    min_score: float = DEFAULT_EMBEDDING_MIN_SCORE,
    trait_embeddings: Path | str | None = None,
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    max_retries: int = DEFAULT_EMBEDDING_MAX_RETRIES,
    sleep: Callable[[float], None] | None = None,
    embedder: Embedder | None = None,
) -> SemanticRetriever:
    """Load an ontology store and the query-vector source for it.

    The ontology store's own model id is authoritative: passing ``model_id``
    only chooses the default artifact path when no explicit one is given. The
    trait store, when supplied, must agree on model, release, and dimension.
    An embedder is built only when needed -- for labels the trait store does not
    cover and only when an endpoint is configured. Raises
    :class:`EmbeddingError` for every way the channel can be unavailable, so a
    CLI can catch it once and continue lexical-only.
    """
    path = (
        Path(ontology_embeddings)
        if ontology_embeddings
        else default_ontology_embeddings_path(ontology_release, model_id)
    )
    store = EmbeddingStore.load(path)
    if store.ontology_release != ontology_release:
        raise EmbeddingUnavailableError(
            f"embedding store {path} was built for ontology release "
            f"{store.ontology_release!r}, not {ontology_release!r}"
        )
    trait_store: EmbeddingStore | None = None
    if trait_embeddings:
        trait_store = EmbeddingStore.load(trait_embeddings)
        if trait_store.model_id != store.model_id:
            raise EmbeddingUnavailableError(
                f"trait store model {trait_store.model_id!r} does not match "
                f"ontology store model {store.model_id!r}"
            )
        if trait_store.ontology_release != store.ontology_release:
            raise EmbeddingUnavailableError(
                f"trait store release {trait_store.ontology_release!r} does not "
                f"match ontology store release {store.ontology_release!r}"
            )
        if trait_store.dimension != store.dimension:
            raise EmbeddingUnavailableError(
                f"trait store dimension {trait_store.dimension} does not match "
                f"ontology store dimension {store.dimension}"
            )
    if embedder is None:
        if trait_store is None or endpoint:
            embedder = embedder_for_model(
                store.model_id,
                endpoint=endpoint,
                api_key=api_key,
                batch_size=batch_size,
                max_retries=max_retries,
                sleep=sleep,
            )
    return SemanticRetriever(
        store=store,
        embedder=embedder,
        trait_store=trait_store,
        top_k=top_k,
        min_score=min_score,
    )


# ---------------------------------------------------------------------------
# CLI: build the resumable stores
# ---------------------------------------------------------------------------


def _read_trait_labels(path: Path | str) -> list[str]:
    """Read the ``trait_label`` column of a gap-scan work queue TSV."""
    import csv

    label_path = Path(path)
    if not label_path.is_file():
        raise EmbeddingError(f"work queue does not exist: {label_path}")
    with open(label_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        columns = list(header) if header is not None else []
        if "trait_label" not in columns:
            raise EmbeddingError(
                f"{label_path} has no trait_label column; is this a gap-scan queue?"
            )
        index = columns.index("trait_label")
        labels: list[str] = []
        for row_index, fields in enumerate(reader):
            if len(fields) != len(columns):
                raise EmbeddingError(
                    f"{label_path} data row {row_index} has {len(fields)} fields; "
                    f"header has {len(columns)}"
                )
            labels.append(fields[index])
    return labels


def _progress(label: str):
    def report(done: int, total: int) -> None:
        print(f"{label}: chunk {done}/{total}", file=sys.stderr)

    return report


def build_ontology_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embedding embed-ontology",
        description=(
            "Embed the pinned ontology retrieval index into a resumable "
            "directory vector store."
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
        metavar="DIR",
        help="store directory (default: the release+model path)",
    )
    parser.add_argument(
        "--model",
        default=PINNED_EMBEDDING_MODEL_ID,
        metavar="MODEL",
        help=(
            "embedding model identifier; the offline stub "
            f"{HASHING_EMBEDDING_MODEL_ID!r} needs no endpoint "
            f"(default: {PINNED_EMBEDDING_MODEL_ID})"
        ),
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("OPENGWASDB_EMBEDDING_ENDPOINT"),
        metavar="URL",
        help="hosted OpenAI-compatible /v1/embeddings URL for a non-offline model",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENGWASDB_EMBEDDING_API_KEY"),
        metavar="KEY",
        help="bearer token for the hosted endpoint",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_EMBEDDING_BATCH_SIZE,
        metavar="N",
        help=f"texts per request (default: {DEFAULT_EMBEDDING_BATCH_SIZE})",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_EMBEDDING_CHUNK_SIZE,
        metavar="N",
        help=(
            "texts per resumable chunk file "
            f"(default: {DEFAULT_EMBEDDING_CHUNK_SIZE})"
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_EMBEDDING_MAX_RETRIES,
        metavar="N",
        help=f"retries per request (default: {DEFAULT_EMBEDDING_MAX_RETRIES})",
    )
    return parser


def build_traits_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embedding embed-traits",
        description=(
            "Embed a work queue's trait labels with the same model as an "
            "ontology vector store, keyed by the normalised trait label."
        ),
    )
    parser.add_argument(
        "--work-queue",
        required=True,
        metavar="TSV",
        help="gap-scan work queue TSV (trait_label, occurrence_count, store_families)",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="DIR",
        help="store directory (default: the release+model trait path)",
    )
    parser.add_argument(
        "--model-of",
        required=True,
        metavar="DIR",
        help="ontology embedding store whose model the trait vectors must match",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("OPENGWASDB_EMBEDDING_ENDPOINT"),
        metavar="URL",
        help="hosted OpenAI-compatible /v1/embeddings URL for a non-offline model",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENGWASDB_EMBEDDING_API_KEY"),
        metavar="KEY",
        help="bearer token for the hosted endpoint",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_EMBEDDING_BATCH_SIZE,
        metavar="N",
        help=f"texts per request (default: {DEFAULT_EMBEDDING_BATCH_SIZE})",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_EMBEDDING_CHUNK_SIZE,
        metavar="N",
        help=(
            "texts per resumable chunk file "
            f"(default: {DEFAULT_EMBEDDING_CHUNK_SIZE})"
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_EMBEDDING_MAX_RETRIES,
        metavar="N",
        help=f"retries per request (default: {DEFAULT_EMBEDDING_MAX_RETRIES})",
    )
    return parser


def _embed_ontology_main(argv: Sequence[str]) -> int:
    parser = build_ontology_parser()
    args = parser.parse_args(argv)
    try:
        ontology_index = load_index(args.ontology_index)
        embedder = embedder_for_model(
            args.model,
            endpoint=args.endpoint,
            api_key=args.api_key,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
        )
        output = (
            Path(args.output)
            if args.output
            else default_ontology_embeddings_path(
                ontology_index.ontology_release, embedder.model_id
            )
        )
        store = build_ontology_embedding_store(
            ontology_index,
            embedder,
            chunks_dir=output / _CHUNKS_DIRNAME,
            chunk_size=args.chunk_size,
            on_chunk=_progress("embed-ontology"),
        )
        store.save(output)
    except (EmbeddingError, IndexFormatError) as exc:
        print(f"embed-ontology: error: {exc}", file=sys.stderr)
        return 1
    print(
        f"embed-ontology: wrote {store.count} vectors "
        f"(dimension {store.dimension}, model {store.model_id}) for "
        f"{store.ontology_release} to {output}",
        file=sys.stderr,
    )
    return 0


def _embed_traits_main(argv: Sequence[str]) -> int:
    parser = build_traits_parser()
    args = parser.parse_args(argv)
    try:
        model_store = EmbeddingStore.load(args.model_of)
        labels = _read_trait_labels(args.work_queue)
        embedder = embedder_for_model(
            model_store.model_id,
            endpoint=args.endpoint,
            api_key=args.api_key,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
        )
        output = (
            Path(args.output)
            if args.output
            else default_trait_embeddings_path(
                model_store.ontology_release, model_store.model_id
            )
        )
        store = build_trait_embedding_store(
            labels,
            embedder,
            ontology_release=model_store.ontology_release,
            chunks_dir=output / _CHUNKS_DIRNAME,
            chunk_size=args.chunk_size,
            on_chunk=_progress("embed-traits"),
        )
        if store.model_id != model_store.model_id:
            raise EmbeddingError(
                f"trait embedder model {store.model_id!r} does not match the "
                f"ontology store model {model_store.model_id!r}"
            )
        if store.dimension != model_store.dimension:
            raise EmbeddingError(
                f"trait embedding dimension {store.dimension} does not match "
                f"the ontology store dimension {model_store.dimension}"
            )
        store.save(output)
    except (EmbeddingError, IndexFormatError) as exc:
        print(f"embed-traits: error: {exc}", file=sys.stderr)
        return 1
    print(
        f"embed-traits: wrote {store.count} vectors "
        f"(dimension {store.dimension}, model {store.model_id}) for "
        f"{store.ontology_release} to {output}",
        file=sys.stderr,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "embed-traits":
        return _embed_traits_main(arguments[1:])
    if arguments and arguments[0] == "embed-ontology":
        return _embed_ontology_main(arguments[1:])
    # Backwards-compatible default: the old ``embedding-index`` invocation is
    # treated as ``embed-ontology``.
    return _embed_ontology_main(arguments)


if __name__ == "__main__":
    sys.exit(main())
