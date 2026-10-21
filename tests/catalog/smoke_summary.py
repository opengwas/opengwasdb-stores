#!/usr/bin/env python3
"""Smoke render of the Analysis Catalog summary report (#325), run by `pixi run docs-smoke`.

Builds a fixture catalog with the real renderer (Quarto), then checks the HTML
for the lines the report must carry. It is not part of the `catalog` suite: it
needs Quarto and R.

Run from the repository root, in an environment with the reporting feature:
    pixi run -e dev python3 tests/catalog/smoke_summary.py
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from test_catalog import analysis_row, write_bundle

from analysis_catalog.build import build_catalog

EXPECTED = (
    "unknown (1 of 2 Stores report it)",
    "Analyses by Assigned Ancestry",
    "Trait Ontology Mapping coverage",
    "Excluded and refused Stores",
    "Change since the previous version",
    "No previous version: nothing to compare.",
    "OGS-00003",
    "Trial Store Release",
)


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html, flags=re.S))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="catalog_smoke_") as tmp:
        registry, root = Path(tmp, "stores"), Path(tmp, "artifact-root")
        registry.mkdir()
        write_bundle(registry, "OGS-00001", observed_extra={"n_associations": 12345})
        write_bundle(registry, "OGS-00002", rows=[analysis_row("B1", assigned_ancestry="", source_ancestry_label="META")])
        write_bundle(registry, "OGS-00003", release={"trial": True})
        result = build_catalog(registry, root)
        summary = result.path / "summary.html"
        text = text_of(summary.read_text(encoding="utf-8"))
        missing = [line for line in EXPECTED if line not in text]
        if missing:
            print(f"smoke render: {summary} lacks: {missing}", file=sys.stderr)
            return 1
        # A second version, so the change section reads a changes.tsv.
        write_bundle(registry, "OGS-00002", rows=[analysis_row("B1", assigned_ancestry="", source_ancestry_label="META"),
                                                  analysis_row("B2")])
        second = build_catalog(registry, root)
        changed = text_of((second.path / "summary.html").read_text(encoding="utf-8"))
        if "Analyses added 1 " not in changed:
            print(f"smoke render: {second.path / 'summary.html'} does not report the added Analysis\n{changed}", file=sys.stderr)
            return 1
        print(text)
        print(f"smoke render ok: {len(text)} characters of text, {summary.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
