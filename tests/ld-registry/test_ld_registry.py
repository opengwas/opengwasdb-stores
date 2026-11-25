#!/usr/bin/env python3
"""The LD Store Registry (#536, epic #496; docs/spec/ld-store-registry.md, ADR 0042).

Four seams, and only these:

1. a record is validated, and each refusal names the record and the field;
2. the writer fills a record from a Store's provenance, read through
   `opengwasdb ld info` (ADR 0023), keeping every field a person wrote and
   refusing a Store that is not the one the record describes;
3. a record's typed values, in the spec's columns, and their rendering: a
   missing value is `None`, then an empty cell;
4. the index regenerates the committed `ld-resources.tsv`.

No test opens a Store. The writer's input is a recorded `opengwasdb ld info`
of the served EUR LD Store (`fixtures/ld-info-eur.json`), trimmed of its
source checksums, its PC scan and its PC diagnostic. The validation tests start
from a record written by hand (`fixtures/record-ogl-00001.yaml`), not from the
writer. Expected values are literals from #496's draft row for OGL-00001 and
from that provenance; the digest was computed with
`jq -c -S -a . fixtures/ld-info-eur.json | tr -d '\n' | sha256sum`, not by
this package.

Run from the repository root:
    python3 tests/ld-registry/test_ld_registry.py
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from ld_registry import index as ld_index
from ld_registry import record, rows, writer
from panel_declarations import copy_declarations

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LD_INFO = FIXTURES / "ld-info-eur.json"
RECORD = FIXTURES / "record-ogl-00001.yaml"
SPEC = REPO_ROOT / "docs" / "spec" / "ld-store-registry.md"
LOCATION = "/data/opengwasdb/work/ogld/stores/hgdp1kgp-v2-EUR-k10.store"
# The draft's OGL-00006: the same panel, ancestry, n and k, but another Store.
TWIN_LOCATION = "/data/opengwasdb/work/ogld/scratch/64/81-eur"
# sha256 of the fixture's canonical JSON, by
# `jq -c -S -a . fixtures/ld-info-eur.json | tr -d '\n' | sha256sum`.
FIXTURE_SHA256 = "facaa4629a943cec0b1b0d35b6161755c470cc367e9db73d7ba2d046c2e855c4"

PERSON_FIELDS = ("label", "status", "licence", "used_for", "issue", "notes", "rebuild")


def recorded_ld_info() -> dict[str, Any]:
    return json.loads(LD_INFO.read_text(encoding="utf-8"))


def literal_record() -> dict[str, Any]:
    return yaml.safe_load(RECORD.read_text(encoding="utf-8"))


def stub_runner(payload: dict[str, Any], seen: list[list[str]] | None = None):
    def run(argv):
        if seen is not None:
            seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    return run


def write_yaml(path: Path, document: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


class RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.ld_root = self.tmp / "stores" / "ld"
        self.path = self.ld_root / "OGL-00001" / "record.yaml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def person_record(self) -> Path:
        """OGL-00001 as a person writes it, before the writer has run."""
        document = literal_record()
        return write_yaml(self.path, {"ld_id": "OGL-00001", **{f: document[f] for f in PERSON_FIELDS}})

    def write(self, info: dict[str, Any] | None = None, location: str = LOCATION, size: int = 79279262640):
        return writer.write_record(
            self.ld_root, "OGL-00001", location, runner=stub_runner(info or recorded_ld_info()), size=lambda _: size,
        )

    def read(self) -> dict[str, Any]:
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))


class TestTheFixturesAreTheServedEurLdStore(unittest.TestCase):
    def test_the_recorded_provenance_is_the_eur_panel_of_665(self) -> None:
        info = recorded_ld_info()
        self.assertEqual(info["panel"]["id"], "hgdp1kgp-v2-EUR")
        self.assertEqual(info["panel"]["ancestry"], "EUR")
        self.assertEqual(info["panel"]["n"], 665)
        self.assertEqual(info["adjustment"]["k"], 10)
        self.assertEqual(info["format_version"], "0.1.0")
        self.assertNotIn("t_partner", info, "this Store records no Partner Rows threshold")

    def test_the_literal_record_describes_the_same_store(self) -> None:
        document = literal_record()
        self.assertEqual(document["ld_id"], "OGL-00001")
        self.assertEqual(document["provenance"]["source_panel"], "hgdp1kgp-v2-EUR")
        self.assertEqual(document["provenance"]["n"], 665)
        self.assertEqual(document["provenance_sha256"], FIXTURE_SHA256)


class TestRecordValidation(RegistryCase):
    """Seam 1: every refusal names the record and the field."""

    def check(self, document: dict[str, Any], ld_id: str = "OGL-00001") -> list[record.RecordError]:
        self.path = write_yaml(self.ld_root / ld_id / "record.yaml", document)
        return record.load_record(self.path)[1]

    def assert_refused(self, errors: list[record.RecordError], field: str) -> None:
        self.assertIn(field, [error.field for error in errors], errors)
        message = next(str(error) for error in errors if error.field == field)
        self.assertIn(str(self.path), message)
        self.assertIn(field, message)

    def test_the_literal_record_is_valid(self) -> None:
        self.assertEqual(self.check(literal_record()), [])

    def test_a_licence_to_confirm_is_valid_and_kept_as_written(self) -> None:
        document = literal_record()
        self.assertEqual(document["licence"], "to confirm")
        self.assertEqual(self.check(document), [])
        self.assertEqual(record.load_record(self.path)[0]["licence"], "to confirm")

    def test_each_missing_required_field_is_refused_by_name(self) -> None:
        for field in ("label", "status", "licence", "used_for", "rebuild", "location", "provenance_sha256"):
            with self.subTest(field=field):
                document = literal_record()
                del document[field]
                self.assert_refused(self.check(document), field)
        for section, field in (
            ("provenance", "n"), ("provenance", "t_far"), ("provenance", "created_at"),
            ("provenance", "source_description"), ("rebuild", "commands"), ("measured", "store_bytes"),
        ):
            with self.subTest(field=f"{section}.{field}"):
                document = literal_record()
                del document[section][field]
                self.assert_refused(self.check(document), f"{section}.{field}")

    def test_a_malformed_ld_id_is_refused(self) -> None:
        document = literal_record()
        document["ld_id"] = "OGL-1"
        self.assert_refused(self.check(document, ld_id="OGL-1"), "ld_id")

    def test_an_ld_id_that_is_not_its_directory_is_refused(self) -> None:
        document = literal_record()
        document["ld_id"] = "OGL-00002"
        self.assert_refused(self.check(document), "ld_id")

    def test_an_unknown_status_is_refused(self) -> None:
        for status in ("live", "building"):
            with self.subTest(status=status):
                document = literal_record()
                document["status"] = status
                self.assert_refused(self.check(document), "status")

    def test_an_empty_licence_is_refused(self) -> None:
        for empty in ("", "  ", None):
            with self.subTest(licence=empty):
                document = literal_record()
                document["licence"] = empty
                self.assert_refused(self.check(document), "licence")

    def test_a_count_that_is_not_a_whole_number_is_refused(self) -> None:
        document = literal_record()
        document["provenance"]["n"] = "665"
        self.assert_refused(self.check(document), "provenance.n")

    def test_created_at_must_be_an_iso_8601_utc_time(self) -> None:
        for value in ("yesterday", "2026-11-13", "2026-11-13T04:27:31", "2026-11-13T05:27:31+01:00"):
            with self.subTest(created_at=value):
                document = literal_record()
                document["provenance"]["created_at"] = value
                self.assert_refused(self.check(document), "provenance.created_at")
        document = literal_record()
        document["provenance"]["created_at"] = "2026-11-13T04:27:31+00:00"
        self.assertEqual(self.check(document), [])

    def test_a_provenance_digest_that_is_not_a_sha256_is_refused(self) -> None:
        document = literal_record()
        document["provenance_sha256"] = "abc"
        self.assert_refused(self.check(document), "provenance_sha256")

    def test_a_key_the_spec_does_not_define_is_refused(self) -> None:
        document = literal_record()
        document["recipe"] = document.pop("rebuild")
        errors = self.check(document)
        self.assert_refused(errors, "recipe")
        self.assert_refused(errors, "rebuild")

    def test_a_symlinked_record_is_refused(self) -> None:
        real = write_yaml(self.tmp / "elsewhere.yaml", literal_record())
        self.path.parent.mkdir(parents=True)
        self.path.symlink_to(real)
        self.assert_refused(record.load_record(self.path)[1], "record.yaml")


class TestTheWriter(RegistryCase):
    """Seam 2: the writer fills a record from the Store's provenance."""

    def test_it_runs_opengwasdb_ld_info_on_the_location(self) -> None:
        self.person_record()
        seen: list[list[str]] = []
        writer.write_record(self.ld_root, "OGL-00001", LOCATION, runner=stub_runner(recorded_ld_info(), seen), size=lambda _: 1)
        self.assertEqual(seen, [["opengwasdb", "ld", "info", LOCATION]])

    def test_it_writes_the_draft_rows_values(self) -> None:
        self.person_record()
        self.write()
        written = self.read()
        self.assertEqual(record.load_record(self.path)[1], [])
        provenance = written["provenance"]
        self.assertEqual(written["kind"], "ld_store")
        self.assertEqual(provenance["source_panel"], "hgdp1kgp-v2-EUR")
        self.assertEqual(provenance["ancestry"], "EUR")
        self.assertEqual(provenance["n"], 665)
        self.assertEqual(provenance["k"], 10)
        self.assertEqual(provenance["n_variants"], 8586889)
        self.assertEqual(provenance["near_bp"], 3000000)
        self.assertEqual(provenance["far_bp"], 10000000)
        self.assertEqual(provenance["t_far"], 0.03186544342507645)
        self.assertEqual(provenance["r_scale"], 511)
        self.assertEqual(provenance["format_version"], "0.1.0")
        self.assertEqual(provenance["created_at"], "2026-11-13T04:27:31Z")
        self.assertEqual(provenance["software"]["ogld"], "0.0.1.dev292+g25af8039f")
        self.assertEqual(
            provenance["source_description"],
            "HGDP+1kGP phased v2 (Koenig et al. 2024), EUR, converted by build-inputs §3",
        )
        self.assertNotIn("t_partner", provenance)
        self.assertEqual(written["provenance_sha256"], FIXTURE_SHA256)
        self.assertEqual(written["measured"]["store_bytes"], 79279262640)
        self.assertEqual(written["location"], LOCATION)

        row = rows.ld_row(written)
        for column, value in {
            "ld_id": "OGL-00001",
            "label": "hgdp1kgp-v2-EUR-k10",
            "kind": "ld_store",
            "provenance": "genotypes",
            "status": "served",
            "ancestry": "EUR",
            "source_panel": "hgdp1kgp-v2-EUR",
            "n": "665",
            "k": "10",
            "reference_assembly": "GRCh38",
            "format_version": "0.1.0",
            "representation": "pairwise r (Near Band, Far Pairs)",
            "n_variants": "8586889",
            "store_bytes": "79279262640",
            "r_scale": "511",
            "near_bp": "3000000",
            "far_bp": "10000000",
            "t_far": "0.03186544342507645",
            "t_partner": "",
            "created_at": "2026-11-13T04:27:31Z",
            "build_wall_s": "2029",
            "built_with": "ogld 0.0.1.dev292+g25af8039f (scripts/build_hgdp1kgp.py)",
            "source": "HGDP+1kGP phased v2 (Koenig et al. 2024), EUR, converted by build-inputs §3",
            "licence": "to confirm",
            "location": LOCATION,
            "record": "stores/ld/OGL-00001/record.yaml",
            "issue": "ogld#7",
        }.items():
            with self.subTest(column=column):
                self.assertEqual(row[column], value)

    def test_the_fields_a_person_wrote_survive_a_rewrite(self) -> None:
        self.person_record()
        document = self.read()
        document["notes"] = "a note only a person could write"
        document["status"] = "retiring"
        write_yaml(self.path, document)
        self.write()
        self.write()
        written = self.read()
        for field in PERSON_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(written[field], document[field])

    def test_a_rewrite_reports_a_changed_size_on_disk(self) -> None:
        self.person_record()
        first = self.write(size=100)
        self.assertIsNone(first.previous_store_bytes)
        again = self.write(size=101)
        self.assertEqual((again.previous_store_bytes, again.store_bytes), (100, 101))
        self.assertEqual(self.read()["measured"]["store_bytes"], 101)

    def test_another_store_with_the_same_panel_is_refused(self) -> None:
        """The draft's OGL-00006 shares OGL-00001's panel, ancestry, n and k."""
        self.person_record()
        self.write()
        text = self.path.read_text(encoding="utf-8")
        twin = recorded_ld_info()
        twin["format_version"] = "0.2.0"
        twin["t_partner"] = 0.01
        self.assertEqual(
            (twin["panel"]["id"], twin["panel"]["ancestry"], twin["panel"]["n"], twin["adjustment"]["k"]),
            ("hgdp1kgp-v2-EUR", "EUR", 665, 10),
        )
        with self.assertRaises(writer.WriterRefusal) as caught:
            self.write(twin, location=TWIN_LOCATION)
        message = str(caught.exception)
        for field in ("location", "provenance.format_version", "provenance.t_partner", "provenance_sha256"):
            with self.subTest(field=field):
                self.assertIn(field, message)
        self.assertIn(str(self.path), message)
        self.assertEqual(self.path.read_text(encoding="utf-8"), text, "a refused write changes nothing")

    def test_a_changed_provenance_at_the_same_location_is_refused(self) -> None:
        self.person_record()
        self.write()
        text = self.path.read_text(encoding="utf-8")
        rebuilt = recorded_ld_info()
        rebuilt["build"]["created_at"] = "2026-11-26T00:00:00Z"
        with self.assertRaises(writer.WriterRefusal) as caught:
            self.write(rebuilt)
        self.assertIn("provenance.created_at", str(caught.exception))
        self.assertIn("provenance_sha256", str(caught.exception))
        self.assertNotIn("location", str(caught.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_a_malformed_value_from_ld_info_is_refused_and_writes_nothing(self) -> None:
        malformed = recorded_ld_info()
        malformed["build"]["created_at"] = "yesterday"
        with self.assertRaises(writer.WriterRefusal) as caught:
            self.write(malformed)
        self.assertIn("provenance.created_at", str(caught.exception))
        self.assertFalse(self.path.exists(), "no record is created")
        self.assertFalse(list(self.ld_root.rglob("*.tmp")), "no staged file is left")

        self.person_record()
        text = self.path.read_text(encoding="utf-8")
        with self.assertRaises(writer.WriterRefusal) as caught:
            self.write(malformed)
        self.assertIn("provenance.created_at", str(caught.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), text, "an existing record is untouched")


class TestRows(unittest.TestCase):
    """Seam 3: typed values in the spec's columns, then their rendering."""

    def spec_columns(self) -> list[str]:
        text = SPEC.read_text(encoding="utf-8")
        block = text.split("```tsv\n", 1)[1].split("```", 1)[0]
        return block.strip().split("\t")

    def test_the_columns_are_the_specs_in_its_order(self) -> None:
        self.assertEqual(len(self.spec_columns()), 32, "the draft's 31 columns, and #537's `default`")
        self.assertEqual(list(rows.COLUMNS), self.spec_columns())
        self.assertEqual(list(rows.ld_values(literal_record())), self.spec_columns())
        self.assertEqual(list(rows.ld_row(literal_record())), self.spec_columns())

    def test_values_are_typed_and_a_missing_value_is_none(self) -> None:
        values = rows.ld_values(literal_record())
        for column in ("t_partner", "n_blocks", "genotype_bytes", "default"):
            with self.subTest(column=column):
                self.assertIsNone(values[column])
        self.assertEqual(values["n"], 665)
        self.assertIsInstance(values["n"], int)
        self.assertEqual(values["t_far"], 0.03186544342507645)
        self.assertEqual(values["build_wall_s"], 2029)
        self.assertIsInstance(values["build_wall_s"], int)
        self.assertEqual(values["store_bytes"], 79279262640)

    def test_a_missing_value_is_an_empty_cell_never_zero(self) -> None:
        rendered = rows.render_tsv([literal_record()])
        (row,) = csv.DictReader(rendered.splitlines(), delimiter="\t")
        for column in ("t_partner", "n_blocks", "genotype_bytes", "default"):
            with self.subTest(column=column):
                self.assertEqual(row[column], "")
        self.assertEqual(rendered.splitlines()[0].split("\t"), self.spec_columns())

    def test_representation_is_keyed_on_kind_before_partner_rows(self) -> None:
        document = literal_record()
        document["provenance"]["t_partner"] = 0.01
        self.assertEqual(rows.ld_values(document)["representation"], "pairwise r (Near Band, Far Pairs, Partner Rows)")
        document["kind"] = "genotype_store"
        self.assertEqual(rows.ld_values(document)["representation"], "pgen + sidecar, r computed per query")


GENOTYPE_INFO = FIXTURES / "ld-info-amr-genotype-store.json"


def genotype_info() -> dict[str, Any]:
    """The AMR Genotype Store's `ld info` (OGL-00009), trimmed to two filesets."""
    return json.loads(GENOTYPE_INFO.read_text(encoding="utf-8"))


class TestGenotypeStores(RegistryCase):
    """#539: the kind from `filesets`, `genotype_bytes` from the pins, and the representation."""

    def sidecar(self, sizes: dict[str, int] | None = None, pins: dict[str, int] | None = None) -> tuple[Path, dict[str, Any]]:
        """A temporary sidecar `<tmp>/s/x.store` with stub pins beside it, and an `ld info` that pins them."""
        sizes = sizes or {"chr1.pgen": 7, "chr1.psam": 3, "chr1.pvar": 11}
        location = self.tmp / "s" / "x.store"
        location.mkdir(parents=True, exist_ok=True)
        (location / "arrays.bin").write_bytes(b"x" * 5)
        (self.tmp / "s" / "pgen").mkdir(exist_ok=True)
        for name, size in sizes.items():
            (self.tmp / "s" / "pgen" / name).write_bytes(b"x" * size)
        pins = pins or {"chr1.pgen": 7, "chr1.psam": 3, "chr1.pvar": 11}
        info = genotype_info()
        info["filesets"] = [{
            kind: {"path": f"../pgen/chr1.{kind}", "size": pins[f"chr1.{kind}"], "sha256": "0" * 64, "mtime_ns": 1}
            for kind in ("pgen", "psam", "pvar")
        }]
        return location, info

    def write_genotype(self, location: Path, info: dict[str, Any]):
        self.person_record()
        return writer.write_record(self.ld_root, "OGL-00001", location, runner=stub_runner(info))

    def test_the_fixture_is_the_amr_genotype_store(self) -> None:
        info = genotype_info()
        self.assertEqual(info["panel"]["id"], "hgdp1kgp-v2-AMR")
        self.assertEqual(info["panel"]["n"], 341)
        self.assertEqual(info["n_variants"], 7561067)
        self.assertIn("filesets", info)
        self.assertEqual(info["software"]["ogld"], "0.5.0")

    def test_filesets_with_inputs_and_arrays_is_a_genotype_store(self) -> None:
        self.assertEqual(writer.infer_kind(genotype_info()), "genotype_store")

    def test_no_filesets_is_an_ld_store(self) -> None:
        self.assertEqual(writer.infer_kind(recorded_ld_info()), "ld_store")

    def test_filesets_without_arrays_is_refused_as_neither_kind(self) -> None:
        info = genotype_info()
        del info["arrays"]
        with self.assertRaises(writer.WriterRefusal) as caught:
            writer.infer_kind(info)
        self.assertIn("arrays", str(caught.exception))
        del info["inputs"]
        info["arrays"] = {}
        with self.assertRaises(writer.WriterRefusal) as caught:
            writer.infer_kind(info)
        self.assertIn("inputs", str(caught.exception))

    def test_any_one_marker_without_the_other_two_is_neither_kind(self) -> None:
        markers = ("filesets", "inputs", "arrays")
        for present in markers:
            with self.subTest(only=present):
                info = recorded_ld_info()
                info[present] = genotype_info()[present]
                with self.assertRaises(writer.WriterRefusal) as caught:
                    writer.infer_kind(info)
                self.assertIn(present, str(caught.exception))
        for absent in markers:
            with self.subTest(without=absent):
                info = genotype_info()
                del info[absent]
                with self.assertRaises(writer.WriterRefusal) as caught:
                    writer.infer_kind(info)
                self.assertIn(absent, str(caught.exception))

    def test_a_malformed_pin_is_a_named_refusal(self) -> None:
        cases = {
            "a missing psam": (lambda i: i["filesets"][0].pop("psam"), "psam"),
            "a pin without size": (lambda i: i["filesets"][0]["pgen"].pop("size"), "size"),
            "a pin without path": (lambda i: i["filesets"][0]["pvar"].pop("path"), "path"),
            "filesets that is a mapping": (lambda i: i.update(filesets={"a": 1}), "filesets"),
            "a fileset that is not a mapping": (lambda i: i.update(filesets=[7]), "fileset"),
            "a pin that is not a mapping": (lambda i: i["filesets"][0].update(pgen="x"), "pgen"),
        }
        for name, (damage, mentioned) in cases.items():
            with self.subTest(name):
                location, info = self.sidecar()
                damage(info)
                with self.assertRaises(writer.WriterRefusal) as caught:
                    writer.genotype_bytes(location, info)
                self.assertIn(mentioned, str(caught.exception))

    def test_an_absolute_pin_is_refused(self) -> None:
        location, info = self.sidecar()
        info["filesets"][0]["pgen"]["path"] = str(self.tmp / "s" / "pgen" / "chr1.pgen")
        with self.assertRaises(writer.WriterRefusal) as caught:
            writer.genotype_bytes(location, info)
        self.assertIn("relative", str(caught.exception))

    def test_a_genotype_store_record_requires_genotype_bytes(self) -> None:
        document = literal_record()
        document["kind"] = "genotype_store"
        errors = record.validate(document, self.path)
        self.assertEqual([e.field for e in errors], ["measured.genotype_bytes"])
        self.assertIn("is required", str(errors[0]))
        document["measured"]["genotype_bytes"] = 5
        self.assertEqual(record.validate(document, self.path), [])

    def test_an_ld_store_record_refuses_genotype_bytes(self) -> None:
        document = literal_record()
        document["measured"]["genotype_bytes"] = 5
        errors = record.validate(document, self.path)
        self.assertEqual([e.field for e in errors], ["measured.genotype_bytes"])
        self.assertIn(str(self.path), str(errors[0]))

    def test_a_refused_kind_writes_nothing(self) -> None:
        location, info = self.sidecar()
        del info["arrays"]
        self.person_record()
        before = self.path.read_bytes()
        with self.assertRaises(writer.WriterRefusal):
            writer.write_record(self.ld_root, "OGL-00001", location, runner=stub_runner(info))
        self.assertEqual(self.path.read_bytes(), before)

    def test_genotype_bytes_is_the_sum_of_the_pinned_files_and_store_bytes_the_sidecar_alone(self) -> None:
        location, info = self.sidecar()
        self.write_genotype(location, info)
        written = self.read()
        self.assertEqual(written["kind"], "genotype_store")
        self.assertEqual(written["measured"], {"store_bytes": 5, "genotype_bytes": 7 + 3 + 11})
        self.assertEqual(record.load_record(self.path)[1], [], "the field is declared")

    def test_a_missing_pin_is_refused(self) -> None:
        location, info = self.sidecar()
        (self.tmp / "s" / "pgen" / "chr1.psam").unlink()
        with self.assertRaises(writer.WriterRefusal) as caught:
            self.write_genotype(location, info)
        self.assertIn("chr1.psam", str(caught.exception))
        self.assertFalse(self.path.exists() and "genotype_bytes" in self.read().get("measured", {}))

    def test_a_pin_whose_size_disagrees_is_refused(self) -> None:
        location, info = self.sidecar(sizes={"chr1.pgen": 8, "chr1.psam": 3, "chr1.pvar": 11})
        with self.assertRaises(writer.WriterRefusal) as caught:
            self.write_genotype(location, info)
        self.assertIn("chr1.pgen", str(caught.exception))
        self.assertIn("8", str(caught.exception))
        self.assertIn("7", str(caught.exception))

    def test_an_ld_store_has_no_genotype_bytes(self) -> None:
        self.person_record()
        self.write()
        self.assertEqual(self.read()["measured"], {"store_bytes": 79279262640})

    def test_the_values_give_the_literal_representation_and_genotype_bytes(self) -> None:
        document = literal_record()
        document["kind"] = "genotype_store"
        document["measured"] = {"store_bytes": 1297730705, "genotype_bytes": 3551447413}
        values = rows.ld_values(document)
        self.assertEqual(values["representation"], "pgen + sidecar, r computed per query")
        self.assertEqual(values["genotype_bytes"], 3551447413)
        self.assertEqual(values["store_bytes"], 1297730705, "the sidecar's alone")
        self.assertEqual(rows.ld_row(document)["genotype_bytes"], "3551447413")
        self.assertIsNone(rows.ld_values(literal_record())["genotype_bytes"])


class TestColumnTypes(unittest.TestCase):
    """Every column declares its type; a column without one, or a type without a column, is refused."""

    def test_every_column_has_a_declared_type_and_no_other_does(self) -> None:
        self.assertEqual(list(rows.COLUMN_TYPES), list(rows.COLUMNS))
        self.assertEqual(set(rows.COLUMN_TYPES.values()), {"text", "integer", "real"})
        self.assertEqual(rows.COLUMN_TYPES["n"], "integer")
        self.assertEqual(rows.COLUMN_TYPES["t_far"], "real")
        self.assertEqual(rows.COLUMN_TYPES["default"], "integer")

    def test_a_new_column_without_a_type_is_refused_not_defaulted_to_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "brand_new"):
            rows.checked_types((*rows.COLUMNS, "brand_new"), rows.COLUMN_TYPES)

    def test_a_misspelt_entry_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "n_variant"):
            rows.checked_types(rows.COLUMNS, {**rows.COLUMN_TYPES, "n_variant": "integer"})

    def test_an_unknown_type_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "number"):
            rows.checked_types(("ld_id",), {"ld_id": "number"})

    def test_a_column_without_a_type_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "no_type"):
            rows.checked_types(("ld_id", "no_type"), {"ld_id": "text"})

    def test_a_type_for_a_missing_column_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "ghost"):
            rows.checked_types(("ld_id",), {"ld_id": "text", "ghost": "integer"})


class TestValidRecords(RegistryCase):
    def test_it_returns_the_records_or_raises_invalid_records_naming_the_record(self) -> None:
        write_yaml(self.path, literal_record())
        self.assertEqual([r["ld_id"] for r in record.valid_records(self.ld_root)], ["OGL-00001"])
        document = literal_record()
        document["status"] = "live"
        write_yaml(self.path, document)
        with self.assertRaises(record.InvalidRecords) as caught:
            record.valid_records(self.ld_root)
        self.assertIn(str(self.path), str(caught.exception))
        self.assertIs(ld_index.InvalidRecords, record.InvalidRecords)


class TestTheDefault(RegistryCase):
    """Seam 4 of #537: a person-written `default: true`, on at most one served Store per ancestry."""

    def records(self, *declared: tuple[str, str, bool | None]) -> list[Path]:
        """One record per (ld_id, status, default), all of the EUR ancestry; `None` omits `default`."""
        paths = []
        for ld_id, status, default in declared:
            document = literal_record()
            document.update(ld_id=ld_id, status=status)
            if default is not None:
                document["default"] = default
            paths.append(write_yaml(self.ld_root / ld_id / "record.yaml", document))
        return paths

    def test_a_declared_default_is_one_in_the_column_and_the_rest_are_null(self) -> None:
        document = literal_record()
        self.assertIsNone(rows.ld_values(document)["default"])
        document["default"] = True
        self.assertEqual(rows.ld_values(document)["default"], 1)
        self.assertIsInstance(rows.ld_values(document)["default"], int)
        self.assertEqual(list(rows.ld_values(document))[-1], "default")
        (row,) = csv.DictReader(rows.render_tsv([document]).splitlines(), delimiter="\t")
        self.assertEqual(row["default"], "1")

    def test_a_served_record_may_declare_the_default(self) -> None:
        (path,) = self.records(("OGL-00001", "served", True))
        document, errors = record.load_record(path)
        self.assertEqual(errors, [])
        self.assertIs(document["default"], True)

    def test_a_default_that_is_not_true_is_refused(self) -> None:
        for value in (False, 1, "yes"):
            with self.subTest(value=value):
                (path,) = self.records(("OGL-00001", "served", value))
                self.assertEqual([e.field for e in record.load_record(path)[1]], ["default"])

    def test_default_on_a_record_that_is_not_served_is_refused(self) -> None:
        for status in ("built", "retiring", "reference", "retired"):
            with self.subTest(status=status):
                (path,) = self.records(("OGL-00001", status, True))
                (error,) = record.load_record(path)[1]
                self.assertEqual((error.path, error.field), (path, "default"))
                self.assertIn("served", error.message)

    def test_two_defaults_for_one_ancestry_fail_the_index(self) -> None:
        a, b = self.records(("OGL-00001", "served", True), ("OGL-00002", "served", True))
        with self.assertRaises(ld_index.InvalidRecords) as caught:
            ld_index.generate_ld_index(self.ld_root, self.tmp, self.tmp / "resources" / "reference-resources")
        self.assertIn("default", str(caught.exception))
        self.assertIn(str(b), str(caught.exception))
        self.assertIn("OGL-00001", str(caught.exception))
        self.assertFalse((self.tmp / "ld-resources.tsv").exists())

    def test_one_default_per_ancestry_is_valid(self) -> None:
        self.records(("OGL-00001", "served", True), ("OGL-00002", "served", None), ("OGL-00003", "served", True))
        other = yaml.safe_load((self.ld_root / "OGL-00003" / "record.yaml").read_text(encoding="utf-8"))
        other["provenance"]["ancestry"] = "AFR"
        write_yaml(self.ld_root / "OGL-00003" / "record.yaml", other)
        _, errors = record.load_records(self.ld_root)
        self.assertEqual(errors, [])


class TestResourceRows(RegistryCase):
    def test_it_is_the_tsvs_rows_in_columns_order_records_and_panels_together(self) -> None:
        resources = REPO_ROOT / "resources" / "reference-resources"
        listed = ld_index.resource_rows(REPO_ROOT / "stores" / "ld", resources)
        with open(REPO_ROOT / "ld-resources.tsv", encoding="utf-8", newline="") as handle:
            tsv = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual([row["ld_id"] for row in listed], [row["ld_id"] for row in tsv])
        self.assertTrue(all(list(row) == list(rows.COLUMNS) for row in listed))
        self.assertIn("ld_panel", {row["kind"] for row in listed})
        self.assertIn("ld_store", {row["kind"] for row in listed})

    def test_it_refuses_an_invalid_record(self) -> None:
        document = literal_record()
        document["status"] = "live"
        write_yaml(self.path, document)
        with self.assertRaises(record.InvalidRecords):
            ld_index.resource_rows(self.ld_root, self.tmp / "resources" / "reference-resources")


class TestTheIndex(RegistryCase):
    """Seam 4: the committed records regenerate the committed `ld-resources.tsv`.

    STORES.md's clean-tree check, with the LD section, is tests/index's.
    """

    def valid_registry(self, ld_root: Path) -> None:
        (ld_root / "OGL-00001").mkdir(parents=True)
        shutil.copy(RECORD, ld_root / "OGL-00001" / "record.yaml")

    def test_the_committed_records_regenerate_the_committed_tsv(self) -> None:
        # The LD panels' rows (#540) come from the declarations under resources/.
        # Only `self.tmp` is written; the declarations are read in place.
        ld_index.generate_ld_index(REPO_ROOT / "stores" / "ld", self.tmp, REPO_ROOT / "resources" / "reference-resources")
        committed = (REPO_ROOT / "ld-resources.tsv").read_text(encoding="utf-8")
        self.assertEqual(
            (self.tmp / "ld-resources.tsv").read_text(encoding="utf-8"), committed,
            "ld-resources.tsv is stale. Run 'pixi run index' to regenerate.",
        )
        (row,) = (r for r in csv.DictReader(committed.splitlines(), delimiter="\t") if r["ld_id"] == "OGL-00001")
        self.assertEqual(
            (row["label"], row["status"], row["n"], row["k"], row["licence"]),
            ("hgdp1kgp-v2-EUR-k10", "served", "665", "10", "to confirm"),
        )

    def test_a_hand_edit_leaves_a_dirty_tree_after_regeneration(self) -> None:
        repo = self.tmp / "repo"
        shutil.copytree(REPO_ROOT / "stores" / "ld", repo / "stores" / "ld")
        resources = copy_declarations(repo)
        ld_index.generate_ld_index(repo / "stores" / "ld", repo, resources)
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "generated"], check=True)
        tsv = repo / "ld-resources.tsv"
        tsv.write_text(tsv.read_text(encoding="utf-8").replace("\tserved\t", "\tretired\t"), encoding="utf-8")
        subprocess.run([*git, "commit", "-qam", "a hand edit"], check=True)
        ld_index.generate_ld_index(repo / "stores" / "ld", repo, resources)
        status = subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(status.strip(), "M ld-resources.tsv")

    def test_a_record_that_breaks_the_spec_fails_the_index(self) -> None:
        document = literal_record()
        document["status"] = "live"
        write_yaml(self.path, document)
        with self.assertRaises(ld_index.InvalidRecords) as caught:
            ld_index.generate_ld_index(self.ld_root, self.tmp, self.tmp / "resources" / "reference-resources")
        self.assertIn(str(self.path), str(caught.exception))
        self.assertIn("status", str(caught.exception))
        self.assertFalse((self.tmp / "ld-resources.tsv").exists(), "nothing is written from a broken registry")

    def test_anything_but_an_ld_id_directory_fails_the_index(self) -> None:
        for stray in ("OGL-2/", "notes/", "OGL-00002.yaml", "README.md", "OGL-00002@"):
            with self.subTest(stray=stray):
                # A registry of its own per case, so one failure cannot leak into the next.
                ld_root = Path(tempfile.mkdtemp(dir=self.tmp)) / "stores" / "ld"
                self.valid_registry(ld_root)
                target = ld_root / stray.rstrip("/@")
                if stray.endswith("/"):
                    target.mkdir()
                elif stray.endswith("@"):
                    target.symlink_to(ld_root / "OGL-00001")
                else:
                    target.write_text("x\n", encoding="utf-8")
                with self.assertRaises(ld_index.InvalidRecords) as caught:
                    ld_index.generate_ld_index(ld_root, self.tmp, self.tmp / "resources" / "reference-resources")
                self.assertIn(str(target), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
