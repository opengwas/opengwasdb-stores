#!/usr/bin/env python3
"""Five dbGaP MVP Selections and the retired partition's bound counts (#290).

The MVP must split into five disjoint ancestry Selections. Its adapter writes
the post-split `store_key`, so each Selection is a rule on a canonical column;
it also declares `expect_rows`, which turns the retired partition's bound counts
into a check that fails by name when the Source Metadata Table drifts.

Run from the repository root:
    python3 tests/dbgap-mvp/test_selection_partition.py
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

import yaml

from manifest_generation.acquisition_manifest import ACQUISITION_MANIFEST
from manifest_generation import inventory
from manifest_generation.connector_tsv import write_connector
from manifest_generation.inventory import freeze_release
from manifest_generation.release_config import load_release_config
from manifest_generation.selection import SelectionError, parse_selection, select
from manifest_generation.source_inventory import selection_partition_problems
from manifest_generation.source_metadata_table import SOURCE_METADATA_TABLE, load_source_metadata_table

import test_end_to_end as end_to_end

#: bucket -> (store_key, expected bound counts, number of fixture rows).
BUCKETS = (
    ("EUR", "dense__pmid-39024449__European", 1_669, 2),
    ("AFR", "dense__pmid-39024449__African", 1_368, 2),
    ("AMR", "dense__pmid-39024449__Hispanic-or-Latin-American", 1_053, 1),
    ("EAS", "dense__pmid-39024449__East-Asian", 241, 1),
    ("META", "dense__pmid-39024449__Multi-ancestry", 1_691, 1),
)


def build_table(root: Path) -> Path:
    rows: list[dict[str, str]] = []
    number = 0
    for _bucket, store_key, _bound, count in BUCKETS:
        for _ in range(count):
            number += 1
            rows.append({
                "source_analysis_id": f"GCST9000{number:04d}",
                "source_ancestry_label": _bucket,
                "store_key": store_key,
                "study_design": "quantitative",
                "sample_size": "1000",
            })
    path = root / "source-metadata.tsv"
    write_connector(path, SOURCE_METADATA_TABLE, SOURCE_METADATA_TABLE.columns, rows)
    return path


class SelectionPartitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dbgap-selection-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.table = load_source_metadata_table(build_table(self.root))

    def selections(self) -> list[tuple[str, object]]:
        return [
            (bucket, parse_selection({"where": {"store_key": store_key}, "expect_rows": bound}))
            for bucket, store_key, bound, _count in BUCKETS
        ]

    def test_five_selections_are_disjoint_and_cover_the_table(self) -> None:
        chosen: list[str] = []
        for bucket, selection in self.selections():
            # The fixture's expected count is its row count, not the real bound.
            count = next(count for name, _key, _bound, count in BUCKETS if name == bucket)
            selection = parse_selection({"where": {"store_key": selection.rule[0][1][0]}, "expect_rows": count})
            selected = select(self.table, selection)
            self.assertEqual(len(selected), count, bucket)
            chosen.extend(row["source_analysis_id"] for row in selected)
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertEqual(set(chosen), set(self.table.analysis_ids))

    def test_the_bound_count_is_part_of_the_selection_declaration(self) -> None:
        selection = parse_selection({"where": {"store_key": BUCKETS[0][1]}, "expect_rows": 1_669})
        self.assertEqual(selection.expect_rows, 1_669)
        self.assertEqual(
            selection.as_dict(),
            {"where": {"store_key": BUCKETS[0][1]}, "expect_rows": 1_669},
        )

    def test_a_count_that_drifts_fails_by_name(self) -> None:
        selection = parse_selection({"where": {"store_key": BUCKETS[0][1]}, "expect_rows": 1_668})
        with self.assertRaisesRegex(SelectionError, "EUR|European|1668|1669"):
            select(self.table, selection)

    def test_expect_rows_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, "1669", True, 1.5):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(SelectionError, "expect_rows"):
                    parse_selection({"where": {"store_key": BUCKETS[0][1]}, "expect_rows": bad})

    def test_the_old_forms_are_unchanged(self) -> None:
        self.assertIsNone(parse_selection({"all": True}).expect_rows)
        self.assertEqual(parse_selection({"analyses": ["GCST90000001"]}).form, "analyses")


class MultiConfigPreflightTests(unittest.TestCase):
    """Five Selections of one collection must partition it, or fail by name."""

    def test_disjoint_selections_over_one_collection_are_accepted(self) -> None:
        releases = [
            ("EUR", "dbgap-phs002453", ["A", "B"]),
            ("AFR", "dbgap-phs002453", ["C"]),
        ]
        self.assertEqual(
            selection_partition_problems(releases, collection_ids={"A", "B", "C"}), []
        )

    def test_an_analysis_in_two_selections_is_named_with_both_labels(self) -> None:
        problems = selection_partition_problems([
            ("EUR", "dbgap-phs002453", ["A", "B"]),
            ("META", "dbgap-phs002453", ["B"]),
        ])
        self.assertTrue(any("B" in problem and "EUR" in problem and "META" in problem for problem in problems))

    def test_selections_of_two_collections_are_refused(self) -> None:
        problems = selection_partition_problems([
            ("EUR", "dbgap-phs002453", ["A"]),
            ("other", "gwas-catalog-ssf", ["B"]),
        ])
        self.assertTrue(any("one preflight is one collection" in problem for problem in problems))

    def test_a_collection_row_no_selection_takes_is_named(self) -> None:
        problems = selection_partition_problems(
            [("EUR", "dbgap-phs002453", ["A"])], collection_ids={"A", "B"}
        )
        self.assertTrue(any("selected by no Selection" in problem and "B" in problem for problem in problems))


class CommandPreflightTests(unittest.TestCase):
    """The multi-config preflight proves completeness through the command (#290 review).

    Three Selections part the fixture collection between them. Leaving one out
    must fail, naming the Analyses no Selection covers; so must a collection
    row no Selection takes, which no ``expect_rows`` bound can catch.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """One fixture collection and three frozen Selections for the whole class."""
        cls.tmp = tempfile.TemporaryDirectory(prefix="dbgap-partition-cli-")
        cls.root = Path(cls.tmp.name)
        cls.config_path = end_to_end.build(cls.root)
        cls.document = yaml.safe_load(cls.config_path.read_text(encoding="utf-8"))
        cls.table = Path(cls.document["source_collection"]["connectors"]["source_metadata_table"])
        cls.acquisition = Path(
            cls.document["source_collection"]["connectors"]["acquisition_manifest"]
        )
        cls.add_bucket(cls, "GCST90001003", "dense__pmid-39024449__Multi-ancestry")
        cls.configs = {
            name: cls.write_config(cls, name, [analysis_id])
            for name, analysis_id in (
                ("one", "GCST90001001"),
                ("two", "GCST90001002"),
                ("three", "GCST90001003"),
            )
        }
        for path in cls.configs.values():
            freeze_release(load_release_config(path, end_to_end.REPO_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def add_bucket(self, analysis_id: str, store_key: str) -> None:
        """A further bucket of the collection: a converted file and its two connector rows."""
        data = self.root / "mirror" / f"{analysis_id}.tsv.gz"
        data.write_bytes(f"fixture body for {analysis_id}\n".encode())
        sidecar = data.with_name(data.name + "-meta.yaml")
        sidecar.write_text("genome_assembly: GRCh38\n", encoding="utf-8")
        with self.table.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        template = dict(rows[0])
        template.update({
            "source_analysis_id": analysis_id,
            "source_trait_label": f"trait {analysis_id}",
            "store_key": store_key,
        })
        write_connector(
            self.table,
            SOURCE_METADATA_TABLE,
            SOURCE_METADATA_TABLE.columns,
            rows + [template],
        )
        with self.acquisition.open(newline="", encoding="utf-8") as handle:
            entries = list(csv.DictReader(handle, delimiter="\t"))
        entry = dict(entries[0])
        entry.update({
            "source_analysis_id": analysis_id,
            "location": str(data),
            "bytes": str(data.stat().st_size),
            "sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
            "sidecar_location": str(sidecar),
            "sidecar_bytes": str(sidecar.stat().st_size),
            "original_location": "",
            "original_sha256": "",
            "reformat_tool": "",
        })
        write_connector(self.acquisition, ACQUISITION_MANIFEST, ACQUISITION_MANIFEST.columns,
                        entries + [entry])

    def write_config(self, name: str, analyses: list[str]) -> Path:
        document = json.loads(json.dumps(self.document))
        document["label"] = f"fixture-mvp-{name}"
        document["source_collection"]["inventory"] = {
            "snapshot_id": f"fixture-mvp-{name}",
            "directory": str(self.root / "inventory"),
        }
        document["selection"] = {"analyses": analyses, "expect_rows": len(analyses)}
        path = self.root / f"release-{name}.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    def arguments(self, *names: str) -> list[str]:
        command = ["--repo-root", str(end_to_end.REPO_ROOT), "preflight"]
        for name in names:
            command += ["--config", str(self.configs[name])]
        return command + ["--work-root", str(self.root / "work")]

    def preflight(self, *names: str) -> types.SimpleNamespace:
        """Run the command in-process: the CLI path, without a second interpreter per case."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = inventory.main(self.arguments(*names))
        return types.SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())

    def preflight_as_command(self, *names: str) -> subprocess.CompletedProcess:
        """One subprocess run, so the command seam itself is covered too."""
        return subprocess.run(
            [sys.executable, "-m", "manifest_generation.inventory", *self.arguments(*names)],
            cwd=str(end_to_end.REPO_ROOT), capture_output=True, text=True, check=False,
        )

    def test_three_selections_that_part_the_collection_pass(self) -> None:
        result = self.preflight_as_command("one", "two", "three")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Selections partition", result.stdout)

    def test_leaving_a_bucket_out_fails_naming_the_analyses_it_covers(self) -> None:
        result = self.preflight("one", "two")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("GCST90001003", result.stderr)
        self.assertIn("selected by no Selection", result.stderr)
        self.assertNotIn("Selections partition", result.stdout)

    def test_a_collection_row_no_selection_takes_fails_by_name(self) -> None:
        """No expect_rows bound can see an added row: only the collection's intent can."""
        saved = {path: path.read_bytes() for path in (self.table, self.acquisition)}
        provenance_path = self.root / "inventory" / "fixture-mvp-one.meta.yaml"
        saved[provenance_path] = provenance_path.read_bytes()
        self.addCleanup(
            lambda: [path.write_bytes(data) for path, data in saved.items()]
        )
        self.add_bucket("GCST90001004", "dense__pmid-39024449__East-Asian")
        # The collection's recorded intent is the table as it now is.
        provenance = yaml.safe_load(provenance_path.read_text(encoding="utf-8"))
        for key, path in (
            ("source_metadata_table", self.table),
            ("acquisition_manifest", self.acquisition),
        ):
            original = provenance[key]["original"]
            original["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            original["rows"] = len(path.read_text(encoding="utf-8").splitlines()) - 1
        provenance_path.write_text(yaml.safe_dump(provenance, sort_keys=False), encoding="utf-8")

        result = self.preflight("one", "two", "three")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("GCST90001004", result.stderr)
        self.assertIn("selected by no Selection", result.stderr)


if __name__ == "__main__":
    unittest.main()
