#!/usr/bin/env python3
"""No per-source module computes ancestry, allele alignment or phenotype SD (#287, ADR 0036).

ADR 0036: a source plugs into Manifest Generation through files, and
OpenGWASDB measures what release decisions need -- ancestry fit, allele
alignment, phenotype SD -- in Analysis Resolution, which the shared pipeline
drives (``opengwasdb resolve-analyses``). Per-source code acquires and
describes; it never computes those statistics, and never drives the
computation itself. Epic #282's definition of done asks for exactly that.

This scans every module under ``src/manifest_generation/providers/`` -- the
per-Source-Provider connector code -- with the AST, and refuses:

- an import of OpenGWASDB's ancestry, phenotype-SD, allele-orientation or
  resolution internals, or of the AF extraction they read through
  (:data:`FORBIDDEN_MODULES`, :data:`FORBIDDEN_NAMES`);
- an import of a shared helper in this repository that does it
  (:data:`FORBIDDEN_HELPERS`);
- a string naming one of those modules (a ``python3 -m`` or
  ``importlib.import_module`` route around the import check), or naming an
  OpenGWASDB subcommand that computes them (:data:`FORBIDDEN_SUBCOMMANDS`).
  Docstrings are not scanned.

Provider modules that still do it retire with their sources' moves onto the
pipeline. Each is on :data:`EXEMPTIONS`, citing its ticket; an exemption that
no longer finds a violation fails, so the list shrinks as the tickets land.

Run from the repository root:
    python3 tests/packaging/test_per_source_statistics.py
"""

from __future__ import annotations

import ast
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = REPO_ROOT / "src" / "manifest_generation" / "providers"

#: OpenGWASDB modules that compute ancestry, phenotype SD or resolution, or
#: whose only job is feeding them. Any import from them, or a submodule, is
#: per-source statistics.
FORBIDDEN_MODULES: tuple[str, ...] = (
    "opengwasdb.ancestry",               # the mixture fit, gates, reference loading
    "opengwasdb.build.phenotype_sd",     # phenotype-SD estimation (ADR 0029 tiers)
    "opengwasdb.build.resolve",          # Analysis Resolution, in-process
    "opengwasdb.build.resolve_manifest",  # resolve-analyses' implementation
)

#: Single OpenGWASDB functions that align alleles or extract allele
#: frequencies at reference sites for ancestry and SD, from modules whose other
#: names (readers, enums) per-source code may use.
FORBIDDEN_NAMES: frozenset[tuple[str, str]] = frozenset(
    {
        ("opengwasdb.variants.normalise", "orient_to_canonical"),  # allele alignment
        ("opengwasdb.readers", "is_palindromic"),                    # strand alignment
        ("opengwasdb.readers.tabular", "extract_at_sites"),          # AF at reference sites
    }
)

#: Shared helpers in this repository that compute ancestry or phenotype SD.
FORBIDDEN_HELPERS: tuple[str, ...] = (
    "manifest_generation.phenotype_sd_estimate",
    "manifest_generation.qc_panel_concordance",
    "manifest_generation.providers.opengwas.annotate",
    "manifest_generation.providers.gwas_catalog.ancestry_assign",
)

#: OpenGWASDB subcommands that compute them. The shared pipeline invokes
#: ``resolve-analyses``; per-source code invoking any of these drives the
#: computation itself.
FORBIDDEN_SUBCOMMANDS: frozenset[str] = frozenset(
    {"assign-ancestry", "estimate-phenotype-sd", "resolve-analyses"}
)

#: Provider modules that still compute or drive these statistics, each with the
#: ticket that moves its source onto the shared pipeline and retires it.
EXEMPTIONS: dict[str, str] = {
    # FinnGen R13 and ukb-b dense generators call it for AF-based ancestry and
    # phenotype SD (resources/generators/lib/opengwas_gwas_vcf_dense.R).
    "opengwas/annotate.py": "#288 (FinnGen), #289 (ukb-b)",
}

#: Tickets an exemption may cite: the source moves still to come (epic #282).
RETIRING_TICKETS: frozenset[str] = frozenset({"#288", "#289", "#291"})


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of every docstring constant, which describe rather than invoke."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    found.add(id(body[0].value))
    return found


def _forbidden_module(module: str) -> str | None:
    for forbidden in FORBIDDEN_MODULES + FORBIDDEN_HELPERS:
        if module == forbidden or module.startswith(forbidden + "."):
            return forbidden
    return None


def violations(source: str, *, filename: str = "<source>") -> list[str]:
    """Every way ``source`` imports, invokes or names per-source statistics."""
    tree = ast.parse(source, filename=filename)
    docstrings = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (hit := _forbidden_module(alias.name)) is not None:
                    found.append(f"line {node.lineno}: imports {alias.name} ({hit})")
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if (hit := _forbidden_module(node.module)) is not None:
                found.append(f"line {node.lineno}: imports from {node.module} ({hit})")
                continue
            for alias in node.names:
                qualified = f"{node.module}.{alias.name}"
                if (hit := _forbidden_module(qualified)) is not None:
                    found.append(f"line {node.lineno}: imports {qualified} ({hit})")
                elif (node.module, alias.name) in FORBIDDEN_NAMES:
                    found.append(f"line {node.lineno}: imports {qualified}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            text = node.value.strip()
            if text in FORBIDDEN_SUBCOMMANDS:
                found.append(f"line {node.lineno}: invokes opengwasdb {text}")
            elif (hit := _forbidden_module(text)) is not None:
                found.append(f"line {node.lineno}: names {text} ({hit})")
    return found


def provider_modules() -> list[Path]:
    return sorted(PROVIDERS.rglob("*.py"))


class PerSourceStatisticsTests(unittest.TestCase):
    def test_no_provider_module_computes_ancestry_alignment_or_phenotype_sd(self) -> None:
        offending: dict[str, list[str]] = {}
        for path in provider_modules():
            relative = path.relative_to(PROVIDERS).as_posix()
            if relative in EXEMPTIONS:
                continue
            found = violations(path.read_text(encoding="utf-8"), filename=str(path))
            if found:
                offending[relative] = found
        self.assertEqual(
            offending,
            {},
            "per-source code must leave ancestry, allele alignment and phenotype SD to "
            "Analysis Resolution, which the shared pipeline drives (ADR 0036)",
        )

    def test_the_scan_covers_the_provider_modules(self) -> None:
        names = {path.relative_to(PROVIDERS).as_posix() for path in provider_modules()}
        for expected in (
            "gwas_catalog/source_metadata_adapter.py",
            "gwas_catalog/write_acquisition_manifest.py",
            "ukb_b/generate_candidate.py",
            "dbgap/metadata.py",
            "dbgap/mvp/source_metadata_adapter.py",
            "dbgap/mvp/acquire_dbgap_phs002453.py",
            "finngen/acquire.py",
            "eqtlgen/acquire.py",
            "eqtlgen/source_metadata_adapter.py",
        ):
            self.assertIn(expected, names)

    def test_every_exemption_is_still_needed(self) -> None:
        """An exemption that finds nothing to exempt must be removed."""
        for relative, ticket in EXEMPTIONS.items():
            with self.subTest(module=relative):
                path = PROVIDERS / relative
                self.assertTrue(
                    path.is_file(),
                    f"{relative} is gone: remove its exemption ({ticket})",
                )
                self.assertNotEqual(
                    violations(path.read_text(encoding="utf-8"), filename=str(path)),
                    [],
                    f"{relative} no longer computes per-source statistics: remove its "
                    f"exemption ({ticket})",
                )

    def test_every_exemption_cites_a_ticket_that_retires_it(self) -> None:
        for relative, ticket in EXEMPTIONS.items():
            with self.subTest(module=relative):
                cited = {word.strip("(),") for word in ticket.split() if word.startswith("#")}
                self.assertTrue(cited, f"{relative}'s exemption cites no ticket")
                self.assertLessEqual(cited, RETIRING_TICKETS)


class ScannerControlTests(unittest.TestCase):
    """Positive and negative controls: the scan catches each route, and only those."""

    def assertCaught(self, source: str, fragment: str) -> None:
        found = violations(textwrap.dedent(source))
        self.assertTrue(found, f"not caught:\n{source}")
        self.assertIn(fragment, " ".join(found))

    def test_each_route_to_per_source_statistics_is_caught(self) -> None:
        cases = {
            "from opengwasdb.ancestry import assign_ancestry\n": "opengwasdb.ancestry",
            "from opengwasdb.ancestry.mixture import Gates\n": "opengwasdb.ancestry",
            "import opengwasdb.build.phenotype_sd as sd\n": "opengwasdb.build.phenotype_sd",
            "from opengwasdb.build import phenotype_sd\n": "opengwasdb.build.phenotype_sd",
            "from opengwasdb.build.resolve import resolve_analysis\n": "opengwasdb.build.resolve",
            "from opengwasdb.variants.normalise import orient_to_canonical\n": "orient_to_canonical",
            "from opengwasdb.readers import is_palindromic\n": "is_palindromic",
            "from opengwasdb.readers.tabular import extract_at_sites\n": "extract_at_sites",
            "from manifest_generation import phenotype_sd_estimate\n": "phenotype_sd_estimate",
            "from manifest_generation.providers.opengwas import annotate\n": "annotate",
            "import manifest_generation.qc_panel_concordance\n": "qc_panel_concordance",
            "import importlib\nimportlib.import_module('opengwasdb.ancestry')\n": "opengwasdb.ancestry",
            "argv = ['python3', '-m', 'manifest_generation.providers.gwas_catalog.ancestry_assign']\n":
                "ancestry_assign",
            "subprocess.run(['opengwasdb', 'assign-ancestry', manifest])\n": "assign-ancestry",
            "subprocess.run(['opengwasdb', 'estimate-phenotype-sd'])\n": "estimate-phenotype-sd",
            "argv = ['opengwasdb', 'resolve-analyses', manifest, records]\n": "resolve-analyses",
            "def f():\n    from opengwasdb.ancestry import load_reference\n": "opengwasdb.ancestry",
        }
        for source, fragment in cases.items():
            with self.subTest(source=source):
                self.assertCaught(source, fragment)

    def test_what_per_source_code_may_use_is_not_caught(self) -> None:
        allowed = '''
            """Acquire files; never call assign_ancestry or estimate-phenotype-sd here."""
            from opengwasdb.model.enums import OriginalSdMethod, StoredEffectScale
            from opengwasdb.model.analyses import SampleSizeKind
            from opengwasdb.readers.effect_source import detect_effect_source
            from opengwasdb.variants.normalise import normalise_chromosome
            from manifest_generation.source_ancestry import load_source_label_translation
            from manifest_generation.ancestry_sidecar import format_sidecar_float
            from . import ancestry_helpers

            def describe():
                """The resolver's resolve-analyses measures ancestry."""
                return "ancestry is measured by Analysis Resolution"
        '''
        self.assertEqual(violations(textwrap.dedent(allowed)), [])

    def test_every_exempt_module_is_caught_by_the_scan(self) -> None:
        """Each exemption names a module the scan would otherwise fail on."""
        for relative in EXEMPTIONS:
            with self.subTest(module=relative):
                path = PROVIDERS / relative
                self.assertTrue(violations(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
