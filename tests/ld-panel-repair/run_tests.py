#!/usr/bin/env python3
"""Tests for the in-place #348 repair of a shipped LD Reference Panel.

tools/ld-panel/repair_panel.py inverts every block's EAF and drops the losing
row of each duplicated ALID, and the repaired block must still read as a block:
this suite drives the tool as a subprocess exactly as a run would, then checks
the artifacts, the backups, the provenance records and the receipt.

The fixture panel (fixtures/generate_fixtures.py) is 15 rows over three blocks:
one duplicated ALID whose higher-MAF record is the earlier row, one whose two
records are an exact MAF tie, and one block with no duplicated ALID and no
REF/ALT columns. Every literal asserted here -- the surviving rows, the flipped
frequencies, the dropped indices and the counts -- is computed by hand from the
fixture genotypes, never by the tool under test.

Requires numpy, e.g. via `pixi run test`.
"""
from __future__ import annotations

import csv
import errno
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "tools" / "ld-panel"
FIXTURES = REPO_ROOT / "tests" / "ld-panel-repair" / "fixtures"
PANEL = FIXTURES / "panel"
REFERENCE = FIXTURES / "reference.tsv.gz"

# The panel's repaired form, from the fixture genotypes: 8 individuals, so every
# frequency is a multiple of 1/16 and the flip is exact.
#   22/100-200 drops row 2, the GA>G deletion (MAF .125), against the G>GA
#   insertion's .5; 22/300-400 drops row 2, an exact .375/.375 tie that leaves
#   the earlier row standing; 22/500-600 has nothing to drop.
REPAIRED = {
    "22/100-200": {
        "snps": ["22:100:A:G", "22:101:G:GA", "22:102:C:T", "22:103:A:G", "22:104:C:T"],
        "eaf": [0.0625, 0.5, 0.125, 0.75, 0.25],
        "dropped": [2],
    },
    "22/300-400": {
        "snps": ["22:300:A:G", "22:301:G:GA", "22:302:C:T", "22:303:C:T"],
        "eaf": [0.9375, 0.375, 0.0625, 0.5],
        "dropped": [2],
    },
    "22/500-600": {
        "snps": ["22:500:A:C", "22:501:G:T", "22:502:A:T", "22:503:C:G"],
        "eaf": [0.125, 0.875, 0.3125, 0.6875],
        "dropped": [],
    },
}
SUFFIXES = (".tsv", ".ldeig.npz", ".provenance.json")

checks = 0


def check(value: bool, message: str) -> None:
    global checks
    checks += 1
    if not value: raise AssertionError(message)


def load(name: str):
    """Load one tools/ld-panel script, which is a script rather than a module."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


# The scripts import their siblings by bare name, so they load in that order.
for _dependency in ("scratch", "construct_block", "find_underresolved_blocks", "verify_block", "repair_panel"):
    sys.modules.setdefault(_dependency, load(_dependency))
verify, repair_tool = sys.modules["verify_block"], sys.modules["repair_panel"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def panel(tmp: Path) -> Path:
    """A fresh copy of the fixture panel, which every test repairs in place."""
    destination = tmp / "panel"
    shutil.copytree(PANEL, destination)
    return destination


def repair(tmp: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "repair_panel.py"), str(tmp / "panel"),
         "--ancestry", "EUR", "--reference-group", "United Kingdom",
         "--ancestry-reference", str(REFERENCE), "--backup-dir", str(tmp / "backup"),
         "--receipt", str(tmp / "receipt.json"), "--min-orientation-matches", "5", *extra],
        cwd=REPO_ROOT, capture_output=True, text=True)


def repair_in_process(tmp: Path, *extra: str) -> tuple[int, str, str]:
    """Run the tool's own entry point, for the tests that walk many crash windows.

    ``test_repair_panel`` drives the command line as a subprocess, as a run
    does; the behaviour tests around it would pay a process start each time for
    no extra fidelity (#348 review).
    """
    import contextlib, io
    argv = [str(SCRIPTS / "repair_panel.py"), str(tmp / "panel"),
            "--ancestry", "EUR", "--reference-group", "United Kingdom",
            "--ancestry-reference", str(REFERENCE), "--backup-dir", str(tmp / "backup"),
            "--receipt", str(tmp / "receipt.json"), "--min-orientation-matches", "5", *extra]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        saved, sys.argv = sys.argv, argv
        try:
            code = repair_tool.main()
        except SystemExit as exc:
            # As the interpreter would: a message to stderr, the code to the shell.
            code = exc.code if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int) and exc.code: err.write(f"{exc.code}\n")
        finally:
            sys.argv = saved
    return code, out.getvalue(), err.getvalue()


def rows(base: Path) -> list[dict[str, str]]:
    with base.with_suffix(".tsv").open(newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def assert_repaired(base: Path) -> None:
    """The block's table is the one the fixture genotypes say it must be."""
    key = f"{base.parent.name}/{base.name}"
    expected = REPAIRED[key]
    table = rows(base)
    check([r["SNP"] for r in table] == expected["snps"], f"{key}: wrong surviving rows {[r['SNP'] for r in table]}")
    check([float(r["EAF"]) for r in table] == expected["eaf"], f"{key}: wrong EAFs {[r['EAF'] for r in table]}")
    for row in table:  # EA stays the canonical, smaller allele, and so does the ALID
        check(row["EA"] < row["OA"] and row["SNP"] == f"{row['CHR']}:{row['BP']}:{row['EA']}:{row['OA']}",
              f"{key}: the repair changed an allele or an ALID")


def block_inputs(name: str) -> tuple[np.ndarray, list[dict[str, object]]]:
    """The fixture genotypes behind one block, as verify_block takes them."""
    records = json.loads((FIXTURES / "records.json").read_text())["blocks"][name]
    genotypes = np.array([r["dosages"] for r in records], dtype=np.float64).T
    variants = [{"chrom": r["chrom"], "pos": r["pos"], "allele1": r["ref"], "allele2": r["alt"],
                 "counted": r["ref"]} for r in records]
    return genotypes, variants


def test_repair_panel() -> None:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel_dir = panel(tmp)
        before = snapshot(panel_dir)
        result = repair(tmp)
        check(result.returncode == 0, f"repair failed: {result.stderr}")
        check("repaired 3 blocks" in result.stdout, f"the run did not report the panel repaired: {result.stdout}")

        for name in REPAIRED:
            base = panel_dir / "EUR" / name
            assert_repaired(base)
            # Every field but EAF is carried through byte for byte, row by row.
            # The surviving rows are the backup's, less the dropped indices.
            header, *lines = (PANEL / "EUR" / name).with_suffix(".tsv").read_text().splitlines()
            fields = header.split("\t"); eaf = fields.index("EAF")
            kept = [line.split("\t") for i, line in enumerate(lines) if i not in REPAIRED[name]["dropped"]]
            repaired_header, *repaired_lines = base.with_suffix(".tsv").read_text().splitlines()
            check(repaired_header == header, f"{name}: the TSV header changed")
            check(len(repaired_lines) == len(kept), f"{name}: the table kept the wrong number of rows")
            for line, original in zip(repaired_lines, kept):
                values = line.split("\t")
                check([v for i, v in enumerate(values) if i != eaf] ==
                      [v for i, v in enumerate(original) if i != eaf],
                      f"{name}: a repair rewrote a column other than EAF")
            # vectors loses exactly the dropped rows; values is untouched.
            backup = tmp / "backup" / "EUR" / name
            with np.load(backup.with_suffix(".ldeig.npz")) as old, np.load(base.with_suffix(".ldeig.npz")) as new:
                keep = [i for i in range(old["vectors"].shape[0]) if i not in REPAIRED[name]["dropped"]]
                check(np.array_equal(new["vectors"], old["vectors"][keep]), f"{name}: vectors is not the backup's rows")
                check(np.array_equal(new["values"], old["values"]), f"{name}: values changed")
            provenance = json.loads(base.with_suffix(".provenance.json").read_text())
            record = provenance["repairs"][-1]
            check(provenance["n_variants_retained"] == len(REPAIRED[name]["snps"]),
                  f"{name}: n_variants_retained was not updated")
            check(record["alid_collision_rule"] == "keep_highest_maf" and
                  record["dropped_row_indices"] == REPAIRED[name]["dropped"] and
                  record["eaf_flipped_rows"] == len(REPAIRED[name]["snps"]) and
                  record["n_variants_retained_before"] == len(REPAIRED[name]["snps"]) + len(REPAIRED[name]["dropped"]),
                  f"{name}: the repair record is not what it must be: {record}")
            for suffix in SUFFIXES:
                check(backup.with_suffix(suffix).exists(), f"{name}: no backup {suffix}")
                check(sha256(backup.with_suffix(suffix)) == before[f"EUR/{name}{suffix}"],
                      f"{name}: the backup {suffix} is not the original")
            # A repaired block reads as a block, held to its backup (#348).
            genotypes, variants = block_inputs(name)
            report = verify.verify_block(base, genotypes, variants, thresh=.9, backup=backup)
            check(report["failures"] == [], f"{name}: a repaired block failed verification: {report['failures']}")

        # A block with nothing to drop keeps its npz byte for byte, as the AMR
        # panel must (19 GB of untouched eigendecompositions).
        check(sha256(panel_dir / "EUR/22/500-600.ldeig.npz") == before["EUR/22/500-600.ldeig.npz"],
              "a block with no duplicated ALID had its eigendecomposition rewritten")
        check(sha256(panel_dir / "EUR/22/100-200.ldeig.npz") != before["EUR/22/100-200.ldeig.npz"],
              "a block with a dropped row kept its old eigendecomposition")

        receipt = json.loads((tmp / "receipt.json").read_text())
        check(len(receipt["blocks"]) == 3, "the receipt does not hold every repaired block")
        by_block = {b["block"]: b for b in receipt["blocks"]}
        for name, expected in REPAIRED.items():
            recorded = by_block[name]
            check(recorded["dropped_row_indices"] == expected["dropped"] and
                  recorded["rows_before"] == len(expected["snps"]) + len(expected["dropped"]) and
                  recorded["rows_after"] == len(expected["snps"]) and
                  recorded["eaf_flipped_rows"] == len(expected["snps"]),
                  f"{name}: the receipt's counts are wrong: {recorded}")
            for suffix in SUFFIXES:
                check(recorded["sha256_after"][suffix] == sha256(panel_dir / f"EUR/{name}{suffix}"),
                      f"{name}: the receipt's after hash of {suffix} is not the file's")
                check(recorded["sha256_before"][suffix] == before[f"EUR/{name}{suffix}"],
                      f"{name}: the receipt's before hash of {suffix} is not the original")
        check(receipt["totals"] == {"blocks": 3, "rows_before": 15, "rows_after": 13,
                                    "rows_dropped": 2, "rows_flipped": 13},
              f"the receipt's totals are wrong: {receipt['totals']}")
        check(receipt["orientation_before"]["r"] < -.9 and receipt["orientation_after"]["r"] > .9,
              "the receipt does not record an inverted panel becoming a correctly oriented one: "
              f"{receipt['orientation_before']['r']} -> {receipt['orientation_after']['r']}")


def test_rerun_is_idempotent() -> None:
    # #348: a second run must not flip anything again. It finds the repair
    # records, says so, and leaves every byte as it was.
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel(tmp)
        check(repair(tmp).returncode == 0, "the first run failed")
        after_first = {**snapshot(tmp / "panel"), **snapshot(tmp / "backup"), "receipt": sha256(tmp / "receipt.json")}
        again = repair(tmp)
        check(again.returncode == 0 and "nothing to repair" in again.stdout,
              f"a second run did not report the panel already repaired: {again.stdout} {again.stderr}")
        after_second = {**snapshot(tmp / "panel"), **snapshot(tmp / "backup"), "receipt": sha256(tmp / "receipt.json")}
        check(after_second == after_first, "a second run changed a repaired panel")


def test_positively_oriented_panel_is_refused() -> None:
    # The double-flip guard: a panel whose EAF is already the effect allele's
    # frequency must be refused before anything is written.
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel_dir = panel(tmp)
        for name in REPAIRED:
            base = panel_dir / "EUR" / name
            header, *lines = base.with_suffix(".tsv").read_text().splitlines()
            column = header.split("\t").index("EAF")
            out = []
            for line in lines:
                values = line.split("\t")
                values[column] = repr(1 - float(values[column]))  # undo the shipped inversion
                out.append("\t".join(values))
            base.with_suffix(".tsv").write_text(header + "\n" + "\n".join(out) + "\n")
        before = snapshot(panel_dir)
        result = repair(tmp)
        check(result.returncode != 0 and "refusing" in result.stderr,
              f"a correctly oriented panel was not refused: {result.stdout} {result.stderr}")
        check(snapshot(panel_dir) == before, "a refused run wrote to the panel")
        check(not (tmp / "backup").exists() and not (tmp / "receipt.json").exists(),
              "a refused run wrote a backup or a receipt")


def test_too_few_reference_matches_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel_dir = panel(tmp)
        before = snapshot(panel_dir)
        result = repair(tmp, "--min-orientation-matches", "1000")
        check(result.returncode != 0 and "below" in result.stderr,
              f"a sample too small to decide on was not refused: {result.stderr}")
        check(snapshot(panel_dir) == before, "a refused run wrote to the panel")


def test_dry_run_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel_dir = panel(tmp)
        before = snapshot(panel_dir)
        result = repair(tmp, "--dry-run")
        check(result.returncode == 0, f"--dry-run failed: {result.stderr}")
        check("dropped 1" in result.stdout and "rows 15 -> 13" in result.stdout,
              f"--dry-run did not print the counts: {result.stdout}")
        check(snapshot(panel_dir) == before, "--dry-run changed the panel")
        check(not (tmp / "backup").exists() and not (tmp / "receipt.json").exists(), "--dry-run wrote something")


def test_interrupted_run_resumes_without_flipping_twice() -> None:
    # #348: a run that dies after a block leaves that block repaired and the
    # rest untouched; the next run finishes them and flips nothing twice.
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel_dir = panel(tmp)
        first = repair(tmp, "--fail-after-blocks", "1")
        check(first.returncode != 0 and "stopped after 1" in (first.stdout + first.stderr),
              f"the interruption hook did not stop the run: {first.stdout} {first.stderr}")
        repaired_first = [name for name in REPAIRED if json.loads(
            (panel_dir / "EUR" / name).with_suffix(".provenance.json").read_text()).get("repairs")]
        check(len(repaired_first) == 1, f"the interrupted run left {len(repaired_first)} blocks repaired")
        untouched = [name for name in REPAIRED if name not in repaired_first][0]
        check(sha256((panel_dir / "EUR" / untouched).with_suffix(".tsv")) ==
              sha256((PANEL / "EUR" / untouched).with_suffix(".tsv")),
              "the interrupted run touched a block it had not reached")
        second = repair(tmp)
        check(second.returncode == 0, f"the resumed run failed: {second.stderr}")
        for name in REPAIRED:
            assert_repaired(panel_dir / "EUR" / name)
            provenance = json.loads((panel_dir / "EUR" / name).with_suffix(".provenance.json").read_text())
            check(len(provenance["repairs"]) == 1,
                  f"{name}: {len(provenance['repairs'])} repair records, so something was flipped twice")
        receipt = json.loads((tmp / "receipt.json").read_text())
        check(sorted(b["block"] for b in receipt["blocks"]) == sorted(REPAIRED) and
              receipt["totals"]["rows_dropped"] == 2,
              f"the resumed run's receipt does not hold every repaired block once: {receipt['totals']}")


# The points inside a block where a crash can land: before the block is touched
# (its receipt entry is already written), after the backups, after the table,
# after the eigencomposition, after provenance, and after the entry that says
# the block is complete.
CRASH_STAGES = ("receipt-pending", "backup", "tsv", "npz", "provenance", "receipt-complete")
# A crash before the backups leaves the block untouched, so a resume repairs it;
# one after provenance leaves a repaired block, so a resume must only finish the
# receipt. Between the two, the files are half replaced and the backup is the
# only evidence, so the run is refused by name instead of repairing it again.
RESUMABLE = ("receipt-pending", "provenance", "receipt-complete")


def entries(path: Path) -> dict[str, dict[str, object]]:
    return {entry["block"]: entry for entry in json.loads(path.read_text())["blocks"]}


def test_receipt_survives_a_crash_at_every_stage() -> None:
    # #348 review (hard): a crash must never leave a repaired block without a
    # receipt entry, and a resume must never flip a block twice. Before the table
    # is replaced the backup is the only evidence, so the half-done block is
    # refused by name rather than repaired again.
    for stage in CRASH_STAGES:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            panel_dir = panel(tmp)
            crashed_code, crashed_out, crashed_err = repair_in_process(tmp, "--crash-after", stage)
            crashed = SimpleNamespace(returncode=crashed_code, stdout=crashed_out, stderr=crashed_err)
            check(crashed.returncode != 0 and stage in (crashed.stdout + crashed.stderr),
                  f"{stage}: the crash hook did not stop the run: {crashed.stdout} {crashed.stderr}")
            # Whatever the crash point, the block being worked on is in the
            # receipt already.
            crashed_entries = entries(tmp / "receipt.json")
            check(len(crashed_entries) == 1, f"{stage}: the crash left {len(crashed_entries)} receipt entries")
            first = crashed_entries[next(iter(crashed_entries))]
            if stage != "receipt-complete":
                check(first["state"] == "pending" and not first.get("sha256_after"),
                      f"{stage}: the entry of a block being repaired is not the pending one: {first['state']}")
            before_resume = snapshot(panel_dir)
            resumed_code, resumed_out, resumed_err = repair_in_process(tmp)
            resumed = SimpleNamespace(returncode=resumed_code, stdout=resumed_out, stderr=resumed_err)
            if stage in RESUMABLE:
                check(resumed.returncode == 0, f"{stage}: a resume after the block was repaired failed: {resumed.stderr}")
                done = entries(tmp / "receipt.json")
                check(sorted(done) == sorted(REPAIRED), f"{stage}: the resume's receipt holds {sorted(done)}")
                check(all(entry["state"] == "complete" and entry["sha256_after"] for entry in done.values()),
                      f"{stage}: a repaired block has no complete receipt entry")
                for name in REPAIRED:
                    assert_repaired(panel_dir / "EUR" / name)
                    provenance = json.loads((panel_dir / "EUR" / name).with_suffix(".provenance.json").read_text())
                    check(len(provenance["repairs"]) == 1,
                          f"{stage}: {name} was flipped {len(provenance['repairs'])} times")
                if stage == "provenance":
                    check(done["22/100-200"].get("rebuilt") is True,
                          "the entry the crash truncated was not rebuilt from the backup")
                    check(done["22/100-200"]["sha256_before"] ==
                          {suffix: sha256(tmp / "backup" / f"EUR/22/100-200{suffix}") for suffix in SUFFIXES},
                          "the rebuilt entry's before-hashes are not the backup's")
            else:
                check(resumed.returncode != 0 and str(tmp / "backup") in resumed.stderr,
                      f"{stage}: a half-replaced block was not refused by name: {resumed.stdout} {resumed.stderr}")
                check(snapshot(panel_dir) == before_resume, f"{stage}: a refused resume changed the panel")
                for name in REPAIRED:
                    provenance = json.loads((panel_dir / "EUR" / name).with_suffix(".provenance.json").read_text())
                    check(len(provenance.get("repairs") or []) == 0,
                          f"{stage}: a refused resume repaired a block anyway")


def test_a_receipt_lost_outright_is_rebuilt_from_the_backups() -> None:
    # The reviewers' reproducer: everything repaired, no receipt at all. The
    # repair record and the backup are the evidence, so the entry is rebuilt.
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel_dir = panel(tmp)
        check(repair(tmp).returncode == 0, "the first run failed")
        repaired_files = snapshot(panel_dir)
        (tmp / "receipt.json").unlink()
        again_code, again_out, again_err = repair_in_process(tmp)
        again = SimpleNamespace(returncode=again_code, stdout=again_out, stderr=again_err)
        check(again.returncode == 0 and "nothing to repair" in again.stdout,
              f"a resume without a receipt did not finish quietly: {again.stdout} {again.stderr}")
        rebuilt = entries(tmp / "receipt.json")
        check(sorted(rebuilt) == sorted(REPAIRED), f"the rebuilt receipt holds {sorted(rebuilt)}")
        for name in REPAIRED:
            entry = rebuilt[name]
            check(entry["state"] == "complete" and entry.get("rebuilt") is True and
                  entry["dropped_row_indices"] == REPAIRED[name]["dropped"] and
                  entry["sha256_after"][".tsv"] == sha256(panel_dir / f"EUR/{name}.tsv"),
                  f"{name}: the rebuilt entry is wrong: {entry}")
            check(entry["sha256_before"] == {suffix: sha256(tmp / "backup" / f"EUR/{name}{suffix}")
                                             for suffix in SUFFIXES},
                  f"{name}: the rebuilt entry's before-hashes are not the backup's")
        check(snapshot(panel_dir) == repaired_files, "rebuilding a receipt rewrote the panel")
        check(json.loads((tmp / "receipt.json").read_text())["totals"] ==
              {"blocks": 3, "rows_before": 15, "rows_after": 13, "rows_dropped": 2, "rows_flipped": 13},
              "the rebuilt receipt's totals are wrong")


def test_no_duplicates_gets_the_flip_only() -> None:
    # The AMR panel has no duplicated ALID, so a repair of it must touch the
    # table and nothing else.
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        chrom = tmp / "panel/AMR/22"
        chrom.mkdir(parents=True)
        for suffix in SUFFIXES:
            shutil.copy(PANEL / f"EUR/22/500-600{suffix}", chrom / f"500-600{suffix}")
        npz_before = sha256(chrom / "500-600.ldeig.npz")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "repair_panel.py"), str(tmp / "panel"), "--ancestry", "AMR",
             "--reference-group", "United Kingdom", "--ancestry-reference", str(REFERENCE),
             "--backup-dir", str(tmp / "backup"), "--receipt", str(tmp / "receipt.json"),
             "--min-orientation-matches", "4"], cwd=REPO_ROOT, capture_output=True, text=True)
        check(result.returncode == 0, f"the flip-only repair failed: {result.stderr}")
        base = chrom / "500-600"
        check([float(r["EAF"]) for r in rows(base)] == REPAIRED["22/500-600"]["eaf"],
              f"the flip-only repair gave the wrong EAFs: {[r['EAF'] for r in rows(base)]}")
        check(sha256(base.with_suffix(".ldeig.npz")) == npz_before,
              "a panel with no duplicated ALID had its eigendecomposition rewritten")
        record = json.loads(base.with_suffix(".provenance.json").read_text())["repairs"][-1]
        check(record["dropped_row_indices"] == [] and record["eaf_flipped_rows"] == 4,
              f"the flip-only repair's record is wrong: {record}")
        check(json.loads((tmp / "receipt.json").read_text())["totals"] ==
              {"blocks": 1, "rows_before": 4, "rows_after": 4, "rows_dropped": 0, "rows_flipped": 4},
              "the flip-only receipt's totals are wrong")


def original_state(panel_dir: Path) -> dict[str, tuple[int, str]]:
    """Each original's inode and sha256, before any repair."""
    return {f"EUR/{name}{suffix}": (os.stat(panel_dir / f"EUR/{name}{suffix}").st_ino,
                                    sha256(panel_dir / f"EUR/{name}{suffix}"))
            for name in REPAIRED for suffix in SUFFIXES}


def test_a_hard_link_is_the_backup() -> None:
    # #348: the npz trees are 166 GB and every original is replaced through a
    # temporary sibling, never written in place, so a hard link is an exact
    # backup -- the same inode and the same bytes as before the repair.
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel_dir = panel(tmp)
        before = original_state(panel_dir)
        check(repair(tmp).returncode == 0, "the repair failed")
        receipt = json.loads((tmp / "receipt.json").read_text())
        for entry in receipt["blocks"]:
            block = entry["block"]
            check(entry["backup_mode"] == "hardlink",
                  f"{block}: the backup was not hard-linked ({entry['backup_mode']})")
            for suffix in SUFFIXES:
                inode, digest = before[f"EUR/{block}{suffix}"]
                backup = tmp / "backup" / f"EUR/{block}{suffix}"
                live = panel_dir / f"EUR/{block}{suffix}"
                check(os.stat(backup).st_ino == inode,
                      f"{block}: the backup {suffix} is not the pre-repair inode")
                check(sha256(backup) == digest, f"{block}: the backup {suffix} is not the pre-repair bytes")
                untouched = suffix == ".ldeig.npz" and not entry["dropped_row_indices"]
                if untouched:
                    check(os.stat(live).st_ino == inode,
                          f"{block}: an eigendecomposition the repair leaves alone was replaced")
                else:
                    check(os.stat(live).st_ino != inode,
                          f"{block}: the live {suffix} kept the original inode, so it was written in place")


def test_a_cross_device_backup_dir_is_copied_and_says_so() -> None:
    # Only EXDEV falls back to a copy: a backup directory on another filesystem
    # cannot be linked at all, and the fallback is announced on stderr.
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel_dir = panel(tmp)
        before = original_state(panel_dir)
        real_link = os.link
        def cross_device(source, target, **kwargs):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(source))
        os.link = cross_device
        try:
            code, _, err = repair_in_process(tmp)
        finally:
            os.link = real_link
        check(code == 0, f"the cross-device fallback failed: {err}")
        check("another filesystem" in err and "copied" in err,
              f"the copy fallback was silent: {err}")
        receipt = json.loads((tmp / "receipt.json").read_text())
        for entry in receipt["blocks"]:
            block = entry["block"]
            check(entry["backup_mode"] == "copy", f"{block}: the receipt does not say the backup was copied")
            for suffix in SUFFIXES:
                inode, digest = before[f"EUR/{block}{suffix}"]
                backup = tmp / "backup" / f"EUR/{block}{suffix}"
                check(os.stat(backup).st_ino != inode, f"{block}: a copied backup shares the original's inode")
                check(sha256(backup) == digest, f"{block}: the copied backup is not the pre-repair bytes")
        for name in REPAIRED: assert_repaired(panel_dir / "EUR" / name)


def test_any_other_link_error_stops_the_run() -> None:
    # A link failure that is not a filesystem boundary is a real error, not a
    # reason to fall back: it must stop the run with nothing written.
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        panel_dir = panel(tmp)
        before = snapshot(panel_dir)
        real_link = os.link
        def denied(source, target, **kwargs):
            raise OSError(errno.EACCES, "Permission denied", str(source))
        os.link = denied
        try:
            code, _, err = repair_in_process(tmp)
        finally:
            os.link = real_link
        check(code != 0 and "cannot hard-link" in err, f"a non-EXDEV link error was not loud: {code} {err}")
        check(snapshot(panel_dir) == before, "a refused backup changed a panel file")
        check(not (tmp / "backup").exists() or not any((tmp / "backup").rglob("*.*")),
              "a refused backup left a backup file behind")


def main() -> None:
    subprocess.run([sys.executable, str(FIXTURES / "generate_fixtures.py")], cwd=REPO_ROOT, check=True)
    test_repair_panel()
    test_rerun_is_idempotent()
    test_positively_oriented_panel_is_refused()
    test_too_few_reference_matches_is_refused()
    test_dry_run_writes_nothing()
    test_interrupted_run_resumes_without_flipping_twice()
    test_receipt_survives_a_crash_at_every_stage()
    test_a_receipt_lost_outright_is_rebuilt_from_the_backups()
    test_a_hard_link_is_the_backup()
    test_a_cross_device_backup_dir_is_copied_and_says_so()
    test_any_other_link_error_stops_the_run()
    test_no_duplicates_gets_the_flip_only()
    print(f"ok — {checks} checks passed")


if __name__ == "__main__":
    main()
