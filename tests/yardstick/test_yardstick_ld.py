#!/usr/bin/env python3
"""The yardstick's LD section (#504): workload, runner, result, and the GWAS contract kept.

The runner test builds a tiny LD Store and a tiny Genotype Store with the engine's
own builders (`tests/yardstick/ld_fixture.py`, about a second), records them in a
temporary LD Store Registry, and measures them in worker processes, never a real Store.
Both fixtures carry Partner Rows, because no engine builder makes a Store without them,
so a Store that claims none is tested through the runner's decision (`ld.claims_partner_rows`).

Run from the repository root:
    python3 tests/yardstick/test_yardstick_ld.py
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import ld_fixture  # tests/yardstick/ld_fixture.py: a script's directory is on its path
from test_yardstick import PENDING, timed  # the GWAS suite's scaffolding, beside this file

from ld_registry.writer import genotype_bytes
from yardstick import ld, runner
from yardstick.fixture import write_ld_records
from yardstick import result as result_spec
from yardstick import workload as workload_spec

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = sorted((REPO_ROOT / "docs" / "_yardstick").glob("*.json"))
FAKE = [f"22:{10_000_000 + i}:A:C" for i in range(500)]
NO_INPUT = "/nonexistent/403-analyses"


def ld_section(variants: list[str], *, analyses: int = 3, sha256: str = "0" * 64) -> dict:
    """An LD section over the fixture's variants: 500 for the matrix, 1, 10 and 100 for proxies."""
    return {
        "repetitions": {"default": 2, "clump": 1},
        "stores": [
            {"ld_id": ld_fixture.LD_ID, "kind": "ld_store"},
            {"ld_id": ld_fixture.GENOTYPE_ID, "kind": "genotype_store"},
        ],
        "matrix": {"chromosome": "22", "seed": 8, "variants": variants[:500]},
        "region": {"chromosome": "22", "centre": 11_000_000},
        "proxies": {
            "r2": 0.1, "kb": 1000, "seeds": {"proxies_1": 8, "proxies_10": 8, "proxies_100": 8},
            "variants": {"proxies_1": variants[:1], "proxies_10": variants[:10], "proxies_100": variants[:100]},
        },
        "clump": {
            "env": "OPENGWASDB_LD_403_ANALYSES", "default": NO_INPUT,
            "analyses": analyses, "file": "hits.tsv", "sha256": sha256, "r2": 0.1, "kb": 1000,
        },
    }


def workload(section: dict | None = None) -> dict:
    out = {"schema": 1, "workload_id": "fixture-ld", "repetitions": {"default": 2}, "stores": [dict(PENDING)]}
    if section is not None:
        out["ld"] = section
    return out


def pinned(variants: list[str], root: Path, **kwargs: object) -> dict:
    """A workload whose clump input is the fixture at `root`, its checksum pinned as the committed one is."""
    return workload(ld_section(variants, sha256=workload_spec.clump_sha256(root, "hits.tsv"), **kwargs))


class LdWorkloadTest(unittest.TestCase):
    """Seam 1: an `ld` section parses, and a bad one is refused with a named reason."""

    def test_the_committed_workload_measures_ogl_00006_and_ogl_00007(self) -> None:
        loaded = workload_spec.load()
        self.assertEqual(
            [(s["ld_id"], s["kind"]) for s in loaded["ld"]["stores"]],
            [("OGL-00006", "ld_store"), ("OGL-00007", "genotype_store")],
        )
        self.assertEqual(len(loaded["ld"]["matrix"]["variants"]), 500)
        self.assertEqual(loaded["ld"]["clump"]["analyses"], 37)
        self.assertRegex(loaded["ld"]["clump"]["sha256"], "^[0-9a-f]{64}$")
        # yardstick-5 was committed measuring OGL-00001 (6d3c354b), so this workload is a new one.
        self.assertEqual(loaded["workload_id"], "yardstick-6")

    def test_a_fixture_section_parses(self) -> None:
        workload_spec.check(workload(ld_section(FAKE)))

    def test_an_unknown_kind_is_refused(self) -> None:
        section = ld_section(FAKE)
        section["stores"][0]["kind"] = "ld_panel"
        with self.assertRaisesRegex(workload_spec.WorkloadError, "OGL-00006: kind must be one of"):
            workload_spec.check(workload(section))

    def test_a_malformed_ld_id_is_refused(self) -> None:
        section = ld_section(FAKE)
        section["stores"][0]["ld_id"] = "OGS-00001"
        with self.assertRaisesRegex(workload_spec.WorkloadError, "ld_id"):
            workload_spec.check(workload(section))

    def test_a_short_matrix_list_is_refused(self) -> None:
        section = ld_section(FAKE)
        section["matrix"]["variants"] = FAKE[:499]
        with self.assertRaisesRegex(workload_spec.WorkloadError, "ld.matrix.variants must be 500 distinct"):
            workload_spec.check(workload(section))

    def test_a_missing_proxies_list_is_refused(self) -> None:
        section = ld_section(FAKE)
        del section["proxies"]["variants"]["proxies_10"]
        with self.assertRaisesRegex(workload_spec.WorkloadError, "ld.proxies.variants.proxies_10"):
            workload_spec.check(workload(section))

    def test_a_missing_clump_key_is_refused(self) -> None:
        for key in ("env", "sha256"):
            section = ld_section(FAKE)
            del section["clump"][key]
            with self.subTest(key), self.assertRaisesRegex(workload_spec.WorkloadError, f"ld.clump lacks {key}"):
                workload_spec.check(workload(section))

    def test_a_section_of_the_wrong_type_is_a_workload_error_not_a_type_error(self) -> None:
        for path, value in (
            (("matrix",), 1), (("region",), "x"), (("proxies",), []), (("clump",), 3), (("stores",), {"a": 1}),
            (("proxies", "variants"), 1), (("proxies", "seeds"), [8]), (("repetitions",), 5), ((), 1),
        ):
            section = ld_section(FAKE)
            target = section
            for key in path[:-1]:
                target = target[key]
            if path:
                target[path[-1]] = value
            else:
                section = value
            with self.subTest(path=path, value=value), self.assertRaises(workload_spec.WorkloadError):
                workload_spec.check(workload(section))
        section = ld_section(FAKE)
        section["stores"][0] = "OGL-00006"
        with self.assertRaises(workload_spec.WorkloadError):
            workload_spec.check(workload(section))

    def test_a_malformed_value_is_a_named_refusal_not_a_crash(self) -> None:
        for path, value, message in (
            (("repetitions",), {"default": 5, 3: 1, "x": 2}, "ld.repetitions keys must be measure names"),
            (("clump", "default"), 3, "ld.clump.default must be a path"),
            (("clump", "env"), [], "ld.clump.env must be an environment variable's name"),
            (("clump", "file"), None, "ld.clump.file must be a file name"),
            (("clump", "sha256"), "x", "ld.clump.sha256 must be 64 lower-case hex"),
            (("clump", "r2"), "0.01", "ld.clump.r2 must be a number"),
            (("proxies", "kb"), None, "ld.proxies.kb must be a number"),
        ):
            section = ld_section(FAKE)
            if len(path) == 1:
                section[path[0]] = value
            else:
                section[path[0]][path[1]] = value
            with self.subTest(path=path), self.assertRaisesRegex(workload_spec.WorkloadError, message):
                workload_spec.check(workload(section))


class LdResolveTest(unittest.TestCase):
    """Seam 1, against a registry and an input directory: the Store comes from its record, never a path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def registry(self, **provenance: object) -> Path:
        return write_ld_records(
            self.root / "ld",
            ld_fixture.record(ld_fixture.LD_ID, "ld_store", self.root / "a.store", **provenance),
            ld_fixture.record(ld_fixture.GENOTYPE_ID, "genotype_store", self.root / "b.store"),
        )

    def test_each_store_resolves_to_its_record(self) -> None:
        targets = workload_spec.resolve_ld(workload(ld_section(FAKE)), self.registry())
        self.assertEqual([(t["ld_id"], t["kind"], t["location"], t["far_bp"]) for t in targets], [
            ("OGL-00006", "ld_store", str(self.root / "a.store"), 10_000_000),
            ("OGL-00007", "genotype_store", str(self.root / "b.store"), 10_000_000),
        ])

    def test_the_default_registry_is_the_registry_modules_own(self) -> None:
        from ld_registry.record import LD_ROOT

        self.assertEqual(workload_spec.DEFAULT_LD_ROOT, workload_spec.REPO_ROOT / LD_ROOT)

    def test_an_unknown_ld_id_is_refused(self) -> None:
        section = ld_section(FAKE)
        section["stores"][1]["ld_id"] = "OGL-00099"
        with self.assertRaisesRegex(workload_spec.WorkloadError, "OGL-00099: no record in the LD Store Registry"):
            workload_spec.resolve_ld(workload(section), self.registry())

    def test_a_kind_its_record_contradicts_is_refused(self) -> None:
        section = ld_section(FAKE)
        section["stores"][0]["kind"] = "genotype_store"
        with self.assertRaisesRegex(workload_spec.WorkloadError, "OGL-00006: the workload says genotype_store, its record ld_store"):
            workload_spec.resolve_ld(workload(section), self.registry())

    def test_a_region_wider_than_far_bp_is_refused(self) -> None:
        with self.assertRaisesRegex(workload_spec.WorkloadError, "OGL-00006: region_3mb is 3000000 bp wide, wider than its far_bp of 2000000"):
            workload_spec.resolve_ld(workload(ld_section(FAKE)), self.registry(far_bp=2_000_000))

    def input(self, **kwargs: object) -> tuple[Path, dict]:
        root = ld_fixture.analyses(self.root / "403", FAKE, **kwargs)
        return root, pinned(FAKE, root)

    def resolve(self, root: Path, wanted: dict) -> dict:
        with mock.patch.dict(os.environ, {"OPENGWASDB_LD_403_ANALYSES": str(root)}):
            return workload_spec.clump_input(wanted)

    def test_the_clump_input_is_read_from_the_environment_with_its_checksum(self) -> None:
        root, wanted = self.input()
        found = self.resolve(root, wanted)
        self.assertEqual((found["path"], found["source"], len(found["analyses"]), found["hits"]), (str(root), "env", 3, 500))
        self.assertEqual(found["sha256"], wanted["ld"]["clump"]["sha256"])

    def test_a_clump_input_whose_checksum_is_not_the_workloads_is_refused(self) -> None:
        root, wanted = self.input()
        (root / "OGS-00099__fx0" / "hits.tsv").write_text("alid\tz\n22:10000000:A:C\t9\n")
        with self.assertRaisesRegex(workload_spec.WorkloadError, "is not the workload's: its sha256"):
            self.resolve(root, wanted)

    def test_a_missing_clump_input_directory_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(workload_spec.WorkloadError, f"clump input {NO_INPUT} is not a directory"):
                workload_spec.clump_input(workload(ld_section(FAKE)))

    def test_a_clump_input_with_the_wrong_number_of_analyses_is_refused(self) -> None:
        root, _ = self.input(count=2)
        with self.assertRaisesRegex(workload_spec.WorkloadError, "manifest names 2 Analyses, the workload 3"):
            self.resolve(root, pinned(FAKE, root))

    def test_directories_that_are_not_the_manifests_set_are_refused(self) -> None:
        root, wanted = self.input()
        (root / "OGS-00099__fx2").rename(root / "OGS-00099__other")
        with self.assertRaisesRegex(workload_spec.WorkloadError, r"not the manifest's: missing \['OGS-00099__fx2'\], extra \['OGS-00099__other'\]"):
            self.resolve(root, wanted)

    def test_an_analysis_without_its_hits_is_refused(self) -> None:
        root, wanted = self.input()
        (root / "OGS-00099__fx1" / "hits.tsv").unlink()
        with self.assertRaisesRegex(workload_spec.WorkloadError, "OGS-00099__fx1 has no hits.tsv"):
            self.resolve(root, wanted)

    def test_hits_with_mislabelled_columns_are_refused(self) -> None:
        root = ld_fixture.analyses(self.root / "403", FAKE)
        path = root / "OGS-00099__fx1" / "hits.tsv"
        path.write_text(path.read_text().replace("alid\tz", "alid\tp", 1))
        with self.assertRaisesRegex(workload_spec.WorkloadError, r"OGS-00099__fx1/hits.tsv's header is \['alid', 'p'\], not \['alid', 'z'\]"):
            self.resolve(root, pinned(FAKE, root))
        with self.assertRaisesRegex(ValueError, r"header \['alid', 'p'\], not \['alid', 'z'\]"):
            ld.parse_hits("OGS-00099__fx1/hits.tsv", path.read_bytes())

    def test_the_input_is_read_once_its_digest_over_the_bytes_read(self) -> None:
        root, wanted = self.input()
        names = sorted(p.name for p in root.iterdir() if p.is_dir())
        digest, files = workload_spec.read_clump(root, names, "hits.tsv")
        self.assertEqual(digest, wanted["ld"]["clump"]["sha256"])
        self.assertEqual(files["OGS-00099__fx0"], (root / "OGS-00099__fx0" / "hits.tsv").read_bytes())


def ld_timed(**extra: object) -> dict:
    return timed(result_count=3, **extra)


def ld_result_section() -> dict:
    measures = {name: ld_timed() for name in result_spec.LD_TIMED}
    measures["open"] = timed()
    measures["bytes_on_disk"] = {"status": "measured", "bytes": 10, "source": "walk", "store_bytes": 10}
    measures["peak_rss"] = {"status": "measured", "baseline_mb": 1.0, "peak_mb": 2.0, "delta_mb": 1.0, "result_count": 3}
    digest = "a" * 64
    return {
        "repetitions": {"default": 2},
        "ogquery": {"version": "0.5.0"},
        "inputs": {
            "matrix": {"chromosome": "22", "seed": 8, "count": 500, "variants_sha256": digest},
            "region": {"chromosome": "22", "centre": 25_000_000, "widths_bp": {"region_1mb": 1_000_000, "region_3mb": 3_000_000}},
            "proxies": {"r2": 0.8, "kb": 10_000, "seeds": {"proxies_1": 9, "proxies_10": 8, "proxies_100": 8},
                        "variants_sha256": {"proxies_1": digest, "proxies_10": digest, "proxies_100": digest}},
            "clump": {"path": "/x", "analyses": ["a"], "hits": 1, "sha256": digest},
        },
        "stores": [{"ld_id": "OGL-00006", "kind": "ld_store", "label": "x", "location": "/x",
                    "status": "measured", "measures": measures}],
    }


class LdResultTest(unittest.TestCase):
    """Seam 3: the validator accepts the new section, refuses a bad one, and every committed result unchanged."""

    def doc(self) -> dict:
        doc = json.loads(RESULTS[-1].read_text(encoding="utf-8"))
        doc["ld"] = ld_result_section()
        return doc

    def refused(self, doc: dict, message: str, wanted: dict | None = None) -> None:
        with self.assertRaisesRegex(result_spec.ResultError, message):
            result_spec.validate(doc, wanted)

    def test_every_committed_result_still_validates(self) -> None:
        self.assertTrue(RESULTS)
        for path in RESULTS:
            with self.subTest(path.name):
                result_spec.validate(json.loads(path.read_text(encoding="utf-8")))

    def test_ld_names_are_not_gwas_measures(self) -> None:
        """og-dev-hub draws every `TIMED` name as a GWAS measure, so LD names keep their own constant."""
        self.assertFalse(set(result_spec.LD_TIMED) - {"open"} & set(result_spec.TIMED))
        self.assertEqual(result_spec.SCHEMA_VERSION, 1)

    def test_the_kinds_are_the_registrys(self) -> None:
        from ld_registry.record import KINDS

        self.assertIs(result_spec.LD_KINDS, KINDS)

    def test_a_result_with_an_ld_section_validates(self) -> None:
        result_spec.validate(self.doc())

    def test_an_ld_timed_record_without_its_count_is_refused(self) -> None:
        doc = self.doc()
        del doc["ld"]["stores"][0]["measures"]["clump"]["result_count"]
        self.refused(doc, r"ld.stores\[OGL-00006\].clump lacks result_count")

    def test_an_unknown_ld_measure_is_refused(self) -> None:
        doc = self.doc()
        doc["ld"]["stores"][0]["measures"]["phewas_rsid"] = ld_timed()
        self.refused(doc, "unknown measure 'phewas_rsid'")

    def test_the_inputs_must_carry_the_clump_inputs_evidence(self) -> None:
        for mutate, message in (
            (lambda ld: ld["inputs"].pop("clump"), "ld.inputs lacks clump"),
            (lambda ld: ld["inputs"].__setitem__("clump", []), "ld.inputs.clump must be an object"),
            (lambda ld: ld["inputs"]["clump"].pop("sha256"), "ld.inputs.clump lacks sha256"),
            (lambda ld: ld["inputs"]["clump"].__setitem__("sha256", "x"), "ld.inputs.clump.sha256 must be 64"),
            (lambda ld: ld["inputs"]["clump"].__setitem__("hits", -1), "ld.inputs.clump.hits must not be negative"),
            (lambda ld: ld["inputs"].pop("matrix"), "ld.inputs lacks matrix"),
            (lambda ld: ld["inputs"].__setitem__("region", 1), "ld.inputs.region must be an object"),
            (lambda ld: ld.pop("ogquery"), "ld lacks ogquery"),
            (lambda ld: ld["ogquery"].__setitem__("version", None), "ld.ogquery.version must be text"),
            (lambda ld: ld["inputs"].__setitem__("matrix", {}), "ld.inputs.matrix lacks chromosome"),
            (lambda ld: ld["inputs"]["matrix"].__setitem__("variants_sha256", ""), "ld.inputs.matrix.variants_sha256 must be 64"),
            (lambda ld: ld["inputs"].__setitem__("proxies", {}), "ld.inputs.proxies lacks r2"),
            (lambda ld: ld["inputs"]["proxies"].__setitem__("seeds", {}), "ld.inputs.proxies.seeds lacks proxies_1"),
            (lambda ld: ld["inputs"]["proxies"]["variants_sha256"].pop("proxies_10"),
             "ld.inputs.proxies.variants_sha256 lacks proxies_10"),
            (lambda ld: ld["inputs"]["clump"].__setitem__("path", None), "ld.inputs.clump.path must be text"),
        ):
            doc = self.doc()
            mutate(doc["ld"])
            with self.subTest(message):
                self.refused(doc, message)

    def test_an_ld_store_needs_its_label_and_location(self) -> None:
        for key in ("label", "location"):
            doc = self.doc()
            del doc["ld"]["stores"][0][key]
            with self.subTest(key):
                self.refused(doc, rf"ld.stores\[OGL-00006\] lacks {key}")

    def test_negative_ld_times_bytes_and_sizes_are_refused(self) -> None:
        for measure, key in (("clump", "median_ms"), ("clump", "leads"), ("matrix_500", "absent"),
                             ("bytes_on_disk", "store_bytes"), ("bytes_on_disk", "genotype_bytes"), ("peak_rss", "peak_mb")):
            doc = self.doc()
            doc["ld"]["stores"][0]["measures"][measure][key] = -1
            with self.subTest(f"{measure}.{key}"):
                self.refused(doc, rf"{measure}.{key} must not be negative")

    def test_bytes_must_be_whole_numbers(self) -> None:
        for key, value in (("store_bytes", "10"), ("genotype_bytes", "-1"), ("bytes", 1.5)):
            doc = self.doc()
            doc["ld"]["stores"][0]["measures"]["bytes_on_disk"][key] = value
            with self.subTest(key=key, value=value):
                self.refused(doc, rf"bytes_on_disk.{key} must be a number")

    def test_an_ld_id_that_is_not_one_is_refused_not_a_crash(self) -> None:
        wanted = {"stores": [], "ld": {"stores": [{"ld_id": "OGL-00006", "kind": "ld_store"}]}}
        for value in ([], None, "OGS-00006"):
            doc = self.doc()
            doc["ld"]["stores"][0]["ld_id"] = value
            with self.subTest(value=value):
                self.refused(doc, r"ld.stores\[0\].ld_id must be an LD Store Registry id", wanted)

    def test_a_gwas_store_id_that_is_not_text_is_refused_not_a_crash(self) -> None:
        doc = json.loads(RESULTS[-1].read_text(encoding="utf-8"))
        doc["stores"][0]["store_id"] = []
        self.refused(doc, "store_id must be text", {"stores": [{"store_id": "OGS-00009", "status": "measured"}]})

    def test_an_ld_not_applicable_needs_its_reason(self) -> None:
        doc = self.doc()
        doc["ld"]["stores"][0]["measures"]["clump"] = {"status": "not_applicable"}
        self.refused(doc, r"ld.stores\[OGL-00006\].clump is not_applicable and gives no reason")

    def test_an_ld_section_of_the_wrong_shape_is_refused_by_path(self) -> None:
        doc = self.doc()
        for bad, message in (([], "ld must be an object"), ({"stores": [None]}, "ld lacks repetitions")):
            doc["ld"] = bad
            with self.subTest(bad=bad):
                self.refused(doc, message)
        doc = self.doc()
        doc["ld"]["stores"][0]["kind"] = "ld_panel"
        self.refused(doc, "kind must be one of")

    def test_against_its_workload_every_ld_store_kind_and_measure_is_required(self) -> None:
        doc = self.doc()
        wanted = {"stores": [], "ld": {"stores": [{"ld_id": "OGL-00006", "kind": "ld_store"}]}}
        result_spec.validate(doc, wanted)
        swapped = copy.deepcopy(doc)
        swapped["ld"]["stores"][0]["kind"] = "genotype_store"
        self.refused(swapped, "OGL-00006: kind genotype_store, workload says ld_store", wanted)
        del doc["ld"]["stores"][0]["measures"]["region_3mb"]
        self.refused(doc, "OGL-00006 lacks measure region_3mb", wanted)
        wanted["ld"]["stores"].append({"ld_id": "OGL-00007", "kind": "genotype_store"})
        self.refused(self.doc(), "result lacks LD Store OGL-00007", wanted)
        del doc["ld"]
        self.refused(doc, "result lacks its ld section", wanted)


class GwasOnlyResultIsUnchangedTest(unittest.TestCase):
    """Seam 4: a workload without `ld` writes exactly the top-level keys a committed result has."""

    def test_the_keys_match_the_committed_yardstick_4_result(self) -> None:
        committed = json.loads((REPO_ROOT / "docs" / "_yardstick" / "20261125T130645Z-0.17.0.json").read_text())
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp, \
                mock.patch.object(runner.fixture, "timed_build", return_value={"status": "measured", "layout": "dense", "seconds": 1.0}):
            result = runner.run({**workload(), "fixture_build": {"layout": "dense"}}, scratch=Path(tmp))
        self.assertEqual(sorted(result), sorted(committed))
        self.assertNotIn("ld", result)


class LdRunnerOnFixtureStoresTest(unittest.TestCase):
    """Seam 2: both kinds, every LD row, first read, warm median and p95, peak memory and bytes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        cls.root = Path(cls.tmp.name)
        cls.stores = ld_fixture.build_stores(cls.root / "built")
        import ogquery.ld

        store = ogquery.ld.open(cls.stores["ld_store"])
        cls.variants = [str(a) for c in ("22", "21") for a in store.variants_in(c, 0, 10**9).alid]
        store.close()
        cls.analyses = ld_fixture.analyses(cls.root / "403", cls.variants)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def registry(self, ld_location: Path | None = None, genotype_location: Path | None = None) -> Path:
        return write_ld_records(
            Path(tempfile.mkdtemp(dir=self.root)) / "ld",
            ld_fixture.record(ld_fixture.LD_ID, "ld_store", ld_location or self.stores["ld_store"]),
            ld_fixture.record(ld_fixture.GENOTYPE_ID, "genotype_store", genotype_location or self.stores["genotype_store"]),
        )

    def run_ld(self, ld_root: Path | None = None, **kwargs: object) -> tuple[dict, dict]:
        wanted = pinned(self.variants, self.analyses)
        workload_spec.check(wanted)
        with mock.patch.dict(os.environ, {"OPENGWASDB_LD_403_ANALYSES": str(self.analyses)}):
            result = runner.run(wanted, ld_root=ld_root or self.registry(), scratch=Path(tempfile.mkdtemp(dir=self.root)), **kwargs)
        return result, wanted

    def test_every_ld_row_is_measured_on_both_kinds(self) -> None:
        result, wanted = self.run_ld()
        result_spec.validate(result, wanted)
        self.assertEqual([(s["ld_id"], s["kind"]) for s in result["ld"]["stores"]],
                         [("OGL-00006", "ld_store"), ("OGL-00007", "genotype_store")])
        for store in result["ld"]["stores"]:
            measures = store["measures"]
            with self.subTest(store["ld_id"]):
                self.assertEqual(sorted(measures), sorted(workload_spec.LD_MEASURES))
                self.assertEqual({m["status"] for m in measures.values()}, {"measured"}, measures)
                for name in result_spec.LD_TIMED:
                    self.assertGreaterEqual(measures[name]["first_read_ms"], 0)
                    self.assertIn("median_ms", measures[name])
                    self.assertIn("p95_ms", measures[name])
                self.assertEqual(measures["matrix_500"]["result_count"], 500)
                self.assertEqual(measures["matrix_10"]["result_count"], 10)
                self.assertEqual(measures["clump"]["reps"], 1)
                self.assertEqual(measures["clump"]["analyses"], 3)
                self.assertEqual(measures["clump"]["result_count"], len(self.variants))
                self.assertGreater(measures["clump"]["leads"], 0)
                self.assertGreater(measures["peak_rss"]["peak_mb"], 0)
                self.assertGreater(measures["bytes_on_disk"]["bytes"], 0)
        # A Genotype Store's genotypes are the six pgen, psam and pvar files the panel wrote, the
        # registry writer's figure (`ld_registry.writer.genotype_bytes`) and the files' own sizes alike.
        genotype = result["ld"]["stores"][1]["measures"]["bytes_on_disk"]
        written = sum(p.stat().st_size for p in self.stores["panel"].glob("chr*.p*"))
        self.assertEqual(len(list(self.stores["panel"].glob("chr*.p*"))), 6)
        import ogquery.ld

        store = ogquery.ld.open(self.stores["genotype_store"])
        self.assertEqual(genotype["genotype_bytes"], genotype_bytes(self.stores["genotype_store"], dict(store.provenance)))
        store.close()
        self.assertEqual(genotype["genotype_bytes"], written)
        self.assertNotIn("genotype_bytes", result["ld"]["stores"][0]["measures"]["bytes_on_disk"])
        # The run records what it read: the variants' rule, the clump input with its checksum, and the reader.
        inputs = result["ld"]["inputs"]
        self.assertEqual(inputs["matrix"]["seed"], 8)
        self.assertRegex(inputs["matrix"]["variants_sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(inputs["clump"]["path"], str(self.analyses))
        self.assertEqual(inputs["clump"]["sha256"], wanted["ld"]["clump"]["sha256"])
        from importlib.metadata import version

        self.assertEqual(result["ld"]["ogquery"], {"version": version("ogquery")})
        self.assertEqual(result["guard_violations"], [])

    def worker_entry(self, **clump: object) -> dict:
        """What the runner hands the LD Store's worker (`runner._ld_entry`), from the real resolve and preflight."""
        wanted = pinned(self.variants, self.analyses)
        target = workload_spec.resolve_ld(wanted, self.registry())[0]
        with mock.patch.dict(os.environ, {"OPENGWASDB_LD_403_ANALYSES": str(self.analyses)}):
            found = workload_spec.clump_input(wanted)
        return runner._ld_entry(target, wanted["ld"], {**found, **clump})

    def test_the_worker_records_proxies_and_clump_not_applicable_on_a_store_claiming_no_partner_rows(self) -> None:
        """No engine builder makes a Store without Partner Rows, so the owner's rule is answered for it, in the worker."""
        with mock.patch.object(ld, "claims_partner_rows", lambda provenance: False):
            measures = ld.measure_ld_store(self.worker_entry(), {"default": 1})
        for name in ("proxies_1", "proxies_10", "proxies_100", "clump"):
            self.assertEqual(measures[name]["status"], "not_applicable", name)
            self.assertIn("claims no Partner Rows", measures[name]["reason"])
        self.assertEqual(measures["open"]["status"], "measured")
        self.assertEqual(measures["region_3mb"]["status"], "measured")

    def test_the_worker_refuses_a_clump_input_that_changed_after_the_preflight(self) -> None:
        measures = ld.measure_ld_store(self.worker_entry(sha256="0" * 64), {"default": 1})
        self.assertEqual(measures["clump"]["status"], "unavailable")
        self.assertIn("the clump input changed after it was checked", measures["clump"]["error"])
        self.assertEqual(measures["proxies_10"]["status"], "measured")

    def test_a_spent_budget_leaves_every_ld_row_unavailable_naming_it(self) -> None:
        """Applicability is the worker's to say, so a worker that never ran says nothing: each row names the budget."""
        result, wanted = self.run_ld(budget_s=0.0)
        result_spec.validate(result, wanted)
        for store in result["ld"]["stores"]:
            self.assertEqual({m["status"] for m in store["measures"].values()}, {"unavailable"})
            self.assertIn("budget was already spent", store["measures"]["clump"]["error"])

    def test_an_overlays_size_follows_its_links_and_counts_each_file_once(self) -> None:
        """An overlay's queries read its links' targets, so its size on disk is theirs too (`bytes`), not its own files alone."""
        import shutil

        import ogquery.ld
        from ogstores.store_size import store_size_on_disk

        source = self.stores["ld_store"]
        overlay = Path(tempfile.mkdtemp(dir=self.root)) / "overlay.store"
        overlay.mkdir()
        shutil.copy2(source / "zarr.json", overlay / "zarr.json")
        shutil.copytree(source / "variants", overlay / "variants")
        for name in ("near", "far", "partners"):
            (overlay / name).symlink_to(source / name)
        (overlay / "variants" / "again").symlink_to(source / "near")  # read twice, counted once
        store = ogquery.ld.open(overlay)
        provenance = dict(store.provenance)
        self.assertEqual(len(store.matrix(self.variants[:10]).variants), 10, "the overlay answers through its links")
        store.close()
        record = ld.store_bytes(overlay, "ld_store", provenance)
        self.assertEqual(record["store_bytes"], store_size_on_disk(overlay), "the registry's figure: its own files")
        self.assertEqual(record["bytes"], store_size_on_disk(source), "every file a query reads, once")
        self.assertLess(record["store_bytes"], record["bytes"])

    def test_the_owners_rule_refuses_partner_rows_a_0_1_0_store_merely_records(self) -> None:
        self.assertFalse(ld.claims_partner_rows({"t_partner": 0.01, "format_version": "0.1.0"}))
        self.assertTrue(ld.claims_partner_rows({"t_partner": 0.01, "format_version": "0.2.0"}))

    def test_a_store_whose_location_is_gone_is_unavailable_not_a_crash(self) -> None:
        result, wanted = self.run_ld(self.registry(self.root / "gone.store", self.root / "gone-too.store"))
        result_spec.validate(result, wanted)
        for store in result["ld"]["stores"]:
            self.assertEqual({m["status"] for m in store["measures"].values()}, {"unavailable"})
            self.assertIn("does not exist", store["measures"]["open"]["error"])

    def test_resolving_and_hashing_the_inputs_count_in_the_runs_time(self) -> None:
        real = workload_spec.clump_input

        def slow(*args: object, **kwargs: object) -> dict:
            time.sleep(0.4)
            return real(*args, **kwargs)

        absent = self.registry(self.root / "gone.store", self.root / "gone-too.store")
        with mock.patch.object(workload_spec, "clump_input", slow):
            result, _ = self.run_ld(absent)
        self.assertGreaterEqual(result["duration_s"], 0.4)
        with mock.patch.object(workload_spec, "clump_input", slow):
            result, _ = self.run_ld(absent, budget_s=0.2)
        self.assertIs(result["over_budget"], True)

    def test_matrix_inputs_take_every_fiftieth_of_the_500(self) -> None:
        self.assertEqual(ld.matrix_inputs(FAKE, 10), FAKE[::50])
        self.assertEqual(ld.matrix_inputs(FAKE, 500), FAKE)


if __name__ == "__main__":
    unittest.main()
