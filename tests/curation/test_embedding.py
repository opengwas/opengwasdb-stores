#!/usr/bin/env python3
"""Tests for the semantic embedding channel and vector store (issues #166, #161).

The user-visible contract under test is that candidate generation can add a
semantic nearest-neighbour channel whose ontology vectors live in a numpy
directory store, whose query vectors come from a precomputed trait store when
one covers the label (no network) or from a hosted model over HTTP when an
endpoint is configured, whose model/build pins are recorded only when the
channel actually ran, and which degrades cleanly to lexical-only when its store
or model is unavailable.

The suite is hermetic. The only embedders exercised for retrieval are the
offline ``DictionaryEmbedder`` (explicit dense vectors) and the offline
``HashingEmbedder`` stub; the hosted client's retry/backoff and response
validation are exercised through injected fake clients. Nothing opens a socket.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import importlib.util
import unittest
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation import candidates, embedding
from curation.candidates import (
    CHANNEL_EMBEDDING,
    CHANNEL_EXACT,
    CHANNEL_NORMALISED,
    CHANNEL_SYNONYM,
    CHANNEL_TOKEN_OVERLAP,
    embedding_channel,
    format_shortlist_tsv,
    generate_shortlist,
    generate_shortlists,
    run_channels,
)
from curation.embedding import (
    DICTIONARY_EMBEDDING_MODEL_ID,
    EMBEDDING_STORE_FORMAT_VERSION,
    HASHING_EMBEDDING_MODEL_ID,
    ONTOLOGY_TEXT_RECIPE,
    ONTOLOGY_TEXT_RECIPE_VERSION,
    PINNED_EMBEDDING_MODEL_ID,
    TRAIT_TEXT_RECIPE,
    TRAIT_TEXT_RECIPE_VERSION,
    DictionaryEmbedder,
    EmbeddingChannel,
    EmbeddingError,
    EmbeddingQueryUnavailable,
    EmbeddingStore,
    EmbeddingStoreCorruptError,
    EmbeddingStoreError,
    EmbeddingUnavailableError,
    HashingEmbedder,
    HttpEmbedder,
    SemanticRetriever,
    build_ontology_embedding_store,
    build_trait_embedding_store,
    cosine_similarity,
    default_ontology_embeddings_path,
    default_trait_embeddings_path,
    embedder_for_model,
    nearest_neighbours,
    read_embedding_store_meta,
    resolve_retriever,
    term_embedding_text,
)
from curation.harvest import STRATUM_ANALYTE_MEASUREMENT
from curation.ontology import (
    PINNED_ONTOLOGY_RELEASE,
    OntologyIndex,
    OntologyTerm,
    build_index_from_obo,
    normalise_label,
    write_index,
)
from curation.recall import ValidationPair, compare_recall, evaluate_recall

FIXTURE_MODEL_ID = "fixture-embedder-v1"
DIMENSION = 3

# EFO:0004340 is lexically reachable; EFO:0000999 is the semantic target whose
# label shares no token with the query below.
FIXTURE_OBO = """
format-version: 1.2
ontology: efo

[Term]
id: EFO:0004340
name: Body mass index
def: "A measurement of body mass index." [PMID:123]
synonym: "BMI" EXACT []
synonym: "Quetelet index" EXACT []

[Term]
id: EFO:0000999
name: cardio-metabolic phenotype
def: "A cardiometabolic trait." []

[Term]
id: EFO:0004518
name: systolic blood pressure
def: "A systolic blood pressure measurement." []
"""

SEMANTIC_QUERY = "quercetin bioavailability"


class RecordingEmbedder:
    """Records the texts it was asked to embed and returns fixed-width rows."""

    def __init__(self, model_id: str = FIXTURE_MODEL_ID) -> None:
        self.model_id = model_id
        self.seen: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.seen.extend(texts)
        return [(float(len(text)), 1.0) for text in texts]


class BrokenEmbedder:
    """An embedder that fails at query time, to exercise degradation."""

    model_id = FIXTURE_MODEL_ID

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        raise EmbeddingUnavailableError("hosted embedder is down")


class CountingFailingEmbedder:
    """An endpoint that fails every call and counts how often it was tried."""

    def __init__(self, model_id: str = FIXTURE_MODEL_ID) -> None:
        self.model_id = model_id
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.calls += 1
        raise EmbeddingUnavailableError("endpoint down")


def fixture_ontology_index(release: str = PINNED_ONTOLOGY_RELEASE) -> OntologyIndex:
    import tempfile as _tempfile

    tmp = _tempfile.NamedTemporaryFile("w", suffix=".obo", delete=False, encoding="utf-8")
    try:
        tmp.write(FIXTURE_OBO)
        tmp.close()
        return build_index_from_obo(Path(tmp.name), release)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def fixture_ontology_store(release: str = PINNED_ONTOLOGY_RELEASE) -> EmbeddingStore:
    """A store whose three unit vectors point at the three fixture terms."""
    return EmbeddingStore(
        model_id=FIXTURE_MODEL_ID,
        ontology_release=release,
        ids=("EFO:0004340", "EFO:0000999", "EFO:0004518"),
        vectors=np.eye(DIMENSION, dtype=np.float32),
        text_recipe=ONTOLOGY_TEXT_RECIPE,
        text_recipe_version=ONTOLOGY_TEXT_RECIPE_VERSION,
        built_at="2024-01-01T00:00:00Z",
    )


def fixture_trait_store(release: str = PINNED_ONTOLOGY_RELEASE) -> EmbeddingStore:
    """The query's precomputed vector: it points at EFO:0000999."""
    return EmbeddingStore(
        model_id=FIXTURE_MODEL_ID,
        ontology_release=release,
        ids=(normalise_label(SEMANTIC_QUERY),),
        vectors=np.array([[0.0, 1.0, 0.0]], dtype=np.float32),
        text_recipe=TRAIT_TEXT_RECIPE,
        text_recipe_version=TRAIT_TEXT_RECIPE_VERSION,
        built_at="2024-01-01T00:00:00Z",
    )


def fixture_retriever() -> SemanticRetriever:
    """A retriever that embeds the query on the fly with an explicit vector."""
    embedder = DictionaryEmbedder(
        model_id=FIXTURE_MODEL_ID, vectors={SEMANTIC_QUERY: (0.0, 1.0, 0.0)}
    )
    return SemanticRetriever(
        store=fixture_ontology_store(), embedder=embedder, top_k=5
    )


def parse_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()
    header = lines[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]
    return header, rows


# ---------------------------------------------------------------------------
# Fakes for the hosted client
# ---------------------------------------------------------------------------


class _HttpStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _HttpStatusError(self.status_code)

    def json(self) -> object:
        return self._payload


def _payload_for(model_id: str, inputs: Sequence[str]) -> dict[str, object]:
    return {
        "model": model_id,
        "data": [
            {"index": index, "embedding": [float(len(text)), 1.0]}
            for index, text in enumerate(inputs)
        ],
    }


class RecordingServer:
    """Serves embeddings and records each request; can fail on one call."""

    def __init__(
        self,
        model_id: str = FIXTURE_MODEL_ID,
        fail_on_call: int | None = None,
        fail_status: int = 503,
    ) -> None:
        self.model_id = model_id
        self.fail_on_call = fail_on_call
        self.fail_status = fail_status
        self.requests: list[list[str]] = []
        self.calls = 0

    def client_factory(self, **kwargs: object) -> "_RecordingClient":
        return _RecordingClient(self)

    def handle(self, inputs: Sequence[str]) -> _FakeResponse:
        self.requests.append(list(inputs))
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise _HttpStatusError(self.fail_status)
        return _FakeResponse(_payload_for(self.model_id, inputs))


class _RecordingClient:
    def __init__(self, server: RecordingServer) -> None:
        self._server = server

    def __enter__(self) -> "_RecordingClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, json: object = None, headers: object = None):
        return self._server.handle(list(json["input"]))  # type: ignore[index]


class FlakyServer:
    """A server scripted with a sequence of actions: ``"ok"``, a status, or transport."""

    def __init__(self, pattern: Sequence[object], model_id: str = FIXTURE_MODEL_ID) -> None:
        self.pattern = list(pattern)
        self.model_id = model_id
        self.calls = 0
        self.requests: list[list[str]] = []

    def client_factory(self, **kwargs: object) -> "_FlakyClient":
        return _FlakyClient(self)

    def next_action(self) -> object:
        action = self.pattern[self.calls] if self.calls < len(self.pattern) else "ok"
        self.calls += 1
        return action


class _FlakyClient:
    def __init__(self, server: FlakyServer) -> None:
        self._server = server

    def __enter__(self) -> "_FlakyClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, json: object = None, headers: object = None):
        inputs = list(json["input"])  # type: ignore[index]
        self._server.requests.append(inputs)
        action = self._server.next_action()
        if action == "transport":
            import httpx

            raise httpx.ConnectError("connection refused")
        if action == "ok":
            return _FakeResponse(_payload_for(self._server.model_id, inputs))
        return _FakeResponse(_payload_for(self._server.model_id, inputs), int(action))


# ---------------------------------------------------------------------------
# Store shape and content
# ---------------------------------------------------------------------------


class TestTermEmbeddingText(unittest.TestCase):
    """A term is indexed by label, synonyms, and definition, not label alone."""

    def test_includes_label_synonyms_and_definition(self) -> None:
        text = term_embedding_text(
            OntologyTerm(
                ontology_id="EFO:1",
                label="body mass index",
                definition="a measurement of body mass",
                synonyms=("BMI", "Quetelet index"),
            )
        )
        self.assertIn("body mass index", text)
        self.assertIn("BMI", text)
        self.assertIn("Quetelet index", text)
        self.assertIn("a measurement of body mass", text)

    def test_label_only_when_nothing_else(self) -> None:
        self.assertEqual(
            term_embedding_text(OntologyTerm(ontology_id="EFO:1", label="height")),
            "height",
        )


class TestCosineAndNeighbours(unittest.TestCase):
    """Similarity and neighbour ranking are deterministic and bounded."""

    def test_cosine_bounds(self) -> None:
        self.assertAlmostEqual(cosine_similarity((1.0, 0.0), (1.0, 0.0)), 1.0)
        self.assertAlmostEqual(cosine_similarity((1.0, 0.0), (0.0, 1.0)), 0.0)
        self.assertEqual(cosine_similarity((0.0, 0.0), (1.0, 0.0)), 0.0)
        self.assertEqual(cosine_similarity((1.0,), (1.0, 0.0)), 0.0)

    def test_nearest_neighbours_rank_and_tie_break(self) -> None:
        store = EmbeddingStore(
            model_id="m",
            ontology_release="r",
            ids=("EFO:2", "EFO:1", "EFO:3"),
            vectors=np.array(
                [[1.0, 0.0], [1.0, 0.0], [0.5, 0.5]], dtype=np.float32
            ),
            text_recipe="r",
            text_recipe_version="1",
        )
        ranked = nearest_neighbours((1.0, 0.0), store, top_k=3)
        self.assertEqual(
            [ontology_id for ontology_id, _ in ranked], ["EFO:1", "EFO:2", "EFO:3"]
        )

    def test_nearest_is_batched_in_query_order(self) -> None:
        store = fixture_ontology_store()
        ranked = store.nearest(
            np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32), top_k=1
        )
        self.assertEqual(ranked[0][0][0], "EFO:0004340")
        self.assertEqual(ranked[1][0][0], "EFO:0004518")

    def test_min_score_filters(self) -> None:
        store = EmbeddingStore(
            model_id="m",
            ontology_release="r",
            ids=("EFO:1", "EFO:2"),
            vectors=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
            text_recipe="r",
            text_recipe_version="1",
        )
        ranked = nearest_neighbours((1.0, 0.0), store, min_score=0.5)
        self.assertEqual([ontology_id for ontology_id, _ in ranked], ["EFO:1"])

    def test_dimension_mismatch_raises(self) -> None:
        store = EmbeddingStore(
            model_id="m",
            ontology_release="r",
            ids=("EFO:1",),
            vectors=np.array([[1.0, 0.0]], dtype=np.float32),
            text_recipe="r",
            text_recipe_version="1",
        )
        with self.assertRaises(EmbeddingStoreError):
            nearest_neighbours((1.0, 0.0, 0.0), store)

    def test_on_the_fly_query_is_l2_normalised_before_the_dot_product(self) -> None:
        # The store row is unit length, so a raw dot product is only a cosine
        # when the query is normalised too. An unnormalised (0.3, 0) query is
        # a perfect cosine match for (1, 0) and must survive min_score.
        store = EmbeddingStore(
            model_id="m",
            ontology_release="r",
            ids=("EFO:1",),
            vectors=np.array([[1.0, 0.0]], dtype=np.float32),
            text_recipe="r",
            text_recipe_version="1",
        )
        embedder = DictionaryEmbedder(model_id="m", vectors={"q": (0.3, 0.0)})
        retriever = SemanticRetriever(
            store=store, embedder=embedder, top_k=1, min_score=0.4
        )
        ranked = retriever.rank("q")
        self.assertEqual([ontology_id for ontology_id, _ in ranked], ["EFO:1"])
        self.assertAlmostEqual(ranked[0][1], 1.0)


class TestHashingEmbedder(unittest.TestCase):
    """The offline stub is deterministic and normalised, and is not semantic."""

    def test_same_text_same_vector(self) -> None:
        embedder = HashingEmbedder(dimension=64)
        self.assertEqual(
            embedder.embed(["body mass index"]), embedder.embed(["body mass index"])
        )

    def test_vectors_are_unit_norm(self) -> None:
        vector = HashingEmbedder(dimension=64).embed(["body mass index"])[0]
        norm = sum(component * component for component in vector)
        self.assertAlmostEqual(norm, 1.0, places=9)

    def test_different_text_different_vector(self) -> None:
        embedder = HashingEmbedder(dimension=64)
        self.assertNotEqual(embedder.embed(["height"])[0], embedder.embed(["glucose"])[0])

    def test_empty_text_is_zero_vector(self) -> None:
        vector = HashingEmbedder(dimension=8).embed([""])[0]
        self.assertEqual(len(vector), 8)
        self.assertTrue(all(component == 0.0 for component in vector))

    def test_stub_is_not_described_as_semantic(self) -> None:
        doc = (HashingEmbedder.__doc__ or "").lower().replace("*", "")
        self.assertIn("stub", doc)
        self.assertIn("not a semantic model", doc)


class TestDictionaryEmbedder(unittest.TestCase):
    """The explicit-dictionary embedder replays dense vectors offline."""

    def test_default_model_id(self) -> None:
        self.assertEqual(DictionaryEmbedder().model_id, DICTIONARY_EMBEDDING_MODEL_ID)

    def test_registered_text_returns_its_vector(self) -> None:
        embedder = DictionaryEmbedder(
            model_id="m", vectors={"a": (1.0, 2.0), "b": (3.0, 4.0)}
        )
        self.assertEqual(embedder.embed(["a", "b"]), [(1.0, 2.0), (3.0, 4.0)])

    def test_unregistered_text_is_zero_vector(self) -> None:
        embedder = DictionaryEmbedder(model_id="m", vectors={"a": (1.0, 0.0)})
        self.assertEqual(embedder.embed(["missing"]), [(0.0, 0.0)])


class TestBuildEmbeddingStore(unittest.TestCase):
    """The build embeds every term's full searchable text and records its pins."""

    def setUp(self) -> None:
        self.ontology_index = fixture_ontology_index()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.chunks = Path(self.temp_dir.name) / "chunks"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_embeds_label_synonyms_and_definition(self) -> None:
        recorder = RecordingEmbedder()
        build_ontology_embedding_store(self.ontology_index, recorder, self.chunks)
        joined = "\n".join(recorder.seen)
        self.assertIn("Body mass index", joined)
        self.assertIn("BMI", joined)
        self.assertIn("A measurement of body mass index.", joined)
        self.assertEqual(len(recorder.seen), len(self.ontology_index))

    def test_records_model_release_and_recipe(self) -> None:
        built = build_ontology_embedding_store(
            self.ontology_index, RecordingEmbedder(), self.chunks
        )
        self.assertEqual(built.model_id, FIXTURE_MODEL_ID)
        self.assertEqual(built.ontology_release, PINNED_ONTOLOGY_RELEASE)
        self.assertEqual(built.text_recipe, ONTOLOGY_TEXT_RECIPE)
        self.assertEqual(built.text_recipe_version, ONTOLOGY_TEXT_RECIPE_VERSION)
        self.assertTrue(built.built_at)
        self.assertEqual(built.dimension, 2)
        self.assertEqual(built.count, len(self.ontology_index))
        # Vectors are stored L2-normalised.
        norms = np.linalg.norm(built.vectors, axis=1)
        np.testing.assert_allclose(norms, np.ones_like(norms), atol=1e-6)

    def test_trait_store_is_keyed_by_normalised_label(self) -> None:
        built = build_trait_embedding_store(
            ["Body mass index", "  body-mass index ", "BMI"],
            RecordingEmbedder(),
            PINNED_ONTOLOGY_RELEASE,
            self.chunks,
        )
        self.assertEqual(built.ids, ("bmi", "body mass index"))
        self.assertEqual(built.text_recipe, TRAIT_TEXT_RECIPE)

    def test_records_served_model_and_round_trips_it(self) -> None:
        embedder = HttpEmbedder(
            "https://example.invalid/v1/embeddings",
            model_id=FIXTURE_MODEL_ID,
            client_factory=RecordingServer().client_factory,
            served_model="/models/local-biolord",
        )
        built = build_ontology_embedding_store(
            self.ontology_index, embedder, self.chunks
        )
        self.assertEqual(built.served_model, "/models/local-biolord")
        directory = Path(self.temp_dir.name) / "served-store"
        built.save(directory)
        self.assertEqual(
            EmbeddingStore.load(directory).served_model, "/models/local-biolord"
        )
        self.assertEqual(
            read_embedding_store_meta(directory).served_model,
            "/models/local-biolord",
        )


class TestEmbeddingStoreArtifact(unittest.TestCase):
    """The store directory round-trips and is content-addressed."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.store = fixture_ontology_store()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_round_trips_with_metadata(self) -> None:
        directory = self.td / "ontology-embeddings"
        self.store.save(directory)
        loaded = EmbeddingStore.load(directory)
        self.assertEqual(loaded.ids, self.store.ids)
        self.assertEqual(loaded.model_id, self.store.model_id)
        self.assertEqual(loaded.ontology_release, self.store.ontology_release)
        self.assertEqual(loaded.text_recipe, self.store.text_recipe)
        self.assertEqual(loaded.build_id, self.store.build_id)
        np.testing.assert_array_equal(loaded.vectors, self.store.vectors)
        self.assertTrue((directory / "vectors.npy").is_file())
        self.assertTrue((directory / "ids.tsv").is_file())
        self.assertTrue((directory / "meta.yaml").is_file())

    def test_build_id_is_content_addressed(self) -> None:
        first = fixture_ontology_store()
        second = fixture_ontology_store()
        self.assertEqual(first.build_id, second.build_id)
        changed_vector = EmbeddingStore(
            model_id=first.model_id,
            ontology_release=first.ontology_release,
            ids=first.ids,
            vectors=np.array(
                [[0.5, 0.5, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32
            ),
            text_recipe=first.text_recipe,
            text_recipe_version=first.text_recipe_version,
        )
        self.assertNotEqual(first.build_id, changed_vector.build_id)
        changed_recipe = EmbeddingStore(
            model_id=first.model_id,
            ontology_release=first.ontology_release,
            ids=first.ids,
            vectors=first.vectors,
            text_recipe="a different recipe",
            text_recipe_version=first.text_recipe_version,
        )
        self.assertNotEqual(first.build_id, changed_recipe.build_id)

    def test_unknown_format_version_is_rejected(self) -> None:
        directory = self.td / "stale"
        self.store.save(directory)
        import yaml

        data = yaml.safe_load((directory / "meta.yaml").read_text(encoding="utf-8"))
        data["format_version"] = 999
        (directory / "meta.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        with self.assertRaises(EmbeddingStoreError):
            EmbeddingStore.load(directory)

    def test_corrupted_dimension_is_rejected(self) -> None:
        directory = self._tampered_meta({"dimension": DIMENSION + 1})
        with self.assertRaises(EmbeddingStoreCorruptError):
            EmbeddingStore.load(directory)

    def test_corrupted_count_is_rejected(self) -> None:
        directory = self._tampered_meta({"count": self.store.count + 1})
        with self.assertRaises(EmbeddingStoreCorruptError):
            EmbeddingStore.load(directory)

    def test_corrupted_build_id_is_rejected(self) -> None:
        directory = self._tampered_meta(
            {"build_id": "blake2b:00000000000000000000000000000000"}
        )
        with self.assertRaises(EmbeddingStoreCorruptError):
            EmbeddingStore.load(directory)

    def test_edited_vector_with_stale_build_id_is_rejected(self) -> None:
        directory = self.td / "edited"
        self.store.save(directory)
        vectors = np.load(directory / "vectors.npy")
        vectors[0, 0] = 0.123
        np.save(directory / "vectors.npy", vectors)
        with self.assertRaises(EmbeddingStoreCorruptError):
            EmbeddingStore.load(directory)

    def test_missing_store_raises(self) -> None:
        with self.assertRaises(EmbeddingStoreError):
            EmbeddingStore.load(self.td / "nope")

    def test_default_paths_are_outside_tracked_tree(self) -> None:
        ontology = default_ontology_embeddings_path(PINNED_ONTOLOGY_RELEASE, FIXTURE_MODEL_ID)
        traits = default_trait_embeddings_path(PINNED_ONTOLOGY_RELEASE, FIXTURE_MODEL_ID)
        self.assertEqual(ontology.parts[:2], (".cache", "curation"))
        self.assertEqual(traits.parts[:2], (".cache", "curation"))
        self.assertIn(FIXTURE_MODEL_ID, ontology.name)
        self.assertTrue(ontology.name.endswith(".ontology-embeddings"))
        self.assertTrue(traits.name.endswith(".trait-embeddings"))

    def _tampered_meta(self, changes: Mapping[str, object]) -> Path:
        import yaml

        directory = self.td / "tampered"
        self.store.save(directory)
        data = yaml.safe_load((directory / "meta.yaml").read_text(encoding="utf-8"))
        data.update(changes)
        (directory / "meta.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        return directory


class TestChunkedResume(unittest.TestCase):
    """An interrupted embedding run resumes and requests only missing chunks."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.index = OntologyIndex(
            "efo/chunk-test",
            tuple(
                OntologyTerm(
                    ontology_id=f"EFO:{number:07d}",
                    label=f"term {number}",
                    definition=f"definition {number}",
                )
                for number in range(6)
            ),
        )
        self.texts = [term_embedding_text(term) for term in self.index]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resume_requests_only_the_missing_chunks(self) -> None:
        chunks = self.td / "chunks"
        first = RecordingServer(fail_on_call=2)
        first_embedder = HttpEmbedder(
            "https://example.invalid/v1/embeddings",
            model_id=FIXTURE_MODEL_ID,
            client_factory=first.client_factory,
            batch_size=2,
            max_retries=0,
        )
        with self.assertRaises(EmbeddingError):
            build_ontology_embedding_store(
                self.index, first_embedder, chunks, chunk_size=2
            )
        # The first chunk is durable; the second was never written.
        self.assertTrue((chunks / "00000.npz").is_file())
        self.assertFalse((chunks / "00001.npz").is_file())

        second = RecordingServer()
        second_embedder = HttpEmbedder(
            "https://example.invalid/v1/embeddings",
            model_id=FIXTURE_MODEL_ID,
            client_factory=second.client_factory,
            batch_size=2,
            max_retries=0,
        )
        store = build_ontology_embedding_store(
            self.index, second_embedder, chunks, chunk_size=2
        )
        self.assertEqual(store.count, 6)
        # Only chunks 1 and 2 were requested on the resume.
        self.assertEqual(
            second.requests,
            [self.texts[2:4], self.texts[4:6]],
        )

    def test_corrupt_chunk_is_re_embedded(self) -> None:
        chunks = self.td / "chunks"
        embedder = HttpEmbedder(
            "https://example.invalid/v1/embeddings",
            model_id=FIXTURE_MODEL_ID,
            client_factory=RecordingServer().client_factory,
            batch_size=2,
            max_retries=0,
        )
        build_ontology_embedding_store(self.index, embedder, chunks, chunk_size=2)
        chunk = chunks / "00000.npz"
        data = np.load(chunk, allow_pickle=False)
        np.savez(chunk, ids=data["ids"], vectors=np.zeros_like(data["vectors"]),
                 model_id=np.array("other-model"), text_recipe=data["text_recipe"],
                 text_recipe_version=data["text_recipe_version"])
        resumed = RecordingServer()
        resumed_embedder = HttpEmbedder(
            "https://example.invalid/v1/embeddings",
            model_id=FIXTURE_MODEL_ID,
            client_factory=resumed.client_factory,
            batch_size=2,
            max_retries=0,
        )
        build_ontology_embedding_store(self.index, resumed_embedder, chunks, chunk_size=2)
        self.assertEqual(resumed.requests[0], self.texts[0:2])

    def test_truncated_chunk_is_re_embedded(self) -> None:
        chunks = self.td / "chunks"
        embedder = HttpEmbedder(
            "https://example.invalid/v1/embeddings",
            model_id=FIXTURE_MODEL_ID,
            client_factory=RecordingServer().client_factory,
            batch_size=2,
            max_retries=0,
        )
        build_ontology_embedding_store(self.index, embedder, chunks, chunk_size=2)
        chunk = chunks / "00000.npz"
        payload = chunk.read_bytes()
        chunk.write_bytes(payload[: max(1, len(payload) // 2)])

        resumed = RecordingServer()
        resumed_embedder = HttpEmbedder(
            "https://example.invalid/v1/embeddings",
            model_id=FIXTURE_MODEL_ID,
            client_factory=resumed.client_factory,
            batch_size=2,
            max_retries=0,
        )
        build_ontology_embedding_store(
            self.index, resumed_embedder, chunks, chunk_size=2
        )
        # The truncated first chunk was treated as missing and re-requested.
        self.assertEqual(resumed.requests[0], self.texts[0:2])


class TestHttpEmbedderRetries(unittest.TestCase):
    """429/5xx/transport errors are retried with exponential backoff; 4xx is not."""

    def _embedder(self, server: FlakyServer, **kwargs: object) -> HttpEmbedder:
        return HttpEmbedder(
            "https://example.invalid/v1/embeddings",
            model_id=FIXTURE_MODEL_ID,
            client_factory=server.client_factory,
            **kwargs,
        )

    def test_429_and_5xx_are_retried_with_backoff(self) -> None:
        server = FlakyServer(["503", "429", "ok"])
        sleeps: list[float] = []
        embedder = self._embedder(
            server, max_retries=5, backoff_base=0.5, sleep=sleeps.append
        )
        vectors = embedder.embed(["a", "b"])
        self.assertEqual(server.calls, 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertEqual(len(vectors), 2)

    @unittest.skipUnless(
        importlib.util.find_spec("httpx"), "httpx ships in the curation environment"
    )
    def test_transport_error_is_retried(self) -> None:
        server = FlakyServer(["transport", "ok"])
        sleeps: list[float] = []
        embedder = self._embedder(
            server, max_retries=3, backoff_base=0.5, sleep=sleeps.append
        )
        embedder.embed(["a"])
        self.assertEqual(server.calls, 2)
        self.assertEqual(sleeps, [0.5])

    def test_400_is_not_retried(self) -> None:
        server = FlakyServer(["400"])
        sleeps: list[float] = []
        embedder = self._embedder(
            server, max_retries=5, backoff_base=0.5, sleep=sleeps.append
        )
        with self.assertRaises(EmbeddingUnavailableError):
            embedder.embed(["a"])
        self.assertEqual(server.calls, 1)
        self.assertEqual(sleeps, [])

    def test_retries_are_bounded(self) -> None:
        server = FlakyServer(["503", "503", "503"])
        sleeps: list[float] = []
        embedder = self._embedder(
            server, max_retries=1, backoff_base=0.5, sleep=sleeps.append
        )
        with self.assertRaises(EmbeddingUnavailableError):
            embedder.embed(["a"])
        self.assertEqual(server.calls, 2)
        self.assertEqual(sleeps, [0.5])

    def test_matching_model_is_accepted(self) -> None:
        server = FlakyServer(["ok"])
        self.assertEqual(
            self._embedder(server).embed(["hello"]), [(5.0, 1.0)]
        )

    def test_returned_model_mismatch_is_rejected(self) -> None:
        server = FlakyServer(["ok"], model_id="some-other-model")
        with self.assertRaises(EmbeddingUnavailableError) as ctx:
            self._embedder(server).embed(["hello"])
        message = str(ctx.exception)
        self.assertIn("some-other-model", message)
        self.assertIn(FIXTURE_MODEL_ID, message)
        self.assertIn("--served-model", message)

    def test_served_model_alias_is_accepted_when_named(self) -> None:
        server = FlakyServer(["ok"], model_id="/models/local-biolord")
        embedder = self._embedder(server, served_model="/models/local-biolord")
        self.assertEqual(embedder.embed(["hello"]), [(5.0, 1.0)])
        self.assertEqual(embedder.served_model, "/models/local-biolord")

    def test_served_model_does_not_accept_another_name(self) -> None:
        server = FlakyServer(["ok"], model_id="unexpected")
        embedder = self._embedder(server, served_model="/models/local-biolord")
        with self.assertRaises(EmbeddingUnavailableError):
            embedder.embed(["hello"])


# ---------------------------------------------------------------------------
# The channel
# ---------------------------------------------------------------------------


class TestEmbeddingChannel(unittest.TestCase):
    """The semantic channel reaches what the lexical channels cannot."""

    def setUp(self) -> None:
        self.ontology_index = fixture_ontology_index()
        self.retriever = fixture_retriever()

    def test_retrieves_term_with_no_lexical_overlap(self) -> None:
        self.assertEqual(generate_shortlist(SEMANTIC_QUERY, self.ontology_index), [])
        semantic = generate_shortlist(
            SEMANTIC_QUERY, self.ontology_index, embedding=self.retriever
        )
        self.assertEqual(
            [candidate.ontology_id for candidate in semantic], ["EFO:0000999"]
        )

    def test_embedding_channel_is_attributed_and_ranked(self) -> None:
        candidate = generate_shortlist(
            SEMANTIC_QUERY, self.ontology_index, embedding=self.retriever
        )[0]
        self.assertEqual(candidate.channels, (CHANNEL_EMBEDDING,))
        self.assertEqual(dict(candidate.channel_ranks)[CHANNEL_EMBEDDING], 1)

    def test_candidate_records_model_and_store_build(self) -> None:
        candidate = generate_shortlist(
            SEMANTIC_QUERY, self.ontology_index, embedding=self.retriever
        )[0]
        self.assertEqual(candidate.embedding_model, FIXTURE_MODEL_ID)
        self.assertEqual(candidate.embedding_index_build, self.retriever.build_id)

    def test_run_channels_always_has_the_embedding_key(self) -> None:
        channels = run_channels("Body mass index", self.ontology_index)
        self.assertIn(CHANNEL_EMBEDDING, channels)
        self.assertEqual(channels[CHANNEL_EMBEDDING], [])

    def test_disabled_channel_returns_nothing(self) -> None:
        self.assertEqual(embedding_channel(SEMANTIC_QUERY, None), [])

    def test_no_lexical_overlap_for_semantic_target(self) -> None:
        channels = run_channels(SEMANTIC_QUERY, self.ontology_index)
        for channel in (
            CHANNEL_EXACT,
            CHANNEL_NORMALISED,
            CHANNEL_TOKEN_OVERLAP,
            CHANNEL_SYNONYM,
        ):
            self.assertEqual(channels[channel], [])

    def test_broken_embedder_degrades_to_nothing(self) -> None:
        retriever = SemanticRetriever(
            store=fixture_ontology_store(), embedder=BrokenEmbedder(), top_k=5
        )
        self.assertEqual(embedding_channel(SEMANTIC_QUERY, retriever), [])

    def test_precomputed_trait_store_needs_no_embedder(self) -> None:
        retriever = SemanticRetriever(
            store=fixture_ontology_store(), trait_store=fixture_trait_store(), top_k=5
        )
        ranked = retriever.rank(SEMANTIC_QUERY)
        self.assertEqual(ranked[0][0], "EFO:0000999")

    def test_missing_trait_vector_raises_query_unavailable(self) -> None:
        retriever = SemanticRetriever(
            store=fixture_ontology_store(), trait_store=fixture_trait_store(), top_k=5
        )
        with self.assertRaises(EmbeddingQueryUnavailable):
            retriever.rank("a label with no precomputed vector")

    def test_falls_back_to_embedder_when_trait_vector_missing(self) -> None:
        embedder = DictionaryEmbedder(
            model_id=FIXTURE_MODEL_ID, vectors={"fallback label": (1.0, 0.0, 0.0)}
        )
        retriever = SemanticRetriever(
            store=fixture_ontology_store(),
            trait_store=fixture_trait_store(),
            embedder=embedder,
            top_k=5,
        )
        self.assertEqual(retriever.rank("fallback label")[0][0], "EFO:0004340")


class TestLexicalOnlyUnchanged(unittest.TestCase):
    """Without a retriever the shortlist is exactly the lexical one."""

    def setUp(self) -> None:
        self.ontology_index = fixture_ontology_index()

    def test_disabled_shortlist_has_no_embedding_pin(self) -> None:
        rows = generate_shortlist("Body mass index", self.ontology_index)
        self.assertTrue(rows)
        top = rows[0]
        self.assertEqual(top.ontology_id, "EFO:0004340")
        self.assertNotIn(CHANNEL_EMBEDDING, top.channels)
        self.assertEqual(top.embedding_model, "")
        self.assertEqual(top.embedding_index_build, "")

    def test_tsv_columns_carry_the_pins(self) -> None:
        candidate = generate_shortlist(
            SEMANTIC_QUERY, self.ontology_index, embedding=fixture_retriever()
        )[0]
        header, rows = parse_tsv(format_shortlist_tsv([candidate]))
        self.assertEqual(header, list(candidates.SHORTLIST_COLUMNS))
        self.assertEqual(rows[0]["embedding_model"], FIXTURE_MODEL_ID)
        self.assertEqual(
            rows[0]["embedding_index_build"], candidate.embedding_index_build
        )


class TestProvenanceAndCircuitBreaker(unittest.TestCase):
    """Degraded channels claim no provenance and stop after an endpoint failure."""

    def setUp(self) -> None:
        self.ontology_index = fixture_ontology_index()

    def test_degraded_rows_do_not_claim_semantic_provenance(self) -> None:
        retriever = SemanticRetriever(
            store=fixture_ontology_store(), embedder=BrokenEmbedder(), top_k=5
        )
        rows = generate_shortlist(
            "Body mass index", self.ontology_index, embedding=retriever
        )
        self.assertTrue(rows)
        self.assertTrue(all(row.embedding_model == "" for row in rows))
        self.assertTrue(all(row.embedding_index_build == "" for row in rows))
        self.assertFalse(any(CHANNEL_EMBEDDING in row.channels for row in rows))

    def test_healthy_channel_records_provenance_even_without_neighbours(self) -> None:
        retriever = SemanticRetriever(
            store=fixture_ontology_store(),
            embedder=DictionaryEmbedder(
                model_id=FIXTURE_MODEL_ID, default=(0.0, 0.0, 0.0)
            ),
            top_k=5,
        )
        rows = generate_shortlist(
            "Body mass index", self.ontology_index, embedding=retriever
        )
        self.assertTrue(rows)
        self.assertTrue(all(row.embedding_model == FIXTURE_MODEL_ID for row in rows))
        self.assertTrue(all(row.embedding_index_build for row in rows))

    def test_circuit_breaker_disables_later_labels(self) -> None:
        embedder = CountingFailingEmbedder()
        channel = EmbeddingChannel(
            SemanticRetriever(
                store=fixture_ontology_store(), embedder=embedder, top_k=5
            )
        )
        rows = generate_shortlists(
            [
                "Body mass index",
                "systolic blood pressure",
                "cardio-metabolic phenotype",
            ],
            self.ontology_index,
            embedding=channel,
        )
        self.assertTrue(channel.tripped)
        self.assertFalse(channel.available)
        self.assertIn("endpoint down", channel.failure)
        self.assertEqual(embedder.calls, 1)
        self.assertTrue(rows)
        self.assertTrue(all(row.embedding_model == "" for row in rows))

    def test_missing_trait_vectors_are_counted_not_tripped(self) -> None:
        channel = EmbeddingChannel(
            SemanticRetriever(
                store=fixture_ontology_store(), trait_store=fixture_trait_store(), top_k=5
            )
        )
        generate_shortlists(
            [SEMANTIC_QUERY, "Body mass index", "systolic blood pressure"],
            self.ontology_index,
            embedding=channel,
        )
        self.assertFalse(channel.tripped)
        self.assertEqual(channel.query_vector_misses, 2)


class TestRecallDeltaWithEmbedding(unittest.TestCase):
    """Recall scored with the channel reports a real incremental delta."""

    def test_semantic_channel_improves_recall(self) -> None:
        ontology_index = fixture_ontology_index()
        pairs = [
            ValidationPair(
                trait_label=SEMANTIC_QUERY,
                ontology_id="EFO:0000999",
                ontology_label="cardio-metabolic phenotype",
                stratum=STRATUM_ANALYTE_MEASUREMENT,
                store_families=("metabolome",),
                is_obsolete=False,
            ),
            ValidationPair(
                trait_label="Body mass index",
                ontology_id="EFO:0004340",
                ontology_label="Body mass index",
                stratum=STRATUM_ANALYTE_MEASUREMENT,
                store_families=("ukb-b",),
                is_obsolete=False,
            ),
        ]
        sizes = (1, 5)
        baseline = evaluate_recall(pairs, index=ontology_index, sizes=sizes)
        semantic = evaluate_recall(
            pairs, index=ontology_index, sizes=sizes, embedding=fixture_retriever()
        )
        self.assertEqual(baseline.aggregate_hits[5], 1)
        self.assertEqual(semantic.aggregate_hits[5], 2)
        delta = compare_recall(baseline, semantic)
        self.assertAlmostEqual(delta.delta_for(STRATUM_ANALYTE_MEASUREMENT, 5), 0.5)
        self.assertAlmostEqual(delta.aggregate_delta(5), 0.5)


class TestResolveRetriever(unittest.TestCase):
    """Resolution degrades loudly enough to report and softly enough to run."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _save(self, store: EmbeddingStore, name: str) -> Path:
        directory = self.td / name
        store.save(directory)
        return directory

    def test_pinned_model_without_endpoint_is_unavailable(self) -> None:
        with self.assertRaises(EmbeddingUnavailableError):
            embedder_for_model(PINNED_EMBEDDING_MODEL_ID, endpoint=None)

    def test_hashing_model_needs_no_endpoint(self) -> None:
        self.assertEqual(
            embedder_for_model(HASHING_EMBEDDING_MODEL_ID).model_id,
            HASHING_EMBEDDING_MODEL_ID,
        )

    def test_missing_store_raises(self) -> None:
        with self.assertRaises(EmbeddingStoreError):
            resolve_retriever(self.td / "nope", PINNED_ONTOLOGY_RELEASE)

    def test_release_mismatch_raises(self) -> None:
        directory = self._save(fixture_ontology_store("efo/v1"), "store")
        with self.assertRaises(EmbeddingUnavailableError):
            resolve_retriever(directory, "efo/v2")

    def test_reuses_a_trait_store_with_no_endpoint(self) -> None:
        ontology = self._save(fixture_ontology_store(), "ontology")
        # A different model id on the trait store is refused.
        bad = EmbeddingStore(
            model_id="some-other-model",
            ontology_release=PINNED_ONTOLOGY_RELEASE,
            ids=fixture_trait_store().ids,
            vectors=fixture_trait_store().vectors,
            text_recipe=TRAIT_TEXT_RECIPE,
            text_recipe_version=TRAIT_TEXT_RECIPE_VERSION,
        )
        with self.assertRaises(EmbeddingUnavailableError):
            resolve_retriever(ontology, PINNED_ONTOLOGY_RELEASE, trait_embeddings=self._save(bad, "bad"))

        traits = self._save(fixture_trait_store(), "traits")
        retriever = resolve_retriever(
            ontology, PINNED_ONTOLOGY_RELEASE, trait_embeddings=traits
        )
        self.assertIsNotNone(retriever.trait_store)
        self.assertEqual(retriever.rank(SEMANTIC_QUERY)[0][0], "EFO:0000999")

    def test_release_mismatch_between_stores_is_refused(self) -> None:
        ontology = self._save(fixture_ontology_store(), "ontology")
        bad = self._save(fixture_trait_store("efo/other"), "bad-release")
        with self.assertRaises(EmbeddingUnavailableError):
            resolve_retriever(
                ontology, PINNED_ONTOLOGY_RELEASE, trait_embeddings=bad
            )

    def test_dimension_mismatch_between_stores_is_refused(self) -> None:
        ontology = self._save(fixture_ontology_store(), "ontology")
        bad = EmbeddingStore(
            model_id=FIXTURE_MODEL_ID,
            ontology_release=PINNED_ONTOLOGY_RELEASE,
            ids=(normalise_label(SEMANTIC_QUERY),),
            vectors=np.array([[1.0, 0.0]], dtype=np.float32),
            text_recipe=TRAIT_TEXT_RECIPE,
            text_recipe_version=TRAIT_TEXT_RECIPE_VERSION,
        )
        with self.assertRaises(EmbeddingUnavailableError):
            resolve_retriever(
                ontology,
                PINNED_ONTOLOGY_RELEASE,
                trait_embeddings=self._save(bad, "bad-dimension"),
            )

    def test_no_trait_store_and_no_endpoint_is_unavailable(self) -> None:
        ontology = self._save(fixture_ontology_store(), "ontology")
        with self.assertRaises(EmbeddingUnavailableError):
            resolve_retriever(ontology, PINNED_ONTOLOGY_RELEASE)

    def test_resolves_matching_store_and_embedder(self) -> None:
        index = fixture_ontology_index()
        chunks = self.td / "chunks"
        built = build_ontology_embedding_store(
            index, HashingEmbedder(model_id=HASHING_EMBEDDING_MODEL_ID), chunks
        )
        directory = self._save(built, "ontology")
        retriever = resolve_retriever(
            directory, PINNED_ONTOLOGY_RELEASE, model_id=HASHING_EMBEDDING_MODEL_ID
        )
        self.assertEqual(retriever.model_id, HASHING_EMBEDDING_MODEL_ID)
        self.assertEqual(retriever.build_id, built.build_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCliEmbedOntology(unittest.TestCase):
    """``embed-ontology`` writes a resumable store from the ontology index."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.index_path = self.td / "index.json"
        write_index(fixture_ontology_index(), self.index_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_writes_store_offline(self) -> None:
        output = self.td / "ontology-embeddings"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = embedding.main(
                [
                    "embed-ontology",
                    "--ontology-index", str(self.index_path),
                    "--output", str(output),
                    "--model", HASHING_EMBEDDING_MODEL_ID,
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        store = EmbeddingStore.load(output)
        self.assertEqual(store.model_id, HASHING_EMBEDDING_MODEL_ID)
        self.assertEqual(store.ontology_release, PINNED_ONTOLOGY_RELEASE)
        self.assertEqual(store.count, 3)
        self.assertTrue((output / "chunks").is_dir())

    def test_missing_ontology_index_exits_one(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = embedding.main(
                [
                    "embed-ontology",
                    "--ontology-index", str(self.td / "absent.json"),
                    "--model", HASHING_EMBEDDING_MODEL_ID,
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("embed-ontology: error:", stderr.getvalue())


class TestCliEmbedTraits(unittest.TestCase):
    """``embed-traits`` reuses the ontology store's model and keys by label."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.index = fixture_ontology_index()
        self.model_of = self.td / "ontology-embeddings"
        build_ontology_embedding_store(
            self.index,
            HashingEmbedder(model_id=HASHING_EMBEDDING_MODEL_ID),
            self.model_of / "chunks",
        ).save(self.model_of)
        self.queue = self.td / "queue.tsv"
        self.queue.write_text(
            "trait_label\toccurrence_count\tstore_families\n"
            f"{SEMANTIC_QUERY}\t1\tukb-b\n"
            "Body mass index\t2\tukb-b\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_embeds_trait_labels_offline(self) -> None:
        output = self.td / "trait-embeddings"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = embedding.main(
                [
                    "embed-traits",
                    "--work-queue", str(self.queue),
                    "--output", str(output),
                    "--model-of", str(self.model_of),
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        store = EmbeddingStore.load(output)
        self.assertEqual(store.model_id, HASHING_EMBEDDING_MODEL_ID)
        self.assertEqual(store.ontology_release, PINNED_ONTOLOGY_RELEASE)
        self.assertEqual(
            set(store.ids),
            {normalise_label(SEMANTIC_QUERY), normalise_label("Body mass index")},
        )

    def test_non_offline_model_without_endpoint_exits_one(self) -> None:
        # Rebuild the model-of store for the pinned hosted model.
        model_of = self.td / "hosted"
        source = EmbeddingStore(
            model_id=PINNED_EMBEDDING_MODEL_ID,
            ontology_release=PINNED_ONTOLOGY_RELEASE,
            ids=("EFO:1",),
            vectors=np.array([[1.0, 0.0]], dtype=np.float32),
            text_recipe=ONTOLOGY_TEXT_RECIPE,
            text_recipe_version=ONTOLOGY_TEXT_RECIPE_VERSION,
        )
        source.save(model_of)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = embedding.main(
                [
                    "embed-traits",
                    "--work-queue", str(self.queue),
                    "--output", str(self.td / "out"),
                    "--model-of", str(model_of),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("embed-traits: error:", stderr.getvalue())


class TestCliCandidatesWithPrecomputedVectors(unittest.TestCase):
    """Candidate generation reads precomputed vectors with no network at all."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.index = fixture_ontology_index()
        self.index_path = self.td / "index.json"
        write_index(self.index, self.index_path)
        self.ontology_store = self.td / "ontology-embeddings"
        fixture_ontology_store().save(self.ontology_store)
        self.trait_store = self.td / "trait-embeddings"
        fixture_trait_store().save(self.trait_store)
        self.queue = self.td / "queue.tsv"
        self.queue.write_text(
            "trait_label\toccurrence_count\tstore_families\n"
            f"{SEMANTIC_QUERY}\t1\tukb-b\n"
            "Body mass index\t2\tukb-b\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = candidates.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_precomputed_vectors_generate_semantic_candidates(self) -> None:
        output = self.td / "out.tsv"
        code, _, err = self.run_cli(
            [
                "--work-queue", str(self.queue),
                "--index", str(self.index_path),
                "--output", str(output),
                "--ontology-embeddings", str(self.ontology_store),
                "--trait-embeddings", str(self.trait_store),
            ]
        )
        self.assertEqual(code, 0, err)
        _, rows = parse_tsv(output.read_text(encoding="utf-8"))
        semantic = [row for row in rows if CHANNEL_EMBEDDING in row["channels"]]
        self.assertEqual([row["ontology_id"] for row in semantic], ["EFO:0000999"])
        self.assertEqual(semantic[0]["embedding_model"], FIXTURE_MODEL_ID)
        self.assertTrue(semantic[0]["embedding_index_build"])
        # The label with no precomputed vector is lexical-only and recorded.
        self.assertIn("no precomputed trait vector", err)

    def test_missing_store_degrades_to_lexical_only(self) -> None:
        output = self.td / "out.tsv"
        code, _, err = self.run_cli(
            [
                "--work-queue", str(self.queue),
                "--index", str(self.index_path),
                "--output", str(output),
                "--ontology-embeddings", str(self.td / "absent"),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("warning: semantic channel disabled", err)
        _, rows = parse_tsv(output.read_text(encoding="utf-8"))
        self.assertTrue(rows)
        self.assertFalse(any(CHANNEL_EMBEDDING in row["channels"] for row in rows))

    def test_disabled_channel_is_absent(self) -> None:
        output = self.td / "out.tsv"
        code, _, err = self.run_cli(
            [
                "--work-queue", str(self.queue),
                "--index", str(self.index_path),
                "--output", str(output),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        _, rows = parse_tsv(output.read_text(encoding="utf-8"))
        self.assertFalse(any(CHANNEL_EMBEDDING in row["channels"] for row in rows))


class DefaultEndpointTest(unittest.TestCase):
    def test_environment_overrides_the_local_server(self) -> None:
        from unittest import mock

        from curation import embedding

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                embedding.default_embedding_endpoint(),
                embedding.DEFAULT_EMBEDDING_ENDPOINT,
            )
        with mock.patch.dict(
            "os.environ", {"OPENGWASDB_EMBEDDING_ENDPOINT": "http://gpu:9000"}
        ):
            self.assertEqual(embedding.default_embedding_endpoint(), "http://gpu:9000")

    def test_a_term_without_any_text_is_embedded_by_its_id(self) -> None:
        self.assertEqual(
            term_embedding_text(OntologyTerm(ontology_id="GO:0023052", label="")),
            "GO:0023052",
        )

    def test_a_base_url_is_completed_to_the_embeddings_route(self) -> None:
        from curation.embedding import embeddings_url

        for endpoint in (
            "http://localhost:8080",
            "http://localhost:8080/",
            "http://localhost:8080/v1",
            "http://localhost:8080/v1/embeddings",
        ):
            self.assertEqual(
                embeddings_url(endpoint), "http://localhost:8080/v1/embeddings"
            )

    def test_batch_size_fits_text_embeddings_inference(self) -> None:
        from curation import embedding

        self.assertLessEqual(embedding.DEFAULT_EMBEDDING_BATCH_SIZE, 32)


if __name__ == "__main__":
    unittest.main()
