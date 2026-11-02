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
             "gen": "registry", "ogs": "registry"},
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
        self.assertEqual([c["id"] for c in result["components"]], ["gen", "ogs"])
        self.assertEqual(result["gaps"]["engine"], {"unclaimed": [], "stale": []})
        self.assertEqual(result["model"]["layers"], [])

    def test_component_size_is_counted_from_the_files_it_claims(self) -> None:
        result = Fixture(self).collect()
        store = next(c for c in result["components"] if c["id"] == "store")
        self.assertEqual((store["files"], store["lines"]), (3, 3))


if __name__ == "__main__":
    unittest.main()
