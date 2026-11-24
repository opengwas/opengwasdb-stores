#!/usr/bin/env python3
"""`tools/benchmarks/observed-view-check.py` maps a parent's axis into a child's through the
engine's ALID index, so it reads a 0.2 release (fixed-width pair) and a 0.3.0 one (sidecar)
alike (opengwas/opengwasdb-stores#498)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layouts  # noqa: E402

from opengwasdb.variants.normalise import CanonicalVariant  # noqa: E402

TOOL = Path(__file__).resolve().parents[2] / "tools" / "benchmarks" / "observed-view-check.py"


def variant(position: int, alt: str = "G") -> CanonicalVariant:
    return CanonicalVariant("1", position, "A", alt)


def load_tool():
    spec = importlib.util.spec_from_file_location("observed_view_check", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AxisMapTest(unittest.TestCase):
    LAYOUT = "0.3"

    def release(self, root: Path, name: str, variants: list[CanonicalVariant]) -> Path:
        path = root / name
        path.mkdir()
        (path / "manifest.json").write_text(json.dumps({"format_version": layouts.FORMAT_VERSION[self.LAYOUT]}))
        layouts.write_axis(path, variants, {}, self.LAYOUT)
        return path

    def test_every_parent_row_finds_its_child_row_and_the_order_is_kept(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = Path(tmp)
            parent = self.release(root, "parent", [variant(100), variant(200), variant(300)])
            child = self.release(root, "child", [variant(100), variant(150), variant(200), variant(300), variant(400)])
            p2c, chrom, stats = load_tool().axis_map(parent, child)
        self.assertEqual(p2c.tolist(), [0, 2, 3])
        self.assertEqual(list(chrom), ["1", "1", "1"])
        self.assertEqual(stats["parent_rows_unmapped"], 0)
        self.assertEqual(stats["parent_rows_indexed"], 3)
        self.assertEqual(stats["child_rows_indexed"], 5)
        self.assertIs(stats["order_preserved"], True)

    def test_a_parent_row_the_child_lacks_is_unmapped(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = Path(tmp)
            parent = self.release(root, "parent", [variant(100), variant(200)])
            child = self.release(root, "child", [variant(100)])
            p2c, _, stats = load_tool().axis_map(parent, child)
        self.assertEqual(p2c.tolist(), [0, -1])
        self.assertEqual(stats["parent_rows_unmapped"], 1)


class AxisMapOnFormat0_2(AxisMapTest):
    LAYOUT = "0.2"


if __name__ == "__main__":
    unittest.main()
