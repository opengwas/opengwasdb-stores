#!/usr/bin/env python3
"""Tests for the semantic embedding channel (issue #166).

The user-visible contract under test is that candidate generation can add a
semantic nearest-neighbour channel with no network access, that the channel
contributes candidates and is attributed exactly like the lexical channels,
that it indexes labels *and* synonyms *and* definitions, that its model and
index build are pinned and recorded alongside the ontology release, and that a
disabled, missing, or unusable channel degrades cleanly to lexical-only rather
than failing candidate generation.

The suite is hermetic. The only embedder used for retrieval is a deterministic
in-memory stub with explicit fixture vectors; the only real embedder exercised
is the offline :class:`HashingEmbedder`. Nothing here opens a socket.

Verifies:
- the semantic channel retrieves a term that shares no lexical overlap with the
  query while the lexical channels retrieve nothing;
- ``generate_shortlist`` records the ``embedding`` channel, its rank, the model
  id, and the content-addressed index build on the candidate and on the TSV row;
- the indexed text for a term includes its label, synonyms, and definition;
- an embedding index round-trips, records its model/release/build metadata, and
  rejects an unknown format version;
- ``--enable-embedding`` / ``--embedding-index`` enable the channel and a
  missing or mismatched index prints a warning and leaves the run lexical-only;
- no retriever yields the lexical-only shortlist unchanged.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Sequence

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
    run_channels,
)
from curation.embedding import (
    EMBEDDING_INDEX_FORMAT_VERSION,
    HASHING_EMBEDDING_MODEL_ID,
    PINNED_EMBEDDING_MODEL_ID,
    EmbeddedTerm,
    EmbeddingIndex,
    EmbeddingIndexError,
    EmbeddingUnavailableError,
    HashingEmbedder,
    SemanticRetriever,
    build_embedding_index,
    cosine_similarity,
    default_embedding_index_path,
    embedder_for_model,
    load_embedding_index,
    nearest_neighbours,
    resolve_retriever,
    term_embedding_text,
    write_embedding_index,
)
from curation.ontology import (
    PINNED_ONTOLOGY_RELEASE,
    build_index_from_obo,
    write_index,
)
from curation.recall import (
    ValidationPair,
    compare_recall,
    evaluate_recall,
)
from curation.harvest import STRATUM_ANALYTE_MEASUREMENT

# A tiny fixture ontology. EFO:0004340 is lexically reachable; EFO:0000999 is
# the semantic target whose label, synonyms, and definition share no token with
# the query used below.
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

# The query has no token in common with EFO:0000999's label or definition.
SEMANTIC_QUERY = "quercetin bioavailability"

FIXTURE_MODEL_ID = "fixture-embedder-v1"


class StubEmbedder:
    """A deterministic embedder backed by explicit fixture vectors."""

    def __init__(
        self,
        vectors: Mapping[str, Sequence[float]],
        model_id: str = FIXTURE_MODEL_ID,
        default: Sequence[float] = (0.0, 0.0, 0.0),
    ) -> None:
        self.model_id = model_id
        self._vectors = {text: tuple(vector) for text, vector in vectors.items()}
        self._default = tuple(default)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._vectors.get(text, self._default) for text in texts]


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


def fixture_ontology_index(release: str = PINNED_ONTOLOGY_RELEASE):
    import tempfile as _tempfile

    tmp = _tempfile.NamedTemporaryFile("w", suffix=".obo", delete=False, encoding="utf-8")
    try:
        tmp.write(FIXTURE_OBO)
        tmp.close()
        return build_index_from_obo(Path(tmp.name), release)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def fixture_semantic_index(release: str = PINNED_ONTOLOGY_RELEASE) -> EmbeddingIndex:
    """An embedding index whose vectors deliberately encode a semantic match."""
    return EmbeddingIndex(
        model_id=FIXTURE_MODEL_ID,
        ontology_release=release,
        terms=(
            EmbeddedTerm("EFO:0004340", (1.0, 0.0, 0.0)),
            EmbeddedTerm("EFO:0000999", (0.0, 1.0, 0.0)),
            EmbeddedTerm("EFO:0004518", (0.0, 0.0, 1.0)),
        ),
        built_at="2024-01-01T00:00:00Z",
    )


def fixture_retriever() -> SemanticRetriever:
    embedder = StubEmbedder(
        {SEMANTIC_QUERY: (0.0, 1.0, 0.0)}, model_id=FIXTURE_MODEL_ID
    )
    return SemanticRetriever(index=fixture_semantic_index(), embedder=embedder, top_k=5)


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


def parse_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()
    header = lines[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]
    return header, rows


class TestTermEmbeddingText(unittest.TestCase):
    """A term is indexed by label, synonyms, and definition, not label alone."""

    def test_includes_label_synonyms_and_definition(self) -> None:
        term = embedding.OntologyTerm(
            ontology_id="EFO:1",
            label="body mass index",
            definition="a measurement of body mass",
            synonyms=("BMI", "Quetelet index"),
        )
        text = term_embedding_text(term)
        self.assertIn("body mass index", text)
        self.assertIn("BMI", text)
        self.assertIn("Quetelet index", text)
        self.assertIn("a measurement of body mass", text)

    def test_label_only_when_nothing_else(self) -> None:
        term = embedding.OntologyTerm(ontology_id="EFO:1", label="height")
        self.assertEqual(term_embedding_text(term), "height")


class TestCosineAndNeighbours(unittest.TestCase):
    """Similarity and neighbour ranking are deterministic and bounded."""

    def test_cosine_bounds(self) -> None:
        self.assertAlmostEqual(cosine_similarity((1.0, 0.0), (1.0, 0.0)), 1.0)
        self.assertAlmostEqual(cosine_similarity((1.0, 0.0), (0.0, 1.0)), 0.0)
        self.assertEqual(cosine_similarity((0.0, 0.0), (1.0, 0.0)), 0.0)
        self.assertEqual(cosine_similarity((1.0,), (1.0, 0.0)), 0.0)

    def test_nearest_neighbours_rank_and_tie_break(self) -> None:
        index = EmbeddingIndex(
            model_id="m",
            ontology_release="r",
            terms=(
                EmbeddedTerm("EFO:2", (1.0, 0.0)),
                EmbeddedTerm("EFO:1", (1.0, 0.0)),
                EmbeddedTerm("EFO:3", (0.5, 0.5)),
            ),
        )
        ranked = nearest_neighbours((1.0, 0.0), index, top_k=3)
        self.assertEqual([ontology_id for ontology_id, _ in ranked], ["EFO:1", "EFO:2", "EFO:3"])

    def test_min_score_filters(self) -> None:
        index = EmbeddingIndex(
            model_id="m",
            ontology_release="r",
            terms=(EmbeddedTerm("EFO:1", (1.0, 0.0)), EmbeddedTerm("EFO:2", (0.0, 1.0))),
        )
        ranked = nearest_neighbours((1.0, 0.0), index, min_score=0.5)
        self.assertEqual([ontology_id for ontology_id, _ in ranked], ["EFO:1"])

    def test_dimension_mismatch_raises(self) -> None:
        index = EmbeddingIndex(
            model_id="m", ontology_release="r", terms=(EmbeddedTerm("EFO:1", (1.0, 0.0)),)
        )
        with self.assertRaises(EmbeddingIndexError):
            nearest_neighbours((1.0, 0.0, 0.0), index)


class TestHashingEmbedder(unittest.TestCase):
    """The offline embedder is deterministic, normalised, and real."""

    def test_same_text_same_vector(self) -> None:
        embedder = HashingEmbedder(dimension=64)
        self.assertEqual(embedder.embed(["body mass index"]), embedder.embed(["body mass index"]))

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


class TestBuildEmbeddingIndex(unittest.TestCase):
    """The build embeds every term's full searchable text and records its pins."""

    def setUp(self) -> None:
        self.ontology_index = fixture_ontology_index()

    def test_embeds_label_synonyms_and_definition(self) -> None:
        recorder = RecordingEmbedder()
        build_embedding_index(self.ontology_index, recorder)
        joined = "\n".join(recorder.seen)
        self.assertIn("Body mass index", joined)
        self.assertIn("BMI", joined)
        self.assertIn("A measurement of body mass index.", joined)
        self.assertEqual(len(recorder.seen), len(self.ontology_index))

    def test_records_model_and_release(self) -> None:
        built = build_embedding_index(self.ontology_index, RecordingEmbedder())
        self.assertEqual(built.model_id, FIXTURE_MODEL_ID)
        self.assertEqual(built.ontology_release, PINNED_ONTOLOGY_RELEASE)
        self.assertTrue(built.built_at)
        self.assertEqual(built.dimension, 2)
        self.assertEqual(len(built.terms), len(self.ontology_index))

    def test_embedder_returning_wrong_count_raises(self) -> None:
        class OneVectorEmbedder:
            model_id = "bad"

            def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
                return [(1.0,)]

        with self.assertRaises(EmbeddingIndexError):
            build_embedding_index(self.ontology_index, OneVectorEmbedder())


class TestEmbeddingIndexArtifact(unittest.TestCase):
    """The index is versioned, content-addressed, and round-trips."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_round_trips_with_metadata(self) -> None:
        built = build_embedding_index(fixture_ontology_index(), RecordingEmbedder())
        artifact = self.td / "embedding.json"
        write_embedding_index(built, artifact)
        loaded = load_embedding_index(artifact)
        self.assertEqual(loaded, built)
        self.assertEqual(loaded.build_id, built.build_id)
        data = built.to_dict()
        self.assertEqual(data["index_format_version"], EMBEDDING_INDEX_FORMAT_VERSION)
        self.assertEqual(data["embedding_model"], FIXTURE_MODEL_ID)
        self.assertEqual(data["ontology_release"], PINNED_ONTOLOGY_RELEASE)
        self.assertTrue(data["index_build_id"].startswith("blake2b:"))

    def test_build_id_is_content_addressed(self) -> None:
        first = fixture_semantic_index()
        second = fixture_semantic_index()
        self.assertEqual(first.build_id, second.build_id)
        changed = EmbeddingIndex(
            model_id=first.model_id,
            ontology_release=first.ontology_release,
            terms=(
                EmbeddedTerm("EFO:0004340", (0.5, 0.5, 0.0)),
                *first.terms[1:],
            ),
        )
        self.assertNotEqual(first.build_id, changed.build_id)

    def test_unknown_format_version_is_rejected(self) -> None:
        artifact = self.td / "stale.json"
        artifact.write_text(
            '{"index_format_version": 999, "embedding_model": "m", '
            '"ontology_release": "r", "terms": []}',
            encoding="utf-8",
        )
        with self.assertRaises(EmbeddingIndexError):
            load_embedding_index(artifact)

    def test_missing_index_raises(self) -> None:
        with self.assertRaises(EmbeddingIndexError):
            load_embedding_index(self.td / "nope.json")

    def test_inconsistent_dimensions_are_rejected(self) -> None:
        artifact = self.td / "ragged.json"
        artifact.write_text(
            '{"index_format_version": 1, "embedding_model": "m", '
            '"ontology_release": "r", "terms": ['
            '{"ontology_id": "EFO:1", "vector": [1.0, 0.0]}, '
            '{"ontology_id": "EFO:2", "vector": [1.0]}]}',
            encoding="utf-8",
        )
        with self.assertRaises(EmbeddingIndexError):
            load_embedding_index(artifact)

    def test_default_path_is_outside_tracked_tree(self) -> None:
        path = default_embedding_index_path(PINNED_ONTOLOGY_RELEASE, FIXTURE_MODEL_ID)
        self.assertEqual(path.parts[:2], (".cache", "curation"))
        self.assertIn("efo", path.name)
        self.assertIn(FIXTURE_MODEL_ID, path.name)


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
        self.assertEqual([candidate.ontology_id for candidate in semantic], ["EFO:0000999"])

    def test_embedding_channel_is_attributed_and_ranked(self) -> None:
        candidate = generate_shortlist(
            SEMANTIC_QUERY, self.ontology_index, embedding=self.retriever
        )[0]
        self.assertEqual(candidate.channels, (CHANNEL_EMBEDDING,))
        self.assertEqual(dict(candidate.channel_ranks)[CHANNEL_EMBEDDING], 1)

    def test_candidate_records_model_and_index_build(self) -> None:
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
        for channel in (CHANNEL_EXACT, CHANNEL_NORMALISED, CHANNEL_TOKEN_OVERLAP, CHANNEL_SYNONYM):
            self.assertEqual(channels[channel], [])

    def test_broken_embedder_degrades_to_nothing(self) -> None:
        retriever = SemanticRetriever(
            index=fixture_semantic_index(), embedder=BrokenEmbedder(), top_k=5
        )
        self.assertEqual(embedding_channel(SEMANTIC_QUERY, retriever), [])


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
        self.assertEqual(rows[0]["embedding_index_build"], candidate.embedding_index_build)


class TestRetrieverResolution(unittest.TestCase):
    """Resolution degrades loudly enough to report and softly enough to run."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pinned_model_without_endpoint_is_unavailable(self) -> None:
        with self.assertRaises(EmbeddingUnavailableError):
            embedder_for_model(PINNED_EMBEDDING_MODEL_ID, endpoint=None)

    def test_hashing_model_needs_no_endpoint(self) -> None:
        embedder = embedder_for_model(HASHING_EMBEDDING_MODEL_ID)
        self.assertEqual(embedder.model_id, HASHING_EMBEDDING_MODEL_ID)

    def test_missing_index_raises(self) -> None:
        with self.assertRaises(EmbeddingIndexError):
            resolve_retriever(self.td / "nope.json", PINNED_ONTOLOGY_RELEASE)

    def test_release_mismatch_raises(self) -> None:
        built = build_embedding_index(fixture_ontology_index("efo/v1"), RecordingEmbedder())
        artifact = self.td / "embedding.json"
        write_embedding_index(built, artifact)
        with self.assertRaises(EmbeddingUnavailableError):
            resolve_retriever(artifact, "efo/v2")

    def test_model_mismatch_is_rejected(self) -> None:
        index = fixture_semantic_index()
        with self.assertRaises(EmbeddingUnavailableError):
            SemanticRetriever(
                index=index,
                embedder=StubEmbedder({}, model_id="some-other-model"),
            )

    def test_resolves_matching_index_and_embedder(self) -> None:
        built = build_embedding_index(
            fixture_ontology_index(), HashingEmbedder(model_id=HASHING_EMBEDDING_MODEL_ID)
        )
        artifact = self.td / "embedding.json"
        write_embedding_index(built, artifact)
        retriever = resolve_retriever(
            artifact, PINNED_ONTOLOGY_RELEASE, model_id=HASHING_EMBEDDING_MODEL_ID
        )
        self.assertEqual(retriever.model_id, HASHING_EMBEDDING_MODEL_ID)
        self.assertEqual(retriever.build_id, built.build_id)


class TestCli(unittest.TestCase):
    """``--enable-embedding`` / ``--embedding-index`` and graceful degradation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.ontology_index = fixture_ontology_index()
        self.index_path = self.td / "index.json"
        write_index(self.ontology_index, self.index_path)
        self.queue_path = self.td / "queue.tsv"
        self.queue_path.write_text(
            "trait_label\toccurrence_count\tstore_families\n"
            f"{SEMANTIC_QUERY}\t1\tukb-b\n"
            "Body mass index\t2\tukb-b\n",
            encoding="utf-8",
        )
        # A real, offline embedding index: the hashing model indexes the fixture
        # ontology, and the CLI resolves the same embedder for the query.
        self.embedding_path = self.td / "embedding.json"
        write_embedding_index(
            build_embedding_index(
                self.ontology_index, HashingEmbedder(model_id=HASHING_EMBEDDING_MODEL_ID)
            ),
            self.embedding_path,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_candidates_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = candidates.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_enabled_channel_records_pins(self) -> None:
        output = self.td / "out.tsv"
        code, _, err = self.run_candidates_cli(
            [
                "--work-queue", str(self.queue_path),
                "--index", str(self.index_path),
                "--output", str(output),
                "--enable-embedding",
                "--embedding-index", str(self.embedding_path),
            ]
        )
        self.assertEqual(code, 0, err)
        header, rows = parse_tsv(output.read_text(encoding="utf-8"))
        self.assertEqual(header, list(candidates.SHORTLIST_COLUMNS))
        self.assertTrue(rows)
        embedded = [row for row in rows if row["embedding_model"]]
        self.assertTrue(embedded)
        self.assertTrue(
            all(row["embedding_model"] == HASHING_EMBEDDING_MODEL_ID for row in embedded)
        )
        self.assertTrue(all(row["embedding_index_build"] for row in embedded))
        self.assertTrue(any(CHANNEL_EMBEDDING in row["channels"] for row in rows))

    def test_missing_index_degrades_to_lexical_only(self) -> None:
        output = self.td / "out.tsv"
        code, _, err = self.run_candidates_cli(
            [
                "--work-queue", str(self.queue_path),
                "--index", str(self.index_path),
                "--output", str(output),
                "--enable-embedding",
                "--embedding-index", str(self.td / "absent.json"),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("warning: semantic channel disabled", err)
        _, rows = parse_tsv(output.read_text(encoding="utf-8"))
        self.assertTrue(rows)
        self.assertTrue(all(row["embedding_model"] == "" for row in rows))
        self.assertFalse(any(CHANNEL_EMBEDDING in row["channels"] for row in rows))

    def test_disabled_channel_is_absent(self) -> None:
        output = self.td / "out.tsv"
        code, _, err = self.run_candidates_cli(
            [
                "--work-queue", str(self.queue_path),
                "--index", str(self.index_path),
                "--output", str(output),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        _, rows = parse_tsv(output.read_text(encoding="utf-8"))
        self.assertFalse(any(CHANNEL_EMBEDDING in row["channels"] for row in rows))

    def test_build_embedding_index_cli(self) -> None:
        output = self.td / "built.json"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = embedding.main(
                [
                    "--ontology-index", str(self.index_path),
                    "--output", str(output),
                    "--model", HASHING_EMBEDDING_MODEL_ID,
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        built = load_embedding_index(output)
        self.assertEqual(built.model_id, HASHING_EMBEDDING_MODEL_ID)
        self.assertEqual(built.ontology_release, PINNED_ONTOLOGY_RELEASE)
        self.assertEqual(len(built.terms), len(self.ontology_index))

    def test_build_embedding_index_cli_missing_ontology_exits_one(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = embedding.main(
                ["--ontology-index", str(self.td / "absent.json")]
            )
        self.assertEqual(code, 1)
        self.assertIn("embedding-index: error:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
