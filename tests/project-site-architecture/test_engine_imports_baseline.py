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
import tempfile
import unittest
from pathlib import Path

import architecture_fixtures as fx

from project_site.collect import architecture
from project_site.collect import architecture_graph as graph
from project_site.collect import architecture_model as model

REPO = Path(__file__).resolve().parents[2]


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


if __name__ == "__main__":
    unittest.main()
