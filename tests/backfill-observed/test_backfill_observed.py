#!/usr/bin/env python3
"""`ogstores.backfill_observed` on a fixture registry and Artifact Root (#502).

1. It fills `store_bytes`, `has_hm3`, `has_rho` and `n_top_hits` in each
   `built` or `validated` record, and changes no other line of the file: the
   parsed documents are equal but for those keys, and so is the text once their
   lines are dropped.
2. A second run changes no byte.
3. `--dry-run` writes nothing, in the registry or under the Artifact Root.
4. A real run writes nothing under the Artifact Root.
5. A record with no `observed` block, and a bundle with no built Store, are
   skipped; a Store with no register record reports `has_hm3`/`has_rho` as null.

Run from the repository root:
    pixi run -e dev python3 tests/backfill-observed/test_backfill_observed.py
"""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from ogstores import backfill_observed, paths, run

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
NEW_KEYS: tuple[str, ...] = ("store_bytes", "has_hm3", "has_rho", "n_top_hits")

HM3 = {
    "name": "hm3",
    "resource_id": "hapmap3-hg38",
    "variant_list": "/refs/hm3.grch38.alid.txt",
    "reference_assembly": "GRCh38",
}

# A register-shaped record from before #502, with blocks above and below
# `observed`, a list, a nested mapping and a comment, so a careless edit shows.
VALIDATION_TEMPLATE = """\
status: passed
validated_at: '2026-10-01T00:00:00Z'
validator:
  name: opengwasdb validate
  version: opengwasdb@abc
build_environment:
  opengwasdb_version: 0.3.0
  opengwasdb_commit: abc
  engines:
  - opengwasdb_commit: abc
    steps:
    - build
    - validate
observed:
  format_version: 0.2.0
  n_analyses: 2
  n_variants: 1000
  n_associations: null
  store_bytes: null
  build_elapsed_s: 12.5
  validate_status: passed
  rsid_conflicts:
    policy: drop
    n_keys: 0
checks:
  store: passed
# a reviewer's note
warnings:
- 'kept: as written'
errors: []
"""


def write_bundle(
    registry: Path,
    store_id: str,
    *,
    status: str = "validated",
    post: dict[str, Any] | None = None,
    indexed_subsets: list[dict[str, Any]] | None = None,
    validation: str | None = VALIDATION_TEMPLATE,
) -> None:
    root = registry / store_id
    root.mkdir(parents=True)
    release = {
        "store_id": store_id,
        "label": f"label-{store_id.lower()}",
        "status": status,
        "association_coverage": "gwas",
        "created_at": "2026-08-18T08:51:59Z",
        "description": "fixture",
        "source_snapshot_id": "snap",
    }
    build: dict[str, Any] = {
        "store_id": store_id,
        "layout": "dense",
        "completion_state": "observed_only",
        "build": {"command": "build-dense-vcf", "options": {"source-assembly": "hg38"}},
        "post": post or {"top_hits": False, "rho": False, "overview": True, "validate": True},
    }
    if indexed_subsets is not None:
        build["indexed_subsets"] = indexed_subsets
    (root / "release.yaml").write_text(yaml.safe_dump(release), encoding="utf-8")
    (root / "build.yaml").write_text(yaml.safe_dump(build), encoding="utf-8")
    (root / "analyses.tsv").write_text("analysis_id\tsource_file\na1\tf1.vcf.gz\na2\tf2.vcf.gz\n", encoding="utf-8")
    if validation is not None:
        (root / "validation.yaml").write_text(validation, encoding="utf-8")


def write_records(artifact_root: Path, store_id: str, steps: dict[str, bool]) -> None:
    for step, succeeded in steps.items():
        run._write_record_atomically(
            {"step": step, "store_id": store_id, "success": succeeded, "exit_code": 0 if succeeded else 1},
            paths.record_path(store_id, step, root=artifact_root),
        )


def write_store(artifact_root: Path, store_id: str, files: dict[str, bytes]) -> int:
    store = paths.store_path(store_id, root=artifact_root)
    for relative, content in files.items():
        (store / relative).parent.mkdir(parents=True, exist_ok=True)
        (store / relative).write_bytes(content)
    return sum(len(content) for content in files.values())


def tree(root: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every file under `root` with its size, mtime and bytes."""
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def without_store_facts(text: str) -> str:
    """The text with the Store-fact lines (and a nested value's lines) dropped."""
    kept, skipping = [], False
    for line in text.splitlines(keepends=True):
        key = line.strip().split(":", 1)[0]
        if line.startswith("  ") and not line.startswith("   ") and key in NEW_KEYS:
            skipping = True
            continue
        if skipping and line.startswith("    "):
            continue
        skipping = False
        kept.append(line)
    return "".join(kept)


class BackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="backfill_observed_"))
        self.registry = self.tmp / "stores"
        self.artifact_root = self.tmp / "artifacts"
        self.registry.mkdir()
        self.artifact_root.mkdir()

        # Everything built: HM3 and Rho planned, both records passed.
        write_bundle(
            self.registry, "OGS-00041",
            post={"top_hits": True, "rho": True, "overview": True, "validate": True},
            indexed_subsets=[HM3],
        )
        write_records(self.artifact_root, "OGS-00041",
                      {"build": True, "rho": True, "indexed-subset-hm3": True, "register": True})
        self.bytes_41 = write_store(self.artifact_root, "OGS-00041", {
            "manifest.json": b'{"store_id": "OGS-00041"}',
            "data.zarr/beta/c/0/0": bytes(range(256)) * 9,
            "variants.tsv.gz": b"v" * 1234,
        })
        # Neither declared; registered, and its Store no longer on disk.
        write_bundle(self.registry, "OGS-00042", status="built")
        write_records(self.artifact_root, "OGS-00042", {"build": True, "register": True})
        # HM3 declared, but no register record says which steps built the Store.
        write_bundle(self.registry, "OGS-00043", indexed_subsets=[HM3])
        self.bytes_43 = write_store(self.artifact_root, "OGS-00043", {"manifest.json": b"{}"})
        # Skipped: a legacy record with no `observed` block, and a candidate.
        write_bundle(self.registry, "OGS-00044",
                     validation="status: passed\nchecks:\n  schema: passed\nwarnings: []\n")
        write_bundle(self.registry, "OGS-00045", status="candidate")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def arguments(self, *extra: str) -> list[str]:
        return ["--registry-root", str(self.registry), "--artifact-root", str(self.artifact_root), *extra]

    def backfill(self, *extra: str) -> str:
        """Run the tool in this process and return what it printed."""
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            status = backfill_observed.main(self.arguments(*extra))
        self.assertEqual(status, 0, printed.getvalue())
        return printed.getvalue()

    def record(self, store_id: str) -> str:
        return (self.registry / store_id / "validation.yaml").read_text(encoding="utf-8")

    def test_fills_the_store_facts_and_changes_nothing_else(self) -> None:
        before = {store_id: self.record(store_id) for store_id in ("OGS-00041", "OGS-00042", "OGS-00043", "OGS-00044")}

        self.backfill()

        expected = {
            "OGS-00041": {"store_bytes": self.bytes_41, "has_hm3": True, "has_rho": True, "n_top_hits": None},
            "OGS-00042": {"store_bytes": None, "has_hm3": False, "has_rho": False, "n_top_hits": None},
            "OGS-00043": {"store_bytes": self.bytes_43, "has_hm3": None, "has_rho": None, "n_top_hits": None},
        }
        for store_id, facts in expected.items():
            after_text = self.record(store_id)
            after, original = yaml.safe_load(after_text), yaml.safe_load(before[store_id])
            self.assertEqual({key: after["observed"][key] for key in NEW_KEYS}, facts, store_id)
            for document in (after, original):
                for key in NEW_KEYS:
                    document["observed"].pop(key, None)
            self.assertEqual(after, original, f"{store_id}: a key other than the Store facts changed")
            self.assertEqual(
                without_store_facts(after_text), without_store_facts(before[store_id]),
                f"{store_id}: a line other than the Store facts changed",
            )
            # The keys keep register's order, after `store_bytes`.
            observed_keys = list(yaml.safe_load(after_text)["observed"])
            index = observed_keys.index("store_bytes")
            self.assertEqual(observed_keys[index:index + 4], list(NEW_KEYS))
        self.assertEqual(self.record("OGS-00044"), before["OGS-00044"], "no observed block: skipped")
        self.assertEqual(self.record("OGS-00045"), VALIDATION_TEMPLATE, "a candidate is skipped")

    def test_a_second_run_changes_nothing(self) -> None:
        self.backfill()
        first = tree(self.registry)

        printed = self.backfill()

        self.assertEqual(tree(self.registry), first)
        self.assertIn("3 unchanged", printed)

    def test_dry_run_writes_nothing(self) -> None:
        """Run as the recorded command runs it, from the command line."""
        registry, artifacts = tree(self.registry), tree(self.artifact_root)

        result = subprocess.run(
            [sys.executable, "-m", "ogstores.backfill_observed", *self.arguments("--dry-run")],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self.assertEqual(tree(self.registry), registry)
        self.assertEqual(tree(self.artifact_root), artifacts)
        self.assertIn(f"OGS-00041: would write store_bytes: null -> {self.bytes_41}", result.stdout)
        self.assertIn("has_hm3: (absent) -> true", result.stdout)
        self.assertIn("3 would write", result.stdout)
        self.assertIn("2 skipped", result.stdout)

    def test_writes_nothing_under_the_artifact_root(self) -> None:
        artifacts = tree(self.artifact_root)
        self.backfill()
        self.assertEqual(tree(self.artifact_root), artifacts)

    def test_a_record_already_filled_is_rewritten_in_place(self) -> None:
        """Facts already recorded, by `register` from #502 on, are replaced where they stand."""
        path = self.registry / "OGS-00041" / "validation.yaml"
        path.write_text(
            VALIDATION_TEMPLATE.replace(
                "  store_bytes: null\n",
                "  store_bytes: 1\n  has_hm3: false\n  has_rho: null\n  n_top_hits:\n    5e-08: 3\n",
            ),
            encoding="utf-8",
        )

        self.backfill()

        observed = yaml.safe_load(path.read_text(encoding="utf-8"))["observed"]
        self.assertEqual(
            {key: observed[key] for key in NEW_KEYS},
            {"store_bytes": self.bytes_41, "has_hm3": True, "has_rho": True, "n_top_hits": None},
        )
        self.assertEqual(
            without_store_facts(path.read_text(encoding="utf-8")), without_store_facts(VALIDATION_TEMPLATE)
        )


if __name__ == "__main__":
    unittest.main()
