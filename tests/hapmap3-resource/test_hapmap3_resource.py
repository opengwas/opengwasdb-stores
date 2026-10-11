#!/usr/bin/env python3
"""Tests for the maintained HapMap3 GRCh38 Reference Resource (#208).

Covers the three acceptance criteria of opengwas/opengwasdb-stores#208:

1. the declaration records provenance, reference assembly, variant identity
   convention, row count, source checksum and derived ALID checksum;
2. the maintained list is the benchmarked 1,206,903-row list, identified by the
   declared byte checksum (verified in place when the reference root is present,
   skipped when it is not -- CI has no large data);
3. verification refuses a missing, malformed, duplicate, non-canonical,
   wrong-length or checksum-mismatched list, and a source or reconciliation
   count that does not match the declaration.

The fixtures are tiny and hermetic; the only environment-dependent test is the
in-place check of the real list, which skips when the reference root is absent.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOURCE_DIR = REPO_ROOT / "resources" / "reference-resources" / "hapmap3-hg38"
if str(RESOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(RESOURCE_DIR))

import verify_hm3_resource as verifier  # noqa: E402

RESOURCE_YAML = RESOURCE_DIR / "resource.yaml"
PROVENANCE_JSON = RESOURCE_DIR / "provenance.json"

BENCHMARKED_ROW_COUNT = 1206903
BENCHMARKED_SHA256 = "4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398"
SOURCE_MD5 = "153ecc2bcfa740afafe656e6a384d769"

# A GRCh38 alt-contig ALID from the real list: canonical, and deliberately not
# an autosome, so a regex that only allowed 1-22 would fail it.
ALT_CONTIG_ALID = "8_KI270821v1_alt:559250:G:T"


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def md5_bytes(raw: bytes) -> str:
    return hashlib.md5(raw).hexdigest()


def write_yaml(directory: Path, alid_path: Path, raw: bytes, **overrides) -> Path:
    document = {
        "resource_id": "hapmap3-hg38",
        "kind": "variant_list",
        "genome_build": "GRCh38",
        "location": str(alid_path),
        "row_count": 3,
        "checksums": {
            "alid_sha256": sha256_bytes(raw),
            "alid_bytes": len(raw),
        },
        "source": {},
        "derivation": {
            "rsids_requested": 5,
            "resolved": 3,
            "absent_from_store_axis": 2,
            "allele_incompatible": 0,
            "duplicate_alids": 0,
        },
    }
    document.update(overrides)
    path = directory / "resource.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


class TestDeclarationIntegrity(unittest.TestCase):
    """Acceptance criterion 1: the declaration records what it must."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(RESOURCE_YAML.read_text(encoding="utf-8"))

    def test_declares_provenance_and_identity(self) -> None:
        document = self.document
        self.assertEqual(document["resource_id"], "hapmap3-hg38")
        self.assertEqual(document["kind"], "variant_list")
        self.assertEqual(document["genome_build"], "GRCh38")
        self.assertEqual(document["location_kind"], "external_file")
        # Large data lives outside Git; never a repo-relative tracked file.
        self.assertTrue(document["location"].startswith("/data/opengwasdb/reference/"))
        self.assertIn("chr:pos:a1:a2", document["variant_id_convention"])

    def test_records_row_count_and_checksums(self) -> None:
        document = self.document
        self.assertEqual(document["row_count"], BENCHMARKED_ROW_COUNT)
        checksums = document["checksums"]
        self.assertEqual(checksums["alid_sha256"], BENCHMARKED_SHA256)
        self.assertEqual(len(checksums["alid_sha256"]), 64)
        self.assertEqual(checksums["source_md5"], SOURCE_MD5)
        self.assertEqual(len(checksums["source_sha256"]), 64)
        self.assertEqual(checksums["alid_bytes"], 18892749)

    def test_source_is_the_publisher_verified_ldsc_list(self) -> None:
        source = self.document["source"]
        self.assertEqual(source["file"], "w_hm3.snplist.gz")
        self.assertIn("zenodo.org", source["url"])
        self.assertEqual(source["rsid_rows"], 1217311)
        self.assertEqual(source["columns"], ["SNP", "A1", "A2"])

    def test_reconciliation_balances(self) -> None:
        derivation = verifier.verify_reconciliation(
            self.document, row_count=BENCHMARKED_ROW_COUNT
        )
        self.assertEqual(derivation["resolved"], BENCHMARKED_ROW_COUNT)
        self.assertEqual(
            derivation["rsids_requested"]
            - derivation["absent_from_store_axis"]
            - derivation["allele_incompatible"],
            BENCHMARKED_ROW_COUNT,
        )

    def test_derivation_cites_the_resolution_script_not_the_consumer(self) -> None:
        # `build-indexed-subset --variant-list` consumes an already-canonical
        # list; the rsID -> ALID allele-safe resolution is the sibling
        # benchmark script's job (stores#208 review correction).
        derivation = self.document["derivation"]
        self.assertIn("benchmark_indexed_subset.py", derivation["resolution_script"])
        self.assertEqual(derivation["resolution_revision"], "a36ea78")
        self.assertEqual(
            derivation["resolution_functions"],
            ["read_hapmap3", "resolve_hapmap3", "write_alid_list"],
        )
        self.assertIn("benchmark_indexed_subset.py", derivation["method"])
        self.assertIn("does not derive ALIDs from rsIDs", derivation["method"])

    def test_provenance_json_matches_declaration(self) -> None:
        provenance = json.loads(PROVENANCE_JSON.read_text(encoding="utf-8"))
        self.assertEqual(provenance["resource_id"], self.document["resource_id"])
        self.assertEqual(
            provenance["alid_file"]["sha256"],
            self.document["checksums"]["alid_sha256"],
        )
        self.assertEqual(
            provenance["alid_file"]["row_count"], self.document["row_count"]
        )
        self.assertEqual(
            provenance["source"]["md5"], self.document["checksums"]["source_md5"]
        )
        self.assertEqual(
            provenance["derivation"]["resolved"],
            self.document["derivation"]["resolved"],
        )
        self.assertEqual(
            provenance["derivation"]["resolution_revision"],
            self.document["derivation"]["resolution_revision"],
        )
        self.assertIn("benchmark_indexed_subset.py", provenance["derivation"]["method"])


class TestCanonicalAlidRule(unittest.TestCase):
    """The verifier reads the same canonical-ALID rule the consumer does."""

    def test_accepts_canonical_alids(self) -> None:
        for alid in ("1:817186:A:G", "22:1:C:T", "X:100:A:C", "MT:1:A:G", ALT_CONTIG_ALID):
            with self.subTest(alid=alid):
                self.assertEqual(verifier.canonical_alid_or_none(alid), alid)

    def test_refuses_noncanonical_spellings(self) -> None:
        for alid in (
            "chr1:817186:A:G",       # chr prefix
            "23:817186:A:G",         # numeric sex-chromosome alias
            "1:817186:a:g",          # lower-case alleles
            "1:817186:G:A",          # unsorted allele pair
            "1:0:A:G",               # non-positive position
            "1:817186:A",            # too few fields
            "1:817186:A:G:T",        # too many fields
            "1:817186:A:N",          # non-ACGT allele
            "not-an-alid",
        ):
            with self.subTest(alid=alid):
                self.assertIsNone(verifier.canonical_alid_or_none(alid))

    def test_identical_alleles_match_the_consumer_grammar(self) -> None:
        # The authoritative parse accepts an identical allele pair, so the
        # verifier must not be stricter than the reader it mirrors (which would
        # refuse a list the build would accept). The real list contains none.
        self.assertEqual(verifier.canonical_alid_or_none("1:817186:A:A"), "1:817186:A:A")

    def test_matches_the_future_variant_list_reader_when_available(self) -> None:
        # Forward guard for stores#209: once the pin advances, the verifier's
        # round-trip must agree with the authoritative `read_variant_list`.
        try:
            from opengwasdb.layouts.dense.indexed_subsets import read_variant_list
        except ImportError:
            self.skipTest("pinned opengwasdb predates build-indexed-subset (#264)")

        candidates = ["1:1:A:G", "chr1:1:A:G", "1:1:G:A", ALT_CONTIG_ALID, "23:1:A:G"]
        with tempfile.TemporaryDirectory() as tmp:
            for candidate in candidates:
                path = Path(tmp) / "list.txt"
                path.write_text(candidate + "\n", encoding="utf-8")
                try:
                    read_variant_list(path)
                    consumer_ok = True
                except Exception:  # noqa: BLE001 - the consumer's rejection type
                    consumer_ok = False
                with self.subTest(candidate=candidate):
                    self.assertEqual(consumer_ok, verifier.canonical_alid_or_none(candidate) is not None)


class TestAlidFileVerification(unittest.TestCase):
    """Acceptance criterion 3: every refusal class is loud and named."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hm3_resource_")
        self.tmp = Path(self._tmp.name)
        self.alid_path = self.tmp / "hm3.grch38.alid.txt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, text: str) -> bytes:
        raw = text.encode("utf-8")
        self.alid_path.write_bytes(raw)
        return raw

    def verify(self, raw: bytes, **overrides):
        return verifier.verify_alid_file(
            self.alid_path,
            expected_sha256=overrides.pop("sha256", sha256_bytes(raw)),
            expected_bytes=overrides.pop("bytes", len(raw)),
            expected_rows=overrides.pop("rows", 3),
        )

    def test_accepts_a_canonical_alt_contig_list(self) -> None:
        raw = self.write(f"1:817186:A:G\n1:817341:A:G\n{ALT_CONTIG_ALID}\n")
        self.assertEqual(self.verify(raw), 3)
        self.assertEqual(verifier.canonical_alid_or_none(ALT_CONTIG_ALID), ALT_CONTIG_ALID)

    def test_missing_file_is_refused(self) -> None:
        with self.assertRaises(verifier.VerificationError) as ctx:
            verifier.verify_alid_file(
                self.tmp / "absent.txt", expected_sha256=None, expected_bytes=None, expected_rows=None
            )
        self.assertIn("not found", str(ctx.exception))

    def test_malformed_line_is_refused_with_its_number(self) -> None:
        raw = self.write("1:817186:A:G\nnot-an-alid\n1:817341:A:G\n")
        with self.assertRaises(verifier.VerificationError) as ctx:
            self.verify(raw)
        self.assertIn(":2:", str(ctx.exception))
        self.assertIn("not a canonical ALID", str(ctx.exception))

    def test_duplicate_is_refused(self) -> None:
        raw = self.write("1:817186:A:G\n1:817186:A:G\n1:817341:A:G\n")
        with self.assertRaises(verifier.VerificationError) as ctx:
            self.verify(raw)
        self.assertIn("duplicate ALID", str(ctx.exception))

    def test_noncanonical_line_is_refused(self) -> None:
        raw = self.write("1:817186:A:G\nchr1:817341:A:G\n1:818802:C:T\n")
        with self.assertRaises(verifier.VerificationError) as ctx:
            self.verify(raw)
        self.assertIn("not a canonical ALID", str(ctx.exception))

    def test_checksum_mismatch_is_refused(self) -> None:
        raw = self.write("1:817186:A:G\n1:817341:A:G\n1:818802:C:T\n")
        with self.assertRaises(verifier.VerificationError) as ctx:
            self.verify(raw, sha256="0" * 64)
        self.assertIn("SHA-256 mismatch", str(ctx.exception))

    def test_byte_count_mismatch_is_refused(self) -> None:
        raw = self.write("1:817186:A:G\n1:817341:A:G\n1:818802:C:T\n")
        with self.assertRaises(verifier.VerificationError) as ctx:
            self.verify(raw, bytes=len(raw) + 1)
        self.assertIn("byte size mismatch", str(ctx.exception))

    def test_row_count_mismatch_is_refused(self) -> None:
        raw = self.write("1:817186:A:G\n1:817341:A:G\n1:818802:C:T\n")
        with self.assertRaises(verifier.VerificationError) as ctx:
            self.verify(raw, rows=2)
        self.assertIn("row count mismatch", str(ctx.exception))

    def test_invalid_utf8_is_refused(self) -> None:
        (self.alid_path).write_bytes(b"1:817186:A:G\n\xff\xfe\n")
        with self.assertRaises(verifier.VerificationError) as ctx:
            verifier.verify_alid_file(
                self.alid_path, expected_sha256=None, expected_bytes=None, expected_rows=None
            )
        self.assertIn("UTF-8", str(ctx.exception))


class TestSourceAndReconciliation(unittest.TestCase):
    """The declared source checksum and the rsID balance are gated too."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hm3_source_")
        self.tmp = Path(self._tmp.name)
        self.source_path = self.tmp / "w_hm3.snplist.gz"
        with gzip.open(self.source_path, "wt", encoding="utf-8") as handle:
            handle.write("SNP\tA1\tA2\nrs1\tG\tA\nrs2\tA\tG\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_source_checksums_and_row_count(self) -> None:
        raw = self.source_path.read_bytes()
        rows = verifier.verify_source(
            self.source_path,
            expected_md5=md5_bytes(raw),
            expected_sha256=sha256_bytes(raw),
            expected_bytes=len(raw),
            expected_rsid_rows=2,
        )
        self.assertEqual(rows, 2)

    def test_source_md5_mismatch_is_refused(self) -> None:
        with self.assertRaises(verifier.VerificationError) as ctx:
            verifier.verify_source(
                self.source_path,
                expected_md5="0" * 32,
                expected_sha256=None,
                expected_bytes=None,
                expected_rsid_rows=None,
            )
        self.assertIn("MD5 mismatch", str(ctx.exception))

    def test_source_rsid_count_mismatch_is_refused(self) -> None:
        with self.assertRaises(verifier.VerificationError) as ctx:
            verifier.verify_source(
                self.source_path,
                expected_md5=None,
                expected_sha256=None,
                expected_bytes=None,
                expected_rsid_rows=99,
            )
        self.assertIn("rsID row count mismatch", str(ctx.exception))

    def test_source_header_is_checked(self) -> None:
        (self.tmp / "bad.gz").write_bytes(gzip.compress(b"not a header\nrs1\tG\tA\n"))
        with self.assertRaises(verifier.VerificationError) as ctx:
            verifier.verify_source(
                self.tmp / "bad.gz",
                expected_md5=None,
                expected_sha256=None,
                expected_bytes=None,
                expected_rsid_rows=None,
            )
        self.assertIn("header", str(ctx.exception))

    def test_unbalanced_reconciliation_is_refused(self) -> None:
        document = {
            "derivation": {
                "rsids_requested": 10,
                "resolved": 3,
                "absent_from_store_axis": 2,
                "allele_incompatible": 0,
                "duplicate_alids": 0,
            }
        }
        with self.assertRaises(verifier.VerificationError) as ctx:
            verifier.verify_reconciliation(document, row_count=3)
        self.assertIn("does not balance", str(ctx.exception))

    def test_resolved_row_count_mismatch_is_refused(self) -> None:
        document = {
            "derivation": {
                "rsids_requested": 5,
                "resolved": 3,
                "absent_from_store_axis": 2,
                "allele_incompatible": 0,
                "duplicate_alids": 0,
            }
        }
        with self.assertRaises(verifier.VerificationError) as ctx:
            verifier.verify_reconciliation(document, row_count=4)
        self.assertIn("rows", str(ctx.exception))


class TestCommandLine(unittest.TestCase):
    """`main` exits 0 on a matching fixture and 2 on every refusal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hm3_cli_")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_matching_fixture_exits_zero(self) -> None:
        alid_path = self.tmp / "hm3.grch38.alid.txt"
        raw = b"1:817186:A:G\n1:817341:A:G\n1:818802:C:T\n"
        alid_path.write_bytes(raw)
        resource = write_yaml(self.tmp, alid_path, raw)
        self.assertEqual(verifier.main(["--resource", str(resource), "--quiet"]), 0)

    def test_checksum_mismatch_exits_two(self) -> None:
        alid_path = self.tmp / "hm3.grch38.alid.txt"
        raw = b"1:817186:A:G\n1:817341:A:G\n1:818802:C:T\n"
        alid_path.write_bytes(raw)
        resource = write_yaml(
            self.tmp, alid_path, raw, checksums={"alid_sha256": "0" * 64, "alid_bytes": len(raw)}
        )
        self.assertEqual(verifier.main(["--resource", str(resource), "--quiet"]), 2)

    def test_missing_declaration_exits_two(self) -> None:
        self.assertEqual(verifier.main(["--resource", str(self.tmp / "nope.yaml"), "--quiet"]), 2)


class TestMaintainedResourceInPlace(unittest.TestCase):
    """Acceptance criterion 2, on hosts that hold the reference root."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(RESOURCE_YAML.read_text(encoding="utf-8"))
        cls.alid_path = Path(cls.document["location"])

    @unittest.skipUnless(
        Path("/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt").is_file(),
        "reference root not present (CI has no large data)",
    )
    def test_maintained_list_matches_the_benchmarked_bytes(self) -> None:
        rows = verifier.verify_alid_file(
            self.alid_path,
            expected_sha256=BENCHMARKED_SHA256,
            expected_bytes=self.document["checksums"]["alid_bytes"],
            expected_rows=BENCHMARKED_ROW_COUNT,
        )
        self.assertEqual(rows, BENCHMARKED_ROW_COUNT)
        verifier.verify_reconciliation(self.document, row_count=rows)

    def test_declared_location_is_the_maintained_reference_root(self) -> None:
        self.assertEqual(
            str(self.alid_path),
            "/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt",
        )


def main() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("hapmap3-resource tests passed")


if __name__ == "__main__":
    main()
