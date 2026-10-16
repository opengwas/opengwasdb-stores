#!/usr/bin/env python3
"""pyproject.toml lists every package under src/manifest_generation (#249).

`[tool.setuptools] packages` is written out by hand, so a new provider
subpackage that is not added there would install from an editable checkout
(subpackages resolve through the filesystem) but be missing from any other
install. This test keeps the list equal to the directories that hold an
`__init__.py`.

Run from the repository root:
    python3 tests/packaging/test_packages.py
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "src" / "manifest_generation"

# Directories under the package that are deliberately not packages, and why.
NOT_PACKAGES: dict[str, str] = {
    # The pilot copies guard/sitecustomize.py into a run directory and puts that
    # on the resolver child's PYTHONPATH; it is never imported from here.
    "manifest_generation.providers.dbgap.mvp.pilot.guard": "a sitecustomize.py copied into a resolver child's PYTHONPATH",
}


def packages_on_disk() -> set[str]:
    return {
        ".".join(init.parent.relative_to(PACKAGE_ROOT.parent).parts)
        for init in PACKAGE_ROOT.rglob("__init__.py")
        if "__pycache__" not in init.parts
    }


def python_directories_on_disk() -> set[str]:
    return {
        ".".join(source.parent.relative_to(PACKAGE_ROOT.parent).parts)
        for source in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in source.parts
    }


class PackageListTest(unittest.TestCase):
    def setUp(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.listed = {
            name for name in pyproject["tool"]["setuptools"]["packages"]
            if name.split(".")[0] == "manifest_generation"
        }

    def test_pyproject_lists_exactly_the_manifest_generation_packages(self) -> None:
        self.assertEqual(self.listed, packages_on_disk())

    def test_every_directory_of_python_is_a_package_or_exempt(self) -> None:
        missing = python_directories_on_disk() - packages_on_disk() - set(NOT_PACKAGES)
        self.assertEqual(missing, set(), f"add an __init__.py, or list the directory in NOT_PACKAGES: {missing}")

    def test_every_exemption_is_still_a_directory_without_a_package(self) -> None:
        stale = {name for name in NOT_PACKAGES if name in packages_on_disk() or name not in python_directories_on_disk()}
        self.assertEqual(stale, set())


if __name__ == "__main__":
    unittest.main()
