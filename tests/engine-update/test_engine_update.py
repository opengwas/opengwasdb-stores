#!/usr/bin/env python3
"""`pixi run engine-update` moves the lock to `main`'s tip, or leaves it as it was (#253).

A fake runner stands in for `git`, `pixi update` and each environment's engine
probe, so these tests move a written lock, never the repository's.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ogstores import runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "engine_update.py"

OLD = "0d59f151c7618933747a59f825deeef6a51de9e1"
NEW = "bc22e8476c4378af582477f09b56af01e3f5f065"


def _load_tool():
    spec = importlib.util.spec_from_file_location("engine_update_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _lock_text(commit: str, version: str) -> str:
    ref = f"git+https://github.com/opengwas/opengwasdb?branch=main#{commit}"
    return (
        "version: 7\nenvironments:\n  default:\n    packages:\n      linux-64:\n"
        f"      - pypi: {ref}\npackages:\n- pypi: {ref}\n  name: opengwasdb\n  version: {version}\n"
    )


class FakeRunner:
    """Answers git status, `pixi update` (writing `relocked`) and the engine probe."""

    def __init__(self, lock: Path, *, relocked: str, installed: str, written: str, dirty: str = ""):
        self.lock, self.relocked, self.installed, self.written, self.dirty = lock, relocked, installed, written, dirty
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, stdout=self.dirty, stderr="")
        if argv[1:3] == ["update", "opengwasdb"]:
            self.lock.write_text(self.relocked, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1] == "run":
            payload = json.dumps([self.installed, "0.3.9", self.written])
            return subprocess.CompletedProcess(argv, 0, stdout=payload + "\n", stderr="")
        raise AssertionError(f"unexpected command {argv}")


class EngineUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load_tool()
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))))
        self.lock, self.manifest = root / "pixi.lock", root / "pixi.toml"
        self.before = _lock_text(OLD, "0.3.2")
        self.lock.write_text(self.before, encoding="utf-8")
        self.manifest.write_text(
            '[environments]\ndefault = { features = ["python", "store-build"] }\n'
            'docs = { features = ["r"] }\n',
            encoding="utf-8",
        )

    def _runner(self, **kwargs) -> FakeRunner:
        kwargs.setdefault("relocked", _lock_text(NEW, "0.3.9"))
        kwargs.setdefault("installed", NEW)
        kwargs.setdefault("written", runtime.REQUIRED_FORMAT_VERSION)
        return FakeRunner(self.lock, **kwargs)

    def test_an_update_moves_the_lock_and_reports_both_ends(self) -> None:
        run = self._runner()
        self.assertEqual(
            self.tool.update(self.lock, self.manifest, run=run), (OLD, "0.3.2", NEW, "0.3.9")
        )
        self.assertIn(NEW, self.lock.read_text(encoding="utf-8"))
        probed = [call for call in run.calls if call[1] == "run"]
        self.assertEqual([call[call.index("-e") + 1] for call in probed], ["default"], "only store-build environments")

    def test_an_engine_writing_another_format_is_refused_and_the_lock_restored(self) -> None:
        with self.assertRaises(self.tool.EngineUpdateError) as ctx:
            self.tool.update(self.lock, self.manifest, run=self._runner(written="0.3.0"))
        self.assertIn("0.3.0", str(ctx.exception))
        self.assertIn(runtime.REQUIRED_FORMAT_VERSION, str(ctx.exception))
        self.assertEqual(self.lock.read_text(encoding="utf-8"), self.before)

    def test_an_environment_installing_another_commit_is_refused_and_the_lock_restored(self) -> None:
        with self.assertRaises(self.tool.EngineUpdateError) as ctx:
            self.tool.update(self.lock, self.manifest, run=self._runner(installed=OLD))
        self.assertIn(OLD, str(ctx.exception))
        self.assertEqual(self.lock.read_text(encoding="utf-8"), self.before)

    def test_a_relock_with_two_commits_is_refused_and_the_lock_restored(self) -> None:
        two = _lock_text(NEW, "0.3.9").replace(f"      - pypi: git+https://github.com/opengwas/opengwasdb?branch=main#{NEW}",
                                                f"      - pypi: git+https://github.com/opengwas/opengwasdb?branch=main#{OLD}")
        self.assertNotEqual(two, _lock_text(NEW, "0.3.9"), "the fixture must carry two commits")
        with self.assertRaises(Exception):
            self.tool.update(self.lock, self.manifest, run=self._runner(relocked=two))
        self.assertEqual(self.lock.read_text(encoding="utf-8"), self.before)

    def test_uncommitted_lock_changes_are_refused_before_relocking(self) -> None:
        run = self._runner(dirty=" M pixi.lock")
        with self.assertRaises(self.tool.EngineUpdateError):
            self.tool.update(self.lock, self.manifest, run=run)
        self.assertFalse([call for call in run.calls if call[0] != "git"], "nothing but git status ran")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), self.before)

    def test_the_task_is_declared(self) -> None:
        import tomllib

        tasks = tomllib.loads((REPO_ROOT / "pixi.toml").read_text(encoding="utf-8"))["feature"]["dev"]["tasks"]
        self.assertEqual(tasks["engine-update"], {"cmd": "python3 tools/engine_update.py"})


if __name__ == "__main__":
    unittest.main()
