#!/usr/bin/env python3
"""The eQTLGen metadata adapter: a BESD file set -> Source Metadata Table (#291, #314).

eQTLGen's file set holds one Analysis per probe and carries no study metadata,
so the adapter writes one row per ``.epi`` probe: the Analysis id (the bare
probe id, never ``<probe>::<tissue>``: an ``analysis_id`` never contains ``:``,
issue #319), the probe it reads, the probe's position as its target, and the
tissue the operator states on every row. A blank tissue is refused before
anything is written (#319). The probe's ``sample_size`` is the maximum published
``NrSamples`` over its pairs, read from the per-pair table ``sample_size_table``
writes (#302): never recovered from the BESD, so it agrees with the Store's own
``n`` plane. The fixture BESD is still built here, from a known N per pair, so the
test can show that the formula's maxima differ from the published ones and that
the published ones win; every expected value is a literal.

Run from the repository root:
    python3 tests/eqtlgen-pipeline/test_source_metadata_adapter.py
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import math
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
import yaml

from manifest_generation.providers.eqtlgen.source_metadata_adapter import (
    AdapterError,
    convert,
    main,
)
from manifest_generation.source_metadata_table import load_source_metadata_table

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "eqtlgen-fixture.epi"

FIRST_AUTHOR = "Võsa"
PUBLICATION_PMID = "34475573"
#: The fixture SNPs' frequencies, in .esi order.
SNP_FREQ = [0.3, 0.25, 0.4, 0.1, 0.0]
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
        idx = np.array([r[1] if r[0] == "raw" else r[0] for r in rows], dtype=np.uint32)
        b_pair, se_pair = [], []
        for r in rows:
            if r[0] == "raw":  # ("raw", snp index, beta, se): stored as given
                b_pair.append(r[2])
                se_pair.append(r[3])
                continue
            p, n_pair, z = SNP_FREQ[r[0]], r[1], r[2]
            se = 1.0 / np.sqrt(2 * p * (1 - p) * (n_pair + z**2))
            b_pair.append(z * se)
            se_pair.append(se)
        rowid += [idx, idx]
        beta.append(np.array(b_pair).astype(np.float32))
        se_values.append(np.array(se_pair).astype(np.float32))
        offset += 2 * len(rows)
    cols[-1] = offset
    with open(f"{prefix}.besd", "wb") as fh:
        fh.write(struct.pack("<I", 3) + b"\x00" * 60 + struct.pack("<Q", offset))
        # Per probe: its betas, then its SEs; rowid repeats the SNP indices.
        values = np.concatenate([np.concatenate([b, s]) for b, s in zip(beta, se_values)])
        fh.write(cols.tobytes() + np.concatenate(rowid).tobytes() + values.tobytes())


#: What the formula would recover from the fixture BESD (the last capped at the study total).
FORMULA_SAMPLE_SIZE = {
    "ENSG00000175445": "20000",
    "ENSG00000134243": "12000",
    "ENSG00000232810": "31684",
}

#: The per-pair table the stores side writes from the published NrSamples
#: (``sample_size_table``): probe, SNP, N. ENSG00000999999 is not in the file set.
TABLE_ROWS = [
    ("ENSG00000175445", "rs0", "1000"),
    ("ENSG00000175445", "rs1", "19990"),
    ("ENSG00000175445", "rs2", "18000"),
    ("ENSG00000134243", "rs0", "12345"),
    ("ENSG00000134243", "rs1", "9000"),
    ("ENSG00000134243", "rs3", "100"),
    ("ENSG00000232810", "rs2", "31000"),
    ("ENSG00000232810", "rs0", "500"),
    ("ENSG00000999999", "rs0", "31684"),
]

#: Each probe's maximum published pair N: the Analysis's sample_size.
EXPECTED_SAMPLE_SIZE = {
    "ENSG00000175445": "19990",
    "ENSG00000134243": "12345",
    "ENSG00000232810": "31000",
}


def write_table(path: Path, rows) -> None:
    path.write_bytes(
        gzip.compress(("probe_id\tsnp_id\tn\n" + "".join("\t".join(r) + "\n" for r in rows)).encode())
    )

EXPECTED_HEADER = (
    "source_analysis_id", "source_trait_label", "study_design", "sample_size",
    "publication_pmid", "first_author", "target_ensembl_gene_id", "target_chromosome", "target_start", "target_end",
    "tissue", "source_probe_id",
)

EXPECTED = [
    {
        "source_analysis_id": "ENSG00000175445",
        "source_probe_id": "ENSG00000175445",
        "source_trait_label": "ENSG00000175445",
        "publication_pmid": "34475573",
        "first_author": "Võsa",
        "study_design": "quantitative",
        "sample_size": "19990",
        "tissue": "whole_blood",
        "target_ensembl_gene_id": "ENSG00000175445",
        "target_chromosome": "8",
        "target_start": "19791998",
        "target_end": "19791998",
    },
    {
        "source_analysis_id": "ENSG00000134243",
        "source_probe_id": "ENSG00000134243",
        "source_trait_label": "SORT1",
        "publication_pmid": "34475573",
        "first_author": "Võsa",
        "study_design": "quantitative",
        "sample_size": "12345",
        "tissue": "whole_blood",
        "target_ensembl_gene_id": "ENSG00000134243",
        "target_chromosome": "1",
        "target_start": "109896382",
        "target_end": "109896382",
    },
    {
        "source_analysis_id": "ENSG00000232810",
        "source_probe_id": "ENSG00000232810",
        "source_trait_label": "TNF",
        "publication_pmid": "34475573",
        "first_author": "Võsa",
        "study_design": "quantitative",
        "sample_size": "31000",
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
        self.table = self.tmp / "sample-size.tsv.gz"
        write_table(self.table, TABLE_ROWS)
        # eQTLGen's gene symbols for the fixture probes: the .epi names none. The
        # first has none (its label stays its Ensembl id).
        self.symbols = self.tmp / "gene-symbols.tsv"
        self.symbols.write_text(
            "gene\tgene_symbol\nENSG00000134243\tSORT1\nENSG00000232810\tTNF\n", encoding="utf-8"
        )

    def convert(self, prefix=None, **overrides):
        arguments = dict(
            tissue="whole_blood", first_author=FIRST_AUTHOR, publication_pmid=PUBLICATION_PMID,
            gene_symbols=self.symbols, sample_size_table=self.table,
        )
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
                        "--sample-size-table", str(self.table), "--first-author", FIRST_AUTHOR,
                        "--publication-pmid", PUBLICATION_PMID,
                        "--gene-symbols", str(self.symbols), "--out", str(self.out),
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
        with self.assertRaises(AdapterError) as caught:
            self.convert(bad)
        message = str(caught.exception)
        self.assertIn("ENSG00000000001", message)
        self.assertIn("line 3", message)
        self.assertFalse(self.out.exists())

    # -- the sample size is the published maximum, never recovered from the BESD ------

    def test_the_sample_size_is_the_maximum_published_pair_n_not_the_formula_maximum(self) -> None:
        """The fixture BESD's own pairs recover 20,000 / 12,000 / 31,684 (N = 1/(2p(1-p)se^2) - z^2,
        the last capped); the table holds the published NrSamples, whose maxima are literals."""
        self.convert()
        table = load_source_metadata_table(self.out)
        self.assertEqual(
            {i: table.row(i)["sample_size"] for i in table.analysis_ids}, EXPECTED_SAMPLE_SIZE
        )
        self.assertNotEqual(EXPECTED_SAMPLE_SIZE, FORMULA_SAMPLE_SIZE)

    def test_the_adapter_reads_the_epi_and_the_table_and_never_the_besd(self) -> None:
        for member in ("besd", "esi"):
            Path(f"{self.prefix}.{member}").unlink()
        self.convert()
        table = load_source_metadata_table(self.out)
        self.assertEqual(
            {i: table.row(i)["sample_size"] for i in table.analysis_ids}, EXPECTED_SAMPLE_SIZE
        )

    def test_a_probe_the_table_has_no_pair_for_is_refused_by_name(self) -> None:
        write_table(self.table, [r for r in TABLE_ROWS if r[0] != "ENSG00000134243"])
        with self.assertRaises(AdapterError) as caught:
            self.convert()
        self.assertIn("ENSG00000134243", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_an_n_that_is_not_a_whole_number_from_one_is_refused_by_name(self) -> None:
        for bad in ("0", "-5", "12.5", "NA", ""):
            with self.subTest(n=bad):
                write_table(self.table, TABLE_ROWS + [("ENSG00000134243", "rs9", bad)])
                with self.assertRaises(AdapterError) as caught:
                    self.convert()
                self.assertIn("ENSG00000134243", str(caught.exception))
                self.assertFalse(self.out.exists())

    def test_a_table_without_the_columns_is_refused(self) -> None:
        self.table.write_bytes(gzip.compress(b"probe\tsnp\tcount\nENSG00000134243\trs0\t5\n"))
        with self.assertRaises(AdapterError) as caught:
            self.convert()
        self.assertIn("probe_id", str(caught.exception))
        self.assertFalse(self.out.exists())

    # -- publication metadata and labels -------------------------------------------

    def test_publication_metadata_is_on_every_row_and_the_label_is_the_gene_symbol(self) -> None:
        self.convert()
        table = load_source_metadata_table(self.out)
        labels = {i: table.row(i)["source_trait_label"] for i in table.analysis_ids}
        # A probe with no symbol keeps its Ensembl id; the id itself is never the symbol.
        self.assertEqual(
            labels,
            {"ENSG00000175445": "ENSG00000175445", "ENSG00000134243": "SORT1", "ENSG00000232810": "TNF"},
        )
        for analysis_id in table.analysis_ids:
            row = table.row(analysis_id)
            self.assertEqual(row["source_analysis_id"], analysis_id)
            self.assertEqual(row["source_probe_id"], analysis_id)
            self.assertEqual(row["first_author"], "Võsa")
            self.assertEqual(row["publication_pmid"], "34475573")

    def test_the_provenance_records_the_arguments_and_the_label_counts(self) -> None:
        self.convert()
        record = yaml.safe_load(Path(f"{self.out}.provenance.yaml").read_text(encoding="utf-8"))
        self.assertEqual(record["first_author"], "Võsa")
        self.assertEqual(record["publication_pmid"], "34475573")
        self.assertEqual(record["probes"], 3)
        self.assertEqual(record["labelled_with_symbol"], 2)
        self.assertEqual(record["labelled_with_ensembl_id"], 1)
        self.assertEqual(record["gene_symbols"]["sha256"], hashlib.sha256(self.symbols.read_bytes()).hexdigest())
        self.assertEqual(record["sample_size_table"]["sha256"], hashlib.sha256(self.table.read_bytes()).hexdigest())
        self.assertEqual(record["sample_size_table"]["rows"], 9)
        self.assertNotIn("study_total", record)

    def test_a_blank_author_or_pmid_is_refused(self) -> None:
        for field in ("first_author", "publication_pmid"):
            with self.subTest(field=field):
                with self.assertRaises(AdapterError) as caught:
                    self.convert(**{field: " "})
                self.assertIn(field, str(caught.exception))
                self.assertFalse(self.out.exists())

    def test_a_non_numeric_pmid_is_refused(self) -> None:
        with self.assertRaises(AdapterError) as caught:
            self.convert(publication_pmid="PMC123")
        self.assertIn("publication_pmid", str(caught.exception))

    def test_a_symbol_map_naming_one_gene_twice_is_refused_by_name(self) -> None:
        self.symbols.write_text(
            "gene\tgene_symbol\nENSG00000232810\tTNF\nENSG00000232810\tLTA\n", encoding="utf-8"
        )
        with self.assertRaises(AdapterError) as caught:
            self.convert()
        self.assertIn("ENSG00000232810", str(caught.exception))
        self.assertIn("TNF", str(caught.exception))
        self.assertIn("LTA", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
