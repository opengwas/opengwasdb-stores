#!/usr/bin/env python3
"""The eQTLGen metadata adapter: a BESD file set -> Source Metadata Table (#291, #314).

eQTLGen's file set holds one Analysis per probe and carries no study metadata,
so the adapter writes one row per ``.epi`` probe: the Analysis id (the bare
probe id, never ``<probe>::<tissue>``: an ``analysis_id`` never contains ``:``,
issue #319), the probe it reads, the probe's position as its target, and the
tissue the operator states on every row. A blank tissue is refused before
anything is written (#319). The probe's ``sample_size`` is derived from the file
(#314): the maximum over its pairs of N = 1/(2p(1-p)se^2) - z^2, over pairs with
finite N and |z| >= 1, rounded, then capped at the study total. The fixture BESD
is built here from a known N per pair, so the expected sample sizes are literals,
not the adapter's own output.

Run from the repository root:
    python3 tests/eqtlgen-pipeline/test_source_metadata_adapter.py
"""

from __future__ import annotations

import contextlib
import io
import math
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from manifest_generation.providers.eqtlgen.source_metadata_adapter import (
    AdapterError,
    convert,
    main,
)
from manifest_generation.source_metadata_table import load_source_metadata_table

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "eqtlgen-fixture.epi"

#: Study total, the cap on every probe's N.
STUDY_TOTAL = 31684
#: The fixture SNPs' frequencies, in .esi order.
SNP_FREQ = [0.3, 0.25, 0.4, 0.1]
#: Each probe's pairs as (snp index, the pair's own N, z). A pair's beta and SE are
#: written from them by eQTLGen's rule, b = z se and se = 1/sqrt(2p(1-p)(N+z^2)),
#: so the N the adapter should derive for it is known.
PAIRS = {
    # Largest qualifying N is 20,000 (z 10); 18,000 (z 1.2) and 1,000 are smaller.
    "ENSG00000175445": [(0, 1000, 2.0), (1, 20000, 10.0), (2, 18000, 1.2)],
    # The 90,000 pair has z 0.0005 and the 50,000 pair z 0.9: both are under |z| 1
    # and ignored, so the maximum is the 12,000 pair.
    "ENSG00000134243": [(0, 12000, 10.0), (1, 90000, 0.0005), (3, 50000, 0.9)],
    # 31,700 is above the study total, so it is capped.
    "ENSG00000232810": [(2, 31700, 10.0), (0, 500, 3.0)],
}


def write_file_set(prefix: Path, epi_text: str, pairs) -> None:
    """A SPARSE_FILE_TYPE_3 BESD triple whose pairs have the stated N and z."""
    probes = [line.split("\t")[1] for line in epi_text.splitlines() if line.strip()]
    Path(f"{prefix}.epi").write_text(epi_text, encoding="utf-8")
    Path(f"{prefix}.esi").write_text(
        "".join(f"1\trs{i}\t0\t{1000 * (i + 1)}\tA\tG\t{f}\n" for i, f in enumerate(SNP_FREQ)),
        encoding="utf-8",
    )
    cols = np.zeros(2 * len(probes) + 1, dtype=np.int64)
    rowid, beta, se_values, offset = [], [], [], 0
    for n, probe in enumerate(probes):
        rows = pairs[probe]
        cols[2 * n], cols[2 * n + 1] = offset, offset + len(rows)
        idx = np.array([r[0] for r in rows], dtype=np.uint32)
        p = np.array([SNP_FREQ[r[0]] for r in rows])
        n_pair = np.array([r[1] for r in rows], dtype=float)
        z = np.array([r[2] for r in rows], dtype=float)
        se = 1.0 / np.sqrt(2 * p * (1 - p) * (n_pair + z**2))
        rowid += [idx, idx]
        beta.append((z * se).astype(np.float32))
        se_values.append(se.astype(np.float32))
        offset += 2 * len(rows)
    cols[-1] = offset
    with open(f"{prefix}.besd", "wb") as fh:
        fh.write(struct.pack("<I", 3) + b"\x00" * 60 + struct.pack("<Q", offset))
        # Per probe: its betas, then its SEs; rowid repeats the SNP indices.
        values = np.concatenate([np.concatenate([b, s]) for b, s in zip(beta, se_values)])
        fh.write(cols.tobytes() + np.concatenate(rowid).tobytes() + values.tobytes())


EXPECTED_SAMPLE_SIZE = {
    "ENSG00000175445": "20000",
    "ENSG00000134243": "12000",
    "ENSG00000232810": "31684",
}

EXPECTED_HEADER = (
    "source_analysis_id", "source_trait_label", "study_design", "sample_size",
    "target_ensembl_gene_id", "target_chromosome", "target_start", "target_end",
    "tissue", "source_probe_id",
)

EXPECTED = [
    {
        "source_analysis_id": "ENSG00000175445",
        "source_probe_id": "ENSG00000175445",
        "source_trait_label": "ENSG00000175445",
        "study_design": "quantitative",
        "sample_size": "20000",
        "tissue": "whole_blood",
        "target_ensembl_gene_id": "ENSG00000175445",
        "target_chromosome": "8",
        "target_start": "19791998",
        "target_end": "19791998",
    },
    {
        "source_analysis_id": "ENSG00000134243",
        "source_probe_id": "ENSG00000134243",
        "source_trait_label": "ENSG00000134243",
        "study_design": "quantitative",
        "sample_size": "12000",
        "tissue": "whole_blood",
        "target_ensembl_gene_id": "ENSG00000134243",
        "target_chromosome": "1",
        "target_start": "109896382",
        "target_end": "109896382",
    },
    {
        "source_analysis_id": "ENSG00000232810",
        "source_probe_id": "ENSG00000232810",
        "source_trait_label": "ENSG00000232810",
        "study_design": "quantitative",
        "sample_size": "31684",
        "tissue": "whole_blood",
        "target_ensembl_gene_id": "ENSG00000232810",
        "target_chromosome": "6",
        "target_start": "31544728",
        "target_end": "31544728",
    },
]


class EqtlgenSourceMetadataAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "source-metadata.tsv"
        self.prefix = self.tmp / "fixture"
        write_file_set(self.prefix, FIXTURE.read_text(encoding="utf-8"), PAIRS)

    def convert(self, prefix=None, **overrides):
        arguments = dict(tissue="whole_blood", study_total=STUDY_TOTAL)
        arguments.update(overrides)
        return convert(prefix or self.prefix, self.out, **arguments)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_every_probe_becomes_one_canonical_row(self) -> None:
        self.convert()
        table = load_source_metadata_table(self.out)
        self.assertEqual(table.columns, EXPECTED_HEADER)
        self.assertEqual(
            [dict(table.row(analysis_id)) for analysis_id in table.analysis_ids], EXPECTED
        )

    def test_the_analysis_id_is_the_bare_probe_and_the_tissue_its_own_column(self) -> None:
        """Issue #319: no id carries ``:``; the tissue is recorded beside it, not in it."""
        self.convert()
        table = load_source_metadata_table(self.out)
        for analysis_id in table.analysis_ids:
            self.assertNotIn(":", analysis_id)
            self.assertEqual(analysis_id, table.row(analysis_id)["source_probe_id"])
            self.assertEqual(table.row(analysis_id)["tissue"], "whole_blood")

    def test_a_blank_tissue_is_refused_before_anything_is_written(self) -> None:
        """Issue #319: a blank tissue fails loudly, never ships a blank column.

        The adapter is the only writer of eQTLGen's tissue, so a blank or
        whitespace-only ``--tissue`` is refused here, by the function and by the
        command line, and no table is left for freeze to read.
        """
        for tissue in ("", "   ", " whole_blood"):
            with self.subTest(tissue=tissue):
                with self.assertRaises(AdapterError) as caught:
                    self.convert(tissue=tissue)
                self.assertIn("tissue", str(caught.exception))
                self.assertFalse(self.out.exists())
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    returncode = main([
                        "--prefix", str(self.prefix), "--tissue", tissue,
                        "--study-total", "31684", "--out", str(self.out),
                    ])
                self.assertEqual(returncode, 1)
                self.assertIn("tissue", stderr.getvalue())
                self.assertFalse(self.out.exists())

    def test_a_probe_on_a_chromosome_the_table_does_not_know_is_refused_by_name(self) -> None:
        odd = self.tmp / "odd"
        write_file_set(odd, "23\tENSG00000000001\t0\t5\tENSG00000000001\tN\n",
                       {"ENSG00000000001": [(0, 5000, 3.0)]})
        with self.assertRaises(AdapterError) as caught:
            self.convert(odd)
        self.assertIn("ENSG00000000001", str(caught.exception))
        self.assertIn("chromosome '23'", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_a_repeated_probe_and_a_short_row_are_refused(self) -> None:
        bad = self.tmp / "bad"
        Path(f"{bad}.epi").write_text(
            "1\tENSG00000000001\t0\t5\tENSG00000000001\tN\n"
            "1\tENSG00000000001\t0\t9\tENSG00000000001\tN\n"
            "2\tENSG00000000002\t0\n",
            encoding="utf-8",
        )
        shutil.copy(f"{self.prefix}.besd", f"{bad}.besd")
        shutil.copy(f"{self.prefix}.esi", f"{bad}.esi")
        with self.assertRaises(AdapterError) as caught:
            self.convert(bad)
        message = str(caught.exception)
        self.assertIn("ENSG00000000001", message)
        self.assertIn("line 3", message)
        self.assertFalse(self.out.exists())

    def test_the_sample_size_is_the_maximum_pair_n_over_pairs_with_abs_z_at_least_one_capped_at_the_total(self) -> None:
        """Literals: 20,000; 12,000 (the 90,000 and 50,000 pairs have |z| < 1); 31,684 (31,700 capped)."""
        self.convert()
        table = load_source_metadata_table(self.out)
        self.assertEqual(
            {i: table.row(i)["sample_size"] for i in table.analysis_ids}, EXPECTED_SAMPLE_SIZE
        )

    def test_the_cap_is_the_study_total_the_operator_states(self) -> None:
        self.convert(study_total=15000)
        table = load_source_metadata_table(self.out)
        self.assertEqual(
            {i: table.row(i)["sample_size"] for i in table.analysis_ids},
            {"ENSG00000175445": "15000", "ENSG00000134243": "12000", "ENSG00000232810": "15000"},
        )

    def test_a_probe_with_no_pair_at_abs_z_one_is_refused_by_name(self) -> None:
        weak = self.tmp / "weak"
        write_file_set(weak, "1\tENSG00000000009\t0\t5\tENSG00000000009\tN\n",
                       {"ENSG00000000009": [(0, 5000, 0.5), (1, 6000, 0.2)]})
        with self.assertRaises(AdapterError) as caught:
            self.convert(weak)
        self.assertIn("ENSG00000000009", str(caught.exception))
        self.assertIn("|z|", str(caught.exception))
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
