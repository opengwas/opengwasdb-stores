#!/usr/bin/env python3
"""The architecture collector: both repositories' models combined (#418).

Fixture repositories stand in for this repository and the engine. Expected
values are literals written from the fixtures in `architecture_fixtures.py`.

Run from the repository root:
    python3 tests/project-site-architecture/test_architecture_collector.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import architecture_fixtures as fx

from project_site.collect import architecture
from project_site.collect.common import CollectError


class Fixture:
    def __init__(self, test: unittest.TestCase, engine_files: dict[str, str] | None = None,
                 registry_files: dict[str, str] | None = None) -> None:
        root = Path(tempfile.mkdtemp(prefix="arch-"))
        test.addCleanup(__import__("shutil").rmtree, root, True)
        self.registry = root / "registry"
        self.engine = root / "engine"
        self.registry_commit = fx.make_repo(self.registry, registry_files or fx.REGISTRY_FILES, branch="trunk")
        self.engine_commit = fx.make_repo(self.engine, engine_files or fx.ENGINE_FILES)

    def collect(self) -> dict:
        return architecture.collect(self.registry, engine_git=self.engine, engine_ref="main",
                                    registry_ref="HEAD")


class CombinedModelTests(unittest.TestCase):
    def test_components_of_both_repositories_are_listed_with_their_repository(self) -> None:
        result = Fixture(self).collect()
        by_id = {c["id"]: c["repo"] for c in result["components"]}
        self.assertEqual(
            by_id,
            {"cli": "engine", "query": "engine", "check": "engine", "store": "engine", "model": "engine",
             "gen": "registry", "ogs": "registry", "tools": "registry"},
        )
        self.assertEqual([layer["name"] for layer in result["model"]["layers"]],
                         ["Entry", "Use", "Storage", "Foundations"])

    def test_each_repository_records_the_ref_and_commit_read(self) -> None:
        fixture = Fixture(self)
        result = fixture.collect()
        self.assertEqual(result["engine"]["ref"], "main")
        self.assertEqual(result["engine"]["commit"], fixture.engine_commit)
        self.assertTrue(result["engine"]["model"])
        self.assertEqual(result["registry"]["commit"], fixture.registry_commit)

    def test_an_unclaimed_engine_file_and_a_stale_claim_are_gaps_not_errors(self) -> None:
        engine = dict(fx.ENGINE_FILES)
        engine["opengwasdb/orphan.py"] = "x = 1\n"
        engine["docs/architecture/components.yaml"] = fx.ENGINE_MODEL.replace(
            "paths: [opengwasdb/model/]", "paths: [opengwasdb/model/, opengwasdb/gone/]")
        result = Fixture(self, engine_files=engine).collect()
        self.assertEqual(result["gaps"]["engine"]["unclaimed"], ["opengwasdb/orphan.py"])
        self.assertEqual(result["gaps"]["engine"]["stale"],
                         [{"path": "opengwasdb/gone/", "component": "model"}])
        self.assertEqual(result["gaps"]["registry"], {"unclaimed": [], "stale": []})

    def test_an_engine_ref_without_a_model_says_so_and_keeps_this_repositorys_components(self) -> None:
        engine = {k: v for k, v in fx.ENGINE_FILES.items() if k != "docs/architecture/components.yaml"}
        fixture = Fixture(self, engine_files=engine)
        result = fixture.collect()
        self.assertFalse(result["engine"]["model"])
        self.assertEqual(result["engine"]["ref"], "main")
        self.assertEqual(result["engine"]["commit"], fixture.engine_commit)
        self.assertEqual([c["id"] for c in result["components"]], ["gen", "ogs", "tools"])
        self.assertEqual(result["gaps"]["engine"], {"unclaimed": [], "stale": []})
        self.assertEqual(result["model"]["layers"], [])

    def test_component_size_is_counted_from_the_files_it_claims(self) -> None:
        result = Fixture(self).collect()
        store = next(c for c in result["components"] if c["id"] == "store")
        self.assertEqual((store["files"], store["lines"]), (3, 3))

    def test_every_code_file_is_read_and_its_lines_counted(self) -> None:
        result = Fixture(self).collect()
        ogs = next(c for c in result["components"] if c["id"] == "ogs")
        # src/ogs/run.py (2 lines), workflow/run.sh (2) and workflow/Snakefile (5)
        self.assertEqual((ogs["files"], ogs["lines"]), (3, 9))

    def test_a_file_that_does_not_parse_makes_the_source_fail_naming_the_path(self) -> None:
        engine = {**fx.ENGINE_FILES, "opengwasdb/store/broken.py": "def (:\n"}
        with self.assertRaises(CollectError) as caught:
            Fixture(self, engine_files=engine).collect()
        self.assertIn("opengwasdb/store/broken.py", str(caught.exception))

    def test_the_engine_commit_date_is_recorded(self) -> None:
        fixture = Fixture(self)
        committed = fx.git(fixture.engine, "show", "-s", "--format=%cI", "HEAD")
        self.assertEqual(fixture.collect()["engine"]["committed"], committed)


class RefreshTests(unittest.TestCase):
    def clone(self, fixture: Fixture, origin: str) -> Path:
        bare = fixture.engine.parent / "engine.git"
        fx.git(fixture.engine, "clone", "-q", "--bare", str(fixture.engine), str(bare))
        fx.git(bare, "remote", "set-url", "origin", origin)
        return bare

    def test_a_refresh_that_fails_is_recorded_with_gits_error(self) -> None:
        fixture = Fixture(self)
        bare = self.clone(fixture, str(fixture.engine.parent / "no-such-origin"))
        result = architecture.collect(fixture.registry, engine_git=bare, refresh_engine=True)
        self.assertFalse(result["engine"]["refresh"]["ok"])
        self.assertIn("no-such-origin", result["engine"]["refresh"]["error"])
        self.assertEqual(result["engine"]["commit"], fixture.engine_commit)

    def test_a_refresh_that_works_reads_the_new_main(self) -> None:
        fixture = Fixture(self)
        bare = self.clone(fixture, str(fixture.engine))
        (fixture.engine / "opengwasdb" / "store" / "extra.py").write_text("x = 1\n")
        fx.git(fixture.engine, "add", "opengwasdb/store/extra.py")
        fx.git(fixture.engine, "-c", "user.name=t", "-c", "user.email=t@e.org", "commit", "-q", "-m", "more")
        head = fx.git(fixture.engine, "rev-parse", "HEAD")
        result = architecture.collect(fixture.registry, engine_git=bare, refresh_engine=True)
        self.assertEqual(result["engine"]["refresh"], {"ok": True, "error": None})
        self.assertEqual(result["engine"]["commit"], head)
        self.assertNotEqual(head, fixture.engine_commit)

    def test_no_refresh_is_recorded_as_not_attempted(self) -> None:
        self.assertIsNone(Fixture(self).collect()["engine"]["refresh"])


if __name__ == "__main__":
    unittest.main()
