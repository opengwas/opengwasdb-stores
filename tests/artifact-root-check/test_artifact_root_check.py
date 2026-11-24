#!/usr/bin/env python3
"""The Artifact Root check on a fixture registry and Artifact Root (#510).

1. A fixture with one case of each category gives exactly those findings: a
   `built` bundle with no Store, a Store directory with no bundle, a Store whose
   bundle is `accepted`, a `by-label` link whose target is gone, and a pending
   publication beside a staged Store.
2. The exit status is 1 for a category 1 or 4 finding and 0 otherwise.
3. It is read-only: no byte, mtime or link under the fixture root changes.
4. The report records the time, host, Artifact Root and each finding with its
   category and Store id, and no path outside `<root>/<id>`.

Run from the repository root:
    pixi run -e dev python3 tests/artifact-root-check/test_artifact_root_check.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from ogstores import artifact_root_check as check
from ogstores import paths

NOW = dt.datetime(2026, 11, 24, 21, 0, 0, tzinfo=dt.timezone.utc)


def write_bundle(registry: Path, store_id: str, status: str, label: str | None = None) -> None:
    root = registry / store_id
    root.mkdir(parents=True)
    release = {
        "store_id": store_id,
        "label": label or f"label-{store_id.lower()}",
        "status": status,
        "association_coverage": "gwas",
        "created_at": "2026-08-18T08:51:59Z",
        "description": "fixture",
        "source_snapshot_id": "snap",
    }
    build = {
        "store_id": store_id,
        "layout": "dense",
        "completion_state": "observed_only",
        "build": {"command": "build-dense-vcf", "options": {}},
    }
    (root / "release.yaml").write_text(yaml.safe_dump(release), encoding="utf-8")
    (root / "build.yaml").write_text(yaml.safe_dump(build), encoding="utf-8")
    (root / "analyses.tsv").write_text("analysis_id\tsource_file\na1\tf1.vcf.gz\n", encoding="utf-8")


def write_store(artifact_root: Path, store_id: str) -> None:
    store = paths.store_path(store_id, root=artifact_root)
    (store / "data.zarr").mkdir(parents=True)
    (store / "manifest.json").write_text(json.dumps({"store_id": store_id}), encoding="utf-8")
    records = paths.records_dir(store_id, root=artifact_root)
    records.mkdir(parents=True, exist_ok=True)
    (records / "build.json").write_text("{}", encoding="utf-8")


def link_label(artifact_root: Path, label: str, store_id: str) -> None:
    link = paths.by_label_link(store_id, label, root=artifact_root)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(Path("..") / store_id)


def tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every entry under `root`: its type, mtime, and bytes or link target."""
    out: dict[str, tuple[Any, ...]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.lstat()
            if p.is_symlink():
                out[str(p.relative_to(root))] = ("link", st.st_mtime_ns, os.readlink(p))
            elif p.is_dir():
                out[str(p.relative_to(root))] = ("dir", st.st_mtime_ns, st.st_mode)
            else:
                out[str(p.relative_to(root))] = ("file", st.st_mtime_ns, st.st_mode, p.read_bytes())
    st = root.lstat()
    out["."] = ("dir", st.st_mtime_ns, st.st_mode)
    return out


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="artifact_root_check_"))
        self.registry = self.tmp / "stores"
        self.root = self.tmp / "artifacts"
        self.out = self.tmp / "out"
        for d in (self.registry, self.root, self.out):
            d.mkdir()
        # Clean: validated, Store present, its label resolves.
        write_bundle(self.registry, "OGS-00001", "validated")
        write_store(self.root, "OGS-00001")
        link_label(self.root, "label-ogs-00001", "OGS-00001")
        # Never built: a candidate with no directory is not a finding.
        write_bundle(self.registry, "OGS-00007", "candidate")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_check(self, *extra: str) -> tuple[int, str]:
        argv = ["--artifact-root", str(self.root), "--registry-root", str(self.registry), *extra]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = check.main(argv, now=NOW)
        return code, buf.getvalue()

    def findings(self) -> list[tuple[int, str | None]]:
        result = check.check_artifact_root(self.root, registry_root=self.registry)
        return [(f.category, f.store_id) for f in result.findings]

    def one_of_each(self) -> None:
        # 1. Registered as built, no Store.
        write_bundle(self.registry, "OGS-00002", "built")
        # 2. A Store directory with no bundle.
        write_store(self.root, "OGS-00040")
        # 3. A Store present while its bundle says it is not built.
        write_bundle(self.registry, "OGS-00003", "accepted")
        write_store(self.root, "OGS-00003")
        # 4. A label whose target is gone.
        link_label(self.root, "gone", "OGS-00009")
        # 5. A pending publication beside a staged Store.
        write_bundle(self.registry, "OGS-00005", "validated")
        write_store(self.root, "OGS-00005")
        paths.publication_marker("OGS-00005", root=self.root).write_text("{}", encoding="utf-8")
        paths.partial_store_path("OGS-00005", root=self.root).mkdir()


class FindingsTest(Fixture):
    def test_a_clean_root_has_no_findings(self) -> None:
        self.assertEqual(self.findings(), [])

    def test_one_case_of_each_category_gives_exactly_those_findings(self) -> None:
        self.one_of_each()
        self.assertEqual(
            self.findings(),
            [(1, "OGS-00002"), (2, "OGS-00040"), (3, "OGS-00003"), (4, "OGS-00009"),
             (5, "OGS-00005"), (5, "OGS-00005")],
        )
        result = check.check_artifact_root(self.root, registry_root=self.registry)
        by_cat = {f.category: f for f in result.findings}
        self.assertEqual(by_cat[1].release_status, "built")
        self.assertEqual(by_cat[3].release_status, "accepted")
        self.assertEqual(by_cat[4].label, "gone")
        stray = [f.detail for f in result.findings if f.category == 5]
        self.assertIn("a publication is pending, and `pixi run release OGS-00005` finishes it", stray[0])
        self.assertIn("a Store is staged", stray[1])

    def test_a_withdrawn_or_superseded_bundle_whose_store_is_present_disagrees(self) -> None:
        write_bundle(self.registry, "OGS-00011", "withdrawn")
        write_store(self.root, "OGS-00011")
        write_bundle(self.registry, "OGS-00012", "superseded")
        write_store(self.root, "OGS-00012")
        self.assertEqual(self.findings(), [(3, "OGS-00011"), (3, "OGS-00012")])

    def test_a_built_bundle_whose_directory_holds_no_store_is_missing(self) -> None:
        write_bundle(self.registry, "OGS-00002", "validated")
        paths.records_dir("OGS-00002", root=self.root).mkdir(parents=True)
        self.assertEqual(self.findings(), [(1, "OGS-00002")])

    def test_an_accepted_bundle_mid_build_is_not_a_disagreement(self) -> None:
        """Work under `<root>/<id>` with no published Store is a build in progress, not a Store."""
        write_bundle(self.registry, "OGS-00002", "accepted")
        paths.work_dir("OGS-00002", root=self.root).mkdir(parents=True)
        self.assertEqual(self.findings(), [])

    def test_only_store_id_directories_are_read_as_release_directories(self) -> None:
        (self.root / "catalog").mkdir()
        (self.root / ".tmp").mkdir()
        self.assertEqual(self.findings(), [])

    def test_hidden_staged_files_and_a_live_run_are_described_as_the_workflow_does(self) -> None:
        write_bundle(self.registry, "OGS-00011", "accepted")
        write_store(self.root, "OGS-00011")
        partial = paths.partial_store_path("OGS-00011", root=self.root)
        partial.mkdir()
        paths.completion_checkpoint_path("OGS-00011", root=self.root).mkdir()
        lock = paths.release_lock_path("OGS-00011", root=self.root)
        lock.parent.mkdir()
        lock.write_text("pid 123, started 20261122T224225Z\n", encoding="utf-8")
        fd = os.open(lock, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            result = check.check_artifact_root(self.root, registry_root=self.registry)
        finally:
            os.close(fd)
        stray = [f for f in result.findings if f.category == 5]
        self.assertEqual([f.path for f in stray], [str(partial), str(partial.parent / f".{partial.name}.checkpoint")])
        for f in stray:
            self.assertIn("a Store is staged", f.detail)
            self.assertIn("a run of OGS-00011 is in progress (pid 123, started 20261122T224225Z)", f.detail)
        self.assertEqual([f.category for f in result.findings], [3, 5, 5])


class ExitStatusTest(Fixture):
    def test_category_1_exits_1(self) -> None:
        write_bundle(self.registry, "OGS-00002", "built")
        self.assertEqual(self.run_check()[0], 1)

    def test_category_4_exits_1(self) -> None:
        link_label(self.root, "gone", "OGS-00009")
        self.assertEqual(self.run_check()[0], 1)

    def test_categories_2_3_and_5_exit_0(self) -> None:
        write_store(self.root, "OGS-00040")
        write_bundle(self.registry, "OGS-00003", "accepted")
        write_store(self.root, "OGS-00003")
        paths.partial_store_path("OGS-00001", root=self.root).mkdir()
        code, out = self.run_check()
        self.assertEqual(self.findings(), [(2, "OGS-00040"), (3, "OGS-00003"), (5, "OGS-00001")])
        self.assertEqual(code, 0, out)

    def test_a_clean_root_exits_0(self) -> None:
        self.assertEqual(self.run_check()[0], 0)

    def test_a_root_that_is_not_a_directory_is_refused(self) -> None:
        code, out = self.run_check("--artifact-root", str(self.tmp / "nowhere"))
        self.assertEqual(code, 2)
        self.assertIn("not a directory", out)

    def test_a_report_under_the_artifact_root_is_refused(self) -> None:
        before = tree(self.root)
        code, out = self.run_check("--json", str(self.root / "report.json"))
        self.assertEqual(code, 2)
        self.assertIn("read-only", out)
        self.assertEqual(tree(self.root), before)


class ReadOnlyTest(Fixture):
    def test_nothing_under_the_root_changes(self) -> None:
        self.one_of_each()
        lock = paths.release_lock_path("OGS-00005", root=self.root)
        lock.parent.mkdir()
        lock.write_text("pid 1\n", encoding="utf-8")
        before = tree(self.root)
        code, _ = self.run_check("--json", str(self.out / "r.json"), "--markdown", str(self.out / "r.md"))
        self.assertEqual(code, 1)
        self.assertEqual(tree(self.root), before)


class ReportTest(Fixture):
    def report(self) -> tuple[dict[str, Any], str]:
        self.one_of_each()
        self.run_check("--json", str(self.out / "r.json"), "--markdown", str(self.out / "r.md"))
        return (
            json.loads((self.out / "r.json").read_text(encoding="utf-8")),
            (self.out / "r.md").read_text(encoding="utf-8"),
        )

    def test_the_report_records_time_host_root_and_each_finding(self) -> None:
        doc, md = self.report()
        self.assertEqual(doc["checked_at"], "2026-11-24T21:00:00Z")
        self.assertTrue(doc["host"])
        self.assertEqual(doc["artifact_root"], str(self.root))
        self.assertEqual(doc["exit_status"], 1)
        self.assertEqual(
            [(f["category"], f["store_id"]) for f in doc["findings"]],
            [(1, "OGS-00002"), (2, "OGS-00040"), (3, "OGS-00003"), (4, "OGS-00009"),
             (5, "OGS-00005"), (5, "OGS-00005")],
        )
        self.assertEqual(
            [f["name"] for f in doc["findings"]][:5],
            ["registered_but_missing", "unregistered", "status_disagreement",
             "broken_by_label_link", "stray_state"],
        )
        self.assertEqual(doc["counts"], {
            "registered_but_missing": 1, "unregistered": 1, "status_disagreement": 1,
            "broken_by_label_link": 1, "stray_state": 2,
        })
        for fragment in ("2026-11-24T21:00:00Z", doc["host"], "`OGS-00002`", "registered but missing", "exit status 1"):
            self.assertIn(fragment, md)

    def test_no_path_outside_a_release_directory_is_recorded(self) -> None:
        doc, md = self.report()
        allowed = re.compile(re.escape(str(self.root)) + r"(/OGS-\d{5}(/[^\s`|]*)?)?\Z")
        for value in strings(doc):
            if str(self.tmp) in value:
                self.assertRegex(value, allowed)
        for token in re.findall(re.escape(str(self.tmp)) + r"[^\s`|)]*", md):
            self.assertRegex(token, allowed)
        for forbidden in (str(self.registry), str(self.root / "by-label"), str(self.root / ".release-locks"), str(self.out)):
            self.assertNotIn(forbidden, json.dumps(doc))
            self.assertNotIn(forbidden, md)

    def test_a_label_pointing_outside_the_root_is_reported_without_its_target(self) -> None:
        link = paths.by_label_dir(root=self.root) / "elsewhere"
        link.symlink_to(self.tmp / "outside" / "OGS-00099")
        result = check.check_artifact_root(self.root, registry_root=self.registry)
        doc = check.report_document(result, now=NOW, host="h")
        found = [f for f in doc["findings"] if f["category"] == 4]
        self.assertEqual([(f["label"], f["store_id"]) for f in found], [("elsewhere", None)])
        self.assertNotIn(str(self.tmp / "outside"), json.dumps(doc))


if __name__ == "__main__":
    unittest.main()
