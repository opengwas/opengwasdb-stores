"""Tiny, offline gnomAD producer fixtures at its CLI and engine-reader seams (#472)."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from manifest_generation.source_inventory import INVENTORY_COLUMNS
from opengwasdb.variants.normalise import canonical_alid
from opengwasdb.variants.reference import REFERENCE_FIRST_MARKER, read_variant_reference

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools/reference-resources/gnomad-v4-common-hg38"


class TestGnomadReference(unittest.TestCase):
    def test_producer_preserves_insertion_deletion_and_snv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Replace only the remote bcftools transport. REF/ALT remain the
            # source's declaration; no internal producer function is mocked.
            executable = root / "bcftools"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                "mt = 'AF_hom' in sys.argv[sys.argv.index('-f') + 1]\n"
                "chrom = 'chrM' if mt else 'chr1'\n"
                "for pos, ref, alt in [(100, 'G', 'GG'), (100, 'GG', 'G'), (200, 'T', 'C')]:\n"
                "    fields = [chrom, str(pos), ref, alt] + (['0.1'] if mt else ['0.1', '100'] * 10)\n"
                "    print('\\t'.join(fields))\n"
            )
            executable.chmod(0o755)
            for contig, chromosome in (("chr1", "1"), ("chrM", "MT")):
                with self.subTest(contig=contig):
                    out = root / contig
                    result = subprocess.run(
                        [sys.executable, str(TOOLS / "build_common_panel.py"),
                         "--out-dir", str(out), "--contigs", contig,
                         "--jobs", "1", "--shard-mb", "1000"],
                        env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]},
                        text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    manifest = json.loads((out / "manifest.json").read_text())
                    self.assertEqual(len(manifest["outputs"]), 17)
                    for output in manifest["outputs"].values():
                        path = Path(output["path"])
                        reference = read_variant_reference(path)
                        self.assertEqual(reference.alids, [
                            f"{chromosome}:100:G:GG", f"{chromosome}:100:GG:G", f"{chromosome}:200:C:T",
                        ])
                        self.assertEqual(reference.alids, [
                            canonical_alid(chromosome, pos, ref, alt)
                            for pos, ref, alt in ((100, "G", "GG"), (100, "GG", "G"), (200, "T", "C"))
                        ])
                        raw = gzip.decompress(path.read_bytes())
                        self.assertEqual(raw.splitlines(keepends=True)[0], REFERENCE_FIRST_MARKER.encode())
                        self.assertEqual(output["variants"], 3)
                        self.assertEqual(output["content_sha256"], hashlib.sha256(raw).hexdigest())
                        self.assertEqual(output["file_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_coverage_verifier_uses_the_engine_reference_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.tsv"
            row = {"analysis_id": "unavailable", "readiness_status": "data_failed"}
            inventory.write_text("\t".join(INVENTORY_COLUMNS) + "\n"
                                 + "\t".join(row.get(name, "") for name in INVENTORY_COLUMNS) + "\n")
            cases = (
                ("declared", REFERENCE_FIRST_MARKER + "1:100:G:GG\n1:100:GG:G\n1:200:C:T\n", 0, "3 ALIDs"),
                ("legacy-indel", "1:100:G:GG\n", 1, "regenerated with reference-first indels"),
                ("legacy-snv", "1:200:C:T\n", 0, "1 ALIDs"),
            )
            for name, text, code, message in cases:
                with self.subTest(name=name):
                    axis = root / (name + ".txt.gz")
                    with gzip.open(axis, "wt") as handle:
                        handle.write(text)
                    result = subprocess.run(
                        [sys.executable, str(TOOLS / "verify_axis_coverage.py"),
                         "--inventory", str(inventory), "--axis", str(axis)],
                        text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertIn(message, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
