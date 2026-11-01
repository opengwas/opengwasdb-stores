#!/usr/bin/env python3
"""Which opengwasdb engine this is, and which one the lock expects (#253).

Covers `ogstores.engine`:

* `locked_commit` parses every opengwasdb reference in `pixi.lock` -- per
  environment and the package entry -- and returns their one commit, whatever
  the request (`branch=main`, `tag=`, `rev=`). It refuses a lock with none,
  with two, with a malformed fragment, and a reference on another host is not
  the engine;
* `identify` takes commit and version from the executable's own install;
* `mismatch` is the one refusal both workflows give.

Fixtures are written lock files and a fake install with a `direct_url.json`
and `METADATA`, so no real engine is consulted except where a test says so.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from ogstores import engine

REPO_ROOT = Path(__file__).resolve().parents[2]

OLD = "0d59f151c7618933747a59f825deeef6a51de9e1"
NEW = "bc22e8476c4378af582477f09b56af01e3f5f065"


def _reference(request: str, commit: str, host: str = "github.com") -> str:
    return f"git+https://{host}/opengwas/opengwasdb?{request}#{commit}"


class LockedCommitTest(unittest.TestCase):
    def _lock(self, *package_refs: str, environment_refs: tuple[str, ...] = ()) -> Path:
        tmp = self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))
        path = Path(tmp) / "pixi.lock"
        lines = ["version: 7", "environments:", "  default:", "    packages:", "      linux-64:"]
        lines += [f"      - pypi: {ref}" for ref in environment_refs] or ["      - conda: x"]
        lines += ["packages:"]
        lines += [f"- pypi: {ref}\n  name: opengwasdb" for ref in package_refs]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_a_branch_tag_or_rev_request_reads_its_commit(self) -> None:
        for request in ("branch=main", "tag=v0.3.0", f"rev={NEW}"):
            with self.subTest(request=request):
                ref = _reference(request, NEW)
                self.assertEqual(engine.locked_commit(self._lock(ref, environment_refs=(ref,))), NEW)

    def test_no_reference_is_refused(self) -> None:
        with self.assertRaises(engine.EnginePinError):
            engine.locked_commit(self._lock())

    def test_an_environment_on_another_commit_is_refused(self) -> None:
        """A stale per-environment entry is refused, not overlooked beside a current package entry."""
        with self.assertRaises(engine.EnginePinError) as ctx:
            engine.locked_commit(
                self._lock(_reference("branch=main", NEW), environment_refs=(_reference("branch=main", OLD),))
            )
        self.assertIn(OLD, str(ctx.exception))
        self.assertIn(NEW, str(ctx.exception))

    def test_two_different_commits_are_ambiguous(self) -> None:
        with self.assertRaises(engine.EnginePinError):
            engine.locked_commit(self._lock(_reference(f"rev={OLD}", OLD), _reference("tag=v0.3.0", NEW)))

    def test_another_host_is_not_the_engine(self) -> None:
        with self.assertRaises(engine.EnginePinError):
            engine.locked_commit(self._lock(_reference("tag=x", NEW, host="example.com")))

    def test_a_malformed_commit_is_refused_not_skipped(self) -> None:
        for fragment in (f"{NEW}f", f"{NEW}z", NEW[:12], ""):
            with self.subTest(fragment=fragment):
                good = _reference("branch=main", NEW)
                with self.assertRaises(engine.EnginePinError):
                    engine.locked_commit(self._lock(good, _reference("branch=main", fragment)))

    def test_the_committed_lock_resolves_one_commit(self) -> None:
        self.assertTrue(engine.is_exact_commit_hash(engine.locked_commit(REPO_ROOT / "pixi.lock")))


#: Tracked files that record which engine *did* something -- a Release
#: Bundle's records, the generated Store catalogue, a yardstick result, an
#: epic record, a receipt, a changelog fragment, an ADR's history -- and so may
#: name the locked commit. A report dated in its name is history too.
HISTORICAL = (
    "stores/", "stores.tsv", "docs/_yardstick/", "docs/_epics/", "docs/data/", "changelog.d/", "docs/adr/",
)
DATED = re.compile(r"\d{4}-\d{2}-\d{2}")


class OnePlaceTest(unittest.TestCase):
    def test_no_tracked_file_but_the_lock_restates_the_commit(self) -> None:
        """`pixi.lock` is the only place the commit is written (#253).

        Ten characters are searched for, so neither the full commit nor an
        abbreviation of it hides; history and records of what ran are exempt.
        """
        import subprocess

        commit = engine.locked_commit(REPO_ROOT / "pixi.lock")
        listed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, check=True
        ).stdout.decode().split("\0")
        tracked = [
            name
            for name in listed
            if name and name != "pixi.lock" and not name.startswith(HISTORICAL) and not DATED.search(name)
        ]
        self.assertGreater(len(tracked), 100, "the listing must reach the repository's files")
        restating = [
            name
            for name in tracked
            if (REPO_ROOT / name).is_file() and commit[:10].encode() in (REPO_ROOT / name).read_bytes()
        ]
        self.assertEqual(restating, [], f"these restate the locked commit {commit}; refer to pixi.lock instead")


class IdentifyTest(unittest.TestCase):
    def _install(self, commit: str | None, version: str | None) -> Path:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))))
        (root / "bin").mkdir()
        exe = root / "bin" / "opengwasdb"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
        dist = root / "lib" / "python3.12" / "site-packages" / "opengwasdb-9.9.9.dist-info"
        dist.mkdir(parents=True)
        if commit is not None:
            (dist / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": commit}}), encoding="utf-8")
        if version is not None:
            (dist / "METADATA").write_text(f"Name: opengwasdb\nVersion: {version}\n", encoding="utf-8")
        return exe

    def test_commit_and_version_come_from_the_executable(self) -> None:
        exe = self._install(NEW, "9.9.9")
        self.assertEqual(engine.identify(str(exe)), engine.EngineIdentity(str(exe.resolve()), NEW, "9.9.9"))

    def test_an_unprovable_commit_is_unavailable(self) -> None:
        exe = self._install("not-a-commit", "9.9.9")
        self.assertEqual(engine.identify(str(exe)).commit, engine.UNAVAILABLE)
        self.assertFalse(engine.identify(str(exe)).is_provable())

    def test_no_executable_is_unavailable(self) -> None:
        ran = engine.identify(engine.UNAVAILABLE)
        self.assertEqual((ran.commit, ran.version), (engine.UNAVAILABLE, engine.UNAVAILABLE))


class MismatchTest(unittest.TestCase):
    def test_the_locked_engine_passes(self) -> None:
        self.assertIsNone(engine.mismatch(engine.EngineIdentity("/x/opengwasdb", NEW, "0.3.2"), NEW))

    def test_another_or_unprovable_commit_is_refused_naming_both(self) -> None:
        for commit in (OLD, engine.UNAVAILABLE):
            with self.subTest(commit=commit):
                refusal = engine.mismatch(engine.EngineIdentity("/x/opengwasdb", commit, "0.3.2"), NEW)
                self.assertIsNotNone(refusal)
                self.assertIn(commit, refusal)
                self.assertIn(NEW, refusal)
                self.assertIn("engine-update", refusal)

    def test_no_executable_is_refused(self) -> None:
        refusal = engine.mismatch(engine.EngineIdentity(engine.UNAVAILABLE, engine.UNAVAILABLE, engine.UNAVAILABLE), NEW)
        self.assertIn("no opengwasdb executable", refusal or "")


if __name__ == "__main__":
    unittest.main()
