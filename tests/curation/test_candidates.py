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
- exact, normalised, token-overlap, and synonym/acronym channels each contribute
  candidates;
- a candidate records which channels found it and each channel's rank;
- a candidate carries the term's label, definition, and parent term;
- the pinned ontology release travels on every shortlist row;
- candidates from several channels are deduplicated by ontology id;
- the shortlist size is configurable and respected;
- a label no channel matches yields an empty shortlist, never a fabricated term;
- obsolete terms are flagged rather than silently offered as live terms;
- the retrieval index round-trips through its rebuildable artifact, rejects an
  unknown format version, and defaults outside the tracked tree;
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
    CHANNEL_NORMALISED,
    CHANNEL_SYNONYM,
    CHANNEL_TOKEN_OVERLAP,
    Candidate,
    exact_channel,
    format_shortlist_tsv,
    generate_shortlist,
    generate_shortlists,
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
#   EFO:9999001 -- an obsolete term.
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
        self.assertIn(CHANNEL_NORMALISED, channels)
        self.assertIn(CHANNEL_TOKEN_OVERLAP, channels)


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

    def test_obsolete_term_is_flagged(self) -> None:
        shortlist = generate_shortlist("legacy obsolete trait", self.index)
        self.assertTrue(shortlist)
        self.assertTrue(shortlist[0].is_obsolete)
        self.assertFalse(generate_shortlist("Body mass index", self.index)[0].is_obsolete)

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
