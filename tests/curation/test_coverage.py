#!/usr/bin/env python3
"""Tests for curation round coverage reporting: curation.coverage (issue #170).

The user-visible contract under test is that a curation round is reported in
the language of the corpus, not the machinery:

* the before/after unmapped rate is computed **per Store Family** and expressed
  in **Analyses resolved**, never in rows added;
* the number of rows appended to the Canonical Trait Mapping Table is reported
  separately and is explicitly distinguished from the Analyses those rows
  unblock -- one row can resolve many Analyses across several families;
* the review queue size (labels awaiting a human curator) and the count of
  labels with no candidate retrieved at all are reported, and the latter stay
  unmapped by design rather than being forced to an approximate term;
* the total round cost is carried, and an untracked (offline) run is not
  mistaken for a free one;
* the report renders as text, Markdown, and TSV with the same figures;
* a full round run never modifies an existing Release Manifest, accepted
  Release Bundle, or built Store Release.

The suite is hermetic: tiny in-memory OBO/index fixtures, the fixture-backed
stub chooser, temporary Reference Resource copies, and no network.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation import choice, coverage, curation_round, gap_scan, promotion
from curation.ontology import build_index_from_obo, write_index
from curation.promotion import MAPPING_COLUMNS
from curation.stub_chooser import StubChooser

RELEASE = "efo/v3.78.0"

FIXTURE_OBO = """\
format-version: 1.2
ontology: efo

[Term]
id: EFO:0004340
name: body mass index
def: "A measurement of body mass index." [PMID:123]
synonym: "BMI" EXACT []

[Term]
id: EFO:0004338
name: body weights and measures
def: "Any measurement of body weight." []

[Term]
id: EFO:0004324
name: body height
def: "A measurement of standing height." []
synonym: "height" EXACT []
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
  Coverage-suite fixture resource.
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


def parse_tsv_comments(text: str) -> tuple[dict[str, str], list[str], list[dict[str, str]]]:
    """Split a TSV with leading ``# key: value`` comments into its parts."""
    comments: dict[str, str] = {}
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            body = line[1:].strip()
            key, _, value = body.partition(":")
            comments[key.strip()] = value.strip()
        elif line:
            data_lines.append(line)
    header = data_lines[0].split("\t") if data_lines else []
    rows = [dict(zip(header, line.split("\t"))) for line in data_lines[1:]]
    return comments, header, rows


def snapshot(root: Path) -> dict[str, bytes]:
    """Content snapshot of every file under ``root``, keyed by relative path."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# Manifest scanning
# ---------------------------------------------------------------------------


class ScanCoverageManifestsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _manifest(self, family: str, release: str, rows: list[dict[str, str]]) -> Path:
        path = self.base / "families" / family / "releases" / release / "analyses.tsv"
        write_table(path, ANALYSES_COLUMNS, rows)
        return path

    def test_totals_and_unmapped_counts_are_per_family(self) -> None:
        manifest_a = self._manifest(
            "fam-a",
            "rel-a",
            [
                {"analysis_id": "a1", "source_label": "Body mass index", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "a2", "source_label": "body mass index", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "a3", "source_label": "Mystery trait", "trait_ontology_mapping_method": "unmapped"},
            ],
        )
        manifest_b = self._manifest(
            "fam-b",
            "rel-b",
            [
                {"analysis_id": "b1", "source_label": "Body height", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "b2", "source_label": "body height", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "b3", "source_label": "Body height", "trait_ontology_mapping_method": "source_provided"},
            ],
        )

        stats = coverage.scan_coverage_manifests([manifest_a, manifest_b])

        self.assertEqual(stats["fam-a"].total_analyses, 3)
        self.assertEqual(stats["fam-a"].unmapped_before, 3)
        self.assertEqual(stats["fam-a"].unmapped_label_counts[BMI_LABEL], 2)
        self.assertEqual(stats["fam-a"].unmapped_label_counts[MYSTERY_LABEL], 1)

        self.assertEqual(stats["fam-b"].total_analyses, 3)
        # The source_provided row is counted in the denominator but not unmapped.
        self.assertEqual(stats["fam-b"].unmapped_before, 2)
        self.assertEqual(stats["fam-b"].unmapped_label_counts[HEIGHT_LABEL], 2)

    def test_case_and_whitespace_variants_collapse(self) -> None:
        manifest = self._manifest(
            "fam",
            "rel",
            [
                {"analysis_id": "1", "source_label": "  Body mass index ", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "2", "source_label": "body MASS index", "trait_ontology_mapping_method": "unmapped"},
            ],
        )
        stats = coverage.scan_coverage_manifests([manifest])
        self.assertEqual(stats["fam"].unmapped_label_counts, {BMI_LABEL: 2})

    def test_blank_unmapped_label_is_not_a_zero_count(self) -> None:
        manifest = self._manifest(
            "fam",
            "rel",
            [
                {"analysis_id": "1", "source_label": "", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "2", "source_label": "x", "trait_ontology_mapping_method": "unmapped"},
            ],
        )
        stats = coverage.scan_coverage_manifests([manifest])
        self.assertEqual(stats["fam"].total_analyses, 2)
        self.assertEqual(stats["fam"].unmapped_label_counts, {"x": 1})

    def test_manifest_without_mapping_column_is_skipped(self) -> None:
        path = self.base / "families" / "fam" / "releases" / "rel" / "analyses.tsv"
        write_table(path, ["analysis_id", "source_label"], [{"analysis_id": "1", "source_label": "x"}])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            stats = coverage.scan_coverage_manifests([path])
        self.assertEqual(stats, {})
        self.assertIn("no trait_ontology_mapping_method column", stderr.getvalue())

    def test_mapping_column_without_label_column_raises(self) -> None:
        path = self.base / "analyses.tsv"
        write_table(
            path,
            ["analysis_id", "trait_ontology_mapping_method"],
            [{"analysis_id": "1", "trait_ontology_mapping_method": "unmapped"}],
        )
        with self.assertRaises(coverage.CoverageError):
            coverage.scan_coverage_manifests([path])


# ---------------------------------------------------------------------------
# Computing the report
# ---------------------------------------------------------------------------


class ComputeCoverageTest(unittest.TestCase):
    def _stats(self, family: str, total: int, counts: dict[str, int]) -> coverage.FamilyStats:
        return coverage.FamilyStats(
            store_family=family, total_analyses=total, unmapped_label_counts=counts
        )

    def test_analyses_resolved_is_not_rows_added(self) -> None:
        """One promoted row can resolve many Analyses across several families."""
        stats = {
            "fam-a": self._stats("fam-a", 100, {BMI_LABEL: 60, "other": 10}),
            "fam-b": self._stats("fam-b", 50, {BMI_LABEL: 5}),
        }
        report = coverage.compute_coverage(stats, promoted_labels=[BMI_LABEL])

        # One row added...
        self.assertEqual(report.rows_added, 1)
        # ...but 65 Analyses resolved.
        self.assertEqual(report.analyses_resolved, 65)

        self.assertEqual(report.total_analyses, 150)
        self.assertEqual(report.unmapped_before, 75)
        self.assertEqual(report.unmapped_after, 10)
        self.assertAlmostEqual(report.unmapped_rate_before, 75 / 150)
        self.assertAlmostEqual(report.unmapped_rate_after, 10 / 150)

        fam_a = report.family("fam-a")
        assert fam_a is not None
        self.assertEqual(fam_a.unmapped_before, 70)
        self.assertEqual(fam_a.analyses_resolved, 60)
        self.assertEqual(fam_a.unmapped_after, 10)
        self.assertAlmostEqual(fam_a.unmapped_rate_before, 0.70)
        self.assertAlmostEqual(fam_a.unmapped_rate_after, 0.10)

        fam_b = report.family("fam-b")
        assert fam_b is not None
        self.assertEqual(fam_b.unmapped_before, 5)
        self.assertEqual(fam_b.analyses_resolved, 5)
        self.assertEqual(fam_b.unmapped_after, 0)
        self.assertAlmostEqual(fam_b.unmapped_rate_after, 0.0)

    def test_promoted_label_absent_from_a_family_resolves_nothing_there(self) -> None:
        stats = {
            "fam-a": self._stats("fam-a", 10, {BMI_LABEL: 4}),
            "fam-b": self._stats("fam-b", 10, {"other": 3}),
        }
        report = coverage.compute_coverage(stats, promoted_labels=[BMI_LABEL])
        self.assertEqual(report.rows_added, 1)
        self.assertEqual(report.analyses_resolved, 4)
        fam_b = report.family("fam-b")
        assert fam_b is not None
        self.assertEqual(fam_b.analyses_resolved, 0)
        self.assertEqual(fam_b.unmapped_after, 3)

    def test_duplicate_and_unnormalised_promoted_labels_count_once(self) -> None:
        stats = {"fam": self._stats("fam", 10, {BMI_LABEL: 4})}
        report = coverage.compute_coverage(
            stats, promoted_labels=["Body Mass Index", BMI_LABEL, "  body mass index  "]
        )
        self.assertEqual(report.rows_added, 1)
        self.assertEqual(report.analyses_resolved, 4)

    def test_review_queue_and_no_candidate_counts_are_carried(self) -> None:
        report = coverage.compute_coverage(
            {},
            promoted_labels=[],
            review_queue_size=7,
            no_candidate_count=3,
            cost_usd=1.25,
            cost_tracked=True,
        )
        self.assertEqual(report.review_queue_size, 7)
        self.assertEqual(report.no_candidate_count, 3)
        self.assertEqual(report.total_cost_usd, 1.25)
        self.assertTrue(report.cost_tracked)

    def test_empty_corpus_has_zero_rates(self) -> None:
        report = coverage.compute_coverage({}, promoted_labels=[])
        self.assertEqual(report.total_analyses, 0)
        self.assertEqual(report.unmapped_rate_before, 0.0)
        self.assertEqual(report.unmapped_rate_after, 0.0)
        self.assertIsNone(report.family("missing"))


# ---------------------------------------------------------------------------
# Reading the round's artifacts
# ---------------------------------------------------------------------------


class ReadArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_promoted_labels_are_the_mapping_difference(self) -> None:
        before = self.base / "before.tsv"
        after = self.base / "after.tsv"
        write_table(before, list(MAPPING_COLUMNS), [{"trait_label": "old"}])
        write_table(
            after,
            list(MAPPING_COLUMNS),
            [{"trait_label": "old"}, {"trait_label": "New"}],
        )
        self.assertEqual(
            coverage.promoted_labels_from_mapping(after, before), {"new"}
        )

    def test_without_a_before_table_every_label_is_new(self) -> None:
        after = self.base / "after.tsv"
        write_table(after, list(MAPPING_COLUMNS), [{"trait_label": "A"}, {"trait_label": "b"}])
        self.assertEqual(coverage.promoted_labels_from_mapping(after), {"a", "b"})

    def test_review_queue_counts_only_undecided_labels(self) -> None:
        queue = self.base / "review.tsv"
        write_table(
            queue,
            ["trait_label", "review_decision"],
            [
                {"trait_label": "awaiting", "review_decision": ""},
                {"trait_label": "awaiting", "review_decision": ""},
                {"trait_label": "done", "review_decision": "accept"},
                {"trait_label": "rejected", "review_decision": "reject"},
            ],
        )
        self.assertEqual(coverage.read_review_queue_size(queue), 1)

    def test_missing_review_queue_is_zero(self) -> None:
        self.assertEqual(coverage.read_review_queue_size(None), 0)
        self.assertEqual(coverage.read_review_queue_size(self.base / "nope.tsv"), 0)

    def test_no_candidate_labels_are_the_queue_minus_shortlist(self) -> None:
        count = coverage.count_no_candidate_labels(
            ["a", "b", "c"], ["A", "b"]
        )
        self.assertEqual(count, 1)

    def test_no_candidate_count_is_zero_when_every_label_retrieved(self) -> None:
        self.assertEqual(coverage.count_no_candidate_labels(["a"], ["a"]), 0)

    def test_cost_report_sums_the_cost_column(self) -> None:
        report = self.base / "cost.tsv"
        write_table(
            report,
            ["trait_label", "cost_usd"],
            [
                {"trait_label": "a", "cost_usd": "0.25"},
                {"trait_label": "b", "cost_usd": "0.75"},
            ],
        )
        total, tracked = coverage.read_cost_report(report)
        self.assertAlmostEqual(total, 1.0)
        self.assertTrue(tracked)

    def test_missing_cost_report_is_untracked(self) -> None:
        self.assertEqual(coverage.read_cost_report(None), (0.0, False))

    def test_invalid_cost_report_raises(self) -> None:
        report = self.base / "cost.tsv"
        write_table(report, ["cost_usd"], [{"cost_usd": "-1"}])
        with self.assertRaises(coverage.CoverageFormatError):
            coverage.read_cost_report(report)


@dataclass
class FakeCostRecord:
    trait_label: str
    cost_usd: float


class ChooserCostTest(unittest.TestCase):
    def test_untracked_chooser_has_no_cost(self) -> None:
        self.assertEqual(coverage.chooser_cost(object()), (0.0, False))

    def test_empty_records_are_untracked(self) -> None:
        chooser = type("C", (), {"cost_records": []})()
        self.assertEqual(coverage.chooser_cost(chooser), (0.0, False))

    def test_records_sum_into_a_tracked_total(self) -> None:
        chooser = type(
            "C",
            (),
            {"cost_records": [FakeCostRecord("a", 0.1), FakeCostRecord("b", 0.2)]},
        )()
        total, tracked = coverage.chooser_cost(chooser)
        self.assertAlmostEqual(total, 0.3)
        self.assertTrue(tracked)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class RenderTest(unittest.TestCase):
    def _report(self) -> coverage.CoverageReport:
        stats = {
            "fam-a": coverage.FamilyStats("fam-a", 10, {BMI_LABEL: 6}),
            "fam-b": coverage.FamilyStats("fam-b", 10, {"other": 4}),
        }
        return coverage.compute_coverage(
            stats,
            promoted_labels=[BMI_LABEL],
            review_queue_size=2,
            no_candidate_count=1,
            cost_usd=0.5,
            cost_tracked=True,
        )

    def test_text_carries_every_metric(self) -> None:
        text = coverage.render_text(self._report())
        for expected in (
            "Analyses resolved: 6",
            "Rows added: 1",
            "Review queue size: 2",
            "No candidates retrieved: 1",
            "Total round cost: $0.5000",
            "fam-a",
            "fam-b",
        ):
            self.assertIn(expected, text)

    def test_markdown_carries_every_metric(self) -> None:
        text = coverage.render_markdown(self._report())
        for expected in (
            "| Analyses resolved | 6 |",
            "| Rows added | 1 |",
            "| Review queue size | 2 label(s) awaiting a human curator |",
            "| No candidates retrieved | 1 label(s) left unmapped by design |",
            "| Total round cost | $0.5000 |",
        ):
            self.assertIn(expected, text)

    def test_tsv_is_machine_readable_with_global_metrics(self) -> None:
        comments, header, rows = parse_tsv_comments(coverage.render_tsv(self._report()))
        self.assertEqual(header, list(coverage.TSV_COLUMNS))
        self.assertEqual(comments["analyses_resolved"], "6")
        self.assertEqual(comments["rows_added"], "1")
        self.assertEqual(comments["review_queue_size"], "2")
        self.assertEqual(comments["no_candidate_count"], "1")
        self.assertEqual(comments["cost_tracked"], "true")
        self.assertEqual(comments["total_cost_usd"], "0.500000")
        by_family = {row["store_family"]: row for row in rows}
        self.assertEqual(by_family["fam-a"]["analyses_resolved"], "6")
        self.assertEqual(by_family["fam-b"]["analyses_resolved"], "0")

    def test_untracked_cost_renders_as_not_tracked(self) -> None:
        report = coverage.compute_coverage({}, promoted_labels=[])
        self.assertIn("not tracked", coverage.render_text(report))
        self.assertIn("not tracked", coverage.render_markdown(report))

    def test_unknown_format_raises(self) -> None:
        with self.assertRaises(coverage.CoverageError):
            coverage.render_report(self._report(), "xml")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class CoverageCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = coverage.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _fixtures(self) -> tuple[Path, Path, Path, Path, Path]:
        manifest = self.base / "families" / "fam" / "releases" / "rel" / "analyses.tsv"
        write_table(
            manifest,
            ANALYSES_COLUMNS,
            [
                {"analysis_id": "1", "source_label": "Body mass index", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "2", "source_label": "Mystery", "trait_ontology_mapping_method": "unmapped"},
            ],
        )
        mapping = self.base / "mapping.tsv"
        write_table(mapping, list(MAPPING_COLUMNS), [{"trait_label": "body mass index"}])
        work_queue = self.base / "queue.tsv"
        write_table(
            work_queue,
            list(gap_scan.OUTPUT_COLUMNS),
            [
                {"trait_label": "body mass index", "occurrence_count": "1", "store_families": "fam"},
                {"trait_label": "mystery", "occurrence_count": "1", "store_families": "fam"},
            ],
        )
        shortlists = self.base / "shortlists.tsv"
        write_table(shortlists, ["trait_label", "ontology_id"], [{"trait_label": "body mass index", "ontology_id": BMI_ID}])
        review_queue = self.base / "review.tsv"
        write_table(
            review_queue,
            ["trait_label", "review_decision"],
            [{"trait_label": "body height", "review_decision": ""}],
        )
        return manifest, mapping, work_queue, shortlists, review_queue

    def test_cli_reports_all_metrics(self) -> None:
        manifest, mapping, work_queue, shortlists, review_queue = self._fixtures()
        code, out, err = self._run(
            [
                "--manifests", str(manifest),
                "--mapping", str(mapping),
                "--work-queue", str(work_queue),
                "--shortlists", str(shortlists),
                "--review-queue", str(review_queue),
                "--cost-usd", "0.25",
                "--format", "tsv",
            ]
        )
        self.assertEqual(code, 0, err)
        comments, _, rows = parse_tsv_comments(out)
        self.assertEqual(comments["analyses_resolved"], "1")
        self.assertEqual(comments["rows_added"], "1")
        self.assertEqual(comments["review_queue_size"], "1")
        self.assertEqual(comments["no_candidate_count"], "1")
        self.assertEqual(comments["cost_tracked"], "true")
        self.assertEqual(comments["total_cost_usd"], "0.250000")
        self.assertEqual(len(rows), 1)

    def test_cli_writes_output_file(self) -> None:
        manifest, mapping, work_queue, shortlists, review_queue = self._fixtures()
        output = self.base / "report.md"
        code, out, err = self._run(
            [
                "--manifests", str(manifest),
                "--mapping", str(mapping),
                "--work-queue", str(work_queue),
                "--shortlists", str(shortlists),
                "--review-queue", str(review_queue),
                "--format", "markdown",
                "--output", str(output),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertIn("Unmapped rate per Store Family", output.read_text(encoding="utf-8"))

    def test_cli_rejects_negative_cost(self) -> None:
        manifest, mapping, _, _, _ = self._fixtures()
        code, _, err = self._run(
            ["--manifests", str(manifest), "--mapping", str(mapping), "--cost-usd", "-1"]
        )
        self.assertEqual(code, 1)
        self.assertIn("non-negative", err)


# ---------------------------------------------------------------------------
# End-to-end round
# ---------------------------------------------------------------------------


class CurationRoundTestCase(unittest.TestCase):
    """Shared hermetic fixtures for a full round."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

        self.manifest_a = self._manifest(
            "fam-a",
            "rel-a",
            [
                {"analysis_id": "a1", "source_label": "Body mass index", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "a2", "source_label": "body mass index", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "a3", "source_label": "Mystery trait", "trait_ontology_mapping_method": "unmapped"},
            ],
        )
        self.manifest_b = self._manifest(
            "fam-b",
            "rel-b",
            [
                {"analysis_id": "b1", "source_label": "Body mass index", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "b2", "source_label": "Body height", "trait_ontology_mapping_method": "unmapped"},
                {"analysis_id": "b3", "source_label": "body height", "trait_ontology_mapping_method": "unmapped"},
            ],
        )

        self.obo = self.base / "efo.obo"
        self.obo.write_text(FIXTURE_OBO, encoding="utf-8")
        self.index = self.base / "efo.index.json"
        write_index(build_index_from_obo(self.obo, RELEASE), self.index)

        self.fixture = self.base / "chooser-fixture.json"
        self.fixture.write_text(
            json.dumps(
                {
                    BMI_LABEL: {
                        "selected_ontology_id": BMI_ID,
                        "probabilities": {BMI_ID: 1.0},
                    },
                    HEIGHT_LABEL: {
                        "selected_ontology_id": HEIGHT_ID,
                        "probabilities": {HEIGHT_ID: 0.55, BMI_ID: 0.45},
                    },
                }
            ),
            encoding="utf-8",
        )

        self.resource_dir = self.base / "canonical-trait-mapping-efo"
        self.resource_dir.mkdir()
        self.mapping_path = self.resource_dir / "mapping.tsv"
        write_table(self.mapping_path, list(MAPPING_COLUMNS), [])
        (self.resource_dir / "resource.yaml").write_text(
            RESOURCE_YAML.format(version=1), encoding="utf-8"
        )

        self.work_dir = self.base / "work"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _manifest(self, family: str, release: str, rows: list[dict[str, str]]) -> Path:
        path = self.base / "families" / family / "releases" / release / "analyses.tsv"
        write_table(path, ANALYSES_COLUMNS, rows)
        return path

    def _run_round(self, chooser=None):
        return curation_round.run_curation_round(
            manifests=[self.manifest_a, self.manifest_b],
            index_path=self.index,
            chooser=chooser or StubChooser.from_path(self.fixture),
            resource_dir=self.resource_dir,
            work_dir=self.work_dir,
            report_format="tsv",
        )


class CurationRoundTest(CurationRoundTestCase):
    def test_round_promotes_confident_and_queues_the_rest(self) -> None:
        result = self._run_round()

        _, mapping_rows = parse_tsv(self.mapping_path.read_text(encoding="utf-8"))
        promoted = {row["trait_label"] for row in mapping_rows}
        self.assertEqual(promoted, {BMI_LABEL})
        self.assertEqual(mapping_rows[0]["trait_ontology_id"], BMI_ID)

        self.assertEqual(len(result.promotion.plan.promoted), 1)
        self.assertEqual(len(result.promotion.plan.queued), 1)
        self.assertEqual(result.promotion.plan.queued[0].proposal.trait_label, HEIGHT_LABEL)
        self.assertIn("version: 2", (self.resource_dir / "resource.yaml").read_text())

    def test_round_reports_analyses_resolved_distinct_from_rows_added(self) -> None:
        result = self._run_round()
        report = result.coverage

        self.assertEqual(report.rows_added, 1)
        # "body mass index" resolves two Analyses in fam-a and one in fam-b.
        self.assertEqual(report.analyses_resolved, 3)
        self.assertEqual(report.review_queue_size, 1)
        self.assertEqual(report.no_candidate_count, 1)

        fam_a = report.family("fam-a")
        assert fam_a is not None
        self.assertEqual(fam_a.total_analyses, 3)
        self.assertEqual(fam_a.unmapped_before, 3)
        self.assertEqual(fam_a.analyses_resolved, 2)
        self.assertEqual(fam_a.unmapped_after, 1)
        self.assertAlmostEqual(fam_a.unmapped_rate_before, 1.0)
        self.assertAlmostEqual(fam_a.unmapped_rate_after, 1 / 3)

        fam_b = report.family("fam-b")
        assert fam_b is not None
        self.assertEqual(fam_b.analyses_resolved, 1)
        self.assertEqual(fam_b.unmapped_after, 2)

    def test_label_with_no_candidate_stays_unmapped(self) -> None:
        result = self._run_round()

        _, mapping_rows = parse_tsv(self.mapping_path.read_text(encoding="utf-8"))
        self.assertNotIn(MYSTERY_LABEL, {row["trait_label"] for row in mapping_rows})

        _, queue_rows = parse_tsv(result.review_queue_path.read_text(encoding="utf-8"))
        self.assertNotIn(MYSTERY_LABEL, {row["trait_label"] for row in queue_rows})

        _, proposal_rows = parse_tsv(result.proposals_path.read_text(encoding="utf-8"))
        self.assertNotIn(MYSTERY_LABEL, {row["trait_label"] for row in proposal_rows})

        self.assertEqual(result.coverage.no_candidate_count, 1)

    def test_round_writes_expected_intermediate_artifacts(self) -> None:
        result = self._run_round()
        for path in (
            result.work_queue_path,
            result.shortlists_path,
            result.proposals_path,
            result.review_queue_path,
        ):
            self.assertTrue(path.is_file(), path)

    def test_round_carries_tracked_cost(self) -> None:
        class CostReportingChooser(StubChooser):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.cost_records: list[FakeCostRecord] = []

            def select(self, trait_label, candidates):
                self.cost_records.append(FakeCostRecord(trait_label, 0.01))
                return super().select(trait_label, candidates)

        result = self._run_round(chooser=CostReportingChooser(self.fixture))
        self.assertTrue(result.coverage.cost_tracked)
        self.assertAlmostEqual(result.coverage.total_cost_usd, 0.02)

    def test_offline_round_cost_is_untracked(self) -> None:
        result = self._run_round()
        self.assertFalse(result.coverage.cost_tracked)
        self.assertEqual(result.coverage.total_cost_usd, 0.0)

    def test_report_file_is_written(self) -> None:
        result = curation_round.run_curation_round(
            manifests=[self.manifest_a, self.manifest_b],
            index_path=self.index,
            chooser=StubChooser.from_path(self.fixture),
            resource_dir=self.resource_dir,
            work_dir=self.work_dir,
            report_format="markdown",
            report_path=self.work_dir / "report.md",
        )
        assert result.report_path is not None
        self.assertTrue(result.report_path.is_file())
        self.assertIn("# Canonical Trait Mapping", result.report_path.read_text(encoding="utf-8"))


class CurationRoundCliTest(CurationRoundTestCase):
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = curation_round.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_runs_a_round(self) -> None:
        code, out, err = self._run(
            [
                "--manifests", str(self.manifest_a), str(self.manifest_b),
                "--index", str(self.index),
                "--chooser", "stub",
                "--fixture", str(self.fixture),
                "--resource-dir", str(self.resource_dir),
                "--work-dir", str(self.work_dir),
                "--format", "tsv",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("rows_added", out)
        self.assertIn("promoted", err)
        # The report is persisted to the default work-dir path as well as
        # printed.
        self.assertTrue((self.work_dir / "coverage-report.tsv").is_file())

    def test_cli_dry_run_leaves_the_real_table_untouched(self) -> None:
        # A sentinel "real" resource the dry run must not write.
        real_resource = self.base / "real-resource"
        real_resource.mkdir()
        (real_resource / "mapping.tsv").write_text(
            "\t".join(MAPPING_COLUMNS) + "\n", encoding="utf-8"
        )
        (real_resource / "resource.yaml").write_text(
            RESOURCE_YAML.format(version=5), encoding="utf-8"
        )
        before = snapshot(real_resource)

        code, _, err = self._run(
            [
                "--manifests", str(self.manifest_a), str(self.manifest_b),
                "--index", str(self.index),
                "--chooser", "stub",
                "--fixture", str(self.fixture),
                "--resource-dir", str(real_resource),
                "--work-dir", str(self.work_dir),
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(snapshot(real_resource), before)


# ---------------------------------------------------------------------------
# Strict boundaries
# ---------------------------------------------------------------------------


class StrictBoundariesTest(CurationRoundTestCase):
    def test_round_never_modifies_a_manifest_or_store(self) -> None:
        stores = self.base / "stores" / "OGS-00001"
        stores.mkdir(parents=True)
        (stores / "analyses.tsv").write_text("analysis_id\ttrait\n", encoding="utf-8")
        bundle = self.base / "families" / "fam-c" / "releases" / "rel-c"
        bundle.mkdir(parents=True)
        (bundle / "analyses.tsv").write_text("analysis_id\ttrait\n", encoding="utf-8")
        (bundle / "release.yaml").write_text("store_family_id: fam-c\n", encoding="utf-8")

        before = snapshot(self.base)
        self._run_round()
        after = snapshot(self.base)

        # The only paths that may change are the Reference Resource directory
        # and the round work directory.
        changed = {
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        }
        allowed_prefixes = (
            str(self.resource_dir.relative_to(self.base)),
            str(self.work_dir.relative_to(self.base)),
        )
        for path in changed:
            self.assertTrue(
                path.startswith(allowed_prefixes),
                f"unexpected modification outside the resource/work dirs: {path}",
            )
        self.assertIn("stores/OGS-00001/analyses.tsv", before)
        self.assertIn("families/fam-c/releases/rel-c/analyses.tsv", before)

    def test_round_over_the_real_ukb_b_manifest_touches_nothing_real(self) -> None:
        """A no-match index leaves every real label unmapped, by design."""
        real_manifest = curation_round.DEFAULT_UKB_B_MANIFEST
        real_store = REPO_ROOT / "stores" / "OGS-00001" / "analyses.tsv"
        manifest_before = real_manifest.read_bytes()
        store_before = real_store.read_bytes()

        # An index that matches nothing: every label has no candidate, so
        # nothing is promoted, queued, or forced to a term.
        empty_obo = self.base / "empty.obo"
        empty_obo.write_text(
            "format-version: 1.2\nontology: efo\n\n[Term]\n"
            "id: EFO:9999999\nname: qzxwvutrplmnb\n",
            encoding="utf-8",
        )
        empty_index = self.base / "empty.index.json"
        write_index(build_index_from_obo(empty_obo, RELEASE), empty_index)

        result = curation_round.run_curation_round(
            manifests=[real_manifest],
            index_path=empty_index,
            chooser=StubChooser({}),
            resource_dir=self.resource_dir,
            work_dir=self.work_dir,
            report_format="tsv",
        )

        self.assertEqual(result.coverage.rows_added, 0)
        self.assertEqual(result.coverage.analyses_resolved, 0)
        # Every distinct work-queue label retrieved no candidate and so stayed
        # unmapped by design.
        distinct_labels = len(gap_scan.scan_manifests([real_manifest]))
        self.assertGreater(distinct_labels, 0)
        self.assertEqual(result.coverage.no_candidate_count, distinct_labels)
        self.assertEqual(real_manifest.read_bytes(), manifest_before)
        self.assertEqual(real_store.read_bytes(), store_before)

    def test_coverage_cli_is_read_only_over_the_real_manifest(self) -> None:
        real_manifest = curation_round.DEFAULT_UKB_B_MANIFEST
        before = real_manifest.read_bytes()
        empty_mapping = self.base / "empty-mapping.tsv"
        write_table(empty_mapping, list(MAPPING_COLUMNS), [])

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = coverage.main(
                [
                    "--manifests", str(real_manifest),
                    "--mapping", str(empty_mapping),
                    "--format", "tsv",
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(real_manifest.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
