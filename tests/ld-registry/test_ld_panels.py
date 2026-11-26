#!/usr/bin/env python3
"""The LD panels in `ld-resources.tsv` (#540, epic #496; docs/spec/ld-store-registry.md).

Four seams, and only these:

1. reading a panel: a `kind: ld_panel` `resource.yaml` gives one row per
   declared ancestry, with that ancestry's id and counts; a declared ancestry
   with no `ld_ids` entry is refused, naming the file and the ancestry;
2. a missing count is `None`, then an empty cell, never `0`;
3. a panel id equal to a record's id fails the index;
4. the real declarations give the draft's OGL-00013 to OGL-00018.

No test reads a panel directory. The fixtures are small declarations written
here; expected values are literals from #496's draft rows (the counts there
were measured on the host by `tools/ld-panel/measure_panel.py`, apart from
`bytes`, which the draft took with `du -sb` and so includes directory entries).

Run from the repository root:
    python3 tests/ld-registry/test_ld_panels.py
"""

from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from ld_registry import index as ld_index
from ld_registry import panels, rows

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORD = Path(__file__).resolve().parent / "fixtures" / "record-ogl-00001.yaml"
REFERENCE_RESOURCES = Path("resources", "reference-resources")
USED_FOR = "reference completion (opengwasdb complete-dense)"


def multi_declaration() -> dict[str, Any]:
    """A multi-ancestry panel in the shape of `hgdp1kgp-hg38-ld`, two ancestries."""
    return {
        "resource_id": "pair-ld",
        "label": "A pair of panels",
        "kind": "ld_panel",
        "ancestry": ["AFR", "EAS"],
        "genome_build": "GRCh38",
        "location": "/ref/pair/panel",
        "ld": {
            "representation": "eigendecomposition",
            "components_cumvar_target": 0.99,
            "n_variants": {"AFR": 1000, "EAS": 2000},
            "n_blocks": {"AFR": 10, "EAS": 20},
            "bytes": {"AFR": 111, "EAS": 222},
        },
        "ld_ids": {"AFR": "OGL-00040", "EAS": "OGL-00041"},
        "source": {"cohort": "A cohort", "n_individuals": {"AFR": 804, "EAS": 817}},
        "status": "available",
        "used_for": USED_FOR,
        "licence": "to confirm",
    }


def single_declaration() -> dict[str, Any]:
    """A single-ancestry panel that declares its block count but not its size, as `ukb-hg38-eur-ld` does."""
    return {
        "resource_id": "solo-ld",
        "label": "One panel",
        "kind": "ld_panel",
        "ancestry": "EUR",
        "genome_build": "GRCh38",
        "location": "/ref/solo",
        "ancestry_dir": "EUR",
        "physical_directory": "ukb",
        "ld": {"representation": "eigendecomposition", "n_blocks": 1357},
        "ld_ids": {"EUR": "OGL-00042"},
        "source": {"cohort": "Another cohort", "n_individuals": 49766},
        "status": "available",
        "used_for": USED_FOR,
        "licence": "restricted: somebody",
    }


def write_resource(repo: Path, document: dict[str, Any]) -> Path:
    path = repo / REFERENCE_RESOURCES / document["resource_id"] / "resource.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


class PanelCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.resources = self.repo / REFERENCE_RESOURCES

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def by_id(self) -> dict[str, dict[str, Any]]:
        return {row["ld_id"]: row for row in panels.panel_rows(self.resources)}


class TestReadingAPanel(PanelCase):
    """Seam 1."""

    def test_the_fixture_declares_two_ancestries(self) -> None:
        self.assertEqual(multi_declaration()["ancestry"], ["AFR", "EAS"])
        self.assertEqual(set(multi_declaration()["ld_ids"]), {"AFR", "EAS"})

    def test_a_multi_ancestry_panel_gives_one_row_per_ancestry_with_its_own_id_and_counts(self) -> None:
        write_resource(self.repo, multi_declaration())
        found = self.by_id()
        self.assertEqual(sorted(found), ["OGL-00040", "OGL-00041"])
        afr, eas = found["OGL-00040"], found["OGL-00041"]
        self.assertEqual(
            (afr["label"], afr["ancestry"], afr["n"], afr["n_variants"], afr["n_blocks"], afr["store_bytes"]),
            ("pair-ld (AFR)", "AFR", 804, 1000, 10, 111),
        )
        self.assertEqual(
            (eas["label"], eas["ancestry"], eas["n"], eas["n_variants"], eas["n_blocks"], eas["store_bytes"]),
            ("pair-ld (EAS)", "EAS", 817, 2000, 20, 222),
        )
        self.assertEqual((afr["location"], eas["location"]), ("/ref/pair/panel/AFR", "/ref/pair/panel/EAS"))
        self.assertEqual(list(afr), list(rows.COLUMNS))

    def test_a_panel_row_carries_the_declaration(self) -> None:
        write_resource(self.repo, multi_declaration())
        row = self.by_id()["OGL-00040"]
        self.assertEqual(
            (row["kind"], row["provenance"], row["status"], row["used_for"], row["reference_assembly"], row["licence"]),
            ("ld_panel", "genotypes", "available", USED_FOR, "GRCh38", "to confirm"),
        )
        self.assertEqual(row["source_panel"], "A cohort")
        self.assertEqual(row["representation"], "per-block eigendecomposition (.ldeig.npz), 0.99 cumulative variance")
        self.assertEqual(row["record"], "resources/reference-resources/pair-ld/resource.yaml")

    def test_a_single_ancestry_panel_is_named_by_its_resource_and_located_through_its_alias(self) -> None:
        write_resource(self.repo, single_declaration())
        row = self.by_id()["OGL-00042"]
        self.assertEqual(row["label"], "solo-ld")
        self.assertEqual(row["location"], "/ref/solo/EUR (-> ukb)")
        self.assertEqual(row["n"], 49766)
        self.assertEqual(row["representation"], "per-block eigendecomposition (.ldeig.npz)")

    def test_a_missing_ld_ids_entry_is_refused_naming_the_file_and_the_ancestry(self) -> None:
        document = multi_declaration()
        del document["ld_ids"]["EAS"]
        path = write_resource(self.repo, document)
        with self.assertRaises(panels.PanelError) as caught:
            panels.panel_rows(self.resources)
        self.assertIn(str(path), str(caught.exception))
        self.assertIn("EAS", str(caught.exception))
        self.assertNotIn("AFR", str(caught.exception).replace(str(path), ""))

    def test_a_declaration_without_ld_ids_is_refused_naming_each_ancestry(self) -> None:
        document = multi_declaration()
        del document["ld_ids"]
        path = write_resource(self.repo, document)
        with self.assertRaises(panels.PanelError) as caught:
            panels.panel_rows(self.resources)
        for needle in (str(path), "AFR", "EAS"):
            self.assertIn(needle, str(caught.exception))

    def test_an_id_that_is_not_an_ld_id_or_is_used_twice_is_refused(self) -> None:
        document = multi_declaration()
        document["ld_ids"]["AFR"] = "OGL-1"
        write_resource(self.repo, document)
        with self.assertRaises(panels.PanelError) as caught:
            panels.panel_rows(self.resources)
        self.assertIn("OGL-1", str(caught.exception))
        document["ld_ids"] = {"AFR": "OGL-00040", "EAS": "OGL-00040"}
        write_resource(self.repo, document)
        with self.assertRaises(panels.PanelError) as caught:
            panels.panel_rows(self.resources)
        self.assertIn("OGL-00040", str(caught.exception))

    def test_a_per_ancestry_count_with_a_missing_or_misspelt_ancestry_is_refused(self) -> None:
        for field, section, key in (
            ("n_variants", "ld", "ld.n_variants"),
            ("n_blocks", "ld", "ld.n_blocks"),
            ("bytes", "ld", "ld.bytes"),
            ("n_individuals", "source", "source.n_individuals"),
        ):
            for ancestry, spelling in (("EAS", None), ("EAS", "EsA")):
                with self.subTest(field=key, ancestry=ancestry, spelling=spelling):
                    document = multi_declaration()
                    counts = document[section][field]
                    del counts["EAS"]
                    if spelling:
                        counts[spelling] = 1
                    path = write_resource(self.repo, document)
                    with self.assertRaises(panels.PanelError) as caught:
                        panels.panel_rows(self.resources)
                    for needle in (str(path), key, ancestry, spelling or ancestry):
                        self.assertIn(needle, str(caught.exception))

    def test_a_declaration_with_no_ancestry_or_no_resource_id_is_a_panel_error(self) -> None:
        for field in ("ancestry", "resource_id"):
            with self.subTest(missing=field):
                document = multi_declaration()
                path = write_resource(self.repo, document)
                del document[field]
                path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
                with self.assertRaises(panels.PanelError) as caught:
                    panels.panel_rows(self.resources)
                self.assertIn(field, str(caught.exception))
                self.assertIn(str(path), str(caught.exception))

    def test_a_panel_without_used_for_or_licence_is_refused_by_name(self) -> None:
        for field in ("used_for", "licence"):
            with self.subTest(missing=field):
                document = multi_declaration()
                del document[field]
                path = write_resource(self.repo, document)
                with self.assertRaises(panels.PanelError) as caught:
                    panels.panel_rows(self.resources)
                self.assertIn(field, str(caught.exception))
                self.assertIn(str(path), str(caught.exception))

    def test_index_notes_are_optional(self) -> None:
        write_resource(self.repo, multi_declaration())
        self.assertIsNone(self.by_id()["OGL-00040"]["notes"])

    def test_a_row_is_keyed_to_the_columns_and_a_misspelt_key_is_an_error(self) -> None:
        write_resource(self.repo, multi_declaration())
        self.assertEqual(tuple(self.by_id()["OGL-00040"]), rows.COLUMNS)
        with self.assertRaises(ValueError):
            rows.keyed({"ld_id": "OGL-00040", "n_variantz": 1})

    def test_a_declaration_of_another_kind_is_not_a_panel(self) -> None:
        write_resource(self.repo, {"resource_id": "a-list", "kind": "variant_list", "location": "/x"})
        self.assertEqual(panels.panel_rows(self.resources), [])


class TestTypedCounts(PanelCase):
    """A declared count is a whole number: a text or fractional one would land as TEXT in an INTEGER column."""

    FIELDS = (("ld", "n_variants"), ("ld", "n_blocks"), ("ld", "bytes"), ("source", "n_individuals"))

    def test_a_count_that_is_not_a_whole_number_is_refused_naming_file_field_and_ancestry(self) -> None:
        for section, field in self.FIELDS:
            for bad in ("about 1580", "1,580", 12.5, -3, True):
                with self.subTest(field=f"{section}.{field}", value=bad):
                    document = multi_declaration()
                    document[section][field]["EAS"] = bad
                    path = write_resource(self.repo, document)
                    with self.assertRaises(panels.PanelError) as caught:
                        panels.panel_rows(self.resources)
                    for needle in (str(path), f"{section}.{field}", "EAS", repr(bad)):
                        self.assertIn(needle, str(caught.exception))

    def test_a_single_ancestry_scalar_count_is_checked_too(self) -> None:
        document = single_declaration()
        document["ld"]["n_blocks"] = "1357"
        path = write_resource(self.repo, document)
        with self.assertRaises(panels.PanelError) as caught:
            panels.panel_rows(self.resources)
        self.assertIn(str(path), str(caught.exception))
        self.assertIn("ld.n_blocks", str(caught.exception))
        self.assertIn("EUR", str(caught.exception))

    def test_the_committed_declarations_pass(self) -> None:
        self.assertEqual(len(panels.panel_rows(REPO_ROOT / "resources" / "reference-resources")), 6)


class TestDeclarationFiles(PanelCase):
    def test_it_lists_the_panels_declarations_and_no_other_kind(self) -> None:
        a = write_resource(self.repo, multi_declaration())
        b = write_resource(self.repo, single_declaration())
        other = self.resources / "other" / "resource.yaml"
        other.parent.mkdir()
        other.write_text("kind: something_else\n", encoding="utf-8")
        self.assertEqual(panels.declaration_files(self.resources), [a, b])
        self.assertEqual(panels.declaration_files(self.repo / "absent"), [])


class TestMissingCounts(PanelCase):
    """Seam 2."""

    def test_a_count_the_declaration_does_not_hold_is_none_never_zero(self) -> None:
        write_resource(self.repo, single_declaration())
        row = self.by_id()["OGL-00042"]
        self.assertIsNone(row["n_variants"])
        self.assertIsNone(row["store_bytes"])
        self.assertIsNone(row["genotype_bytes"])

    def test_a_declared_count_is_kept_as_declared(self) -> None:
        write_resource(self.repo, single_declaration())
        self.assertEqual(self.by_id()["OGL-00042"]["n_blocks"], 1357)

    def test_a_missing_count_renders_as_an_empty_cell(self) -> None:
        write_resource(self.repo, single_declaration())
        text = rows.render_tsv([], panels.panel_rows(self.resources))
        (row,) = csv.DictReader(text.splitlines(), delimiter="\t")
        self.assertEqual((row["n_variants"], row["store_bytes"], row["n_blocks"]), ("", "", "1357"))

    def test_an_absent_count_map_is_none_for_every_ancestry(self) -> None:
        document = multi_declaration()
        del document["ld"]["bytes"]
        write_resource(self.repo, document)
        self.assertEqual([r["store_bytes"] for r in self.by_id().values()], [None, None])

    def test_one_scalar_count_in_a_multi_ancestry_declaration_is_refused(self) -> None:
        document = multi_declaration()
        document["ld"]["n_blocks"] = 10
        path = write_resource(self.repo, document)
        with self.assertRaises(panels.PanelError) as caught:
            panels.panel_rows(self.resources)
        self.assertIn(str(path), str(caught.exception))
        self.assertIn("n_blocks", str(caught.exception))


class TestIdCollision(PanelCase):
    """Seam 3."""

    def registry(self, record_id: str) -> Path:
        ld_root = self.repo / "stores" / "ld"
        (ld_root / record_id).mkdir(parents=True)
        document = yaml.safe_load(RECORD.read_text(encoding="utf-8"))
        document["ld_id"] = record_id
        (ld_root / record_id / "record.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return ld_root

    def test_a_panel_id_that_is_a_records_fails_the_index(self) -> None:
        write_resource(self.repo, multi_declaration())
        ld_root = self.registry("OGL-00041")
        with self.assertRaises(ld_index.IdCollision) as caught:
            ld_index.generate_ld_index(ld_root, self.repo, self.resources)
        self.assertIn("OGL-00041", str(caught.exception))
        self.assertIn("pair-ld", str(caught.exception))
        self.assertFalse((self.repo / "ld-resources.tsv").exists(), "nothing is written from a colliding registry")

    def test_distinct_ids_give_one_row_each_in_id_order(self) -> None:
        write_resource(self.repo, multi_declaration())
        ld_root = self.registry("OGL-00001")
        ld_index.generate_ld_index(ld_root, self.repo, self.resources)
        ids = [r["ld_id"] for r in csv.DictReader((self.repo / "ld-resources.tsv").read_text().splitlines(), delimiter="\t")]
        self.assertEqual(ids, ["OGL-00001", "OGL-00040", "OGL-00041"])


# The draft's rows OGL-00013 to OGL-00018 (#496's comment): (label, ancestry, status, n, n_variants, n_blocks).
# Its `bytes` came from `du -sb` and are not compared here; see the report.
DRAFT = {
    "OGL-00013": ("ukb-hg38-eur-ld", "EUR", "available", 49766, None, 1357),
    "OGL-00014": ("hgdp1kgp-hg38-ld (AFR)", "AFR", "available", 804, 14136388, 1580),
    "OGL-00015": ("hgdp1kgp-hg38-ld (EAS)", "EAS", "available", 817, 8728585, 1120),
    "OGL-00016": ("hgdp1kgp-hg38-ld (SAS)", "SAS", "available", 783, 9618254, 1267),
    "OGL-00017": ("hgdp1kgp-hg38-amr-ld", "AMR", "available", 345, 12669255, 1357),
    "OGL-00018": ("hgdp1kgp-hg38-eur-calibration", "EUR", "available", 771, 9783946, 1357),
}


class TestTheRealDeclarations(unittest.TestCase):
    """Seam 4: the committed `resource.yaml` files against the draft."""

    def setUp(self) -> None:
        self.found = {row["ld_id"]: row for row in panels.panel_rows(REPO_ROOT / REFERENCE_RESOURCES)}

    def test_the_six_ids_are_the_drafts(self) -> None:
        self.assertEqual(sorted(self.found), sorted(DRAFT))

    def test_each_row_has_the_drafts_label_ancestry_status_n_variants_and_blocks(self) -> None:
        for ld_id, (label, ancestry, status, n, n_variants, n_blocks) in DRAFT.items():
            with self.subTest(ld_id=ld_id):
                row = self.found[ld_id]
                self.assertEqual(
                    (row["label"], row["ancestry"], row["status"], row["n"], row["n_variants"], row["n_blocks"]),
                    (label, ancestry, status, n, n_variants, n_blocks),
                )

    def test_the_ukb_panels_size_is_not_declared(self) -> None:
        self.assertIsNone(self.found["OGL-00013"]["n_variants"])
        self.assertIsNone(self.found["OGL-00013"]["store_bytes"])
        self.assertEqual(self.found["OGL-00013"]["licence"], "restricted: UK Biobank")

    def test_the_calibration_panels_role_is_its_used_for_not_its_status(self) -> None:
        row = self.found["OGL-00018"]
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["used_for"], "calibration control; not used for completion")
        for ld_id in DRAFT:
            if ld_id != "OGL-00018":
                self.assertEqual(self.found[ld_id]["used_for"], USED_FOR)

    def test_the_committed_declarations_never_name_the_ukb_directory_as_a_measured_size(self) -> None:
        text = (REPO_ROOT / REFERENCE_RESOURCES / "ukb-hg38-eur-ld" / "resource.yaml").read_text(encoding="utf-8")
        declared = yaml.safe_load(text)
        self.assertNotIn("n_variants", declared["ld"])
        self.assertNotIn("bytes", declared["ld"])


if __name__ == "__main__":
    unittest.main()
