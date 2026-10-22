#!/usr/bin/env python3
"""The eQTLGen acquisition writer: subset a BESD file set to a Selection, record it (#291).

The writer is driven directly against a small synthetic BESD file set built by
hand (the layout is ``subset_besd``'s module docstring's, written here
independently of its writer), a Source Metadata Table and a release config
naming the Selection. It pins what the Acquisition Manifest records, adoption
of an existing prefix, resume, and the refusals: a changed Selection, a changed
member, and a changed original or tool, none of which may relabel retained
output or touch the recorded manifest and lineage.

Run from the repository root:
    python3 tests/eqtlgen-pipeline/test_acquire.py
"""

from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import yaml

from manifest_generation.acquisition_manifest import (
    load_acquisition_manifest,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import write_connector
from manifest_generation.providers.eqtlgen import acquire as acquire_module
from manifest_generation.providers.eqtlgen.acquire import AcquisitionError, acquire
from manifest_generation.source_metadata_table import SOURCE_METADATA_TABLE

SNPS = [("1", "rs1", 1000, "A", "G", 0.1), ("1", "rs2", 2000, "C", "T", 0.2),
        ("1", "rs3", 3000, "G", "A", 0.3), ("2", "rs4", 4000, "T", "C", 0.4)]
PROBES = ["PROBE_A", "PROBE_B", "PROBE_C"]
ASSOCS = {"PROBE_A": [(0, 0.5, 0.1), (1, -0.3, 0.2)],
          "PROBE_B": [(1, 0.7, 0.15), (2, -0.1, 0.05)],
          "PROBE_C": [(3, 0.2, 0.3)]}


def write_source(prefix: Path) -> None:
    """A SPARSE_FILE_TYPE_3 BESD triple: magic 3, 15 reserved words, val_num, offsets, rowids, values."""
    Path(f"{prefix}.esi").write_text(
        "".join(f"{c}\t{i}\t0\t{bp}\t{a1}\t{a2}\t{f}\n" for c, i, bp, a1, a2, f in SNPS))
    Path(f"{prefix}.epi").write_text(
        "".join(f"1\t{p}\t0\t{10000 + n}\t{p}\tN\n" for n, p in enumerate(PROBES)))
    cols = np.zeros(2 * len(PROBES) + 1, dtype=np.int64)
    rowid, val, offset = [], [], 0
    for n, probe in enumerate(PROBES):
        rows = ASSOCS[probe]
        idx = np.array([r[0] for r in rows], dtype=np.uint32)
        cols[2 * n], cols[2 * n + 1] = offset, offset + len(rows)
        rowid += [idx, idx]
        val += [np.array([r[1] for r in rows], dtype=np.float32),
                np.array([r[2] for r in rows], dtype=np.float32)]
        offset += 2 * len(rows)
    cols[-1] = offset
    with open(f"{prefix}.besd", "wb") as fh:
        fh.write(struct.pack("<I", 3) + b"\x00" * 60 + struct.pack("<Q", offset))
        fh.write(cols.tobytes() + np.concatenate(rowid).tobytes() + np.concatenate(val).tobytes())


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class AcquireTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "source" / "full"
        self.source.parent.mkdir()
        write_source(self.source)
        self.table = self.tmp / "table.tsv"
        write_connector(
            self.table, SOURCE_METADATA_TABLE,
            ("source_analysis_id", "source_probe_id", "study_design"),
            [{"source_analysis_id": f"{p}::blood", "source_probe_id": p, "study_design": "quantitative"}
             for p in PROBES],
        )
        self.config = self.config_for(["PROBE_A", "PROBE_C"])
        self.out = self.tmp / "connectors" / "acq.tsv"
        self.out_dir = self.tmp / "subsets"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config_for(self, probes, name="release.yaml") -> Path:
        path = self.tmp / name
        path.write_text(yaml.safe_dump({
            "source_collection": {"connectors": {"source_metadata_table": str(self.table)}},
            "selection": {"analyses": [f"{p}::blood" for p in probes]},
        }), encoding="utf-8")
        return path

    def run_acquire(self, **overrides):
        arguments = dict(config=self.config, source_prefix=self.source, out_dir=self.out_dir,
                         name="subset", out=self.out)
        arguments.update(overrides)
        return acquire(**arguments)

    def snapshot(self) -> dict:
        lineage = pass_lineage_path(self.out)
        return {"manifest": self.out.read_bytes(), "lineage": lineage.read_bytes(),
                "members": {s: (self.out_dir / f"subset{s}").read_bytes() for s in (".besd", ".epi", ".esi")}}

    # -- a first acquisition ----------------------------------------------------

    def test_the_manifest_records_the_file_set_every_selected_analysis_claims_and_its_lineage(self) -> None:
        self.run_acquire()
        manifest = load_acquisition_manifest(self.out)
        prefix = str(self.out_dir / "subset")
        self.assertEqual([e.source_analysis_id for e in manifest.entries],
                         ["PROBE_A::blood", "PROBE_C::blood"])
        self.assertEqual([e.source_probe_id for e in manifest.entries], ["PROBE_A", "PROBE_C"])
        for entry in manifest.entries:
            self.assertEqual(entry.location, prefix)
            self.assertEqual(entry.members[".epi"],
                             {"bytes": len(Path(f"{prefix}.epi").read_bytes()), "sha256": sha(f"{prefix}.epi")})
            self.assertEqual((entry.bytes, entry.sha256),
                             (entry.members[".besd"]["bytes"], sha(f"{prefix}.besd")))
            self.assertEqual(entry.lineage.original_location, str(self.source))
            self.assertEqual(entry.lineage.original_sha256, sha(f"{self.source}.besd"))
            self.assertTrue(entry.lineage.reformat_tool.startswith(acquire_module.TOOL_MODULE + " sha256:"))
        # The subset holds exactly the Selection's probes, in order.
        self.assertEqual(Path(f"{prefix}.epi").read_text().split("\n")[:-1],
                         ["1\tPROBE_A\t0\t10000\tPROBE_A\tN", "1\tPROBE_C\t0\t10002\tPROBE_C\tN"])
        record = yaml.safe_load(pass_lineage_path(self.out).read_text())
        self.assertEqual(record["probes"], ["PROBE_A", "PROBE_C"])
        self.assertEqual(record["selection"], {"analyses": ["PROBE_A::blood", "PROBE_C::blood"]})
        self.assertEqual(record["original"]["members"][".esi"]["sha256"], sha(f"{self.source}.esi"))

    def test_the_lineage_counts_the_original_and_the_subset(self) -> None:
        """Probes, SNPs and associations, so a reader sees what the subset dropped."""
        self.run_acquire()
        record = yaml.safe_load(pass_lineage_path(self.out).read_text())
        self.assertEqual(record["original"]["counts"], {"probes": 3, "snps": 4, "associations": 5})
        self.assertEqual(record["subset"]["counts"], {"probes": 2, "snps": 3, "associations": 3})

    # -- resume ---------------------------------------------------------------

    def test_a_resume_keeps_the_subset_and_the_record_byte_for_byte(self) -> None:
        self.run_acquire()
        before = self.snapshot()
        stamps = {s: (self.out_dir / f"subset{s}").stat().st_mtime_ns for s in (".besd", ".epi", ".esi")}
        self.run_acquire()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(stamps, {s: (self.out_dir / f"subset{s}").stat().st_mtime_ns for s in stamps})

    def test_a_changed_selection_is_refused_and_nothing_changes(self) -> None:
        self.run_acquire()
        before = self.snapshot()
        with self.assertRaises(AcquisitionError) as caught:
            self.run_acquire(config=self.config_for(["PROBE_A", "PROBE_B"], "other.yaml"))
        self.assertIn("different Selection", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_a_changed_member_is_refused_by_name_and_nothing_is_rewritten(self) -> None:
        self.run_acquire()
        member = self.out_dir / "subset.esi"
        member.write_bytes(member.read_bytes() + b"1\trs9\t0\t9\tA\tC\t0.5\n")
        before = self.snapshot()
        with self.assertRaises(AcquisitionError) as caught:
            self.run_acquire()
        self.assertIn(".esi", str(caught.exception))
        self.assertIn("no longer match", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_a_changed_original_is_refused_and_the_record_is_not_relabelled(self) -> None:
        self.run_acquire()
        before = self.snapshot()
        esi = Path(f"{self.source}.esi")
        esi.write_text(esi.read_text().replace("0.1\n", "0.15\n", 1))
        with self.assertRaises(AcquisitionError) as caught:
            self.run_acquire()
        self.assertIn(".esi", str(caught.exception))
        self.assertIn("original", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_a_changed_tool_is_refused_and_the_record_is_not_relabelled(self) -> None:
        self.run_acquire()
        before = self.snapshot()
        with mock.patch.object(acquire_module, "tool_identity", return_value="another-tool sha256:" + "0" * 64):
            with self.assertRaises(AcquisitionError) as caught:
                self.run_acquire()
        self.assertIn("tool", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_a_changed_original_location_is_refused(self) -> None:
        self.run_acquire()
        before = self.snapshot()
        moved = self.tmp / "source" / "moved"
        for suffix in (".besd", ".epi", ".esi"):
            Path(f"{moved}{suffix}").write_bytes(Path(f"{self.source}{suffix}").read_bytes())
        with self.assertRaises(AcquisitionError) as caught:
            self.run_acquire(source_prefix=moved)
        self.assertIn("original", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    # -- adoption -------------------------------------------------------------

    def test_an_existing_prefix_is_adopted_only_when_the_writer_reproduces_every_member(self) -> None:
        self.run_acquire()
        existing = self.out_dir / "subset"
        adopted_out = self.tmp / "connectors" / "adopted.tsv"
        self.run_acquire(out_dir=None, name=None, adopt_prefix=existing, out=adopted_out)
        manifest = load_acquisition_manifest(adopted_out)
        self.assertEqual({e.location for e in manifest.entries}, {str(existing)})
        self.assertEqual(manifest.entries[0].members, load_acquisition_manifest(self.out).entries[0].members)

    def test_an_existing_prefix_the_writer_does_not_reproduce_is_refused_by_name(self) -> None:
        self.run_acquire()
        tampered = self.tmp / "tampered"
        for suffix in (".besd", ".epi", ".esi"):
            Path(f"{tampered}{suffix}").write_bytes((self.out_dir / f"subset{suffix}").read_bytes())
        Path(f"{tampered}.besd").write_bytes(Path(f"{tampered}.besd").read_bytes()[:-4] + b"\x00\x00\x00\x00")
        adopted_out = self.tmp / "connectors" / "adopted.tsv"
        with self.assertRaises(AcquisitionError) as caught:
            self.run_acquire(out_dir=None, name=None, adopt_prefix=tampered, out=adopted_out)
        self.assertIn(".besd", str(caught.exception))
        self.assertIn("not the subset writer's output", str(caught.exception))
        self.assertFalse(adopted_out.exists())

    def test_adoption_and_a_written_subset_are_alternatives(self) -> None:
        with self.assertRaises(AcquisitionError):
            self.run_acquire(adopt_prefix=self.tmp / "x")


if __name__ == "__main__":
    unittest.main()
