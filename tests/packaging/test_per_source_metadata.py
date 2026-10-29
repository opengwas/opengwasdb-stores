#!/usr/bin/env python3
"""Shared pipeline code reads no source's own metadata files (#347, ADR 0036).

ADR 0036: a source plugs into Manifest Generation through files. Its adapter
reads the source's own metadata -- a GWAS-SSF ``-meta.yaml`` sidecar, the
downloader's pass manifests -- and writes canonical connector columns; shared
code reads only those columns. The Source Metadata Table's
``genotyping_technology`` and ``source_genome_build`` are what the MAF
exemption and preflight's build check read, so no shared module needs
``genome_assembly`` or a sidecar path.

This scans every module directly under ``src/manifest_generation/`` -- shared
code, everything outside ``providers/`` -- with the AST, and refuses:

- a string naming ``genome_assembly``, a ``-meta.yaml`` sidecar or the download
  passes' ``freeze_inputs`` key (:data:`FORBIDDEN_STRINGS`); docstrings and
  comments describe rather than read, and are not scanned;
- a name, attribute, import, parameter or definition called
  ``freeze_inputs`` or ``read_acquisition_pass`` (:data:`FORBIDDEN_NAMES`);
- ``genotyping_technology`` as a string in a scope that also parses YAML. The
  canonical column of that name is read from the Source Metadata Table, so the
  name alone is allowed; reading it from a YAML document is what is refused.

:data:`EXEMPT` lists what must stay, each with its reason.

Run from the repository root:
    python3 tests/packaging/test_per_source_metadata.py
"""

from __future__ import annotations

import ast
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED = REPO_ROOT / "src" / "manifest_generation"

#: Strings that only a source's own metadata reader has a use for. A match is a
#: substring, so a path built as ``f"{name}-meta.yaml"`` is caught too.
FORBIDDEN_STRINGS: tuple[str, ...] = ("genome_assembly", "-meta.yaml", "freeze_inputs")

#: Identifiers of the GWAS Catalog download-pass machinery, which lives in
#: ``providers/gwas_catalog/write_acquisition_manifest.py``.
FORBIDDEN_NAMES: frozenset[str] = frozenset({"freeze_inputs", "read_acquisition_pass"})

#: The canonical column whose name is also the YAML key sidecars use.
TECHNOLOGY = "genotyping_technology"

#: Module (relative to ``src/manifest_generation``) -> the forbidden finding
#: fragments it may keep, each with the reason. Empty: after #347 no shared
#: module needs one, including the GWAS Catalog-specific concordance modules.
EXEMPT: dict[str, dict[str, str]] = {}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of every docstring constant, which describe rather than read."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    found.add(id(body[0].value))
    return found


def _scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """The nodes of ``scope``: a function's whole body, or a module outside its functions."""
    if not isinstance(scope, ast.Module):
        return list(ast.walk(scope))
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = list(scope.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return nodes


def _parses_yaml(nodes: list[ast.AST]) -> bool:
    for node in nodes:
        if isinstance(node, ast.Name) and node.id == "yaml":
            return True
        if isinstance(node, ast.Attribute) and node.attr in {"safe_load", "safe_load_all", "load"}:
            return True
    return False


def violations(source: str, *, filename: str = "<source>") -> list[str]:
    """Every way ``source`` reads a source's own metadata."""
    tree = ast.parse(source, filename=filename)
    docstrings = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.alias):
            names = [node.name.rsplit(".", 1)[-1], node.asname or ""]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, ast.keyword) and node.arg:
            names = [node.arg]
        for name in names:
            if name in FORBIDDEN_NAMES:
                found.append(f"line {getattr(node, 'lineno', 0)}: uses {name}")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            for forbidden in FORBIDDEN_STRINGS:
                if forbidden in node.value:
                    found.append(f"line {node.lineno}: names {forbidden}")
    scopes = [tree] + [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in scopes:
        nodes = _scope_nodes(scope)
        technologies = [
            node
            for node in nodes
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == TECHNOLOGY
            and id(node) not in docstrings
        ]
        if technologies and _parses_yaml(nodes):
            label = getattr(scope, "name", "<module>")
            found.append(
                f"line {technologies[0].lineno}: reads {TECHNOLOGY} where {label} parses YAML"
            )
    return sorted(set(found))


def shared_modules() -> list[Path]:
    return sorted(SHARED.glob("*.py"))


class PerSourceMetadataTests(unittest.TestCase):
    def test_no_shared_module_reads_a_sources_own_metadata(self) -> None:
        offending: dict[str, list[str]] = {}
        for path in shared_modules():
            relative = path.relative_to(SHARED).as_posix()
            kept = EXEMPT.get(relative, {})
            found = [
                finding
                for finding in violations(path.read_text(encoding="utf-8"), filename=str(path))
                if not any(fragment in finding for fragment in kept)
            ]
            if found:
                offending[relative] = found
        self.assertEqual(
            offending,
            {},
            "shared code reads canonical connector columns only; a source's adapter reads "
            "its own metadata files (ADR 0036, #347)",
        )

    def test_every_exemption_is_still_needed_and_has_a_reason(self) -> None:
        for relative, fragments in EXEMPT.items():
            path = SHARED / relative
            self.assertTrue(path.is_file(), f"{relative} is exempt but does not exist")
            found = violations(path.read_text(encoding="utf-8"), filename=str(path))
            for fragment, reason in fragments.items():
                self.assertTrue(reason.strip(), f"{relative}: exemption {fragment!r} has no reason")
                self.assertTrue(
                    any(fragment in finding for finding in found),
                    f"{relative}: exemption {fragment!r} is no longer needed",
                )

    def test_the_scan_covers_the_shared_modules_and_not_the_providers(self) -> None:
        names = {path.relative_to(SHARED).as_posix() for path in shared_modules()}
        for expected in (
            "candidate_workflow.py",
            "source_inventory.py",
            "freeze.py",
            "source_metadata_table.py",
            "qc_panel_concordance.py",
            "release_config.py",
        ):
            self.assertIn(expected, names)
        self.assertFalse([name for name in names if "/" in name])


class ScannerControlTests(unittest.TestCase):
    """Positive and negative controls: the scan catches each route, and only those."""

    def assertCaught(self, source: str, fragment: str) -> None:
        found = violations(textwrap.dedent(source))
        self.assertTrue(found, f"not caught:\n{source}")
        self.assertIn(fragment, " ".join(found))

    def test_each_route_to_a_sources_own_metadata_is_caught(self) -> None:
        cases = {
            "def read(path):\n    return yaml.safe_load(path.read_text()).get('genome_assembly')\n":
                "genome_assembly",
            "sidecar = f'{name}-meta.yaml'\n": "-meta.yaml",
            "passes = source['inventory']['freeze_inputs']\n": "freeze_inputs",
            "from manifest_generation.providers.gwas_catalog.write_acquisition_manifest "
            "import read_acquisition_pass\n": "read_acquisition_pass",
            "def read_acquisition_pass(path):\n    return []\n": "read_acquisition_pass",
            "def f(config):\n    return config.freeze_inputs\n": "freeze_inputs",
            "import yaml\n"
            "def technologies(path):\n"
            "    return yaml.safe_load(path.read_text()).get('genotyping_technology')\n":
                "reads genotyping_technology",
            "import yaml\n"
            "document = yaml.safe_load(open('x'))\n"
            "technologies = document['genotyping_technology']\n": "reads genotyping_technology",
        }
        for source, fragment in cases.items():
            with self.subTest(source=source):
                self.assertCaught(source, fragment)

    def test_what_shared_code_may_do_is_not_caught(self) -> None:
        allowed = '''
            """Reads genome_assembly and -meta.yaml sidecars only in this docstring."""
            import yaml

            def metadata(row):
                """The Source Metadata Table's genotyping_technology column."""
                return row.get("genotyping_technology", "")

            def config(path):
                return yaml.safe_load(path.read_text())

            def unrelated():
                return "sidecar_location"
        '''
        self.assertEqual(violations(textwrap.dedent(allowed)), [])


if __name__ == "__main__":
    unittest.main()
