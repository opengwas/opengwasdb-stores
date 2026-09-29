#!/usr/bin/env python3
"""Tests for curation.candidates and curation.ontology (issue #164).

The user-visible contract under test is that candidate generation turns each
queued, unmapped Trait label into a shortlist of plausible ontology terms using
lexical channels only -- no model, no network -- and that every candidate
carries enough evidence for a curator to judge it: which channels retrieved it,
each channel's rank, the term's label, definition and parent, and whether it is
obsolete.

The suite is hermetic: it resolves against a tiny fixture ontology parsed from
an in-memory OBO document, never a real release.

Verifies:
- exact, icd10, normalised, token-overlap, and synonym/acronym channels each
  contribute candidates;
- the icd10 channel retrieves by exact code and by three-character chapter
  prefix, never by an obsolete term's code, and is recorded in provenance;
- the candidate-space restriction drops cell-line/cell-type lineage terms and
  banned prefixes while keeping a normal phenotype, after obsolete folding;
- a candidate records which channels found it and each channel's rank;
- a candidate carries the term's label, definition, and parent term;
- the pinned ontology release travels on every shortlist row;
- candidates from several channels are deduplicated by ontology id;
- the shortlist size is configurable and respected;
- a label no channel matches yields an empty shortlist, never a fabricated term;
- an obsolete term is never offered: it is replaced by its live successor or
  dropped;
- the retrieval index round-trips through its rebuildable artifact, rejects an
  unknown format version, and defaults outside the tracked tree;
- the prebuilt-lookup lexical channels return exactly what the brute-force
  scans return, over a fixture index and a randomised synthetic index;
- the CLI reads the gap-scan work queue and writes the shortlist table.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation import candidates, ontology
from curation.candidates import (
    CHANNEL_EXACT,
    CHANNEL_ICD10,
    CHANNEL_NORMALISED,
    CHANNEL_SYNONYM,
    CHANNEL_TOKEN_OVERLAP,
    Candidate,
    exact_channel,
    format_shortlist_tsv,
    generate_shortlist,
    generate_shortlists,
    icd10_channel,
    normalise_label,
    normalised_channel,
    read_work_queue,
    run_channels,
    synonym_channel,
    token_overlap_channel,
)
from curation.ontology import (
    INDEX_FORMAT_VERSION,
    PINNED_ONTOLOGY_RELEASE,
    IndexFormatError,
    OntologyIndex,
    OntologyTerm,
    build_index_from_obo,
    default_index_path,
    load_index,
    parse_obo,
    write_index,
)

# A tiny fixture ontology, not real curated data. Every term exists to exercise
# one channel or one attribution field:
#   EFO:0004340 -- mixed-case label, a declared synonym and an acronym;
#   EFO:0004338 -- the parent whose label is resolved from `is_a`;
#   EFO:0004324 -- a lowercase label that matches a lowercase queue label exactly;
#   EFO:0004518 -- token overlap with no exact/normalised/synonym match;
#   EFO:100000x -- enough "measurement" terms to overflow a shortlist;
#   EFO:9999001 -- an obsolete term;
#   EFO:0004350..4354 -- ICD-10 xrefs (exact code, chapter-level code,
#       obsolete-with-code), for the icd10 channel equivalence tests;
#   EFO:1002000..1002005, BTO:0000001 -- cell-line/cell-type terms the
#       candidate-space restriction must drop, and a kept phenotype.
FIXTURE_OBO = """
format-version: 1.2
ontology: efo

[Term]
id: EFO:0004340
name: Body mass index
def: "A measurement of body mass index." [PMID:123]
is_a: EFO:0004338 ! body weights and measures
synonym: "BMI" EXACT []
synonym: "Quetelet index" EXACT []

[Term]
id: EFO:0004338
name: body weights and measures
def: "Any measurement of body weight." []
is_a: EFO:0004324

[Term]
id: EFO:0004324
name: measurement
def: "The act or process of measuring." []

[Term]
id: EFO:0004518
name: systolic blood pressure
def: "A systolic blood pressure measurement." []
is_a: EFO:0004325 ! blood pressure

[Term]
id: EFO:0004325
name: blood pressure

[Term]
id: EFO:1000001
name: height measurement

[Term]
id: EFO:1000002
name: weight measurement

[Term]
id: EFO:1000003
name: age measurement

[Term]
id: EFO:1000004
name: depth measurement

[Term]
id: EFO:1000005
name: width measurement

[Term]
id: EFO:9999001
name: legacy obsolete trait
is_obsolete: true
replaced_by: EFO:0004340

[Term]
id: EFO:0004350
name: non-insulin-dependent diabetes mellitus
xref: ICD10:E11

[Term]
id: EFO:0004351
name: malignant neoplasm of rectum
xref: ICD10CM:C20

[Term]
id: EFO:0004352
name: malignant neoplasm of bronchus and lung
xref: ICD10WHO:C34

[Term]
id: EFO:0004353
name: viral infection, unspecified
xref: ICD10:B34.9
is_obsolete: true
replaced_by: EFO:0004354

[Term]
id: EFO:0004354
name: viral infection

[Term]
id: EFO:1002000
name: cell line

[Term]
id: EFO:1002001
name: HeLa cell line
is_a: EFO:1002000 ! cell line

[Term]
id: EFO:1002002
name: cell type

[Term]
id: EFO:1002003
name: pancreatic islet cell
is_a: EFO:1002002 ! cell type

[Term]
id: EFO:1002004
name: type 2 diabetes mellitus

[Term]
id: EFO:1002005
name: antique cell suspension
is_obsolete: true
replaced_by: EFO:1002000

[Term]
id: BTO:0000001
name: breast cancer cell line share
"""


def fixture_index(release: str = PINNED_ONTOLOGY_RELEASE) -> OntologyIndex:
    """The hermetic fixture ontology index."""
    return OntologyIndex(release, tuple(parse_obo(FIXTURE_OBO)))


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(col, "") for col in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()
    header = lines[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]
    return header, rows


class TestLexicalPrimitives(unittest.TestCase):
    """The normalisation the normalised channel is named for."""

    def test_normalise_strips_case_punctuation_and_whitespace(self) -> None:
        self.assertEqual(normalise_label("  Body-mass  index (BMI) "), "body mass index bmi")

    def test_normalise_empty(self) -> None:
        self.assertEqual(normalise_label(None), "")
        self.assertEqual(normalise_label("   "), "")


class TestChannels(unittest.TestCase):
    """Each independent lexical channel retrieves its intended term."""

    def setUp(self) -> None:
        self.index = fixture_index()

    def test_exact_channel_matches_identical_string(self) -> None:
        self.assertEqual(exact_channel("Body mass index", self.index), ["EFO:0004340"])
        # A case difference is not an exact match.
        self.assertEqual(exact_channel("body mass index", self.index), [])

    def test_normalised_channel_matches_case_and_punctuation(self) -> None:
        self.assertEqual(
            normalised_channel("  BODY-MASS index ", self.index), ["EFO:0004340"]
        )

    def test_token_overlap_channel_ranks_by_overlap(self) -> None:
        ranked = token_overlap_channel("systolic blood pressure reading", self.index)
        self.assertEqual(ranked[0], "EFO:0004518")
        self.assertIn("EFO:0004325", ranked)  # "blood pressure" also overlaps

    def test_synonym_channel_matches_declared_synonym(self) -> None:
        self.assertEqual(synonym_channel("Quetelet index", self.index), ["EFO:0004340"])

    def test_synonym_channel_matches_generated_acronym(self) -> None:
        self.assertEqual(synonym_channel("BMI", self.index), ["EFO:0004340"])

    def test_each_channel_contributes_to_run_channels(self) -> None:
        channels = run_channels("Quetelet index", self.index)
        self.assertEqual(channels[CHANNEL_SYNONYM], ["EFO:0004340"])
        # The channels are independent keys, even when only some fire.
        self.assertIn(CHANNEL_EXACT, channels)
        self.assertIn(CHANNEL_ICD10, channels)
        self.assertIn(CHANNEL_NORMALISED, channels)
        self.assertIn(CHANNEL_TOKEN_OVERLAP, channels)


class TestIcd10Channel(unittest.TestCase):
    """The ICD-10 cross-reference channel (issue #185)."""

    def setUp(self) -> None:
        self.index = fixture_index()

    def test_exact_code_finds_the_term(self) -> None:
        self.assertEqual(
            icd10_channel(
                "diagnoses - main icd10: c20 malignant neoplasm of rectum",
                self.index,
            ),
            ["EFO:0004351"],
        )

    def test_three_character_prefix_fallback(self) -> None:
        # The index has only the C34 chapter code; the label's C34.9 key
        # falls back to its three-character prefix.
        self.assertEqual(
            icd10_channel(
                "type of cancer: icd10: c34.9 upper lobe, bronchus or lung",
                self.index,
            ),
            ["EFO:0004352"],
        )

    def test_obsolete_term_is_never_retrieved_by_its_code(self) -> None:
        # EFO:0004353 carries ICD10:B34.9 but is obsolete; its live successor
        # carries no xref, so the code retrieves nothing.
        self.assertEqual(
            icd10_channel(
                "diagnoses - secondary icd10: b34.9 viral infection, unspecified",
                self.index,
            ),
            [],
        )

    def test_label_without_a_code_contributes_nothing(self) -> None:
        self.assertEqual(icd10_channel("Body mass index", self.index), [])
        # An opcs-coded label is not an ICD-10 label.
        self.assertEqual(
            icd10_channel(
                "operative procedures - main opcs: a52.1 therapeutic lumbar epidural injection",
                self.index,
            ),
            [],
        )

    def test_channel_is_recorded_in_provenance(self) -> None:
        shortlist = generate_shortlist(
            "diagnoses - main icd10: c20 malignant neoplasm of rectum",
            self.index,
            shortlist_size=10,
        )
        self.assertTrue(shortlist)
        top = shortlist[0]
        self.assertEqual(top.ontology_id, "EFO:0004351")
        self.assertIn(CHANNEL_ICD10, top.channels)
        self.assertEqual(dict(top.channel_ranks)[CHANNEL_ICD10], 1)


class TestCandidateSpaceRestriction(unittest.TestCase):
    """Phenotype-only candidate spaces (issue #185)."""

    def setUp(self) -> None:
        self.index = fixture_index()
        self.by_id = self.index.by_id()

    def test_marker_term_itself_is_excluded(self) -> None:
        self.assertTrue(
            candidates._term_is_excluded("EFO:1002000", self.by_id)
        )
        self.assertTrue(
            candidates._term_is_excluded("EFO:1002002", self.by_id)
        )

    def test_marker_lineage_is_excluded(self) -> None:
        # "HeLa cell line" is not a marker but its lineage passes through
        # "cell line"; "pancreatic islet cell" passes through "cell type".
        self.assertTrue(
            candidates._term_is_excluded("EFO:1002001", self.by_id)
        )
        self.assertTrue(
            candidates._term_is_excluded("EFO:1002003", self.by_id)
        )

    def test_banned_prefix_is_excluded(self) -> None:
        self.assertTrue(
            candidates._term_is_excluded("BTO:0000001", self.by_id)
        )

    def test_normal_phenotype_is_kept(self) -> None:
        self.assertFalse(
            candidates._term_is_excluded("EFO:1002004", self.by_id)
        )

    def test_predicate_is_configurable(self) -> None:
        # Turning a list off is observable on the same term, so the pins are
        # parameters, not baked-in behaviour.
        self.assertFalse(
            candidates._term_is_excluded(
                "BTO:0000001", self.by_id, excluded_prefixes=frozenset()
            )
        )
        self.assertFalse(
            candidates._term_is_excluded(
                "EFO:1002000", self.by_id, lineage_markers=frozenset()
            )
        )

    def test_excluded_ids_are_countable(self) -> None:
        ranks = {
            "EFO:1002001": {"exact": 1},
            "EFO:1002004": {"exact": 1},
            "BTO:0000001": {"exact": 1},
        }
        excluded = candidates._excluded_candidate_ids(ranks, self.by_id)
        self.assertEqual(excluded, frozenset({"EFO:1002001", "BTO:0000001"}))

    def test_shortlist_drops_excluded_terms(self) -> None:
        # The only lexical matches for these labels are excluded terms.
        self.assertEqual(
            generate_shortlist("HeLa cell line", self.index, shortlist_size=10),
            [],
        )
        self.assertEqual(
            generate_shortlist(
                "breast cancer cell line share", self.index, shortlist_size=10
            ),
            [],
        )

    def test_shortlist_keeps_normal_phenotype(self) -> None:
        shortlist = generate_shortlist(
            "type 2 diabetes mellitus", self.index, shortlist_size=10
        )
        self.assertTrue(shortlist)
        self.assertEqual(shortlist[0].ontology_id, "EFO:1002004")
        self.assertFalse(
            any(
                candidates._term_is_excluded(c.ontology_id, self.by_id)
                for c in shortlist
            )
        )

    def test_exclusion_applies_after_obsolete_folding(self) -> None:
        # The only match for the label is the obsolete "antique cell
        # suspension", which folds to the live "cell line" successor; the
        # restriction must drop the folded successor, not the obsolete id.
        self.assertEqual(
            generate_shortlist("antique cell suspension", self.index, shortlist_size=10),
            [],
        )


class TestLexicalChannelEquivalence(unittest.TestCase):
    """The prebuilt-lookup channels reproduce the brute-force scans exactly.

    The fast channels must return the same ids, in the same order, with the
    same ranks as the scans they replace; otherwise the retrieval ceiling
    moves and every downstream measurement is invalid.
    """

    CHANNEL_PAIRS = (
        ("exact", exact_channel, candidates._brute_exact_channel),
        ("icd10", icd10_channel, candidates._brute_icd10_channel),
        ("normalised", normalised_channel, candidates._brute_normalised_channel),
        ("token_overlap", token_overlap_channel, candidates._brute_token_overlap_channel),
        ("synonym", synonym_channel, candidates._brute_synonym_channel),
    )

    def setUp(self) -> None:
        self.index = fixture_index()

    def test_fixture_labels_match_brute_force(self) -> None:
        self._assert_equivalent(
            [
                "Body mass index",
                "body mass index",
                "BMI",
                "Quetelet index",
                "systolic blood pressure reading",
                "measurement",
                "height measurement",
                "legacy obsolete trait",
                "diagnoses - main icd10: c20 malignant neoplasm of rectum",
                "diagnoses - secondary icd10: b34.9 viral infection, unspecified",
                "type of cancer: icd10: c34.9 upper lobe, bronchus or lung",
                "operative procedures - main opcs: a52.1 therapeutic lumbar epidural injection",
                "",
                "   ",
                "!!!",
                "qwertyuiop asdfghjkl",
            ]
        )

    def test_randomised_synthetic_index_matches_brute_force(self) -> None:
        import random

        rng = random.Random(20240101)
        vocabulary = [
            "body", "mass", "index", "measurement", "blood", "pressure",
            "disease", "level", "serum", "bmi", "height", "weight",
            "cardio", "metabolic", "glucose", "plasma", "alpha", "beta", "x",
        ]
        terms = []
        for number in range(180):
            label = " ".join(
                rng.choice(vocabulary) for _ in range(rng.randint(1, 4))
            )
            synonyms = tuple(
                " ".join(rng.choice(vocabulary) for _ in range(rng.randint(1, 3)))
                for _ in range(rng.randint(0, 2))
            )
            terms.append(
                OntologyTerm(
                    ontology_id=f"EFO:{number:07d}",
                    label=label,
                    definition="a measurement",
                    synonyms=synonyms,
                )
            )
        index = OntologyIndex("efo/test", tuple(terms))
        labels = [
            " ".join(rng.choice(vocabulary) for _ in range(rng.randint(0, 5)))
            for _ in range(400)
        ]
        labels += [
            "".join(rng.choice("abcXYZ()- ") for _ in range(rng.randint(0, 12)))
            for _ in range(100)
        ]
        self._assert_equivalent(labels, index=index)

    def test_lookups_are_built_once_per_index(self) -> None:
        self.assertIs(self.index.lexical_lookups, self.index.lexical_lookups)
        self.assertIs(self.index.by_id(), self.index.by_id())

    def _assert_equivalent(
        self, labels: list[str], index: OntologyIndex | None = None
    ) -> None:
        target = index or self.index
        for label in labels:
            for name, fast, brute in self.CHANNEL_PAIRS:
                with self.subTest(channel=name, label=label):
                    self.assertEqual(
                        fast(label, target),
                        brute(label, target),
                        msg=f"{name} mismatch for {label!r}",
                    )


class TestGenerateShortlist(unittest.TestCase):
    """Attribution, deduplication, fields, and the shortlist cap."""

    def setUp(self) -> None:
        self.index = fixture_index()

    def test_attribution_records_channels_and_ranks(self) -> None:
        shortlist = generate_shortlist("Body mass index", self.index)
        top = shortlist[0]
        self.assertEqual(top.ontology_id, "EFO:0004340")
        self.assertEqual(set(top.channels), {CHANNEL_EXACT, CHANNEL_NORMALISED, CHANNEL_TOKEN_OVERLAP})
        ranks = dict(top.channel_ranks)
        self.assertEqual(ranks[CHANNEL_EXACT], 1)
        self.assertEqual(ranks[CHANNEL_NORMALISED], 1)

    def test_candidate_carries_label_definition_and_parent(self) -> None:
        top = generate_shortlist("Body mass index", self.index)[0]
        self.assertEqual(top.ontology_label, "Body mass index")
        self.assertEqual(top.definition, "A measurement of body mass index.")
        self.assertEqual(top.parent_id, "EFO:0004338")
        self.assertEqual(top.parent_label, "body weights and measures")

    def test_candidates_are_deduplicated_by_ontology_id(self) -> None:
        shortlist = generate_shortlist("Body mass index", self.index)
        ids = [candidate.ontology_id for candidate in shortlist]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids.count("EFO:0004340"), 1)

    def test_pinned_release_travels_on_every_candidate(self) -> None:
        for candidate in generate_shortlist("Body mass index", self.index):
            self.assertEqual(candidate.ontology_release, PINNED_ONTOLOGY_RELEASE)

    def test_shortlist_size_is_respected(self) -> None:
        full = generate_shortlist("measurement", self.index, shortlist_size=20)
        self.assertGreater(len(full), 3)
        capped = generate_shortlist("measurement", self.index, shortlist_size=3)
        self.assertEqual(len(capped), 3)
        self.assertEqual([candidate.rank for candidate in capped], [1, 2, 3])

    def test_shortlist_size_below_one_is_an_error(self) -> None:
        with self.assertRaises(candidates.CandidateGenerationError):
            generate_shortlist("measurement", self.index, shortlist_size=0)

    def test_unmatched_label_yields_empty_shortlist(self) -> None:
        self.assertEqual(generate_shortlist("qwertyuiop asdfghjkl", self.index), [])

    def test_obsolete_term_is_replaced_by_its_successor(self) -> None:
        shortlist = generate_shortlist("legacy obsolete trait", self.index)
        self.assertTrue(shortlist)
        self.assertEqual(shortlist[0].ontology_id, "EFO:0004340")
        self.assertIn("exact", shortlist[0].channels)
        self.assertFalse(any(candidate.is_obsolete for candidate in shortlist))

    def test_obsolete_term_without_a_live_successor_is_dropped(self) -> None:
        from types import SimpleNamespace

        from curation.candidates import _fold_obsolete_terms

        by_id = {
            "EFO:1": SimpleNamespace(is_obsolete=False, replaced_by=""),
            "EFO:2": SimpleNamespace(is_obsolete=True, replaced_by="EFO:1"),
            "EFO:3": SimpleNamespace(is_obsolete=True, replaced_by=""),
            "EFO:4": SimpleNamespace(is_obsolete=True, replaced_by="EFO:5"),
            "EFO:5": SimpleNamespace(is_obsolete=True, replaced_by="EFO:4"),
        }
        folded = _fold_obsolete_terms(
            {
                "EFO:1": {"embedding": 3},
                "EFO:2": {"embedding": 1, "exact": 1},
                "EFO:3": {"embedding": 2},
                "EFO:4": {"embedding": 4},
            },
            by_id,
        )
        self.assertEqual(folded, {"EFO:1": {"embedding": 1, "exact": 1}})

    def test_generate_shortlists_skips_unmatched_labels(self) -> None:
        rows = generate_shortlists(
            ["Body mass index", "qwertyuiop asdfghjkl"], self.index
        )
        self.assertTrue(rows)
        self.assertTrue(all(row.trait_label == "Body mass index" for row in rows))


class TestFormatShortlistTsv(unittest.TestCase):
    """The rendered table is machine-readable and self-describing."""

    def test_header_only_for_empty_shortlist(self) -> None:
        text = format_shortlist_tsv([])
        self.assertEqual(text, "\t".join(candidates.SHORTLIST_COLUMNS) + "\n")

    def test_row_carries_release_and_attribution(self) -> None:
        index = fixture_index()
        candidate = generate_shortlist("Body mass index", index)[0]
        header, rows = parse_tsv(format_shortlist_tsv([candidate]))
        self.assertEqual(header, list(candidates.SHORTLIST_COLUMNS))
        row = rows[0]
        self.assertEqual(row["trait_label"], "Body mass index")
        self.assertEqual(row["ontology_release"], PINNED_ONTOLOGY_RELEASE)
        self.assertEqual(row["ontology_id"], "EFO:0004340")
        self.assertEqual(row["parent_id"], "EFO:0004338")
        self.assertEqual(row["is_obsolete"], "false")
        self.assertIn("exact", row["channels"])
        self.assertIn("exact=1", row["channel_ranks"])

    def test_tab_in_definition_does_not_shift_columns(self) -> None:
        candidate = Candidate(
            trait_label="x",
            ontology_release="efo/v1",
            rank=1,
            ontology_id="EFO:1",
            ontology_label="y",
            definition="line\tone\nline two",
            parent_id="",
            parent_label="",
            channels=("exact",),
            channel_ranks=(("exact", 1),),
            is_obsolete=False,
        )
        header, rows = parse_tsv(format_shortlist_tsv([candidate]))
        self.assertEqual(len(header), len(candidates.SHORTLIST_COLUMNS))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["definition"], "line one line two")


class TestOntologyIndex(unittest.TestCase):
    """The index is rebuildable, versioned, and held outside the tracked tree."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.obo_path = self.td / "fixture.obo"
        self.obo_path.write_text(FIXTURE_OBO, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_build_from_obo_resolves_parent_labels(self) -> None:
        index = build_index_from_obo(self.obo_path, "efo/v9.9.9")
        self.assertEqual(index.ontology_release, "efo/v9.9.9")
        by_id = index.by_id()
        self.assertEqual(by_id["EFO:0004338"].parent_label, "measurement")
        self.assertEqual(by_id["EFO:0004340"].synonyms, ("BMI", "Quetelet index"))

    def test_index_round_trips_through_its_artifact(self) -> None:
        index = build_index_from_obo(self.obo_path)
        artifact = self.td / "index.json"
        write_index(index, artifact)
        self.assertEqual(load_index(artifact), index)

    def test_unknown_index_version_is_rejected(self) -> None:
        artifact = self.td / "stale.json"
        artifact.write_text(
            '{"index_format_version": 999, "ontology_release": "efo/v1", "terms": []}',
            encoding="utf-8",
        )
        with self.assertRaises(IndexFormatError):
            load_index(artifact)

    def test_default_index_path_is_outside_tracked_tree(self) -> None:
        path = default_index_path()
        self.assertEqual(path.parts[:2], (".cache", "curation"))
        self.assertIn("efo", path.name)

    def test_default_index_version_constant_is_recorded(self) -> None:
        index = build_index_from_obo(self.obo_path)
        self.assertEqual(index.to_dict()["index_format_version"], INDEX_FORMAT_VERSION)


class TestReadWorkQueue(unittest.TestCase):
    """The work queue contract from curation.gap_scan is enforced."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reads_rows(self) -> None:
        queue = self.td / "queue.tsv"
        write_tsv(
            queue,
            ["trait_label", "occurrence_count", "store_families"],
            [{"trait_label": "measurement", "occurrence_count": "2", "store_families": "ukb-b"}],
        )
        rows = read_work_queue(queue)
        self.assertEqual(rows[0]["trait_label"], "measurement")

    def test_missing_trait_label_column_raises(self) -> None:
        queue = self.td / "bad.tsv"
        write_tsv(queue, ["occurrence_count"], [{"occurrence_count": "1"}])
        with self.assertRaises(candidates.WorkQueueError):
            read_work_queue(queue)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(candidates.WorkQueueError):
            read_work_queue(self.td / "nope.tsv")


class TestCli(unittest.TestCase):
    """The command reads a queue, resolves against an index, and writes a table."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.obo_path = self.td / "fixture.obo"
        self.obo_path.write_text(FIXTURE_OBO, encoding="utf-8")
        self.index_path = self.td / "index.json"
        write_index(build_index_from_obo(self.obo_path), self.index_path)
        self.queue_path = self.td / "queue.tsv"
        write_tsv(
            self.queue_path,
            ["trait_label", "occurrence_count", "store_families"],
            [
                {"trait_label": "measurement", "occurrence_count": "5", "store_families": "ukb-b"},
                {"trait_label": "Body mass index", "occurrence_count": "3", "store_families": "ukb-b"},
                {"trait_label": "qwertyuiop asdfghjkl", "occurrence_count": "1", "store_families": "ukb-b"},
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = candidates.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_writes_shortlist_to_output_file(self) -> None:
        output = self.td / "out" / "shortlist.tsv"
        code, out, err = self.run_cli(
            [
                "--work-queue", str(self.queue_path),
                "--index", str(self.index_path),
                "--output", str(output),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        header, rows = parse_tsv(output.read_text(encoding="utf-8"))
        self.assertEqual(header, list(candidates.SHORTLIST_COLUMNS))
        self.assertTrue(rows)
        # The pinned release travels with every row.
        self.assertTrue(all(row["ontology_release"] == PINNED_ONTOLOGY_RELEASE for row in rows))
        # A matched label appears; an unmatched label fabricates no row.
        labels = {row["trait_label"] for row in rows}
        self.assertIn("Body mass index", labels)
        self.assertNotIn("qwertyuiop asdfghjkl", labels)

    def test_shortlist_size_option_is_respected(self) -> None:
        output = self.td / "capped.tsv"
        code, _, err = self.run_cli(
            [
                "--work-queue", str(self.queue_path),
                "--index", str(self.index_path),
                "--output", str(output),
                "--shortlist-size", "1",
            ]
        )
        self.assertEqual(code, 0, err)
        _, rows = parse_tsv(output.read_text(encoding="utf-8"))
        # One row per matched label, no more than one candidate each.
        per_label: dict[str, int] = {}
        for row in rows:
            per_label[row["trait_label"]] = per_label.get(row["trait_label"], 0) + 1
        self.assertTrue(all(count == 1 for count in per_label.values()))

    def test_writes_to_stdout_when_no_output(self) -> None:
        code, out, _ = self.run_cli(
            ["--work-queue", str(self.queue_path), "--index", str(self.index_path)]
        )
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("\t".join(candidates.SHORTLIST_COLUMNS)))

    def test_missing_index_exits_one(self) -> None:
        code, out, err = self.run_cli(
            [
                "--work-queue", str(self.queue_path),
                "--index", str(self.td / "nope.json"),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("candidates: error:", err)

    def test_bad_shortlist_size_exits_one(self) -> None:
        code, _, err = self.run_cli(
            [
                "--work-queue", str(self.queue_path),
                "--index", str(self.index_path),
                "--shortlist-size", "0",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("--shortlist-size", err)


if __name__ == "__main__":
    unittest.main()
