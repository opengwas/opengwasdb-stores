#!/usr/bin/env python3
"""The shared dbGaP plumbing: tar tables of contents and ``*.dbGaP.metadata.txt`` (#290).

These tests moved here from the acquisition suite when the reusable dbGaP
plumbing became a helper (``manifest_generation.providers.dbgap.metadata``).
They use the same synthetic fixture shapes the acquisition uses: a ``tar -tv``
table of contents, an ``Info``/``Description`` metadata table and the deposit's
``Total Sample Size=N; Cases=a; Controls=b`` spelling.

Run from the repository root:
    python3 tests/dbgap-mvp/test_dbgap_metadata.py
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manifest_generation.providers.dbgap.metadata import (
    DbgapError,
    load_toc,
    parse_metadata,
    parse_sample_size,
    parse_tar_listing,
    tar_md5,
)

TAR = "phs002453.MVP_R4.1000G_AGR.GIA.Fixture.analysis-PI.MULTI.tar"
INNER = "MVP_R4.1000G_AGR.GIA.Fixture"
MEMBER = f"{INNER}/MVP_R4.1000G_AGR.A1C_Min_INT.EUR.GIA.dbGaP.txt.gz"


class TarListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dbgap-helper-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_table_of_contents_lists_members_with_their_tar_and_size(self) -> None:
        toc = self.root / f"{TAR}.table_of_contents.txt"
        toc.write_text(
            "drwxrwxr-x huffmanj/med112 0 2023-08-22 11:27 "
            f"{INNER}/\n"
            "-rw-rw-r-- huffmanj/med112 12345 2023-08-22 11:27 "
            f"{MEMBER}\n",
            encoding="utf-8",
        )
        self.assertEqual(parse_tar_listing(toc), {MEMBER: (TAR, 12345)})

    def test_an_unsafe_member_path_is_refused_by_name(self) -> None:
        toc = self.root / f"{TAR}.table_of_contents.txt"
        toc.write_text(
            "-rw-rw-r-- huffmanj/med112 1 2023-08-22 11:27 ../escape\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DbgapError, "unsafe member path"):
            parse_tar_listing(toc)

    def test_load_toc_refuses_a_member_listed_twice(self) -> None:
        meta = self.root / "GIA" / "meta"
        meta.mkdir(parents=True)
        line = f"-rw-rw-r-- huffmanj/med112 1 2023-08-22 11:27 {MEMBER}\n"
        for tar in ("a", "b"):
            (meta / f"{tar}.table_of_contents.txt").write_text(line, encoding="utf-8")
        with self.assertRaisesRegex(DbgapError, "more than one table of contents"):
            load_toc(self.root)

    def test_load_toc_reads_every_table_of_contents_beside_the_tars(self) -> None:
        meta = self.root / "GIA" / "meta"
        meta.mkdir(parents=True)
        (meta / f"{TAR}.table_of_contents.txt").write_text(
            f"-rw-rw-r-- huffmanj/med112 7 2023-08-22 11:27 {MEMBER}\n",
            encoding="utf-8",
        )
        self.assertEqual(load_toc(self.root), {MEMBER: (TAR, 7)})

    def test_tar_md5_prefers_the_verified_marker(self) -> None:
        tars = self.root / "GIA" / "tars"
        tars.mkdir(parents=True)
        (tars / f"{TAR}.md5").write_text("aaaa  other.tar\n", encoding="utf-8")
        self.assertEqual(tar_md5(tars, TAR), "aaaa")
        (tars / f"{TAR}.verified").write_text("bbbb\n", encoding="utf-8")
        self.assertEqual(tar_md5(tars, TAR), "bbbb")
        self.assertEqual(tar_md5(tars, "absent.tar"), "")


class MetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dbgap-helper-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def metadata(self, rows: list[tuple[str, str]]) -> Path:
        path = self.root / "MVP_R4.1000G_AGR.A1C_Min_INT.EUR.GIA.dbGaP.metadata.txt"
        path.write_text(
            "Info\tDescription\n" + "".join(f"{key}\t{value}\n" for key, value in rows),
            encoding="utf-8",
        )
        return path

    def test_metadata_is_an_info_description_map(self) -> None:
        path = self.metadata(
            [
                ("Title of analysis", "A1C_Min_INT.EUR.GIA"),
                ("Analyzed variable", "hemoglobin A1c"),
                ("Phenotypic trait type", "Quantitative Trait"),
                ("Sample size", "Total Sample Size=338640"),
            ]
        )
        info = parse_metadata(path)
        self.assertEqual(info["Title of analysis"], "A1C_Min_INT.EUR.GIA")
        self.assertEqual(info["Sample size"], "Total Sample Size=338640")

    def test_a_table_without_the_info_header_is_refused(self) -> None:
        path = self.root / "bad.metadata.txt"
        path.write_text("Key\tValue\nTitle\tsomething\n", encoding="utf-8")
        with self.assertRaisesRegex(DbgapError, "not an Info/Description metadata table"):
            parse_metadata(path)

    def test_sample_size_reads_total_and_optional_case_counts(self) -> None:
        path = self.metadata([])
        self.assertEqual(
            parse_sample_size("Total Sample Size=438595; Cases=5583; Controls=433012", path),
            (438595, 5583, 433012),
        )
        # A count the deposit does not record is absence, never a zero (#290 review).
        self.assertEqual(parse_sample_size("Total Sample Size=338640", path), (338640, None, None))

    def test_sample_size_that_cannot_be_read_is_refused(self) -> None:
        path = self.metadata([])
        with self.assertRaisesRegex(DbgapError, "cannot parse sample size"):
            parse_sample_size("338640 participants", path)


if __name__ == "__main__":
    unittest.main()
