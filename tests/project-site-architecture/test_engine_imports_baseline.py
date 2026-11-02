#!/usr/bin/env python3
"""This repository's imports of engine modules, held to a committed baseline (#421).

The engine's `CLAUDE.md` says this repository depends on the engine's command
line and its manifest columns. In fact many files here import engine modules
directly, and each import is a coupling nobody decided on: an engine refactor
can break it. Epic #416, decision 14, is to measure those imports and ratchet
them, the way the engine ratchets its own upward imports
(`docs/architecture/layering-baseline.yaml` in opengwas/opengwasdb,
opengwas/opengwasdb#359).

The unit is one **(importing file, imported name)** per entry, as the source
writes it and package-qualified, which is the engine's unit too. The imports are
measured with the graph functions the site's collector already uses
(`architecture_graph`), so the page's seam counts and this baseline can never
disagree about what an import is.

The fixtures are small git repositories from `architecture_fixtures.py` and
models written by hand, and every expectation is a literal. An expectation
computed the way the code computes it would pass by construction and could
never disagree with it.

Run from the repository root:
    python3 tests/project-site-architecture/test_engine_imports_baseline.py
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import architecture_fixtures as fx

from project_site import architecture_pages
from project_site.collect import architecture
from project_site.collect import architecture_graph as graph
from project_site.collect import architecture_model as model
from project_site.collect.common import CollectError

REPO = Path(__file__).resolve().parents[2]
BASELINE = "docs/architecture/engine-imports-baseline.yaml"
COLLECTED = "2026-11-02T12:00:00Z"


REGISTRY_MODEL = """\
repo: opengwas/opengwasdb-stores
components:
  - {id: gen, name: Manifest Generation, container: generation, stages: [build], summary: Makes manifests., paths: [src/gen/]}
  - {id: scripts, name: Scripts, container: generation, stages: [build], summary: Developer scripts., paths: [tools/]}
"""

# One file per import form the measurement must see, plus files it must not:
# `src/newpkg/` is claimed by no component, and `src/gen/other.py` imports the
# standard library, the registry and a package whose name only starts like the
# engine's.
REGISTRY_FILES = {
    "docs/architecture/components.yaml": REGISTRY_MODEL,
    "src/gen/__init__.py": "",
    "src/gen/from_module.py": "from opengwasdb.store import paths\n",
    "src/gen/import_module.py": "import opengwasdb.store.util\n",
    "src/gen/local.py": "def go():\n    from opengwasdb.query import facade\n    return facade\n",
    "src/gen/whole.py": "import opengwasdb\n",
    "src/gen/other.py": "import os\nimport opengwasdbx.thing\nfrom gen import local\n",
    "src/newpkg/thing.py": "from opengwasdb.store import paths\n",
    "tools/run.py": "from opengwasdb.cli import main\n",
}

ENGINE_IMPORTS = [
    {"file": "src/gen/from_module.py", "name": "opengwasdb.store.paths", "engine_component": None},
    {"file": "src/gen/import_module.py", "name": "opengwasdb.store.util", "engine_component": None},
    {"file": "src/gen/local.py", "name": "opengwasdb.query.facade", "engine_component": None},
    {"file": "src/gen/whole.py", "name": "opengwasdb", "engine_component": None},
    {"file": "tools/run.py", "name": "opengwasdb.cli.main", "engine_component": None},
]


def measure(
    test: unittest.TestCase,
    registry_files: dict[str, str] = REGISTRY_FILES,
    engine_files: dict[str, str] | None = None,
) -> list[dict]:
    """The engine imports the fixture repositories make, through the site's own graph functions."""
    root = Path(tempfile.mkdtemp(prefix="baseline-"))
    test.addCleanup(shutil.rmtree, root, True)
    fx.make_repo(root / "registry", registry_files)
    trees: dict[str, object] = {"registry": architecture.Tree(root / "registry", "HEAD")}
    claims = {"registry": model.claims_of(model.load_model(registry_files["docs/architecture/components.yaml"])["components"])}
    if engine_files is not None:
        fx.make_repo(root / "engine", engine_files)
        trees["engine"] = architecture.Tree(root / "engine", "main")
        engine_model = model.load_model(engine_files["docs/architecture/components.yaml"])
        claims["engine"] = model.claims_of(engine_model["components"])
    return graph.engine_imports(graph.imports_of(trees, claims))


class MeasurementTests(unittest.TestCase):
    def test_each_import_form_gives_one_file_and_name_entry(self) -> None:
        # `from opengwasdb.store import paths`, `import opengwasdb.store.util`, a
        # function-local import, `import opengwasdb` whole, and a claimed script
        # under tools/: one entry each. The unclaimed `src/newpkg/thing.py` is not
        # measured -- the ownership test fails on that file instead.
        self.assertEqual(measure(self), ENGINE_IMPORTS)

    def test_a_name_that_only_starts_like_the_engine_is_not_one_of_its_modules(self) -> None:
        self.assertNotIn("src/gen/other.py", [entry["file"] for entry in measure(self)])

    def test_the_engine_component_is_named_when_the_engine_is_read(self) -> None:
        # The same measurement with the engine's model in reach: each name resolves
        # to the component that claims the engine module it lands in.
        self.assertEqual(
            measure(self, fx.REGISTRY_FILES, fx.ENGINE_FILES),
            [
                {"file": "src/gen/make.py", "name": "opengwasdb.model.enums", "engine_component": "model"},
                {"file": "src/gen/make.py", "name": "opengwasdb.store.paths", "engine_component": "store"},
                {"file": "src/gen/make.py", "name": "opengwasdb.store.util", "engine_component": "store"},
                {"file": "src/ogs/run.py", "name": "opengwasdb.cli.main", "engine_component": "cli"},
                {"file": "tools/run.py", "name": "opengwasdb.cli.main", "engine_component": "cli"},
            ],
        )


BASELINE_YAML = """\
# Reviewed by a person; it only shrinks.
imports:
  - file: src/gen/make.py
    name: opengwasdb.store.paths
  - file: tools/run.py
    name: opengwasdb.cli.main
"""
# The same two entries as `load_baseline` returns them: sorted by (file, name).
ENTRIES = (("src/gen/make.py", "opengwasdb.store.paths"), ("tools/run.py", "opengwasdb.cli.main"))


def entry(file: str, name: str, component: str | None = None) -> dict:
    return {"file": file, "name": name, "engine_component": component}


def measured(*extra: dict) -> list[dict]:
    """What this repository measures: the baseline's imports, and any `extra`."""
    return [entry(f, n) for f, n in ENTRIES] + list(extra)


class BaselineFileTests(unittest.TestCase):
    def test_a_baseline_loads_as_file_and_name_pairs_sorted(self) -> None:
        self.assertEqual(graph.load_baseline(BASELINE_YAML), ENTRIES)

    def test_an_entry_listed_twice_fails_naming_it(self) -> None:
        text = BASELINE_YAML + "  - file: tools/run.py\n    name: opengwasdb.cli.main\n"
        with self.assertRaises(CollectError) as caught:
            graph.load_baseline(text)
        self.assertIn("tools/run.py imports opengwasdb.cli.main is listed twice", str(caught.exception))

    def test_an_entry_that_is_not_a_file_and_name_pair_fails(self) -> None:
        with self.assertRaises(CollectError) as caught:
            graph.load_baseline("imports:\n  - file: src/gen/make.py\n")
        self.assertIn("entry #0 must be a mapping with only 'file' and 'name'", str(caught.exception))

    def test_a_file_that_is_not_a_baseline_fails(self) -> None:
        with self.assertRaises(CollectError) as caught:
            graph.load_baseline("measurements: []\n")
        self.assertIn("a mapping with an `imports` list", str(caught.exception))


class CheckTests(unittest.TestCase):
    def test_an_engine_import_the_baseline_does_not_list_is_reported_with_its_file_and_name(self) -> None:
        self.assertEqual(
            graph.check_engine_imports(measured(entry("src/gen/make.py", "opengwasdb.model.enums")), ENTRIES),
            [
                "src/gen/make.py: imports opengwasdb.model.enums, which "
                f"{BASELINE} does not list; a new engine import is a coupling nobody decided on,"
                " and that file only shrinks"
            ],
        )

    def test_a_new_import_names_the_engine_component_when_it_is_known(self) -> None:
        self.assertEqual(
            graph.check_engine_imports(measured(entry("src/gen/make.py", "opengwasdb.model.enums", "model")), ENTRIES),
            [
                "src/gen/make.py: imports opengwasdb.model.enums from the engine component model, which "
                f"{BASELINE} does not list; a new engine import is a coupling nobody decided on,"
                " and that file only shrinks"
            ],
        )

    def test_a_listed_import_that_is_gone_is_reported_as_stale(self) -> None:
        self.assertEqual(
            graph.check_engine_imports([entry("tools/run.py", "opengwasdb.cli.main")], ENTRIES),
            [
                "src/gen/make.py: imports opengwasdb.store.paths, listed in "
                f"{BASELINE}, is gone; delete the entry"
            ],
        )

    def test_measured_imports_the_baseline_lists_are_clean(self) -> None:
        self.assertEqual(graph.check_engine_imports(measured(), ENTRIES), [])

    def test_an_import_of_another_file_with_the_same_name_is_not_the_listed_one(self) -> None:
        # The entry is (file, name): the same name imported by another file is a new import.
        problems = graph.check_engine_imports(measured(entry("tools/other.py", "opengwasdb.cli.main")), ENTRIES)
        self.assertEqual(
            problems,
            [
                "tools/other.py: imports opengwasdb.cli.main, which "
                f"{BASELINE} does not list; a new engine import is a coupling nobody decided on,"
                " and that file only shrinks"
            ],
        )


class WorkingTree:
    """This checkout's tracked files, read from disk.

    The collector reads a repository through git at a ref; the test that gates a
    pull request must see the working tree, so that a new import fails before it
    is committed. `files` and `read` are the two things the graph functions use
    of a tree.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
        ).stdout.decode()
        self.files = model.split_nul(listing)

    def read(self, paths: list[str]) -> dict[str, str]:
        return {
            path: (self.root / path).read_text(encoding="utf-8", errors="replace")
            for path in paths
            if (self.root / path).is_file()
        }


def this_repository() -> list[dict]:
    """What this checkout imports from the engine, measured the way the collector measures it."""
    loaded = model.load_model((REPO / "docs/architecture/components.yaml").read_text(encoding="utf-8"))
    claims = {"registry": model.claims_of(loaded["components"])}
    return graph.engine_imports(graph.imports_of({"registry": WorkingTree(REPO)}, claims))


class ThisRepositoryTests(unittest.TestCase):
    def test_every_engine_import_is_in_the_baseline_and_every_entry_is_still_imported(self) -> None:
        found = this_repository()
        self.assertGreater(len(found), 50)  # the seam is wide, not a corner of it
        baseline = graph.load_baseline((REPO / BASELINE).read_text(encoding="utf-8"))
        self.assertEqual(graph.check_engine_imports(found, baseline), [])


BASELINE_FOR_THE_FIXTURE = """\
imports:
  - file: src/gen/make.py
    name: opengwasdb.model.enums
  - file: src/gen/make.py
    name: opengwasdb.store.paths
  - file: src/gen/make.py
    name: opengwasdb.store.util
  - file: src/ogs/run.py
    name: opengwasdb.cli.main
  - file: tools/run.py
    name: opengwasdb.cli.main
"""


class Fixture:
    """This repository and the engine as two small git repositories, with a baseline in this one."""

    def __init__(self, test: unittest.TestCase, *, baseline: str | None = BASELINE_FOR_THE_FIXTURE) -> None:
        root = Path(tempfile.mkdtemp(prefix="baseline-"))
        test.addCleanup(shutil.rmtree, root, True)
        registry = dict(fx.REGISTRY_FILES)
        if baseline is not None:
            registry[BASELINE] = baseline
        self.registry = root / "registry"
        self.engine = root / "engine"
        self.registry_commit = fx.make_repo(self.registry, registry)
        fx.make_repo(self.engine, fx.ENGINE_FILES)

    def collect(self) -> dict:
        return architecture.collect(self.registry, engine_git=self.engine, engine_ref="main")

    def page(self) -> str:
        result = self.collect()
        return architecture_pages.render_structure(
            {"architecture": {"source": "architecture", "available": True, "collected_at": COLLECTED, "error": None, **result}}
        )


class CollectorTests(unittest.TestCase):
    def test_the_collector_reports_the_baselines_size_and_what_it_measures(self) -> None:
        result = Fixture(self).collect()
        self.assertEqual(
            result["baseline"], {"path": BASELINE, "entries": 5, "measured": 5, "note": None}
        )
        # The same five imports the seam counts: one measurement, two views of it.
        self.assertEqual(result["summary"]["registry_to_engine_imports"], 5)

    def test_a_repository_without_a_baseline_reports_an_unknown_size_not_zero(self) -> None:
        fixture = Fixture(self, baseline=None)
        baseline = fixture.collect()["baseline"]
        self.assertIsNone(baseline["entries"])
        self.assertEqual(baseline["measured"], 5)
        self.assertIn(f"{BASELINE} is not in opengwas/opengwasdb-stores at", baseline["note"])
        self.assertIn(fixture.registry_commit[:12], baseline["note"])


class StructurePageTests(unittest.TestCase):
    def test_the_page_states_the_baselines_size_next_to_the_seam_counts(self) -> None:
        page = Fixture(self).page()
        self.assertIn("5 imports from this repository into the engine", page)
        self.assertIn(
            f"**The engine-import baseline** (`{BASELINE}`) lists 5 imports of engine modules,"
            " one per importing file and imported name; this repository makes 5 of them.",
            page,
        )

    def test_an_unknown_baseline_says_so_and_never_shows_a_zero(self) -> None:
        page = Fixture(self, baseline=None).page()
        self.assertIn(f"**The engine-import baseline is unknown**: {BASELINE} is not in opengwas/opengwasdb-stores at", page)
        self.assertIn("This repository makes 5 imports of engine modules.", page)
        self.assertNotIn("lists 0 imports", page)


if __name__ == "__main__":
    unittest.main()
