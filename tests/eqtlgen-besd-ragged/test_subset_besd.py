#!/usr/bin/env python3
"""Round-trip correctness for the BESD subset writer (issue #67).

Builds a small synthetic SPARSE_FILE_TYPE_3 BESD triple by hand (independent
of `subset_besd.py`'s own logic, so this genuinely exercises the writer
rather than checking it against itself), subsets it, and confirms the
subset -- read back through opengwasdb's real `BESDReader`/`read_esi`/
`read_epi` -- reproduces exactly the source associations for the selected
probes: same SNP identities (chrom/bp/alleles), same betas, same SEs.

Run from the repository root:
    python3 tests/eqtlgen-besd-ragged/test_subset_besd.py
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "resources" / "generators" / "lib" / "source-formats" / "eqtlgen-besd-ragged"))

from subset_besd import write_besd_subset, write_besd_subset_from_analyses  # noqa: E402

from opengwasdb.layouts.ragged.besd_reader import BESDReader, read_epi, read_esi  # noqa: E402

# --- Synthetic source fixture -------------------------------------------------
# 4 SNPs, 3 probes:
#   PROBE_A: snps 0, 1        (2 associations)
#   PROBE_B: snps 1, 2        (2 associations, shares snp 1 with PROBE_A)
#   PROBE_C: (no associations -- the zero-association edge case)
_SNPS = [
    # chrom, snp_id, bp, a1, a2, freq
    ("1", "rs1", 1000, "A", "G", 0.1),
    ("1", "rs2", 2000, "C", "T", 0.2),
    ("1", "rs3", 3000, "G", "A", 0.3),
    ("2", "rs4", 4000, "T", "C", 0.4),  # referenced by no probe -- must be dropped
]
_PROBES = ["PROBE_A", "PROBE_B", "PROBE_C"]
_ASSOCS = {
    # probe_id: [(snp_row_idx, beta, se), ...]
    "PROBE_A": [(0, 0.5, 0.1), (1, -0.3, 0.2)],
    "PROBE_B": [(1, 0.7, 0.15), (2, -0.1, 0.05)],
    "PROBE_C": [],
}


def _write_synthetic_besd(prefix: Path) -> None:
    with open(f"{prefix}.esi", "w", encoding="utf-8") as fh:
        for chrom, snp_id, bp, a1, a2, freq in _SNPS:
            fh.write(f"{chrom}\t{snp_id}\t0\t{bp}\t{a1}\t{a2}\t{freq}\n")

    with open(f"{prefix}.epi", "w", encoding="utf-8") as fh:
        for i, probe_id in enumerate(_PROBES):
            fh.write(f"1\t{probe_id}\t0\t{10000 + i}\t{probe_id}\tN\n")

    cols = np.zeros(2 * len(_PROBES) + 1, dtype=np.int64)
    rowid_parts: list[np.ndarray] = []
    val_parts: list[np.ndarray] = []
    offset = 0
    for i, probe_id in enumerate(_PROBES):
        rows = _ASSOCS[probe_id]
        n = len(rows)
        idx = np.array([r[0] for r in rows], dtype=np.uint32)
        betas = np.array([r[1] for r in rows], dtype=np.float32)
        ses = np.array([r[2] for r in rows], dtype=np.float32)
        cols[2 * i] = offset
        cols[2 * i + 1] = offset + n
        rowid_parts.extend([idx, idx])
        val_parts.extend([betas, ses])
        offset += 2 * n
    cols[2 * len(_PROBES)] = offset
    val_num = offset
    rowid = np.concatenate(rowid_parts) if rowid_parts else np.empty(0, dtype=np.uint32)
    val = np.concatenate(val_parts) if val_parts else np.empty(0, dtype=np.float32)

    with open(f"{prefix}.besd", "wb") as fh:
        fh.write(struct.pack("<I", 3))
        fh.write(b"\x00" * (15 * 4))
        fh.write(struct.pack("<Q", val_num))
        fh.write(cols.tobytes())
        fh.write(rowid.astype(np.uint32).tobytes())
        fh.write(val.astype(np.float32).tobytes())


def _read_associations(prefix: Path, probe_id: str) -> dict[str, tuple[float, float]]:
    """{snp_id: (beta, se)} for one probe, read via opengwasdb's real reader."""
    snps = read_esi(f"{prefix}.esi")
    probes = read_epi(f"{prefix}.epi")
    probe = next(p for p in probes if p.probe_id == probe_id)
    reader = BESDReader(f"{prefix}.besd", len(probes))
    snp_idx, betas, ses = reader.get_probe_associations(probe.row_idx)
    return {
        snps[int(i)].snp_id: (float(b), float(s))
        for i, b, s in zip(snp_idx.tolist(), betas.tolist(), ses.tolist(), strict=True)
    }


def main() -> None:
    n_checks = 0

    def check(cond: bool, message: str) -> None:
        nonlocal n_checks
        n_checks += 1
        if not cond:
            raise AssertionError(message)

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "source"
        _write_synthetic_besd(source)

        analyses = tmp_path / "analyses.tsv"
        analyses.write_text(
            "analysis_id\texclude_from_build\n"
            "PROBE_A::whole_blood\t\n"
            "PROBE_B::whole_blood\tfalse\n"
            "PROBE_C::whole_blood\ttrue\n",
            encoding="utf-8",
        )

        # --- accepted analyses.tsv selects probes in manifest order ---
        dest = tmp_path / "subset_ab"
        result = write_besd_subset_from_analyses(analyses, source, dest)
        check(result.n_probes == 2, "n_probes should count only selected probes")
        check(result.n_snps == 3, "n_snps should be rs1/rs2/rs3 -- rs4 is unreferenced")
        check(result.n_associations == 4, "n_associations should sum both probes' association counts")
        for probe_id in ["PROBE_A", "PROBE_B"]:
            check(
                _read_associations(dest, probe_id) == _read_associations(source, probe_id),
                f"{probe_id}: subset associations must exactly match source associations",
            )
        check(
            [p.probe_id for p in read_epi(f"{dest}.epi")] == ["PROBE_A", "PROBE_B"],
            "analysis_id tissue suffixes must be removed and excluded rows skipped",
        )

        # --- command-line interface takes analyses.tsv, input, and output ---
        cli_dest = tmp_path / "subset_cli"
        script = REPO_ROOT / "resources/generators/lib/source-formats/eqtlgen-besd-ragged/subset_besd.py"
        cli = subprocess.run(
            [sys.executable, str(script), str(analyses), str(source), str(cli_dest)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        check(cli.returncode == 0, f"subset CLI failed: {cli.stderr}")
        check(
            [p.probe_id for p in read_epi(f"{cli_dest}.epi")] == ["PROBE_A", "PROBE_B"],
            "CLI subset must follow analyses.tsv",
        )

        # --- drops unreferenced SNPs ---
        dest_a = tmp_path / "subset_a"
        write_besd_subset(source, dest_a, probe_ids=["PROBE_A"])
        snp_ids = {s.snp_id for s in read_esi(f"{dest_a}.esi")}
        check(snp_ids == {"rs1", "rs2"}, f"expected only PROBE_A's SNPs, got {snp_ids}")

        # --- zero-association probe ---
        dest_ac = tmp_path / "subset_ac"
        result_ac = write_besd_subset(source, dest_ac, probe_ids=["PROBE_A", "PROBE_C"])
        check(result_ac.n_probes == 2, "zero-association probe must still be counted")
        check(_read_associations(dest_ac, "PROBE_C") == {}, "PROBE_C has no associations in source")
        check(
            _read_associations(dest_ac, "PROBE_A") == _read_associations(source, "PROBE_A"),
            "PROBE_A must be unaffected by a co-selected zero-association probe",
        )

        # --- shared SNP across probes maps to one row, not duplicated ---
        rs2_count = len([s for s in read_esi(f"{dest}.esi") if s.snp_id == "rs2"])
        check(rs2_count == 1, "a SNP shared by two selected probes must appear once in the new .esi")
        assoc_a = _read_associations(dest, "PROBE_A")
        assoc_b = _read_associations(dest, "PROBE_B")
        # Compared against the source's own re-read value (also float32-rounded),
        # not a hand-typed literal, so this isn't a float32-vs-float64 false negative.
        check(
            assoc_a["rs2"] == _read_associations(source, "PROBE_A")["rs2"],
            "PROBE_A's rs2 association value must survive remapping",
        )
        check(
            assoc_b["rs2"] == _read_associations(source, "PROBE_B")["rs2"],
            "PROBE_B's rs2 association value must survive remapping",
        )

        # --- unknown probe id rejected ---
        try:
            write_besd_subset(source, tmp_path / "subset_bad", probe_ids=["PROBE_A", "PROBE_NOPE"])
            check(False, "an unknown probe id must raise ValueError")
        except ValueError as exc:
            check("PROBE_NOPE" in str(exc), "the ValueError should name the missing probe id")

        # --- destination preserves caller's probe order, not source order ---
        dest_ba = tmp_path / "subset_ba"
        write_besd_subset(source, dest_ba, probe_ids=["PROBE_B", "PROBE_A"])
        order = [p.probe_id for p in read_epi(f"{dest_ba}.epi")]
        check(order == ["PROBE_B", "PROBE_A"], f"expected caller's order [PROBE_B, PROBE_A], got {order}")

    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
