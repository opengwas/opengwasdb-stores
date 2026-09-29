#!/usr/bin/env python3
"""Hermetic tests for the resumable curation round (curation.round).

The contract under test is that a real round over the ukb-b queue is robust:
every trait is an independent unit whose chooser result is written atomically
to its own file, a rerun skips finished work (or repeats it when the shortlist
changed), failures are recorded and retried, a reduce step reconciles every
queued label into exactly one bucket, promotion never writes an abstention as a
term, and coverage reads the after state from the whole mapping table.

The suite is hermetic: a tiny in-memory OBO/index fixture, an injected chooser,
temporary Reference Resource copies, and no network.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation import choice, coverage, promotion, round as round_mod
from curation.candidates import CHANNEL_EXACT, CHANNEL_NORMALISED
from curation.chooser import NONE_SUITABLE, ChoiceResult, Chooser
from curation.embedding import (
    HASHING_EMBEDDING_MODEL_ID,
    HashingEmbedder,
    HttpEmbedder,
    build_ontology_embedding_store,
    build_trait_embedding_store,
)
from curation.jev_chooser import DEFAULT_JEV_MODEL, HttpJevClient, JevChooser
from curation.ontology import build_index_from_obo, load_index, write_index
from curation.promotion import MAPPING_COLUMNS

RELEASE = "efo/v3.94.0"

FIXTURE_OBO = """\
format-version: 1.2
ontology: efo

[Term]
id: EFO:0004340
name: body mass index
def: "A measurement of body mass index." [PMID:123]
synonym: "BMI" EXACT []

[Term]
id: EFO:0004324
name: body height
def: "A measurement of standing height." []
synonym: "height" EXACT []

[Term]
id: EFO:0004338
name: body weights and measures
def: "Any measurement of body weight." []

[Term]
id: EFO:0004351
name: malignant neoplasm of rectum
def: "A malignant neoplasm of the rectum." []
xref: ICD10CM:C20
"""

BMI_LABEL = "body mass index"
HEIGHT_LABEL = "body height"
MYSTERY_LABEL = "mystery trait"
BMI_ID = "EFO:0004340"
HEIGHT_ID = "EFO:0004324"
OTHER_ID = "EFO:0004338"

RESOURCE_YAML = """\
resource_id: canonical-trait-mapping-efo
label: Canonical trait label to ontology-term mapping table
kind: trait_ontology_mapping
version: {version}
location: resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv
location_kind: tracked_file
description: >
  Round-suite fixture resource.
status: available
"""

ANALYSES_COLUMNS = ["analysis_id", "source_label", "trait_ontology_mapping_method"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_table(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(column, "") for column in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()
    header = lines[0].split("\t") if lines else []
    rows = [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]
    return header, rows


def shortlist_row(
    trait_label: str,
    ontology_id: str,
    ontology_label: str,
    *,
    rank: int = 1,
) -> dict[str, str]:
    from curation.candidates import SHORTLIST_COLUMNS

    row = {column: "" for column in SHORTLIST_COLUMNS}
    row.update(
        {
            "trait_label": trait_label,
            "ontology_release": RELEASE,
            "shortlist_rank": str(rank),
            "ontology_id": ontology_id,
            "ontology_label": ontology_label,
            "definition": f"Definition for {ontology_id}",
            "parent_id": f"PARENT:{ontology_id}",
            "parent_label": f"Parent of {ontology_id}",
            "channels": "exact",
            "channel_ranks": "exact=1",
            "is_obsolete": "false",
        }
    )
    return row


def write_showcase_schema(
    directory: Path | str,
    *,
    question_notes: str = "Waist circumference was measured using a Seca 200 device.",
) -> Path:
    """A minimal Showcase schema fixture: one coded and one question field."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    (root / "schema-1.tsv").write_text(
        "field_id\ttitle\tvalue_type\tmain_category\tencoding_id\tunits\tnotes\n"
        "41202\tDiagnoses - main ICD10\t22\t\t0\t\t"
        "This field summarises the participant's hospital diagnoses.\n"
        "48\tWaist circumference\t31\t\t0\tcm\t"
        + question_notes
        + "\n",
        encoding="utf-8",
    )
    (root / "schema-3.tsv").write_text("category_id\ttitle\n", encoding="utf-8")
    (root / "schema-13.tsv").write_text("parent_id\tchild_id\n", encoding="utf-8")
    return root


class RecordingChooser(Chooser):
    """A deterministic in-process chooser with recorded calls and optional cost."""

    def __init__(
        self,
        choices: dict[str, tuple[str, dict[str, float]]],
        *,
        costs: dict[str, float] | None = None,
    ) -> None:
        self.choices = choices
        self.costs = costs or {}
        self.calls: list[str] = []
        self.chooser_id = "test"
        self.chooser_version = "1"

    def select(
        self,
        trait_label: str,
        candidates: list,
        *,
        trait_context: str = "",
    ) -> ChoiceResult:
        self.calls.append(trait_label)
        selected, probabilities = self.choices[trait_label]
        cost = self.costs.get(trait_label)
        return ChoiceResult(
            selected_ontology_id=selected,
            probabilities=dict(probabilities),
            chooser_id=self.chooser_id,
            chooser_version=self.chooser_version,
            input_tokens=10 if cost is not None else None,
            cost_usd=cost,
        )

    def estimate_cost_usd(self, trait_label: str, candidates: list) -> float | None:
        return self.costs.get(trait_label)


class RoundTestCase(unittest.TestCase):
    """Shared temporary workspace with an index, manifests, and a resource."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

        self.obo = self.base / "efo.obo"
        self.obo.write_text(FIXTURE_OBO, encoding="utf-8")
        self.index = self.base / "efo.index.json"
        write_index(build_index_from_obo(self.obo, RELEASE), self.index)

        self.resource_dir = self.base / "canonical-trait-mapping-efo"
        self.resource_dir.mkdir()
        self.mapping_path = self.resource_dir / "mapping.tsv"
        write_table(self.mapping_path, list(MAPPING_COLUMNS), [])
        (self.resource_dir / "resource.yaml").write_text(
            RESOURCE_YAML.format(version=1), encoding="utf-8"
        )

        self.round_dir = self.base / "round"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def manifest(self, family: str, rows: list[dict[str, str]]) -> Path:
        path = self.base / "families" / family / "releases" / "rel" / "analyses.tsv"
        write_table(path, ANALYSES_COLUMNS, rows)
        return path

    def queue_tsv(self, labels: list[str]) -> Path:
        path = self.base / "explicit-queue.tsv"
        write_table(
            path,
            ["trait_label", "occurrence_count", "store_families"],
            [
                {"trait_label": label, "occurrence_count": "1", "store_families": "fam"}
                for label in labels
            ],
        )
        return path

    def init_round(self, **overrides: object) -> round_mod.RoundConfig:
        kwargs: dict[str, object] = {
            "index_path": self.index,
            "chooser_id": "test",
            "chooser_version": "1",
            "resource_dir": self.resource_dir,
            "shortlist_size": 10,
        }
        kwargs.update(overrides)
        return round_mod.init_round(self.round_dir, **kwargs)

    def write_shortlists(self, rows: list[dict[str, str]]) -> None:
        from curation.candidates import SHORTLIST_COLUMNS

        write_table(self.round_dir / "shortlists.tsv", list(SHORTLIST_COLUMNS), rows)

    def write_choice_result(
        self,
        label: str,
        selected: str,
        probabilities: dict[str, float],
        *,
        fingerprint: str | None = None,
    ) -> None:
        """Write a result YAML with the fingerprint reduce would compute."""
        grouped = choice.read_shortlists(self.round_dir / "shortlists.tsv")
        candidates = list(grouped[label])
        expected = round_mod.choice_fingerprint(
            "test", "1", "", round_mod.DEFAULT_JEV_CONTEXT, label, candidates
        )
        normalised = round_mod.gap_scan.normalize_trait_label(label)
        result_path, _ = round_mod.choice_file_paths(self.round_dir, normalised)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            round_mod._yaml_dump(
                {
                    "trait_label": label,
                    "normalised_label": normalised,
                    "fingerprint": fingerprint or expected,
                    "selected_ontology_id": selected,
                    "probabilities": probabilities,
                    "chooser_id": "test",
                    "chooser_version": "1",
                }
            ),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# round-init
# ---------------------------------------------------------------------------


class RoundInitTest(RoundTestCase):
    def test_writes_pins_and_snapshots_the_mapping_table(self) -> None:
        manifest = self.manifest(
            "fam",
            [
                {
                    "analysis_id": "1",
                    "source_label": BMI_LABEL,
                    "trait_ontology_mapping_method": "unmapped",
                }
            ],
        )
        config = self.init_round(manifests=[manifest])

        self.assertEqual(config.ontology_release, RELEASE)
        self.assertEqual(config.chooser.chooser_id, "test")
        self.assertTrue((self.round_dir / "round.yaml").is_file())
        self.assertTrue(config.mapping_before_path.is_file())
        # The snapshot is header-only when the table starts empty.
        _, rows = parse_tsv(config.mapping_before_path.read_text(encoding="utf-8"))
        self.assertEqual(rows, [])

    def test_rerun_with_same_pins_is_a_noop(self) -> None:
        manifest = self.manifest("fam", [])
        first = self.init_round(manifests=[manifest])
        second = self.init_round(manifests=[manifest])
        self.assertEqual(first.created_at, second.created_at)
        self.assertEqual(first.core_signature(), second.core_signature())

    def test_rerun_with_different_pins_is_refused(self) -> None:
        manifest = self.manifest("fam", [])
        self.init_round(manifests=[manifest])
        other = self.base / "other.index.json"
        write_index(build_index_from_obo(self.obo, "efo/v9.99.0"), other)
        with self.assertRaises(round_mod.RoundStateError):
            self.init_round(manifests=[manifest], index_path=other)

    def test_exactly_one_input_kind_is_required(self) -> None:
        with self.assertRaises(round_mod.RoundConfigError):
            self.init_round()
        with self.assertRaises(round_mod.RoundConfigError):
            self.init_round(
                manifests=[self.manifest("fam", [])],
                queue_tsv=self.queue_tsv(["x"]),
            )


class ShowcasePinTest(RoundTestCase):
    """The Showcase schema directory is a round pin (issue #185)."""

    def test_round_without_a_showcase_dir_keeps_none(self) -> None:
        self.init_round(queue_tsv=self.queue_tsv([BMI_LABEL]))
        self.assertIsNone(
            round_mod.read_round_config(self.round_dir).ukb_showcase_dir
        )

    def test_round_with_a_showcase_dir_round_trips_the_pin(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        showcase = write_showcase_schema(self.base / "showcase")
        config = self.init_round(queue_tsv=queue, ukb_showcase_dir=showcase)
        self.assertEqual(config.ukb_showcase_dir, showcase.resolve())
        reloaded = round_mod.read_round_config(self.round_dir)
        self.assertEqual(reloaded.ukb_showcase_dir, showcase.resolve())
        self.assertIn(
            "ukb_showcase_dir",
            (self.round_dir / "round.yaml").read_text(encoding="utf-8"),
        )
        # The directory is part of the pin's equality, so re-initing with a
        # different directory is refused rather than treated as a no-op.
        other = write_showcase_schema(self.base / "other-showcase")
        with self.assertRaises(round_mod.RoundStateError):
            self.init_round(queue_tsv=queue, ukb_showcase_dir=other)

    def test_fingerprint_covers_trait_context(self) -> None:
        base = round_mod.choice_fingerprint(
            "test",
            "1",
            "",
            round_mod.DEFAULT_JEV_CONTEXT,
            "waist circumference",
            [],
        )
        with_context = round_mod.choice_fingerprint(
            "test",
            "1",
            "",
            round_mod.DEFAULT_JEV_CONTEXT,
            "waist circumference",
            [],
            trait_context="field 48 'Waist circumference'",
        )
        other_context = round_mod.choice_fingerprint(
            "test",
            "1",
            "",
            round_mod.DEFAULT_JEV_CONTEXT,
            "waist circumference",
            [],
            trait_context=(
                "field 48 'Waist circumference'. Different question text."
            ),
        )
        self.assertNotEqual(base, with_context)
        self.assertNotEqual(with_context, other_context)


# ---------------------------------------------------------------------------
# gap-scan
# ---------------------------------------------------------------------------


class GapScanTest(RoundTestCase):
    def test_excludes_already_mapped_labels(self) -> None:
        write_table(
            self.mapping_path,
            list(MAPPING_COLUMNS),
            [{"trait_label": BMI_LABEL}],
        )
        queue = self.queue_tsv([BMI_LABEL, "  MYSTERY TRAIT  "])
        self.init_round(queue_tsv=queue)
        outcome = round_mod.run_gap_scan(self.round_dir)

        _, rows = parse_tsv(outcome.queue_path.read_text(encoding="utf-8"))
        self.assertEqual([row["trait_label"] for row in rows], [MYSTERY_LABEL])
        self.assertEqual(outcome.already_mapped_excluded, 1)


# ---------------------------------------------------------------------------
# candidates / pin refusal
# ---------------------------------------------------------------------------


class CandidatesTest(RoundTestCase):
    def test_pin_mismatch_refuses_to_run(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)

        # Replace the pinned index with one built for another release.
        write_index(build_index_from_obo(self.obo, "efo/v9.99.0"), self.index)
        with self.assertRaises(round_mod.RoundPinError):
            round_mod.run_candidates(self.round_dir)

    def test_shortlists_from_the_pinned_index(self) -> None:
        queue = self.queue_tsv([BMI_LABEL, MYSTERY_LABEL])
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)
        outcome = round_mod.run_candidates(self.round_dir)

        self.assertEqual(outcome.labels, 2)
        self.assertEqual(outcome.no_candidate_labels, 1)
        _, rows = parse_tsv(outcome.shortlists_path.read_text(encoding="utf-8"))
        self.assertEqual({row["trait_label"] for row in rows}, {BMI_LABEL})


class ShowcaseWiringTest(RoundTestCase):
    """Pass B wiring: Showcase pins feed retrieval and the chooser."""

    CODED_LABEL = "diagnoses - main icd10: c20 malignant neoplasm of rectum"
    QUESTION_LABEL = "waist circumference"

    def setUp(self) -> None:
        super().setUp()
        self.schema_dir = write_showcase_schema(self.base / "showcase")

    def test_run_candidates_retrieves_on_the_value_text(self) -> None:
        queue = self.queue_tsv([self.CODED_LABEL])
        self.init_round(queue_tsv=queue, ukb_showcase_dir=self.schema_dir)
        round_mod.run_gap_scan(self.round_dir)
        outcome = round_mod.run_candidates(self.round_dir)

        self.assertEqual(outcome.no_candidate_labels, 0)
        _, rows = parse_tsv(outcome.shortlists_path.read_text(encoding="utf-8"))
        # The candidate keeps the raw queue label as its mapping key…
        self.assertEqual([row["trait_label"] for row in rows], [self.CODED_LABEL])
        self.assertEqual([row["ontology_id"] for row in rows], ["EFO:0004351"])
        channels = set(rows[0]["channels"].split(","))
        # …while the exact/normalised channels matched the value text and the
        # icd10 channel still fired from the raw label's code.
        self.assertIn(CHANNEL_EXACT, channels)
        self.assertIn(CHANNEL_NORMALISED, channels)
        self.assertIn("icd10", channels)

    def test_run_candidates_without_a_resolver_keeps_the_old_retrieval(self) -> None:
        queue = self.queue_tsv([self.CODED_LABEL])
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)
        outcome = round_mod.run_candidates(self.round_dir)

        _, rows = parse_tsv(outcome.shortlists_path.read_text(encoding="utf-8"))
        self.assertEqual(rows[0]["ontology_id"], "EFO:0004351")
        channels = set(rows[0]["channels"].split(","))
        # Without the Showcase pin the label is retrieved on its full text, so
        # only the icd10 (and token-overlap) channels fire.
        self.assertNotIn(CHANNEL_EXACT, channels)
        self.assertNotIn(CHANNEL_NORMALISED, channels)
        self.assertIn("icd10", channels)

    def test_choose_reruns_when_the_trait_context_changes(self) -> None:
        queue = self.queue_tsv([self.QUESTION_LABEL])
        self.init_round(queue_tsv=queue, ukb_showcase_dir=self.schema_dir)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists(
            [shortlist_row(self.QUESTION_LABEL, BMI_ID, "body mass index")]
        )
        chooser = RecordingChooser(
            {self.QUESTION_LABEL: (BMI_ID, {BMI_ID: 1.0})}
        )

        first = round_mod.run_choose(self.round_dir, chooser=chooser, workers=1)
        self.assertEqual((first.chosen, first.skipped), (1, 0))
        self.assertEqual(len(chooser.calls), 1)

        # The same label under a different Showcase question text is a
        # different request: its trait_context differs, so its fingerprint
        # differs and the stored result must not be reused.
        write_showcase_schema(
            self.schema_dir,
            question_notes=(
                "Waist circumference was measured using a Seca 200 device and "
                "a laser device."
            ),
        )
        second = round_mod.run_choose(self.round_dir, chooser=chooser, workers=1)
        self.assertEqual((second.chosen, second.skipped), (1, 0))

        # An unchanged trait_context is still skipped on a later run.
        third = round_mod.run_choose(self.round_dir, chooser=chooser, workers=1)
        self.assertEqual((third.chosen, third.skipped), (0, 1))
        self.assertEqual(len(chooser.calls), 2)


class EmbeddingPinTest(RoundTestCase):
    """A pinned store that contradicts the pin refuses; it never degrades."""

    def _build_stores(self) -> tuple[Path, Path]:
        index = load_index(self.index)
        embedder = HashingEmbedder(model_id=HASHING_EMBEDDING_MODEL_ID)
        ontology = self.base / "onto-store"
        build_ontology_embedding_store(
            index, embedder, ontology / "chunks"
        ).save(ontology)
        trait = self.base / "trait-store"
        build_trait_embedding_store(
            [BMI_LABEL], embedder, RELEASE, trait / "chunks"
        ).save(trait)
        return ontology, trait

    def _prepare_round(self, **init_kwargs: object) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(queue_tsv=queue, **init_kwargs)
        round_mod.run_gap_scan(self.round_dir)

    def test_single_pinned_store_with_mismatched_meta_is_refused(self) -> None:
        ontology, _ = self._build_stores()
        self._prepare_round(ontology_embeddings=ontology)

        # Change the store's model id after it was pinned.
        import yaml as _yaml

        meta_path = ontology / "meta.yaml"
        meta = _yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        meta["model_id"] = "some-other-model"
        meta_path.write_text(_yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")

        with self.assertRaises(round_mod.RoundPinError):
            round_mod.run_candidates(self.round_dir)

    def test_corrupt_vectors_with_matching_meta_are_refused(self) -> None:
        ontology, trait = self._build_stores()
        self._prepare_round(ontology_embeddings=ontology, trait_embeddings=trait)

        # The meta still matches the pin, but the vector payload is truncated.
        vectors_path = ontology / "vectors.npy"
        data = vectors_path.read_bytes()
        vectors_path.write_bytes(data[: max(1, len(data) // 2)])

        with self.assertRaises(round_mod.RoundPinError):
            round_mod.run_candidates(self.round_dir)

    def test_single_pinned_corrupt_vectors_are_refused(self) -> None:
        # Only one of the store pair is pinned, so there is no semantic channel
        # to build -- but the pinned store is still loaded and verified rather
        # than silently degrading the round to lexical-only.
        ontology, _ = self._build_stores()
        self._prepare_round(ontology_embeddings=ontology)

        vectors_path = ontology / "vectors.npy"
        data = vectors_path.read_bytes()
        vectors_path.write_bytes(data[: max(1, len(data) // 2)])

        with self.assertRaises(round_mod.RoundPinError):
            round_mod.run_candidates(self.round_dir)


class _FakeEmbeddingResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeEmbeddingClient:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def __enter__(self) -> "_FakeEmbeddingClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, json: dict, headers: dict) -> _FakeEmbeddingResponse:
        inputs = list(json["input"])
        return _FakeEmbeddingResponse(
            {
                "model": self.model_id,
                "data": [
                    {"index": index, "embedding": [float(len(text)), 1.0]}
                    for index, text in enumerate(inputs)
                ],
            }
        )


class _FakeEmbeddingServer:
    """A fake OpenAI-compatible embeddings endpoint (never a socket)."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def client_factory(self, **kwargs: object) -> _FakeEmbeddingClient:
        return _FakeEmbeddingClient(self.model_id)


class RunbookSequenceTest(RoundTestCase):
    """The documented runbook order works end to end, including embed-traits."""

    def test_runbook_round_trip_with_fake_embedding_client(self) -> None:
        model_id = "fixture-embedding-model"
        server = _FakeEmbeddingServer(model_id)
        embedder = HttpEmbedder(
            endpoint="https://fake.invalid/v1/embeddings",
            model_id=model_id,
            client_factory=server.client_factory,
        )

        # 1. embed the ontology terms once.
        index = load_index(self.index)
        ontology_store = self.base / "ontology-embeddings"
        build_ontology_embedding_store(
            index, embedder, ontology_store / "chunks"
        ).save(ontology_store)

        # 2. pin the round with the ontology store only (no trait store yet).
        queue = self.queue_tsv([BMI_LABEL])
        fixture = self.base / "stub-fixture.json"
        fixture.write_text(
            json.dumps(
                {
                    BMI_LABEL: {
                        "selected_ontology_id": BMI_ID,
                        "probabilities": {BMI_ID: 1.0},
                    }
                }
            ),
            encoding="utf-8",
        )
        self.init_round(
            queue_tsv=queue,
            chooser_id="stub",
            chooser_version="1",
            chooser_fixture=fixture,
            ontology_embeddings=ontology_store,
        )
        before_path = self.round_dir / "mapping-before.tsv"
        before_bytes = before_path.read_bytes()

        # 3. derive the queue.
        round_mod.run_gap_scan(self.round_dir)

        # 4. embed the queue's trait labels and record the pin (no --force).
        embedded = round_mod.run_embed_traits(self.round_dir, embedder=embedder)
        self.assertEqual(embedded.model_id, model_id)
        self.assertEqual(embedded.count, 1)
        config = round_mod.read_round_config(self.round_dir)
        self.assertIsNotNone(config.trait_embeddings)
        assert config.trait_embeddings is not None
        self.assertEqual(config.trait_embeddings.model_id, model_id)
        self.assertEqual(config.trait_embeddings.directory, embedded.store_dir)
        # The mapping snapshot was not rewritten.
        self.assertEqual(before_path.read_bytes(), before_bytes)

        # 5-6. shortlist and choose.
        candidates = round_mod.run_candidates(self.round_dir)
        self.assertTrue(candidates.embedding_used)
        chosen = round_mod.run_choose(self.round_dir, workers=1)
        self.assertEqual(chosen.chosen, 1)

        # 7-9. reduce, promote, cover.
        reduced = round_mod.run_reduce(self.round_dir)
        self.assertTrue(reduced.complete)
        promoted = round_mod.run_promote(self.round_dir)
        self.assertEqual(len(promoted.promotion.plan.promoted), 1)
        covered = round_mod.run_coverage(self.round_dir)
        self.assertEqual(covered.report.rows_added, 1)

    def test_stub_round_cannot_promote_into_the_tracked_table(self) -> None:
        tracked = Path(promotion.DEFAULT_RESOURCE_DIR)
        before = {p.name: p.read_bytes() for p in tracked.iterdir() if p.is_file()}
        self.init_round(
            chooser_id="stub", resource_dir=tracked, queue_tsv=self.queue_tsv([BMI_LABEL])
        )

        with self.assertRaisesRegex(round_mod.RoundStateError, "tracked mapping table"):
            round_mod.run_promote(self.round_dir)

        after = {p.name: p.read_bytes() for p in tracked.iterdir() if p.is_file()}
        self.assertEqual(after, before)


# ---------------------------------------------------------------------------
# choose (map)
# ---------------------------------------------------------------------------


class ChooseTest(RoundTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.queue = self.queue_tsv([BMI_LABEL, HEIGHT_LABEL])
        self.init_round(queue_tsv=self.queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists(
            [
                shortlist_row(BMI_LABEL, BMI_ID, "body mass index"),
                shortlist_row(HEIGHT_LABEL, HEIGHT_ID, "body height"),
            ]
        )
        self.chooser = RecordingChooser(
            {
                BMI_LABEL: (BMI_ID, {BMI_ID: 1.0}),
                HEIGHT_LABEL: (HEIGHT_ID, {HEIGHT_ID: 1.0}),
            }
        )

    def _choice_files(self) -> list[Path]:
        choices = self.round_dir / "choices"
        if not choices.exists():
            return []
        return sorted(choices.rglob("*.yaml"))

    def test_writes_one_file_per_trait_and_skips_identical_fingerprint(self) -> None:
        first = round_mod.run_choose(self.round_dir, chooser=self.chooser, workers=1)
        self.assertEqual(first.chosen, 2)
        self.assertEqual(len(self._choice_files()), 2)

        second = round_mod.run_choose(self.round_dir, chooser=self.chooser, workers=1)
        self.assertEqual(second.skipped, 2)
        self.assertEqual(second.chosen, 0)
        # The chooser was not called again for the unchanged shortlists.
        self.assertEqual(len(self.chooser.calls), 2)

    def test_reruns_when_the_shortlist_changes(self) -> None:
        round_mod.run_choose(self.round_dir, chooser=self.chooser, workers=1)
        self.write_shortlists(
            [
                shortlist_row(BMI_LABEL, BMI_ID, "body mass index"),
                shortlist_row(HEIGHT_LABEL, HEIGHT_ID, "body height"),
                shortlist_row(HEIGHT_LABEL, OTHER_ID, "body weights", rank=2),
            ]
        )
        self.chooser.choices[HEIGHT_LABEL] = (
            HEIGHT_ID,
            {HEIGHT_ID: 0.9, OTHER_ID: 0.1},
        )
        outcome = round_mod.run_choose(self.round_dir, chooser=self.chooser, workers=1)
        # BMI is unchanged and skipped; HEIGHT's shortlist changed and reruns.
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.chosen, 1)

    def test_error_file_then_success_on_rerun(self) -> None:
        class FlakyChooser(RecordingChooser):
            def __init__(self, *args, fail: bool, **kwargs):
                super().__init__(*args, **kwargs)
                self.fail = fail

            def select(self, trait_label, candidates, *, trait_context=""):
                if self.fail and trait_label == BMI_LABEL:
                    raise ValueError("server said no")
                return super().select(
                    trait_label, candidates, trait_context=trait_context
                )

        flaky = FlakyChooser(self.chooser.choices, fail=True)
        outcome = round_mod.run_choose(self.round_dir, chooser=flaky, workers=1)
        self.assertEqual(outcome.failed, 1)
        normalised = round_mod.gap_scan.normalize_trait_label(BMI_LABEL)
        _, error_path = round_mod.choice_file_paths(self.round_dir, normalised)
        self.assertTrue(error_path.is_file())
        error_data = round_mod._read_result(error_path)
        assert error_data is not None
        self.assertEqual(error_data["error_class"], "ValueError")
        self.assertIn("server said no", error_data["message"])

        fixed = FlakyChooser(self.chooser.choices, fail=False)
        outcome = round_mod.run_choose(self.round_dir, chooser=fixed, workers=1)
        self.assertEqual(outcome.failed, 0)
        self.assertFalse(error_path.is_file())

    def test_api_key_never_reaches_the_error_file(self) -> None:
        import os

        os.environ["OPENGWASDB_JEV_API_KEY"] = "super-secret-key"

        class LeakyChooser(RecordingChooser):
            def select(self, trait_label, candidates, *, trait_context=""):
                raise ValueError("bad key super-secret-key rejected")

        try:
            round_mod.run_choose(
                self.round_dir,
                chooser=LeakyChooser(self.chooser.choices),
                workers=1,
            )
        finally:
            del os.environ["OPENGWASDB_JEV_API_KEY"]
        _, error_path = round_mod.choice_file_paths(
            self.round_dir, round_mod.gap_scan.normalize_trait_label(HEIGHT_LABEL)
        )
        self.assertIn("***", error_path.read_text(encoding="utf-8"))
        self.assertNotIn("super-secret-key", error_path.read_text(encoding="utf-8"))

    def test_cli_supplied_api_key_never_reaches_the_error_file(self) -> None:
        class LeakyChooser(RecordingChooser):
            def select(self, trait_label, candidates, *, trait_context=""):
                raise ValueError("bad key cli-secret rejected")

        round_mod.run_choose(
            self.round_dir,
            chooser=LeakyChooser(self.chooser.choices),
            workers=1,
            extra_api_keys=("cli-secret",),
        )
        _, error_path = round_mod.choice_file_paths(
            self.round_dir, round_mod.gap_scan.normalize_trait_label(HEIGHT_LABEL)
        )
        text = error_path.read_text(encoding="utf-8")
        self.assertIn("***", text)
        self.assertNotIn("cli-secret", text)

    def test_error_file_records_the_raw_response_of_a_paid_answer(self) -> None:
        class PaidFailure(ValueError):
            pass

        class PaidChooser(RecordingChooser):
            def select(self, trait_label, candidates, *, trait_context=""):
                exc = PaidFailure("distribution rejected after the call")
                exc.raw_response = {
                    "answers": {"term": {"choice": "EFO:1", "confidence": 0.5}}
                }
                raise exc

        round_mod.run_choose(
            self.round_dir, chooser=PaidChooser(self.chooser.choices), workers=1
        )
        _, error_path = round_mod.choice_file_paths(
            self.round_dir, round_mod.gap_scan.normalize_trait_label(BMI_LABEL)
        )
        error_data = round_mod._read_result(error_path)
        assert error_data is not None
        self.assertEqual(
            error_data["raw_response"]["answers"]["term"]["choice"], "EFO:1"
        )

    def test_paid_rejected_response_records_usage_cost_and_ledger(self) -> None:
        class PaidFailure(ValueError):
            pass

        class PaidChooser(RecordingChooser):
            def select(self, trait_label, candidates, *, trait_context=""):
                if trait_label != BMI_LABEL:
                    return super().select(
                        trait_label, candidates, trait_context=trait_context
                    )
                exc = PaidFailure("distribution rejected after the call")
                exc.raw_response = {"usage": {"input_tokens": 1234}}
                exc.input_tokens = 1234
                exc.cost_usd = 0.07
                raise exc

        outcome = round_mod.run_choose(
            self.round_dir, chooser=PaidChooser(self.chooser.choices), workers=1
        )
        self.assertEqual(outcome.failed, 1)
        self.assertAlmostEqual(outcome.total_cost_usd, 0.07)

        _, error_path = round_mod.choice_file_paths(
            self.round_dir, round_mod.gap_scan.normalize_trait_label(BMI_LABEL)
        )
        error_data = round_mod._read_result(error_path)
        assert error_data is not None
        self.assertEqual(error_data["input_tokens"], 1234)
        self.assertAlmostEqual(error_data["cost_usd"], 0.07)

        # The reduce ledger carries the spend of the failed request.
        round_mod.run_reduce(self.round_dir, allow_incomplete=True)
        _, ledger = parse_tsv((self.round_dir / "cost-ledger.tsv").read_text())
        bmi_row = next(row for row in ledger if row["trait_label"] == BMI_LABEL)
        self.assertEqual(bmi_row["input_tokens"], "1234")
        self.assertAlmostEqual(float(bmi_row["cost_usd"]), 0.07)

        # A resume seeds the cap from the error file's recorded cost.
        resumed = round_mod.run_choose(
            self.round_dir,
            chooser=PaidChooser(self.chooser.choices),
            workers=1,
            max_cost_usd=0.01,
        )
        self.assertTrue(resumed.cost_cap_reached)
        self.assertEqual(resumed.processed, 0)

    def test_max_cost_stops_new_requests(self) -> None:
        chooser = RecordingChooser(
            self.chooser.choices,
            costs={BMI_LABEL: 0.1, HEIGHT_LABEL: 0.1},
        )
        # The first request's $0.10 estimate fits exactly; the next would push
        # the round to $0.20, so it is never submitted.
        outcome = round_mod.run_choose(
            self.round_dir, chooser=chooser, workers=1, max_cost_usd=0.10
        )
        self.assertTrue(outcome.cost_cap_reached)
        self.assertEqual(outcome.processed, 1)
        self.assertEqual(len(chooser.calls), 1)
        self.assertAlmostEqual(outcome.total_cost_usd, 0.10)

    def test_limit_caps_a_pilot(self) -> None:
        outcome = round_mod.run_choose(
            self.round_dir, chooser=self.chooser, workers=1, limit=1
        )
        self.assertTrue(outcome.limit_reached)
        self.assertEqual(outcome.processed, 1)

    def test_limit_counts_new_work_not_already_finished_labels(self) -> None:
        round_mod.run_choose(
            self.round_dir, chooser=self.chooser, workers=1, limit=1
        )
        # BMI is already done; a second pilot must not spend its budget
        # skipping it -- it should choose HEIGHT.
        outcome = round_mod.run_choose(
            self.round_dir, chooser=self.chooser, workers=1, limit=1
        )
        self.assertEqual(outcome.processed, 1)
        self.assertEqual(outcome.chosen, 1)
        self.assertEqual(outcome.skipped, 1)

    def test_interrupt_leaves_only_complete_files(self) -> None:
        class InterruptingChooser(RecordingChooser):
            def select(self, trait_label, candidates, *, trait_context=""):
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            round_mod.run_choose(
                self.round_dir,
                chooser=InterruptingChooser(self.chooser.choices),
                workers=1,
            )
        # No result file and no temp file survives the interrupt.
        self.assertEqual(self._choice_files(), [])
        temp_files = list((self.round_dir / "choices").rglob("*.tmp*")) if (
            self.round_dir / "choices"
        ).exists() else []
        self.assertEqual(temp_files, [])


# ---------------------------------------------------------------------------
# --max-cost-usd caps the whole round
# ---------------------------------------------------------------------------


class CostCapTest(RoundTestCase):
    """The spend cap is cumulative and accounts for in-flight requests."""

    def _build(self, labels: list[str]) -> RecordingChooser:
        queue = self.queue_tsv(labels)
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists(
            [shortlist_row(label, f"EFO:{index + 1}", label) for index, label in enumerate(labels)]
        )
        return RecordingChooser(
            {label: (f"EFO:{index + 1}", {f"EFO:{index + 1}": 1.0}) for index, label in enumerate(labels)},
            costs={label: 0.10 for label in labels},
        )

    def test_cap_accounts_for_the_next_request_before_submitting(self) -> None:
        labels = ["alpha", "beta"]
        chooser = self._build(labels)
        # With a $0.15 cap, the first $0.10 request fits but the second would
        # overshoot to $0.20, so it must not be submitted.
        outcome = round_mod.run_choose(
            self.round_dir, chooser=chooser, workers=1, max_cost_usd=0.15
        )
        self.assertTrue(outcome.cost_cap_reached)
        self.assertEqual(outcome.processed, 1)
        self.assertEqual(len(chooser.calls), 1)
        self.assertAlmostEqual(outcome.total_cost_usd, 0.10)

    def test_concurrent_requests_cannot_overshoot_the_cap(self) -> None:
        labels = ["alpha", "beta", "gamma", "delta"]
        chooser = self._build(labels)
        outcome = round_mod.run_choose(
            self.round_dir, chooser=chooser, workers=4, max_cost_usd=0.15
        )
        self.assertTrue(outcome.cost_cap_reached)
        # Only the first in-flight request may be submitted; the rest are
        # blocked by its reservation.
        self.assertEqual(outcome.processed, 1)
        self.assertEqual(len(chooser.calls), 1)
        self.assertAlmostEqual(outcome.total_cost_usd, 0.10)

    def test_resume_does_not_overshoot_the_cap(self) -> None:
        labels = ["alpha", "beta", "gamma", "delta"]
        chooser = self._build(labels)
        first = round_mod.run_choose(
            self.round_dir, chooser=chooser, workers=1, limit=1
        )
        self.assertEqual(first.processed, 1)
        self.assertAlmostEqual(first.total_cost_usd, 0.10)

        # The remaining labels start from the recorded $0.10, already over the
        # $0.05 cap, so nothing new is submitted.
        resumed = round_mod.run_choose(
            self.round_dir, chooser=chooser, workers=1, max_cost_usd=0.05
        )
        self.assertTrue(resumed.cost_cap_reached)
        self.assertEqual(resumed.processed, 0)
        self.assertEqual(len(chooser.calls), 1)
        self.assertAlmostEqual(resumed.total_cost_usd, 0.10)


# ---------------------------------------------------------------------------
# reduce
# ---------------------------------------------------------------------------


class ReduceTest(RoundTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.queue = self.queue_tsv(
            ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
        )
        self.init_round(queue_tsv=self.queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists(
            [
                shortlist_row("alpha", "EFO:1", "one"),
                shortlist_row("beta", "EFO:2", "two"),
                shortlist_row("gamma", "EFO:3", "three"),
                shortlist_row("delta", "EFO:4", "four"),
                shortlist_row("epsilon", "EFO:5", "five"),
            ]
        )
        self.write_choice_result("alpha", "EFO:1", {"EFO:1": 1.0})
        self.write_choice_result(
            "beta", NONE_SUITABLE, {"EFO:2": 0.0, NONE_SUITABLE: 1.0}
        )
        self.write_choice_result(
            "epsilon", "EFO:5", {"EFO:5": 1.0}, fingerprint="stale"
        )
        # gamma has an error file only; delta has no result.
        _, error_path = round_mod.choice_file_paths(self.round_dir, "gamma")
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(
            round_mod._yaml_dump(
                {"error_class": "JevError", "message": "boom", "attempts": 1}
            ),
            encoding="utf-8",
        )

    def test_reconciles_every_label_into_one_bucket(self) -> None:
        outcome = round_mod.run_reduce(self.round_dir)
        recon = outcome.reconciliation
        self.assertEqual(recon.queue_total, 6)
        self.assertEqual(recon.proposed, 1)
        self.assertEqual(recon.none_suitable, 1)
        self.assertEqual(recon.no_candidate, 1)
        self.assertEqual(recon.error, 1)
        self.assertEqual(recon.pending, 2)
        self.assertEqual(recon.stale, 1)
        self.assertFalse(outcome.complete)

        _, rows = parse_tsv(outcome.reconciliation_path.read_text(encoding="utf-8"))
        values = {row["metric"]: row["value"] for row in rows}
        bucket_sum = sum(
            int(values[bucket])
            for bucket in (
                round_mod.BUCKET_NO_CANDIDATE,
                round_mod.BUCKET_PENDING,
                round_mod.BUCKET_ERROR,
                round_mod.BUCKET_NONE_SUITABLE,
                round_mod.BUCKET_PROPOSED,
            )
        )
        self.assertEqual(bucket_sum, int(values["queue_total"]))

    def test_proposals_include_none_suitable_rows(self) -> None:
        outcome = round_mod.run_reduce(self.round_dir)
        _, rows = parse_tsv(outcome.proposals_path.read_text(encoding="utf-8"))
        selected = {row["trait_label"]: row["selected_ontology_id"] for row in rows}
        self.assertEqual(selected["alpha"], "EFO:1")
        self.assertEqual(selected["beta"], NONE_SUITABLE)

    def test_allow_incomplete_is_a_cli_exit_policy(self) -> None:
        # The library always reports the true completeness; a CLI caller opts
        # into proceeding with --allow-incomplete.
        outcome = round_mod.run_reduce(self.round_dir, allow_incomplete=True)
        self.assertFalse(outcome.complete)

        code = round_mod.main([
            "reduce", "--round-dir", str(self.round_dir), "--allow-incomplete"
        ])
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# promote: none_suitable handling
# ---------------------------------------------------------------------------


class PromoteTest(RoundTestCase):
    def _build_round(self, confidence: float, margin: float) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists([shortlist_row(BMI_LABEL, BMI_ID, "body mass index")])
        from curation.choice import PROPOSAL_COLUMNS

        write_table(
            self.round_dir / "proposals.tsv",
            list(PROPOSAL_COLUMNS),
            [
                {
                    "trait_label": BMI_LABEL,
                    "selected_ontology_id": NONE_SUITABLE,
                    "selected_ontology_label": "",
                    "confidence": f"{confidence:.6f}",
                    "runner_up_id": BMI_ID,
                    "runner_up_label": "body mass index",
                    "runner_up_confidence": f"{1.0 - confidence:.6f}",
                    "runner_up_margin": f"{margin:.6f}",
                    "probabilities": json.dumps(
                        {BMI_ID: 1.0 - confidence, NONE_SUITABLE: confidence}
                    ),
                    "chooser_id": "test",
                    "chooser_version": "1",
                    "ontology_release": RELEASE,
                }
            ],
        )

    def test_confident_abstention_is_recorded_not_promoted(self) -> None:
        self._build_round(0.99, 0.98)
        outcome = round_mod.run_promote(self.round_dir)

        self.assertEqual(outcome.promotion.plan.promoted, ())
        self.assertEqual(outcome.promotion.plan.queued, ())
        self.assertEqual(len(outcome.promotion.plan.no_suitable), 1)
        self.assertTrue(outcome.no_suitable_path.is_file())
        mapping_text = self.mapping_path.read_text(encoding="utf-8")
        self.assertNotIn(BMI_LABEL, mapping_text)

    def test_uncertain_abstention_goes_to_the_review_queue(self) -> None:
        self._build_round(0.50, 0.10)
        outcome = round_mod.run_promote(self.round_dir)

        self.assertEqual(outcome.promotion.plan.promoted, ())
        self.assertEqual(outcome.promotion.plan.no_suitable, ())
        self.assertEqual(len(outcome.promotion.plan.queued), 1)
        _, rows = parse_tsv(outcome.review_queue_path.read_text(encoding="utf-8"))
        self.assertEqual(rows[0]["selected_ontology_id"], NONE_SUITABLE)


# ---------------------------------------------------------------------------
# coverage: after state from the full table
# ---------------------------------------------------------------------------


class CoverageTest(RoundTestCase):
    def test_after_state_uses_pre_existing_rows(self) -> None:
        write_table(
            self.mapping_path,
            list(MAPPING_COLUMNS),
            [{"trait_label": BMI_LABEL}],
        )
        manifest = self.manifest(
            "fam-a",
            [
                {
                    "analysis_id": "1",
                    "source_label": BMI_LABEL,
                    "trait_ontology_mapping_method": "unmapped",
                },
                {
                    "analysis_id": "2",
                    "source_label": HEIGHT_LABEL,
                    "trait_ontology_mapping_method": "unmapped",
                },
            ],
        )
        self.init_round(manifests=[manifest])
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists([])
        round_mod.run_reduce(self.round_dir, allow_incomplete=True)
        outcome = round_mod.run_coverage(self.round_dir)

        report = outcome.report
        # No rows were added by this round (BMI already existed).
        self.assertEqual(report.rows_added, 0)
        self.assertEqual(report.analyses_resolved, 0)
        # The after state still resolves BMI from the pre-existing row.
        family = report.family("fam-a")
        assert family is not None
        self.assertEqual(family.unmapped_before, 2)
        self.assertEqual(family.unmapped_after, 1)

    def _abstain_and_promote(self, none_suitable: float) -> coverage.CoverageReport:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists([shortlist_row(BMI_LABEL, BMI_ID, "body mass index")])
        self.write_choice_result(
            BMI_LABEL,
            NONE_SUITABLE,
            {NONE_SUITABLE: none_suitable, BMI_ID: 1.0 - none_suitable},
        )
        round_mod.run_reduce(self.round_dir)
        round_mod.run_promote(self.round_dir)
        return round_mod.run_coverage(self.round_dir).report

    def test_confident_abstention_is_counted_as_no_suitable_term(self) -> None:
        report = self._abstain_and_promote(0.99)
        self.assertEqual(report.none_suitable_count, 1)
        self.assertEqual(report.review_queue_size, 0)

    def test_uncertain_abstention_is_counted_only_in_the_review_queue(self) -> None:
        # reduce buckets every none_suitable choice together; promote sends an
        # uncertain one to review, so it must not also be reported as a
        # confident abstention.
        report = self._abstain_and_promote(0.55)
        self.assertEqual(report.none_suitable_count, 0)
        self.assertEqual(report.review_queue_size, 1)

    def test_missing_reconciliation_is_refused(self) -> None:
        manifest = self.manifest("fam-a", [])
        self.init_round(manifests=[manifest])
        with self.assertRaises(round_mod.RoundStateError):
            round_mod.run_coverage(self.round_dir)

    def test_stale_reconciliation_is_refused(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(queue_tsv=queue)
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists([shortlist_row(BMI_LABEL, BMI_ID, "body mass index")])
        round_mod.run_reduce(self.round_dir, allow_incomplete=True)

        # A result arrives after reduce ran, changing BMI from pending to
        # proposed; the stored reconciliation is now stale.
        self.write_choice_result(BMI_LABEL, BMI_ID, {BMI_ID: 1.0})
        with self.assertRaises(round_mod.RoundStateError):
            round_mod.run_coverage(self.round_dir)


# ---------------------------------------------------------------------------
# Thin convenience runner
# ---------------------------------------------------------------------------


class RunnerTest(RoundTestCase):
    def test_stops_after_reduce_when_incomplete(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(queue_tsv=queue)

        class FailingChooser(RecordingChooser):
            def select(self, trait_label, candidates, *, trait_context=""):
                raise ValueError("upstream unavailable")

        outcome = round_mod.run_round(
            self.round_dir,
            chooser=FailingChooser({BMI_LABEL: (BMI_ID, {BMI_ID: 1.0})}),
            workers=1,
        )
        self.assertIsNone(outcome.promote)
        self.assertIsNone(outcome.coverage)
        self.assertFalse(outcome.reduce.complete)
        self.assertTrue((self.round_dir / "queue.tsv").is_file())
        self.assertTrue((self.round_dir / "proposals.tsv").is_file())
        # The mapping table was not touched by an incomplete round.
        _, rows = parse_tsv(self.mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(rows, [])


class _FakeJevResponse:
    def __init__(self, data: dict, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP status {self.status_code}")

    def json(self) -> dict:
        return self._data


class _FakeJevHttpClient:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)

    def post(self, url: str, json: dict, headers: dict):
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


class RealJevIdentityTest(RoundTestCase):
    """init -> choose -> reduce with a real Jev chooser and no --chooser-model."""

    def test_choose_and_reduce_agree_without_chooser_model(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        # chooser_version defaults to the stub's "1"; init_round must derive a
        # Jev version from the model so choose and reduce fingerprints agree.
        config = self.init_round(queue_tsv=queue, chooser_id="jev")
        self.assertEqual(config.chooser.model, DEFAULT_JEV_MODEL)
        self.assertEqual(config.chooser.version, DEFAULT_JEV_MODEL)
        self.assertEqual(config.chooser.resolved_version(), DEFAULT_JEV_MODEL)

        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists([shortlist_row(BMI_LABEL, BMI_ID, "body mass index")])

        response = {
            "model": DEFAULT_JEV_MODEL,
            "answers": {
                "term": {
                    "type": "choice",
                    "choice": BMI_ID,
                    "confidence": 0.9,
                    "probabilities": {BMI_ID: 0.9, NONE_SUITABLE: 0.1},
                }
            },
            "usage": {"input_tokens": 100, "output_tokens": 5},
        }
        fake = _FakeJevHttpClient([_FakeJevResponse(response)])
        client = HttpJevClient(
            endpoint="https://example.invalid/v1/systemone",
            model=DEFAULT_JEV_MODEL,
            api_key="test-key",
            client_factory=lambda: fake,
            sleep=lambda _seconds: None,
        )
        chooser = JevChooser(
            client, chooser_id="jev", chooser_version=DEFAULT_JEV_MODEL
        )
        chosen = round_mod.run_choose(self.round_dir, chooser=chooser, workers=1)
        self.assertEqual(chosen.chosen, 1)

        reduced = round_mod.run_reduce(self.round_dir)
        self.assertTrue(reduced.complete)
        self.assertEqual(reduced.reconciliation.proposed, 1)
        self.assertEqual(reduced.reconciliation.pending, 0)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class RoundCliTest(RoundTestCase):
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = round_mod.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_choose_cli_honours_jev_endpoint_and_key(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        self.init_round(
            queue_tsv=queue, chooser_id="jev", chooser_version="1"
        )
        round_mod.run_gap_scan(self.round_dir)
        self.write_shortlists([shortlist_row(BMI_LABEL, BMI_ID, "body mass index")])

        captured: dict = {}
        original = round_mod.choice_mod.build_chooser

        def fake_build(name, fixture, **kwargs):
            captured.update(kwargs)
            captured["name"] = name
            return RecordingChooser(
                {BMI_LABEL: (BMI_ID, {BMI_ID: 1.0})}
            )

        round_mod.choice_mod.build_chooser = fake_build
        try:
            code, _, err = self._run(
                [
                    "choose",
                    "--round-dir", str(self.round_dir),
                    "--workers", "1",
                    "--jev-endpoint", "https://cli.example/v1/systemone",
                    "--jev-api-key", "cli-secret",
                ]
            )
        finally:
            round_mod.choice_mod.build_chooser = original
        self.assertEqual(code, 0, err)
        self.assertEqual(captured["name"], "jev")
        self.assertEqual(
            captured["jev_endpoint"], "https://cli.example/v1/systemone"
        )
        self.assertEqual(captured["jev_api_key"], "cli-secret")

    def test_round_init_and_gap_scan_cli(self) -> None:
        queue = self.queue_tsv([BMI_LABEL])
        showcase = write_showcase_schema(self.base / "showcase")
        code, _, err = self._run(
            [
                "round-init",
                "--round-dir", str(self.round_dir),
                "--index", str(self.index),
                "--chooser", "test",
                "--queue-tsv", str(queue),
                "--resource-dir", str(self.resource_dir),
                "--ukb-showcase-dir", str(showcase),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            round_mod.read_round_config(self.round_dir).ukb_showcase_dir,
            showcase.resolve(),
        )
        code, _, err = self._run(
            ["gap-scan", "--round-dir", str(self.round_dir)]
        )
        self.assertEqual(code, 0, err)
        self.assertTrue((self.round_dir / "queue.tsv").is_file())


if __name__ == "__main__":
    unittest.main()
