#!/usr/bin/env python3
"""Repair a shipped LD Reference Panel in place: invert EAF, drop duplicated ALIDs (#348).

The panels written before #348 hold two defects. Both are corrected in place
rather than by a rebuild, because each is exact for every stored artifact:

* **EAF is the other allele's frequency.** plink2's ``--export A`` counts REF
  and names it in the column suffix, and the shipped constructor read it as ALT
  dosage, so every row's EAF is one minus the frequency of its ``EA``. On
  genotype-checked blocks ``EAF + true EAF(EA) == 1`` on every row, so the fix
  is exactly ``EAF := 1 - EAF``; the alleles and the eigenvectors stay as they
  are (the eigenvectors are uniformly sign-flipped, which no stored artifact
  can express, and the LD -- ``V diag(values) V^T`` -- is unchanged).
* **An ALID can name two source records**, an insertion and a deletion at one
  position, and both rows were kept. One is dropped, the one with the higher
  MAF as the table stores it, a tie going to the earlier row. A table records no
  call rate, so unlike the constructor (:mod:`construct_block`, which settles a
  tie on call rate and then input order) this tool can only fall back on row
  order. Either row is an exact LD row for the variant its own source record
  describes, and such ties are rare. Dropping a row
  from the TSV *and* from ``vectors`` is exact for the stored LD; ``values`` is
  untouched, so the consumer's cumulative-variance cut keeps its meaning.

Originals go to ``--backup-dir``, every file is replaced atomically, and each
block's before/after sha256, dropped rows and flip are added to ``--receipt``.

Guards, because a repair that runs twice is a wrong answer that looks right:

* Before anything is written, the orientation gate (:func:`verify_block.
  panel_eaf_vs_reference`) must find the sampled blocks' EAF **negatively**
  correlated with an independent reference, ``r < -0.9`` -- a panel that is
  already correct must be refused, not flipped;
* the sample is repaired first, and re-gated, and only a ``r > 0.9`` unlocks
  the rest;
* a block already carrying a repair record is skipped, so a re-run resumes and
  never flips twice;
* ``--dry-run`` writes nothing and prints the counts.

Usage:
  repair_panel.py /data/opengwasdb/reference/hgdp1kgp-hg38/panel \\
      --ancestry EUR --reference-group "United Kingdom" \\
      --backup-dir /data/opengwasdb/reference/348-backup \\
      --receipt /data/opengwasdb/reference/348-backup/repair-receipt.json [--dry-run]
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from verify_block import (ANCESTRY_REFERENCE, ANCESTRY_GROUPS, panel_eaf_vs_reference, repair_record,
                          sample_blocks)

SUFFIXES = (".tsv", ".ldeig.npz", ".provenance.json")
EAF_RULE = "EAF := 1 - EAF (plink2 --export A counts REF; the shipped builder read it as ALT)"
COLLISION_RULE = "keep_highest_maf"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _terminator(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n"


def _split(line: str) -> list[str]:
    return line.rstrip("\r\n").split("\t")


def plan_repair(base: Path) -> tuple[list[str], list[int], int, list[int]]:
    """What the repair would write: its TSV lines, dropped rows, flips, and kept rows.

    Every field except ``EAF`` is carried through byte for byte, so a block with
    nothing to drop differs from its original only in that column, and a panel
    with no duplicated ALID needs no npz edit at all.
    """
    lines = base.with_suffix(".tsv").read_text().splitlines(keepends=True)
    if not lines: raise SystemExit(f"{base}.tsv is empty")
    fields = _split(lines[0])
    for required in ("SNP", "EA", "EAF"):
        if required not in fields: raise SystemExit(f"{base}.tsv has no {required} column")
    snp, eaf = fields.index("SNP"), fields.index("EAF")
    rows = [_split(line) for line in lines[1:]]
    # The higher-MAF record wins, and a tie goes to the earlier row (#241, #348).
    # MAF is min(EAF, 1 - EAF), so the flip changes no decision. This is the
    # repair's own rule, not the constructor's: a block table records no call
    # rate, so a tie on the stored MAF can only be settled by row order. The
    # constructor settles the same tie on call rate, then input order, so the
    # two tools can keep different rows of one collision, and either row is an
    # exact LD row for the variant its own source record describes.
    best: dict[str, int] = {}
    maf: list[float] = []
    for i, row in enumerate(rows):
        frequency = float(row[eaf]); maf.append(min(frequency, 1 - frequency))
        current = best.get(row[snp])
        if current is None or maf[i] > maf[current]: best[row[snp]] = i
    kept = sorted(best.values())
    keeping = set(kept)  # once: one membership test per row, not a rebuilt set
    dropped = [i for i in range(len(rows)) if i not in keeping]
    repaired = [lines[0]]
    for i in kept:
        row = rows[i]
        row[eaf] = repr(1 - float(row[eaf]))
        repaired.append("\t".join(row) + _terminator(lines[i + 1]))
    return repaired, dropped, len(kept), kept


def _replace(path: Path, write) -> None:
    """Write ``path`` through a temporary sibling, so a reader sees the old or the new file."""
    token = next(tempfile._get_candidate_names())
    temporary = path.with_name(f".{path.name}.{token}.tmp")
    try:
        write(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def backup_file(original: Path, target: Path) -> str:
    """Back up ``original`` at ``target`` and return how: ``hardlink`` or ``copy``.

    Every replacement in this tool goes through :func:`_replace` -- a temporary
    sibling and ``os.replace`` -- so no original inode is ever opened for
    writing, truncated, or replaced in place. A hard link is therefore an exact
    backup of the bytes at no cost, which matters because the shipped npz trees
    are 166 GB and a copy of them is the slowest part of the run (#348). A backup
    directory on another filesystem cannot be linked at all (``EXDEV``), and only
    that case falls back to a copy -- saying so on stderr, because a silently
    different backup is a different rollback. Any other link error is real and
    stops the run.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(original, target)
        return "hardlink"
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise SystemExit(f"cannot hard-link {original} to {target}: {exc}") from exc
        print(f"backup: {target} is on another filesystem, so {original} is copied, not linked",
              file=sys.stderr, flush=True)
    _replace(target, lambda temporary: shutil.copyfile(original, temporary))
    return "copy"


def stopped(stage: str | None, reached: str) -> None:
    """Die at ``reached`` when ``--crash-after`` asked for it.

    A test hook, so the receipt's crash windows can be walked through: a run
    that dies between two file replacements must be resumable without a second
    flip, and must never lose what it already did (#348).
    """
    if stage is not None and stage == reached:
        raise SystemExit(f"stopped at {reached} as --crash-after asked (a test hook)")


def repair_block(base: Path, *, ancestry: str, backup_dir: Path, receipt: dict[str, object] | None = None,
                 receipt_path: Path | None = None,
                 crash_after: str | None = None) -> dict[str, object]:
    """Repair one block in place and return what the receipt records about it.

    The receipt entry is written **before** the block's files are replaced, so a
    crash can lose at most the entry's after-hashes, which a resume rebuilds;
    provenance is still replaced last, because it is what marks the block done.
    """
    provenance_path = base.with_suffix(".provenance.json")
    npz_path = base.with_suffix(".ldeig.npz")
    tsv_path = base.with_suffix(".tsv")
    provenance = json.loads(provenance_path.read_text())
    if repair_record(provenance):
        raise SystemExit(f"{base} already carries a repair record; refusing to repair it twice")
    repaired_lines, dropped, kept_count, kept = plan_repair(base)
    backup_base = backup_dir / ancestry / base.parent.name / base.name
    if any(backup_base.with_suffix(s).exists() for s in SUFFIXES):
        raise SystemExit(f"{backup_base} is already there, but the block records no repair: "
                         "a previous run was interrupted after backing it up; refusing to overwrite the backup")
    before = {s: sha256(base.with_suffix(s)) for s in SUFFIXES}
    original_rows = int(provenance["n_variants_retained"])
    entry: dict[str, object] = {
        "block": f"{base.parent.name}/{base.name}", "ancestry": ancestry, "state": "pending",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "rows_before": original_rows, "rows_after": kept_count,
        "dropped_row_indices": dropped, "eaf_flipped_rows": kept_count, "sha256_before": before,
    }
    if receipt is not None:
        receipt["blocks"] = [b for b in receipt["blocks"] if b["block"] != entry["block"]] + [entry]
        write_receipt(receipt_path, receipt)
    stopped(crash_after, "receipt-pending")
    # The backup is the rollback, so it is written before anything is replaced:
    # a hard link to each original, which costs one directory entry rather than
    # another 166 GB of npz.
    modes = {suffix: backup_file(base.with_suffix(suffix), backup_base.with_suffix(suffix))
             for suffix in SUFFIXES}
    entry["backup_mode"] = "hardlink" if set(modes.values()) == {"hardlink"} else "copy"
    entry["backup_modes"] = modes
    if receipt is not None:
        receipt["blocks"] = [b for b in receipt["blocks"] if b["block"] != entry["block"]] + [entry]
        write_receipt(receipt_path, receipt)
    stopped(crash_after, "backup")
    _replace(tsv_path, lambda tmp: tmp.write_text("".join(repaired_lines)))
    stopped(crash_after, "tsv")
    if dropped:
        # Row i of the eigenvectors is row i of the table, so the drop is a row
        # selection. ``values`` is the full spectrum and stays as it is.
        with np.load(npz_path) as data:
            values, vectors = data["values"], data["vectors"]
        if vectors.shape[0] != original_rows:
            raise SystemExit(f"{base}: {vectors.shape[0]} vector rows for {original_rows} table rows, "
                             f"and {len(dropped)} to drop")
        if vectors.shape[0] - len(dropped) != kept_count:
            raise SystemExit(f"{base}: dropping {len(dropped)} of {vectors.shape[0]} vector rows leaves "
                             f"{vectors.shape[0] - len(dropped)}, not the {kept_count} rows written")
        def write_npz(temporary: Path) -> None:
            # A file handle, because savez appends .npz to a name that lacks it.
            with temporary.open("wb") as fh:
                np.savez_compressed(fh, values=values, vectors=vectors[kept])
        _replace(npz_path, write_npz)
        stopped(crash_after, "npz")
    record = {
        "issue": 348, "tool": "tools/ld-panel/repair_panel.py",
        "repaired_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "eaf": EAF_RULE, "eaf_flipped_rows": kept_count,
        "alid_collision_rule": COLLISION_RULE,
        "dropped_row_indices": dropped,  # 0-based rows of the pre-repair TSV
        "n_variants_retained_before": original_rows, "n_variants_retained": kept_count,
        "sha256_before": before, "backup_mode": entry["backup_mode"], "backup_modes": modes,
    }
    provenance["n_variants_retained"] = kept_count
    provenance["repairs"] = [*(provenance.get("repairs") or []), record]
    # Provenance is the block's completion marker, so it is renamed last.
    _replace(provenance_path, lambda tmp: tmp.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"))
    if entry["backup_mode"] == "hardlink":
        rewritten = [".tsv", ".provenance.json", *([".ldeig.npz"] if dropped else [])]
        for suffix in rewritten:
            if os.stat(backup_base.with_suffix(suffix)).st_ino == os.stat(base.with_suffix(suffix)).st_ino:
                raise SystemExit(f"{base.with_suffix(suffix)} was written in place, so its hard-linked "
                                 f"backup {backup_base.with_suffix(suffix)} changed with it")
    stopped(crash_after, "provenance")
    after = {s: sha256(base.with_suffix(s)) for s in SUFFIXES}
    complete = {**entry, "state": "complete", "sha256_after": after,
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
    if receipt is not None:
        receipt["blocks"] = [b for b in receipt["blocks"] if b["block"] != complete["block"]] + [complete]
        write_receipt(receipt_path, receipt)
    stopped(crash_after, "receipt-complete")
    return complete


def read_receipt(path: Path) -> dict[str, object]:
    if not path.exists(): return {"blocks": []}
    try:
        receipt = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not a receipt this tool wrote ({exc}); refusing to overwrite it") from exc
    receipt.setdefault("blocks", [])
    return receipt


def write_receipt(path: Path, receipt: dict[str, object]) -> None:
    blocks = receipt["blocks"]
    receipt["totals"] = {
        "blocks": len(blocks),
        "rows_before": sum(int(b["rows_before"]) for b in blocks),
        "rows_after": sum(int(b["rows_after"]) for b in blocks),
        "rows_dropped": sum(len(b["dropped_row_indices"]) for b in blocks),
        "rows_flipped": sum(int(b["eaf_flipped_rows"]) for b in blocks),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace(path, lambda tmp: tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n"))


def unfinished(blocks: list[Path], *, ancestry: str, backup_dir: Path) -> list[str]:
    """Blocks a crash left half replaced: a backup, but no repair record.

    The backup is written before any file is replaced and provenance -- the
    repair record -- last, so this is exactly the window in which the table, the
    eigencomposition and the marker disagree. Finishing or rolling such a block
    back is an operator decision, so the run stops and names it rather than
    flipping it a second time; a crash before the backups, or after the repair
    record, is not this state and resumes.
    """
    return [str(backup_dir / ancestry / base.parent.name / base.name)
            for base in blocks
            if (backup_dir / ancestry / base.parent.name / base.name).with_suffix(".provenance.json").exists()
            and repair_record(json.loads(base.with_suffix(".provenance.json").read_text())) is None]


def reconcile(receipt: dict[str, object], blocks: list[Path], *, ancestry: str,
              backup_dir: Path) -> list[str]:
    """Rebuild the receipt entries a crash lost, before any repaired block is skipped.

    A block's repair record and its backup are the evidence, and they outlive a
    crash that killed the run before its receipt was written, so the entry is
    rebuilt from them: before-hashes from the backup, after-hashes from the
    files, and ``rebuilt: true`` saying the run that repaired the block did not
    get to write it. Without this, a resume that skips the block would leave a
    repaired panel with no receipt for it (#348).
    """
    rebuilt: list[str] = []
    known = {entry["block"] for entry in receipt["blocks"] if entry.get("state") == "complete"}
    for base in blocks:
        record = repair_record(json.loads(base.with_suffix(".provenance.json").read_text()))
        key = f"{base.parent.name}/{base.name}"
        if record is None or key in known: continue
        backup_base = backup_dir / ancestry / base.parent.name / base.name
        if not all(backup_base.with_suffix(s).exists() for s in SUFFIXES):
            raise SystemExit(f"{key} records a repair but has no backup under {backup_base}; "
                             "refusing to invent a receipt entry for it")
        entry = {
            "block": key, "ancestry": ancestry, "state": "complete", "rebuilt": True,
            "rebuilt_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "rows_before": int(record["n_variants_retained_before"]),
            "rows_after": int(record["n_variants_retained"]),
            "dropped_row_indices": list(record["dropped_row_indices"]),
            "eaf_flipped_rows": int(record["eaf_flipped_rows"]),
            "sha256_before": {s: sha256(backup_base.with_suffix(s)) for s in SUFFIXES},
            "sha256_after": {s: sha256(base.with_suffix(s)) for s in SUFFIXES},
            "backup_mode": record.get("backup_mode", "unrecorded"),
        }
        receipt["blocks"] = [b for b in receipt["blocks"] if b["block"] != key] + [entry]
        rebuilt.append(key)
    return rebuilt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("panel", type=Path, help="Panel root, holding <ancestry>/<chrom>/<start>-<end>.tsv")
    ap.add_argument("--ancestry", required=True)
    ap.add_argument("--reference-group", help="UKB ancestry-mixture column (default: the one for --ancestry)")
    ap.add_argument("--ancestry-reference", type=Path, default=ANCESTRY_REFERENCE)
    ap.add_argument("--backup-dir", type=Path, required=True)
    ap.add_argument("--receipt", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true", help="Write nothing; print the counts per block and in total")
    ap.add_argument("--orientation-sample", type=int, default=5, help="Un-repaired blocks the gate decides on")
    ap.add_argument("--min-orientation-matches", type=int, default=1000)
    ap.add_argument("--min-orientation-r", type=float, default=.9)
    ap.add_argument("--fail-after-blocks", type=int, help="Testing hook: stop after this many blocks, as a crash would")
    ap.add_argument("--crash-after", choices=("receipt-pending", "backup", "tsv", "npz", "provenance",
                                              "receipt-complete"),
                    help="Testing hook: stop at one point inside a block, to test the crash windows")
    args = ap.parse_args()

    root = args.panel / args.ancestry
    blocks = sorted(path.with_suffix("") for path in root.glob("*/*.tsv"))
    if not blocks: raise SystemExit(f"no blocks under {root}")
    pending = [b for b in blocks if not repair_record(json.loads(b.with_suffix(".provenance.json").read_text()))]
    group = args.reference_group or ANCESTRY_GROUPS.get(args.ancestry)

    if args.dry_run:
        totals = [0, 0, 0]
        for base in pending:
            repaired, dropped, kept, _ = plan_repair(base)
            rows = len(repaired) - 1
            totals[0] += rows; totals[1] += len(dropped); totals[2] += kept
            print(f"{base.parent.name}/{base.name}\trows {rows + len(dropped)} -> {rows}"
                  f"\tdropped {len(dropped)}\tEAF flipped {kept}")
        print(f"total\t{len(pending)} blocks to repair\trows {totals[0] + totals[1]} -> {totals[0]}"
              f"\tdropped {totals[1]}\tEAF flipped {totals[2]}"
              f"\n{len(blocks) - len(pending)} already repaired; nothing written (--dry-run)")
        return 0
    left = unfinished(blocks, ancestry=args.ancestry, backup_dir=args.backup_dir)
    if left:
        raise SystemExit(
            f"refusing: {len(left)} block(s) have a backup but no repair record, so a previous run was "
            f"interrupted between replacements (e.g. {left[0]}). Their files may be half repaired; restore "
            "each from its backup, or finish it by hand, before running again")

    receipt = read_receipt(args.receipt)
    receipt |= {"panel": str(args.panel.resolve()), "ancestry": args.ancestry, "reference_group": group,
                "ancestry_reference": str(args.ancestry_reference),
                "rules": {"eaf": EAF_RULE, "alid_collision_rule": COLLISION_RULE}}
    if not pending:
        # Still reconcile: a crash after the last block's provenance replacement
        # leaves a repaired block whose receipt entry the next run must finish.
        rebuilt = reconcile(receipt, blocks, ancestry=args.ancestry, backup_dir=args.backup_dir)
        if rebuilt:
            write_receipt(args.receipt, receipt)
            print(f"rebuilt the receipt entry of {len(rebuilt)} block(s) from their backups: "
                  f"{', '.join(rebuilt)}")
        print(f"nothing to repair under {root}: every block records a repair")
        return 0

    sample = sample_blocks(pending, args.orientation_sample)
    before = panel_eaf_vs_reference(sample, args.ancestry, reference=args.ancestry_reference, group=group)
    if before["n_matched"] < args.min_orientation_matches:
        raise SystemExit(f"refusing: {before['n_matched']} of the sampled blocks' rows match {group!r} "
                         f"in {args.ancestry_reference}, below the {args.min_orientation_matches} needed to decide")
    if before["r"] is None or not before["r"] < -args.min_orientation_r:
        raise SystemExit(f"refusing: the sampled blocks' EAF correlates r={before['r']} with {group!r}, "
                         f"not below -{args.min_orientation_r}: they do not look inverted, so a flip "
                         "would break a panel that is already correct")

    rebuilt = reconcile(receipt, blocks, ancestry=args.ancestry, backup_dir=args.backup_dir)
    if rebuilt:
        print(f"rebuilt the receipt entry of {len(rebuilt)} block(s) from their backups: {', '.join(rebuilt)}")
    receipt["orientation_before"] = before
    write_receipt(args.receipt, receipt)
    done: set[str] = set()

    def repair(bases: list[Path]) -> None:
        for base in bases:
            if args.fail_after_blocks is not None and len(done) >= args.fail_after_blocks:
                raise SystemExit(f"stopped after {len(done)} blocks as --fail-after-blocks asked")
            record = repair_block(base, ancestry=args.ancestry, backup_dir=args.backup_dir,
                                  receipt=receipt, receipt_path=args.receipt, crash_after=args.crash_after)
            done.add(record["block"])
            print(f"repaired {record['block']}: rows {record['rows_before']} -> {record['rows_after']},"
                  f" dropped {len(record['dropped_row_indices'])}, EAF flipped {record['eaf_flipped_rows']}",
                  flush=True)

    # The sample first, so the gate's second half decides on real writes before
    # the rest of the panel is touched.
    repair(sample)
    after = panel_eaf_vs_reference(sample, args.ancestry, reference=args.ancestry_reference, group=group)
    receipt["orientation_after"] = after
    write_receipt(args.receipt, receipt)
    if after["r"] is None or not after["r"] > args.min_orientation_r:
        raise SystemExit(f"repaired blocks still correlate r={after['r']} with {group!r}, not above "
                         f"{args.min_orientation_r}: stopping with {len(done)} blocks repaired "
                         "(their backups and receipt entries are in place)")
    repair([b for b in pending if b not in set(sample)])
    print(f"repaired {len(done)} blocks: {receipt['totals']['rows_before']} -> "
          f"{receipt['totals']['rows_after']} rows, {receipt['totals']['rows_dropped']} dropped; "
          f"receipt {args.receipt}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
