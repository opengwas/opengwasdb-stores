#!/usr/bin/env python3
"""Tests for curation.recall (issue #165).

The user-visible contract under test is that the recall report scores retrieval
against the source-provided validation set without leaking the ground-truth
ontology id into shortlist generation, breaks recall down by stratum and
shortlist size, excludes obsolete terms from scoring, enumerates misses, and
always states the ukb-b stratum-gap caveat.

A recall number that is computed against a leaked ground truth, or that hides
the stratum gap, would be read as a promise about ukb-b retrieval that the
validation set cannot make.

Verifies:
- the validation set and shortlist TSVs are read and validated;
- a known correct id inside the shortlist is a hit, outside it is a miss, at
  every configured size;
- recall is reported per stratum and in aggregate;
- obsolete pairs are excluded from scoring and counted;
- misses are enumerated per size with the rank at which the id was found;
- every rendered format states the ukb-b stratum-gap disclaimer;
- the CLI scores against an index or pre-generated shortlists and writes a
  report to stdout or `--output`.
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

from curation import recall
from curation.harvest import (
    STRATUM_ANALYTE_MEASUREMENT,
    STRATUM_DISEASE,
    STRATUM_OTHER,
)
from curation.ontology import build_index_from_obo, write_index
from curation.recall import (
    STRATUM_GAP_DISCLAIMER,
    RecallError,
    ValidationPair,
    evaluate_recall,
    parse_sizes,
    read_shortlists,
    read_validation,
    render_markdown,
    render_report,
    render_text,
    render_tsv,
)

FIXTURE_OBO = """
format-version: 1.2
ontology: efo

[Term]
id: EFO:0004340
name: Body mass index
def: "A measurement of body mass index." []

[Term]
id: EFO:0004518
name: systolic blood pressure
"""

VALIDATION_COLUMNS = [
    "trait_label",
    "ontology_id",
    "ontology_label",
    "stratum",
    "store_families",
    "is_obsolete",
]


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(col, "") for col in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture_index(release: str = "efo/vfixture"):
    import tempfile as _tempfile

    tmp = _tempfile.NamedTemporaryFile("w", suffix=".obo", delete=False, encoding="utf-8")
    try:
        tmp.write(FIXTURE_OBO)
        tmp.close()
        return build_index_from_obo(Path(tmp.name), release)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def pair(label: str, ontology_id: str, stratum: str, obsolete: bool = False) -> ValidationPair:
    return ValidationPair(
        trait_label=label,
        ontology_id=ontology_id,
        ontology_label=f"label for {ontology_id}",
        stratum=stratum,
        store_families=("family",),
        is_obsolete=obsolete,
    )


# A deterministic shortlist map: the target id sits at a known rank.
SHORTLISTS: dict[str, tuple[str, ...]] = {
    "alpha": ("EFO:1", "EFO:2", "EFO:3"),
    "beta": ("EFO:9", "EFO:8"),
    "gamma": ("EFO:5", "EFO:6", "EFO:7", "EFO:4"),
}


def scored_pairs() -> list[ValidationPair]:
    return [
        pair("alpha", "EFO:1", STRATUM_ANALYTE_MEASUREMENT),
        pair("alpha", "EFO:3", STRATUM_DISEASE),
        pair("beta", "EFO:7", STRATUM_DISEASE),
        pair("gamma", "EFO:4", STRATUM_ANALYTE_MEASUREMENT),
    ]


class TestReadValidation(unittest.TestCase):
    """The harvest TSV is the scoring input and is validated as such."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reads_rows_and_parses_fields(self) -> None:
        path = self.td / "validation.tsv"
        write_tsv(
            path,
            VALIDATION_COLUMNS,
            [{
                "trait_label": "Carnitine levels",
                "ontology_id": "EFO:0010469",
                "ontology_label": "carnitine measurement",
                "stratum": STRATUM_ANALYTE_MEASUREMENT,
                "store_families": "metabolome-plasma-2023,pqtl-interval-2018",
                "is_obsolete": "true",
            }],
        )
        pairs = read_validation(path)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].trait_label, "Carnitine levels")
        self.assertEqual(pairs[0].store_families, ("metabolome-plasma-2023", "pqtl-interval-2018"))
        self.assertTrue(pairs[0].is_obsolete)

    def test_missing_required_column_raises(self) -> None:
        path = self.td / "bad.tsv"
        write_tsv(path, ["trait_label"], [{"trait_label": "x"}])
        with self.assertRaises(RecallError):
            read_validation(path)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(RecallError):
            read_validation(self.td / "nope.tsv")


class TestReadShortlists(unittest.TestCase):
    """Pre-generated blind shortlists are grouped and rank-ordered."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_groups_and_orders_by_rank(self) -> None:
        path = self.td / "shortlists.tsv"
        write_tsv(
            path,
            ["trait_label", "shortlist_rank", "ontology_id"],
            [
                {"trait_label": "alpha", "shortlist_rank": "2", "ontology_id": "EFO:2"},
                {"trait_label": "alpha", "shortlist_rank": "1", "ontology_id": "EFO:1"},
                {"trait_label": "beta", "shortlist_rank": "1", "ontology_id": "EFO:9"},
            ],
        )
        shortlists = read_shortlists(path)
        self.assertEqual(shortlists["alpha"], ("EFO:1", "EFO:2"))
        self.assertEqual(shortlists["beta"], ("EFO:9",))

    def test_missing_column_raises(self) -> None:
        path = self.td / "bad.tsv"
        write_tsv(path, ["trait_label", "ontology_id"], [{"trait_label": "x", "ontology_id": "y"}])
        with self.assertRaises(RecallError):
            read_shortlists(path)


class TestEvaluateWithShortlists(unittest.TestCase):
    """Hits and misses are known exactly against a deterministic shortlist."""

    def setUp(self) -> None:
        self.result = evaluate_recall(
            scored_pairs(), shortlists=SHORTLISTS, sizes=(1, 2, 4)
        )

    def test_scored_and_stratum_totals(self) -> None:
        self.assertEqual(self.result.scored, 4)
        self.assertEqual(self.result.excluded_obsolete, 0)
        self.assertEqual(self.result.stratum_totals[STRATUM_ANALYTE_MEASUREMENT], 2)
        self.assertEqual(self.result.stratum_totals[STRATUM_DISEASE], 2)

    def test_hits_and_recall_per_stratum_and_size(self) -> None:
        analyte = self.result.stratum_hits[STRATUM_ANALYTE_MEASUREMENT]
        disease = self.result.stratum_hits[STRATUM_DISEASE]
        self.assertEqual([analyte[1], analyte[2], analyte[4]], [1, 1, 2])
        self.assertEqual([disease[1], disease[2], disease[4]], [0, 0, 1])
        self.assertAlmostEqual(self.result.recall_for(STRATUM_ANALYTE_MEASUREMENT, 1), 0.5)
        self.assertAlmostEqual(self.result.recall_for(STRATUM_DISEASE, 4), 0.5)

    def test_aggregate_hits_and_recall(self) -> None:
        self.assertEqual([self.result.aggregate_hits[size] for size in (1, 2, 4)], [1, 1, 3])
        self.assertAlmostEqual(self.result.aggregate_recall(1), 0.25)
        self.assertAlmostEqual(self.result.aggregate_recall(4), 0.75)

    def test_empty_stratum_recall_is_zero(self) -> None:
        self.assertEqual(self.result.recall_for("does-not-exist", 1), 0.0)

    def test_both_sources_or_neither_is_an_error(self) -> None:
        with self.assertRaises(RecallError):
            evaluate_recall(scored_pairs(), sizes=(1,))
        with self.assertRaises(RecallError):
            evaluate_recall(scored_pairs(), index=_fixture_index(), shortlists=SHORTLISTS)

    def test_bad_sizes_raise(self) -> None:
        with self.assertRaises(RecallError):
            evaluate_recall(scored_pairs(), shortlists=SHORTLISTS, sizes=(0,))


class TestMissEnumeration(unittest.TestCase):
    """Misses are enumerated per size with the rank at which they were found."""

    def setUp(self) -> None:
        self.result = evaluate_recall(
            scored_pairs(), shortlists=SHORTLISTS, sizes=(1, 2, 4)
        )

    def test_misses_at_top_one(self) -> None:
        misses = {miss.ontology_id: miss for miss in self.result.misses[1]}
        self.assertEqual(set(misses), {"EFO:3", "EFO:7", "EFO:4"})
        self.assertEqual(misses["EFO:3"].found_rank, 3)
        self.assertEqual(misses["EFO:4"].found_rank, 4)
        self.assertIsNone(misses["EFO:7"].found_rank)

    def test_misses_shrink_as_the_shortlist_grows(self) -> None:
        self.assertEqual(len(self.result.misses[2]), 3)
        self.assertEqual([miss.ontology_id for miss in self.result.misses[4]], ["EFO:7"])

    def test_misses_carry_stratum_and_target(self) -> None:
        miss = self.result.misses[4][0]
        self.assertEqual(miss.stratum, STRATUM_DISEASE)
        self.assertEqual(miss.ontology_id, "EFO:7")
        self.assertEqual(miss.trait_label, "beta")


class TestObsoleteExclusion(unittest.TestCase):
    """Retired terms are not scored as retrieval failures."""

    def test_obsolete_pair_is_excluded_and_counted(self) -> None:
        pairs = scored_pairs() + [pair("legacy", "EFO:9999001", STRATUM_DISEASE, obsolete=True)]
        result = evaluate_recall(pairs, shortlists=SHORTLISTS, sizes=(1, 4))
        self.assertEqual(result.scored, 4)
        self.assertEqual(result.excluded_obsolete, 1)
        # The obsolete pair's stratum count and misses are untouched.
        self.assertEqual(result.stratum_totals[STRATUM_DISEASE], 2)
        self.assertNotIn(
            "EFO:9999001",
            {miss.ontology_id for miss in result.misses[1]},
        )


class TestEvaluateWithIndex(unittest.TestCase):
    """Shortlists generated blind from the labels score an exact hit."""

    def setUp(self) -> None:
        self.index = _fixture_index()

    def test_exact_label_is_a_top_one_hit(self) -> None:
        pairs = [
            pair("Body mass index", "EFO:0004340", STRATUM_ANALYTE_MEASUREMENT),
            pair("qwertyuiop asdfghjkl", "EFO:0000000", STRATUM_DISEASE),
        ]
        result = evaluate_recall(pairs, index=self.index, sizes=(1, 5))
        self.assertEqual(result.ontology_release, "efo/vfixture")
        self.assertEqual(result.aggregate_hits[1], 1)
        self.assertEqual([miss.ontology_id for miss in result.misses[5]], ["EFO:0000000"])

    def test_generation_is_blind_to_the_known_id(self) -> None:
        # A pair whose label matches nothing must not retrieve its own target,
        # even though the target id exists in the index.
        pairs = [pair("qwertyuiop asdfghjkl", "EFO:0004340", STRATUM_DISEASE)]
        result = evaluate_recall(pairs, index=self.index, sizes=(1, 10))
        self.assertEqual(result.aggregate_hits[10], 0)
        self.assertEqual(len(result.misses[10]), 1)


class TestDisclaimer(unittest.TestCase):
    """Every format states the ukb-b stratum-gap caveat plainly."""

    def setUp(self) -> None:
        self.result = evaluate_recall(scored_pairs(), shortlists=SHORTLISTS, sizes=(1, 4))

    def test_constant_names_the_gap(self) -> None:
        self.assertIn("ukb-b", STRATUM_GAP_DISCLAIMER)
        self.assertIn("disease", STRATUM_GAP_DISCLAIMER)
        self.assertIn("procedure", STRATUM_GAP_DISCLAIMER)
        self.assertIn("administrative", STRATUM_GAP_DISCLAIMER)
        self.assertIn("no validation stratum", STRATUM_GAP_DISCLAIMER.lower())

    def test_all_formats_include_the_disclaimer(self) -> None:
        for fmt in ("text", "markdown", "tsv"):
            with self.subTest(fmt=fmt):
                self.assertIn(STRATUM_GAP_DISCLAIMER, render_report(self.result, fmt))


class TestRenderFormats(unittest.TestCase):
    """The report formats are clean and self-describing."""

    def setUp(self) -> None:
        self.result = evaluate_recall(scored_pairs(), shortlists=SHORTLISTS, sizes=(1, 4))

    def test_text_report_sections(self) -> None:
        text = render_text(self.result)
        self.assertIn("Retrieval recall for the source-provided validation set", text)
        self.assertIn("Recall by stratum", text)
        self.assertIn("aggregate", text)
        self.assertIn("Misses at top-4", text)
        self.assertIn("EFO:7", text)

    def test_markdown_report_tables(self) -> None:
        markdown = render_markdown(self.result)
        self.assertTrue(markdown.startswith("# Retrieval recall"))
        self.assertIn("| stratum | n | top-1 | top-4 |", markdown)
        self.assertIn("## Misses at top-4", markdown)

    def test_tsv_report_has_comment_metadata_and_records(self) -> None:
        tsv = render_tsv(self.result)
        self.assertIn("# disclaimer:", tsv)
        self.assertIn("\t".join(recall.MISS_COLUMNS), tsv)
        records = [line.split("\t")[0] for line in tsv.splitlines() if not line.startswith("#")][1:]
        self.assertIn("recall", records)
        self.assertIn("miss", records)

    def test_unknown_format_raises(self) -> None:
        with self.assertRaises(RecallError):
            render_report(self.result, "pdf")


class TestParseSizes(unittest.TestCase):
    def test_parses(self) -> None:
        self.assertEqual(parse_sizes("1,5,10,20"), (1, 5, 10, 20))

    def test_rejects_zero_and_garbage(self) -> None:
        import argparse

        with self.assertRaises(argparse.ArgumentTypeError):
            parse_sizes("0")
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_sizes("abc")


class TestCli(unittest.TestCase):
    """The command scores an index or shortlists and writes a report."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.validation_path = self.td / "validation.tsv"
        write_tsv(
            self.validation_path,
            VALIDATION_COLUMNS,
            [
                {"trait_label": "alpha", "ontology_id": "EFO:1",
                 "ontology_label": "one", "stratum": STRATUM_ANALYTE_MEASUREMENT,
                 "store_families": "metabolome", "is_obsolete": "false"},
                {"trait_label": "beta", "ontology_id": "EFO:7",
                 "ontology_label": "seven", "stratum": STRATUM_DISEASE,
                 "store_families": "gwas", "is_obsolete": "false"},
                {"trait_label": "legacy", "ontology_id": "EFO:9999001",
                 "ontology_label": "legacy obsolete trait", "stratum": STRATUM_DISEASE,
                 "store_families": "pqtl", "is_obsolete": "true"},
            ],
        )
        self.shortlists_path = self.td / "shortlists.tsv"
        write_tsv(
            self.shortlists_path,
            ["trait_label", "shortlist_rank", "ontology_id"],
            [
                {"trait_label": "alpha", "shortlist_rank": "1", "ontology_id": "EFO:1"},
                {"trait_label": "beta", "shortlist_rank": "1", "ontology_id": "EFO:9"},
                {"trait_label": "beta", "shortlist_rank": "2", "ontology_id": "EFO:7"},
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = recall.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_scores_shortlists_to_output(self) -> None:
        output = self.td / "out" / "report.md"
        code, out, err = self.run_cli(
            [
                "--validation", str(self.validation_path),
                "--shortlists", str(self.shortlists_path),
                "--sizes", "1,2",
                "--format", "markdown",
                "--output", str(output),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        text = output.read_text(encoding="utf-8")
        self.assertIn(STRATUM_GAP_DISCLAIMER, text)
        # alpha hits at both sizes; beta hits only at size 2.
        self.assertIn("| analyte_measurement | 1 | 100.0% | 100.0% |", text)

    def test_scores_index_to_stdout(self) -> None:
        index_path = self.td / "index.json"
        write_index(_fixture_index(), index_path)
        index_validation = self.td / "index-validation.tsv"
        write_tsv(
            index_validation,
            VALIDATION_COLUMNS,
            [{"trait_label": "Body mass index", "ontology_id": "EFO:0004340",
              "ontology_label": "body mass index", "stratum": STRATUM_ANALYTE_MEASUREMENT,
              "store_families": "metabolome", "is_obsolete": "false"}],
        )
        code, out, err = self.run_cli(
            ["--validation", str(index_validation), "--index", str(index_path),
             "--sizes", "1", "--format", "tsv"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("# disclaimer:", out)
        self.assertIn("recall\taggregate\t1\t1\t1", out)

    def test_missing_validation_exits_one(self) -> None:
        code, out, err = self.run_cli(
            ["--validation", str(self.td / "nope.tsv"),
             "--shortlists", str(self.shortlists_path)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("recall: error:", err)

    def test_missing_source_is_an_argparse_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            recall.build_parser().parse_args(["--validation", str(self.validation_path)])


if __name__ == "__main__":
    unittest.main()
