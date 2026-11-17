#!/usr/bin/env python3
"""`tools/replace_rescued_copies.sh` never leaves a mixed state before it is sure (#455).

Runs the script against a two-file fixture tree standing in for `derived/`.
"""
from __future__ import annotations

import csv
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "replace_rescued_copies.sh"
MAN = "eur-hybrid-rescue-orientation-manifest.tsv"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class ReplaceScript(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        t = Path(self._t.name)
        self.live, self.scratch = t / "live", t / "scratch"
        self.before, self.receipt = t / "before.tsv", t / "receipt.tsv"
        self.old, self.new = {}, {}
        rows_old, rows_new = [], []
        for aid in ("GCST1", "GCST2"):
            rel = f"B/{aid}/harmonised/{aid}.h.tsv.gz"
            for base, store, body in ((self.live, self.old, b"old-" + aid.encode()), (self.scratch, self.new, b"new-" + aid.encode())):
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / rel).write_bytes(body)
                store[aid] = (rel, body)
            rows_old.append((aid, str(self.live / rel), sha(self.old[aid][1])))
            rows_new.append((aid, str(self.live / rel), sha(self.new[aid][1])))
        for base, rows in ((self.live, rows_old), (self.scratch, rows_new)):
            with open(base / MAN, "w", newline="") as f:
                w = csv.writer(f, delimiter="\t", lineterminator="\n")
                w.writerow(["analysis_id", "data_file", "sha256", "status", "error", "yaml_file", "data_bytes", "yaml_bytes"])
                w.writerows((*r, "already_present", "rescued via swap_alleles", "", "9", "") for r in rows)
        with open(self.receipt, "w", newline="") as f:
            w = csv.writer(f, delimiter="\t", lineterminator="\n")
            w.writerow(["analysis_id", "rows", "mismatching_rows", "header_identical", "row_counts_equal", "sha256", "examples"])
            w.writerows((a, 1, 0, "true", "true", s, "") for a, _, s in rows_new)
        self.live_manifest = (self.live / MAN).read_bytes()

    def run_script(self, *extra):
        return subprocess.run(
            ["bash", str(SCRIPT), "--scratch", str(self.scratch), "--receipt", str(self.receipt),
             "--before", str(self.before), "--live", str(self.live), *extra],
            capture_output=True, text=True)

    def live_bytes(self, aid):
        return (self.live / self.old[aid][0]).read_bytes()

    def assert_untouched(self):
        for aid in self.old:
            self.assertEqual(self.live_bytes(aid), self.old[aid][1])
        self.assertEqual((self.live / MAN).read_bytes(), self.live_manifest)
        self.assertFalse(self.before.exists())
        self.assertEqual(list(self.live.rglob("*.part")), [])

    def edit_tsv(self, path, edit):
        with open(path, newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            columns, rows = reader.fieldnames, list(reader)
        edit(rows)
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)

    def add_unchanged_and_excluded_rows(self):
        frequency = {"analysis_id": "GCST3", "status": "already_present", "error": "rescued via flip_frequency",
                     "data_file": str(self.live / "frequency.tsv.gz"), "sha256": sha(b"frequency")}
        (self.live / "frequency.tsv.gz").write_bytes(b"frequency")
        exclusions = [{"analysis_id": aid, "status": "excluded", "error": "owner-approved exclusion"}
                      for aid in ("GCST90275159", "GCST90275160")]
        for base in (self.live, self.scratch):
            self.edit_tsv(base / MAN, lambda rows: rows.extend([frequency, *exclusions]))
        self.live_manifest = (self.live / MAN).read_bytes()

    def test_unchanged_frequency_only_and_approved_excluded_rows_need_no_receipt(self):
        self.add_unchanged_and_excluded_rows()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.live / MAN).read_bytes(), (self.scratch / MAN).read_bytes())
        self.assertEqual((self.live / "frequency.tsv.gz").read_bytes(), b"frequency")

    def test_a_changed_frequency_only_row_is_refused_before_writing(self):
        self.add_unchanged_and_excluded_rows()
        self.edit_tsv(self.scratch / MAN, lambda rows: rows[2].update(sha256=sha(b"changed")))
        r = self.run_script()
        self.assert_untouched()
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_an_excluded_row_naming_a_file_is_refused_before_writing(self):
        self.add_unchanged_and_excluded_rows()
        self.edit_tsv(self.scratch / MAN, lambda rows: rows[3].update(data_file="unexpected.tsv.gz"))
        r = self.run_script()
        self.assert_untouched()
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_a_failed_verification_receipt_changes_nothing(self):
        good = self.receipt.read_text()
        for failed in ("1\tfalse\tfalse", "1\ttrue\ttrue", "0\tfalse\ttrue", "0\ttrue\tfalse"):
            with self.subTest(result=failed):
                self.receipt.write_text(good.replace("0\ttrue\ttrue", failed))
                r = self.run_script()
                self.assert_untouched()
                self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_an_omitted_replacement_receipt_row_changes_nothing(self):
        lines = self.receipt.read_text().splitlines(keepends=True)
        self.receipt.write_text("".join(lines[:2]))
        r = self.run_script()
        self.assert_untouched()
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_a_duplicate_replacement_receipt_row_changes_nothing(self):
        lines = self.receipt.read_text().splitlines(keepends=True)
        self.receipt.write_text("".join(lines + [lines[1]]))
        r = self.run_script()
        self.assert_untouched()
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_a_receipt_missing_verification_columns_changes_nothing(self):
        self.receipt.write_text("analysis_id\tsha256\n" + "".join(
            f"{aid}\t{sha(body)}\n" for aid, (_, body) in self.new.items()))
        r = self.run_script()
        self.assert_untouched()
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_an_omitted_already_installed_replacement_is_still_refused(self):
        # Resuming must require the whole receipt, even if a manifest row is already current.
        self.edit_tsv(self.live / MAN, lambda rows: rows[1].update(sha256=sha(self.new["GCST2"][1])))
        (self.live / self.old["GCST2"][0]).write_bytes(self.new["GCST2"][1])
        self.old["GCST2"] = self.new["GCST2"]
        self.live_manifest = (self.live / MAN).read_bytes()
        self.edit_tsv(self.receipt, lambda rows: rows.pop())
        r = self.run_script()
        self.assert_untouched()
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_replaces_every_file_then_the_manifest_and_saves_the_old_one(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        for aid in self.new:
            self.assertEqual(self.live_bytes(aid), self.new[aid][1])
        self.assertEqual((self.live / MAN).read_bytes(), (self.scratch / MAN).read_bytes())
        self.assertEqual(self.before.read_bytes(), self.live_manifest)
        self.assertEqual(list(self.live.rglob("*.part")), [])

    def test_dry_run_writes_nothing(self):
        r = self.run_script("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.live_bytes("GCST1"), self.old["GCST1"][1])
        self.assertFalse(self.before.exists())

    def test_a_scratch_file_failing_its_checksum_changes_nothing(self):
        (self.scratch / self.new["GCST2"][0]).write_bytes(b"corrupt")
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        for aid in self.old:
            self.assertEqual(self.live_bytes(aid), self.old[aid][1])
        self.assertEqual((self.live / MAN).read_bytes(), self.live_manifest)
        self.assertEqual(list(self.live.rglob("*.part")), [])

    def test_a_missing_scratch_file_changes_nothing(self):
        (self.scratch / self.new["GCST2"][0]).unlink()
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(self.live_bytes("GCST1"), self.old["GCST1"][1])
        self.assertFalse(self.before.exists())

    def test_the_file_list_comes_from_the_receipt_not_the_directory(self):
        extra = self.scratch / "B/GCST3/harmonised/GCST3.h.tsv.gz"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"stray")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertFalse((self.live / "B/GCST3").exists())

    def test_a_receipt_file_the_manifest_does_not_name_is_refused(self):
        with open(self.receipt, "a") as f:
            f.write("GCST9\t1\t0\ttrue\ttrue\t" + "0" * 64 + "\t\n")
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual(self.live_bytes("GCST1"), self.old["GCST1"][1])

    def test_a_failure_before_the_manifest_leaves_it_old_and_a_rerun_finishes(self):
        d = self.live / self.old["GCST2"][0]
        d.parent.chmod(0o500)  # staging GCST2's .part fails after GCST1's copy was staged
        try:
            r = self.run_script()
        finally:
            d.parent.chmod(0o700)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.live_bytes("GCST1"), self.old["GCST1"][1])  # no rename happened
        self.assertEqual((self.live / MAN).read_bytes(), self.live_manifest)
        self.assertEqual(list(self.live.rglob("*.part")), [])
        self.assertEqual(self.run_script().returncode, 0)  # re-run completes
        self.assertEqual(self.live_bytes("GCST2"), self.new["GCST2"][1])


if __name__ == "__main__":
    unittest.main()
