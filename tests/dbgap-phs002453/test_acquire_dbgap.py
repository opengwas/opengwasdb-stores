#!/usr/bin/env python3
"""Tests for dbGaP phs002453 (PMID 39024449) acquisition (``acquire-dbgap-phs002453.py``).

Covers the contract the acquisition script implements:

1. ``extract`` streams a verified tar once, verifies member sizes against the
   table of contents, refuses unverified tars, and is idempotent and atomic;
2. ``map`` binds every Analysis to one candidate row, identifies META
   Analyses by ``ancestry_fraction < 1`` rather than by their ancestry label,
   and reports ambiguous/unmatched rows instead of guessing;
3. ``convert`` reproduces EBI's raw GWAS-SSF bytes cell-for-cell for the two
   golden rows in the contract, keeps a shape's column set (the META files
   without ``r2``), fails loudly on an unrecognised header, and is idempotent;
4. ``manifest`` writes EBI's raw-pass manifest columns with ``status=ok`` and a
   sha256 of the data file, and the result is accepted by
   ``resources/generators/lib/source_inventory.py``.

The fixture tars and members are synthetic but use the real member names, the
real metadata table shape and the real member headers.

Run from the repository root:  python3 tests/dbgap-phs002453/test_acquire_dbgap.py
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import importlib.util
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "resources/scripts/acquire-dbgap-phs002453.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("acquire_dbgap_phs002453", SCRIPT)
assert _spec and _spec.loader
ACQUIRE = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules, so register before exec.
sys.modules[_spec.name] = ACQUIRE
_spec.loader.exec_module(ACQUIRE)

from resources.generators.lib.source_inventory import (  # noqa: E402
    ACQUISITION_MANIFEST_COLUMNS,
    AcquisitionPass,
    build_snapshot,
    read_acquisition_manifest,
    read_candidate_selection,
    write_snapshot,
)

TAR = "phs002453.MVP_R4.1000G_AGR.GIA.Fixture.analysis-PI.MULTI.tar"
INNER = "MVP_R4.1000G_AGR.GIA.Fixture"
STORE_KEY = "dense__pmid-39024449__European"
META_STORE_KEY = "dense__pmid-39024449__Multi-ancestry"

CANDIDATE_COLUMNS = (
    "STUDY.ACCESSION", "PUBMED.ID", "FIRST.AUTHOR", "STUDY", "DISEASE.TRAIT", "MAPPED_TRAIT",
    "ancestry_group", "ancestry_fraction", "is_molecular", "molecular_subtype", "store_type",
    "store_key", "molecular_type", "study_design", "n_cases", "n_controls", "sample_size",
    "n_variants", "association_count", "MAPPED_TRAIT_URI",
)

QUANT_HEADER = "SNP_ID\tchrom\tpos\tref\talt\tea\taf\tnum_samples\tbeta\tsebeta\tpval\tr2\tq_pval\ti2\tdirection"
QUANT_ROWS = [
    # the contract's golden quantitative source row
    "rs558358031\t1\t60879\tG\tA\tA\t0.9994\t338640\t0.09216\t0.0986\t0.35\t0.331\tNA\tNA\tNA",
    # effect allele equals alt
    "rs62839324\t2\t123\tC\tT\tT\t0.5\t338640\t0.01\t0.02\t0.9\t0.4\tNA\tNA\tNA",
    # effect allele equals ref: other_allele must be alt
    "rs999\t2\t456\tA\tT\tA\t0.25\t338640\t-0.5\t0.1\t1e-09\t0.2\tNA\tNA\tNA",
    # missing trailing fields (NF is short) stay empty -> #NA
    "rs1000\t3\t789\tA\tC\tC\t1\t338640\t0\t0.1\t0.5\t0.3\tNA\tNA",
    # a position the deposit writes in scientific notation, and p_value exactly 1
    "rs3000\t4\t2.4e+07\tG\tT\tT\t0.1\t338640\t0.05\t0.2\t1\t0.1\tNA\tNA\tNA",
    # p_value in scientific notation
    "rs3001\t5\t123\tA\tC\tC\t0.2\t338640\t0.1\t0.2\t6e-04\t0.1\tNA\tNA\tNA",
    # a position in scientific notation that is not an integer
    "rs3002\t6\t1.5e+00\tA\tC\tC\t0.2\t338640\t0.1\t0.2\t0.5\t0.1\tNA\tNA\tNA",
]
QUANT_OUT_HEADER = (
    "chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta\tstandard_error\t"
    "effect_allele_frequency\tp_value\trsid\talt\tn\tr2\tq_pval\ti2\tdirection"
)
GOLDEN_QUANT_ROW = "1\t60879\tA\tG\t0.09216\t0.0986\t0.9994\t0.35\trs558358031\tA\t338640\t0.331\t#NA\t#NA\t#NA"

BINARY_HEADER = (
    "SNP_ID\tchrom\tpos\tref\talt\tea\taf\tnum_samples\tcase_af\tnum_cases\tcontrol_af\t"
    "num_controls\tor\tci\tpval\tr2\tq_pval\ti2\tdirection"
)
BINARY_ROWS = [
    # the contract's golden binary source row
    "rs558358031\t1\t60879\tG\tA\tA\t0.9994\t438595\t0.9995\t5583\t0.9994\t433012\t1.388\t0.3529,5.459\t0.6388\t0.331\tNA\tNA\tNA",
    # a missing confidence interval is #NA on both sides, never a bare NA
    "rs2000\t1\t100\tA\tG\tG\t0.1\t438595\t0.2\t5583\t0.3\t433012\t1.5\tNA\t0.5\t0.1\tNA\tNA\tNA",
]
BINARY_OUT_HEADER = (
    "chromosome\tbase_pair_location\teffect_allele\tother_allele\todds_ratio\tstandard_error\t"
    "effect_allele_frequency\tp_value\trsid\tci_upper\tci_lower\talt\tn\tcase_af\tnum_cases\t"
    "control_af\tnum_controls\tr2\tq_pval\ti2\tdirection"
)
GOLDEN_BINARY_ROW = (
    "1\t60879\tA\tG\t1.388\t#NA\t0.9994\t0.6388\trs558358031\t5.459\t0.3529\tA\t438595\t"
    "0.9995\t5583\t0.9994\t433012\t0.331\t#NA\t#NA\t#NA"
)

#: A real META member shape: no ``r2`` column, populated ``q_pval``/``i2`` and a
#: ``direction`` string. EBI's raw file for the same shape drops ``r2`` too.
META_HEADER = "SNP_ID\tchrom\tpos\tref\talt\tea\taf\tnum_samples\tbeta\tsebeta\tpval\tq_pval\ti2\tdirection"
META_ROWS = ["rs565824523\t1\t48327\tA\tC\tC\t0.9947\t163607\t-0.0406\t0.03671\t0.2688\t0.7613\t0\t--??"]
META_OUT_HEADER = (
    "chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta\tstandard_error\t"
    "effect_allele_frequency\tp_value\trsid\talt\tn\tq_pval\ti2\tdirection"
)
GOLDEN_META_ROW = "1\t48327\tC\tA\t-0.0406\t0.03671\t0.9947\t0.2688\trs565824523\tC\t163607\t0.7613\t0\t--??"

BOGUS_HEADER = QUANT_HEADER + "\tmystery"

MEMBERS = {
    # member name -> (header, rows, metadata rows)
    "A1C_Min_INT.EUR": (
        QUANT_HEADER, QUANT_ROWS,
        {"Title of analysis": "A1C_Min_INT.EUR.GIA",
         "Analysis description": "GWAS of A1C_Min_INT (hemoglobin A1c (HbA1c, minimum, inv-norm transformed)) in European American MVP participants as defined by GIA.",
         "Sample population": "European American Veterans from MVP (GIA)",
         "Sample size": "Total Sample Size=338640",
         "Analyzed variable": "hemoglobin A1c (HbA1c, minimum, inv-norm transformed)",
         "Phenotypic trait type": "Quantitative Trait"},
    ),
    "Phe_394.EUR": (
        BINARY_HEADER, BINARY_ROWS,
        {"Title of analysis": "Phe_394.EUR.GIA",
         "Analysis description": "GWAS of Phe_394 (Rheumatic disease of the heart valves) in European American MVP participants as defined by GIA.",
         "Sample population": "European American Veterans from MVP (GIA)",
         "Sample size": "Total Sample Size=438595; Cases=5583; Controls=433012",
         "Analyzed variable": "Rheumatic disease of the heart valves",
         "Phenotypic trait type": "Binary Trait"},
    ),
    "Albumin_Mean_INT.META": (
        META_HEADER, META_ROWS,
        {"Title of analysis": "Albumin_Mean_INT.META.GIA",
         "Analysis description": "Meta-analysis of Albumin_Mean_INT (albumin (mean, inv-norm transformed)) in AFR, AMR, EAS, and EUR MVP participants as defined by GIA.",
         "Sample population": "Veterans of African, East Asian, European, and Hispanic/Latino genetic ancestry from MVP (GIA)",
         "Sample size": "Total Sample Size=542276",
         "Analyzed variable": "albumin (mean, inv-norm transformed)",
         "Phenotypic trait type": "Quantitative Trait"},
    ),
    "Bogus_INT.EUR": (
        BOGUS_HEADER, [QUANT_ROWS[0]],
        {"Title of analysis": "Bogus_INT.EUR.GIA",
         "Analysis description": "GWAS of a trait whose member header is not recognised.",
         "Sample population": "European American Veterans from MVP (GIA)",
         "Sample size": "Total Sample Size=12345",
         "Analyzed variable": "Bogus trait",
         "Phenotypic trait type": "Quantitative Trait"},
    ),
}

#: ``analysis_id -> (member stem, candidate row)``; the candidate rows mirror the
#: real table's columns.  The META row carries ``ancestry_group=European`` with
#: ``ancestry_fraction < 1``, the state the candidate table is in until a
#: companion change relabels it -- acquisition must map it to META regardless.
CANDIDATES: list[dict[str, str]] = [
    {"STUDY.ACCESSION": "GCST90475097", "PUBMED.ID": "39024449", "FIRST.AUTHOR": "Verma A",
     "STUDY": "Diversity and scale", "DISEASE.TRAIT": "Hemoglobin A1c (HbA1c, minimum, inv-norm transformed)",
     "MAPPED_TRAIT": "HbA1c measurement", "ancestry_group": "European", "ancestry_fraction": "1",
     "is_molecular": "FALSE", "molecular_subtype": "", "store_type": "dense", "store_key": STORE_KEY,
     "molecular_type": "", "study_design": "quantitative", "n_cases": "0", "n_controls": "0",
     "sample_size": "338640", "n_variants": "14903227", "association_count": "14903227",
     "MAPPED_TRAIT_URI": "EFO_0004530"},
    {"STUDY.ACCESSION": "GCST90477833", "PUBMED.ID": "39024449", "FIRST.AUTHOR": "Verma A",
     "STUDY": "Diversity and scale", "DISEASE.TRAIT": "Rheumatic disease of the heart valves (PheCode 394)",
     "MAPPED_TRAIT": "rheumatic heart disease", "ancestry_group": "European", "ancestry_fraction": "1",
     "is_molecular": "FALSE", "molecular_subtype": "", "store_type": "dense", "store_key": STORE_KEY,
     "molecular_type": "", "study_design": "case-control", "n_cases": "5583", "n_controls": "433012",
     "sample_size": "438595", "n_variants": "14903227", "association_count": "14903227",
     "MAPPED_TRAIT_URI": "MONDO_0005400"},
    {"STUDY.ACCESSION": "GCST90479504", "PUBMED.ID": "39024449", "FIRST.AUTHOR": "Verma A",
     "STUDY": "Diversity and scale", "DISEASE.TRAIT": "Albumin (mean, inv-norm transformed)",
     "MAPPED_TRAIT": "serum albumin amount", "ancestry_group": "European",
     "ancestry_fraction": "0.687771540691456", "is_molecular": "FALSE", "molecular_subtype": "",
     "store_type": "dense", "store_key": META_STORE_KEY, "molecular_type": "",
     "study_design": "quantitative", "n_cases": "0", "n_controls": "0", "sample_size": "542276",
     "n_variants": "14903227", "association_count": "14903227", "MAPPED_TRAIT_URI": "EFO_0004531"},
    {"STUDY.ACCESSION": "GCST90479999", "PUBMED.ID": "39024449", "FIRST.AUTHOR": "Verma A",
     "STUDY": "Diversity and scale", "DISEASE.TRAIT": "Bogus trait", "MAPPED_TRAIT": "bogus trait",
     "ancestry_group": "European", "ancestry_fraction": "1", "is_molecular": "FALSE",
     "molecular_subtype": "", "store_type": "dense", "store_key": STORE_KEY, "molecular_type": "",
     "study_design": "quantitative", "n_cases": "0", "n_controls": "0", "sample_size": "12345",
     "n_variants": "10", "association_count": "10", "MAPPED_TRAIT_URI": ""},
]


def gzip_bytes(text: str) -> bytes:
    import io

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(text.encode("utf-8"))
    return buffer.getvalue()


def metadata_text(info: dict[str, str]) -> str:
    lines = ["Info\tDescription"]
    lines += [f"{key}\t{value}" for key, value in info.items()]
    return "\n".join(lines) + "\n"


def write_candidates(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CANDIDATE_COLUMNS), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in CANDIDATE_COLUMNS})


def build_fixture(root: Path, verified: bool = True, candidates: list[dict[str, str]] | None = None) -> Path:
    """Create a synthetic acquisition root with one real-format tar."""
    tars_dir = root / "GIA" / "tars"
    meta_dir = root / "GIA" / "meta"
    tars_dir.mkdir(parents=True)
    meta_dir.mkdir(parents=True)

    payload: dict[str, bytes] = {}
    for stem, (header, rows, info) in MEMBERS.items():
        payload[f"{INNER}/MVP_R4.1000G_AGR.{stem}.GIA.dbGaP.txt.gz"] = gzip_bytes(header + "\n" + "\n".join(rows) + "\n")
        payload[f"{INNER}/MVP_R4.1000G_AGR.{stem}.GIA.dbGaP.metadata.txt"] = metadata_text(info).encode("utf-8")

    tar_path = tars_dir / TAR
    with tarfile.open(tar_path, "w") as handle:
        for name, data in payload.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            import io

            handle.addfile(info, io.BytesIO(data))

    listing = [f"drwxrwsr-x huffmanj/med112 0 2023-08-22 11:32 {INNER}/\n"]
    for name, data in payload.items():
        listing.append(f"-rw-rw-r-- huffmanj/med112 {len(data)} 2023-08-22 11:27 {name}\n")
    (meta_dir / f"{TAR}.table_of_contents.txt").write_text("".join(listing), encoding="utf-8")
    (meta_dir / "tars.txt").write_text(TAR + "\n", encoding="utf-8")

    digest = hashlib.md5(tar_path.read_bytes()).hexdigest()
    (tars_dir / f"{TAR}.md5").write_text(f"{digest}  {TAR}\n", encoding="utf-8")
    if verified:
        (tars_dir / f"{TAR}.verified").write_text(digest + "\n", encoding="utf-8")

    candidate_path = root / "candidates.tsv"
    write_candidates(candidate_path, CANDIDATES if candidates is None else candidates)
    return candidate_path


def run(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(root), *argv],
        capture_output=True, text=True, check=False,
    )


def decompress(path: Path) -> str:
    """Decompressed text with its line terminators intact (EBI's files are CRLF)."""
    with gzip.open(path, "rb") as handle:
        return handle.read().decode("utf-8")


class AcquisitionTest(unittest.TestCase):
    root: Path
    candidates: Path
    tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="dbgap-acquisition-")
        cls.root = Path(cls.tmp.name) / "root"
        cls.candidates = build_fixture(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def member(self, stem: str, suffix: str) -> Path:
        return self.root / "GIA" / "extracted" / INNER / f"MVP_R4.1000G_AGR.{stem}.GIA.dbGaP.{suffix}"

    def data(self, accession: str) -> Path:
        return self.data_from(self.root, accession)

    def data_from(self, root: Path, accession: str) -> Path:
        bucket = root / "gwas-ssf"
        found = list(bucket.glob(f"*/{accession}/{accession}.tsv.gz"))
        self.assertEqual(len(found), 1, f"expected one data file for {accession}, found {found}")
        return found[0]

    # -- extract ----------------------------------------------------------

    def test_01_extract_streams_and_verifies(self) -> None:
        result = run(self.root, "extract")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for stem in MEMBERS:
            for suffix in ("txt.gz", "metadata.txt"):
                path = self.member(stem, suffix)
                self.assertTrue(path.is_file(), f"missing {path}")
                self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(list(self.root.glob("GIA/extracted/**/*.partial")), [])
        self.assertIn("extracted=8", result.stdout)

    def test_02_extract_is_idempotent(self) -> None:
        target = self.member("A1C_Min_INT.EUR", "txt.gz")
        before = target.stat().st_mtime_ns
        result = run(self.root, "extract")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(target.stat().st_mtime_ns, before, "a complete member must not be rewritten")
        self.assertIn("skipped=8", result.stdout)

        # A member whose size disagrees with the table of contents is re-extracted.
        target.write_bytes(b"truncated")
        result = run(self.root, "extract")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("extracted=1", result.stdout)
        self.assertGreater(target.stat().st_size, len(b"truncated"))

    def test_03_extract_refuses_an_unverified_tar(self) -> None:
        unverified = Path(self.tmp.name) / "unverified"
        build_fixture(unverified, verified=False)
        result = run(unverified, "extract")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no MD5-verified tar", result.stderr)
        self.assertFalse((unverified / "GIA" / "extracted").exists())

    # -- map --------------------------------------------------------------

    def test_04_map_binds_every_analysis(self) -> None:
        result = run(self.root, "map", "--candidates", str(self.candidates), "--allow-partial")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        mapping = {row["analysis_id"]: row for row in self.rows(self.root / "mapping.tsv")}
        self.assertEqual(
            set(mapping),
            {"GCST90475097", "GCST90477833", "GCST90479504", "GCST90479999"},
        )
        self.assertEqual(mapping["GCST90475097"]["ancestry"], "EUR")
        self.assertEqual(mapping["GCST90475097"]["match_basis"], "unique-key")
        self.assertEqual(mapping["GCST90475097"]["trait_check"], "exact")
        self.assertEqual(mapping["GCST90477833"]["study_design"], "case-control")
        # META is decided by ancestry_fraction < 1, not by the ancestry label.
        self.assertEqual(mapping["GCST90479504"]["ancestry"], "META")
        self.assertEqual(mapping["GCST90479504"]["member"], f"{INNER}/MVP_R4.1000G_AGR.Albumin_Mean_INT.META.GIA.dbGaP.txt.gz")
        self.assertEqual(self.rows(self.root / "mapping-report.tsv"), [])

    def test_05_map_uses_a_uniquely_named_data_member_from_another_toc(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dbgap-cross-tar-") as tmp:
            root = Path(tmp) / "root"
            candidates = build_fixture(root)
            result = run(root, "extract")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

            original = (
                root / "GIA" / "extracted" / INNER /
                "MVP_R4.1000G_AGR.A1C_Min_INT.EUR.GIA.dbGaP.txt.gz"
            )
            other_inner = "MVP_R4.1000G_AGR.GIA.Other"
            moved = original.parents[1] / other_inner / original.name
            moved.parent.mkdir(parents=True)
            original.replace(moved)

            original_member = f"{INNER}/{original.name}"
            moved_member = f"{other_inner}/{original.name}"
            toc = root / "GIA" / "meta" / f"{TAR}.table_of_contents.txt"
            toc_text = toc.read_text(encoding="utf-8")
            kept = [line for line in toc_text.splitlines() if original_member not in line]
            toc.write_text("\n".join(kept) + "\n", encoding="utf-8")
            other_tar = "phs002453.MVP_R4.1000G_AGR.GIA.Other.analysis-PI.MULTI.tar"
            (root / "GIA" / "meta" / f"{other_tar}.table_of_contents.txt").write_text(
                f"-rw-rw-r-- huffmanj/med112 {moved.stat().st_size} 2023-08-22 11:27 {moved_member}\n",
                encoding="utf-8",
            )

            result = run(root, "map", "--candidates", str(candidates), "--allow-partial")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            mapping = {row["analysis_id"]: row for row in self.rows(root / "mapping.tsv")}
            self.assertEqual(mapping["GCST90475097"]["member"], moved_member)
            self.assertEqual(mapping["GCST90475097"]["tar"], other_tar)

            result = run(root, "convert", "--candidates", str(candidates), "--analysis", "GCST90475097")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            import yaml

            metadata_yaml = self.data_from(root, "GCST90475097").with_name("GCST90475097.tsv.gz-meta.yaml")
            document = yaml.safe_load(metadata_yaml.read_text(encoding="utf-8"))
            self.assertEqual(document["source"]["member"], moved_member)
            self.assertIn("GWAS of A1C_Min_INT", document["dbgap_analysis_description"])

    def test_06_map_requires_a_complete_bijection(self) -> None:
        result = run(self.root, "map", "--candidates", str(self.candidates))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not a complete bijection", result.stderr)

    def test_07_map_reports_ambiguous_and_unmatched(self) -> None:
        # A second candidate row with the same (ancestry, N, cases, controls)
        # and the same trait text cannot be told apart from the first.
        ambiguous = [dict(row) for row in CANDIDATES]
        ambiguous.append({**CANDIDATES[0], "STUDY.ACCESSION": "GCST90475098"})
        ambiguous_path = self.root / "ambiguous.tsv"
        write_candidates(ambiguous_path, ambiguous)
        result = run(self.root, "map", "--candidates", str(ambiguous_path), "--allow-partial")
        self.assertEqual(result.returncode, 1, result.stdout)
        report = self.rows(self.root / "mapping-report.tsv")
        self.assertIn("ambiguous_analysis", [row["kind"] for row in report])

        # A candidate with no extracted Analysis is reported, not silently dropped.
        truncated = [row for row in CANDIDATES if row["STUDY.ACCESSION"] != "GCST90475097"]
        truncated_path = self.root / "unmatched.tsv"
        write_candidates(truncated_path, truncated)
        result = run(self.root, "map", "--candidates", str(truncated_path), "--allow-partial")
        self.assertEqual(result.returncode, 1, result.stdout)
        report = self.rows(self.root / "mapping-report.tsv")
        self.assertIn("unmatched_analysis", [row["kind"] for row in report])

        # A candidate row no Analysis claims is a missing Analysis, not a spare.
        spare = [dict(row) for row in CANDIDATES]
        spare.append({
            **CANDIDATES[0], "STUDY.ACCESSION": "GCST90470001", "DISEASE.TRAIT": "Something else",
            "sample_size": "99999",
        })
        spare_path = self.root / "spare.tsv"
        write_candidates(spare_path, spare)
        result = run(self.root, "map", "--candidates", str(spare_path))
        self.assertEqual(result.returncode, 1, result.stdout)
        report = self.rows(self.root / "mapping-report.tsv")
        self.assertIn("unused_candidate", [row["kind"] for row in report])
        # Restore the fixture mapping for the convert/manifest tests.
        run(self.root, "map", "--candidates", str(self.candidates), "--allow-partial")

    # -- convert ----------------------------------------------------------

    def test_07_convert_reproduces_ebi_bytes(self) -> None:
        result = run(self.root, "convert", "--candidates", str(self.candidates), "--jobs", "4")
        self.assertEqual(result.returncode, 1, result.stdout)  # the bogus member must fail

        # Exact decompressed bytes, line terminators included: EBI's raw files
        # end every line with CRLF.
        quant = decompress(self.data("GCST90475097"))
        self.assertEqual(
            quant,
            "\r\n".join([
                QUANT_OUT_HEADER,
                GOLDEN_QUANT_ROW,
                "2\t123\tT\tC\t0.01\t0.02\t0.5\t0.9\trs62839324\tT\t338640\t0.4\t#NA\t#NA\t#NA",
                # effect allele equals ref -> other_allele is alt
                "2\t456\tA\tT\t-0.5\t0.1\t0.25\t1e-09\trs999\tT\t338640\t0.2\t#NA\t#NA\t#NA",
                # a row shorter than the header keeps its missing fields as #NA
                "3\t789\tC\tA\t0\t0.1\t1\t0.5\trs1000\tC\t338640\t0.3\t#NA\t#NA\t#NA",
                # a round position the source wrote as 2.4e+07 is written as the
                # integer (EBI's own file has an empty cell here)
                "4\t24000000\tT\tG\t0.05\t0.2\t0.1\t1.0\trs3000\tT\t338640\t0.1\t#NA\t#NA\t#NA",
                # p_value is rendered through a float, as EBI's own files are
                "5\t123\tC\tA\t0.1\t0.2\t0.2\t0.0006\trs3001\tC\t338640\t0.1\t#NA\t#NA\t#NA",
                # a non-integer position gets an empty cell, the row stays
                "6\t\tC\tA\t0.1\t0.2\t0.2\t0.5\trs3002\tC\t338640\t0.1\t#NA\t#NA\t#NA",
            ]) + "\r\n",
        )

        binary = decompress(self.data("GCST90477833"))
        self.assertEqual(
            binary,
            "\r\n".join([
                BINARY_OUT_HEADER,
                GOLDEN_BINARY_ROW,
                "1\t100\tG\tA\t1.5\t#NA\t0.1\t0.5\trs2000\t#NA\t#NA\tG\t438595\t0.2\t5583\t0.3\t433012\t0.1\t#NA\t#NA\t#NA",
            ]) + "\r\n",
        )

        meta = decompress(self.data("GCST90479504"))
        self.assertEqual(meta, "\r\n".join([META_OUT_HEADER, GOLDEN_META_ROW]) + "\r\n")

    def test_08_convert_fails_loudly_on_an_unrecognised_header(self) -> None:
        result = run(self.root, "convert", "--candidates", str(self.candidates), "--analysis", "GCST90479999")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("unrecognised source column: mystery", result.stdout + result.stderr)
        self.assertEqual(list((self.root / "gwas-ssf").glob("*/*/*.partial")), [])

    def test_09_convert_is_idempotent_and_atomic(self) -> None:
        target = self.data("GCST90475097")
        before = target.stat().st_mtime_ns
        result = run(self.root, "convert", "--candidates", str(self.candidates), "--analysis", "GCST90475097")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipped=1", result.stdout)
        self.assertEqual(target.stat().st_mtime_ns, before)

        # A leftover .partial from a crashed run is discarded, never renamed in.
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(b"half a file")
        result = run(self.root, "convert", "--candidates", str(self.candidates), "--analysis", "GCST90475097", "--force")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(partial.exists())
        self.assertEqual(decompress(target).splitlines()[1], GOLDEN_QUANT_ROW)

    def test_10_convert_writes_ebi_metadata_with_provenance(self) -> None:
        import yaml

        path = self.data("GCST90475097").with_name("GCST90475097.tsv.gz-meta.yaml")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(document["gwas_id"], "GCST90475097")
        self.assertEqual(document["trait_description"], ["Hemoglobin A1c (HbA1c, minimum, inv-norm transformed)"])
        self.assertEqual(document["genome_assembly"], "GRCh38")
        self.assertEqual(document["coordinate_system"], "1-based")
        self.assertEqual(document["genotyping_technology"], ["Genome-wide genotyping array"])
        self.assertEqual(document["samples"], [{"sample_ancestry_category": ["European"], "sample_size": 338640}])
        self.assertEqual(document["file_type"], "GWAS-SSF v1.0")
        self.assertIs(document["is_harmonised"], False)
        self.assertIs(document["is_sorted"], False)
        self.assertEqual(document["source"]["repository"], "dbGaP")
        self.assertEqual(document["source"]["study"], "phs002453")
        self.assertEqual(document["source"]["tar"], TAR)
        self.assertEqual(document["source"]["member"], f"{INNER}/MVP_R4.1000G_AGR.A1C_Min_INT.EUR.GIA.dbGaP.txt.gz")
        self.assertTrue(document["source"]["tar_md5"])
        self.assertEqual(document["data_file_md5sum"], hashlib.md5(self.data("GCST90475097").read_bytes()).hexdigest())
        self.assertIn("GWAS of A1C_Min_INT", document["dbgap_analysis_description"])

        binary = yaml.safe_load(
            self.data("GCST90477833").with_name("GCST90477833.tsv.gz-meta.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            binary["samples"],
            [{"sample_ancestry_category": ["European"], "sample_size": 438595, "case_control_study": True}],
        )
        meta = yaml.safe_load(
            self.data("GCST90479504").with_name("GCST90479504.tsv.gz-meta.yaml").read_text(encoding="utf-8")
        )
        # The candidate table still labels this row European; the yaml must not.
        self.assertEqual(
            meta["samples"],
            [{"sample_ancestry_category": ["Multi-ancestry"], "sample_size": 542276}],
        )

    # -- manifest ---------------------------------------------------------

    def test_11_manifest_has_the_ebi_columns_and_sha256(self) -> None:
        result = run(self.root, "manifest", "--candidates", str(self.candidates))
        self.assertEqual(result.returncode, 1, result.stdout)  # GCST90479999 has no converted file
        path = self.root / "manifests" / f"{STORE_KEY.replace('__', '-')}-download-manifest.tsv"
        self.assertTrue(path.is_file(), result.stdout + result.stderr)
        rows = {row["analysis_id"]: row for row in self.rows(path)}
        with path.open(encoding="utf-8") as handle:
            self.assertEqual(handle.readline().rstrip("\n"), "\t".join(ACQUIRE.MANIFEST_COLUMNS))
        self.assertEqual(
            ACQUIRE.MANIFEST_COLUMNS,
            ("analysis_id", "publication_pmid", "trait", "study_design", "sample_size",
             "harmonised_status", "status", "data_url", "yaml_url", "data_file", "yaml_file",
             "data_bytes", "yaml_bytes", "sha256", "genome_assembly", "file_type",
             "is_harmonised", "seconds", "error"),
        )
        self.assertTrue(set(ACQUISITION_MANIFEST_COLUMNS).issubset(ACQUIRE.MANIFEST_COLUMNS))
        ready = rows["GCST90475097"]
        self.assertEqual(ready["status"], "ok")
        self.assertEqual(ready["sha256"], hashlib.sha256(self.data("GCST90475097").read_bytes()).hexdigest())
        self.assertEqual(ready["data_bytes"], str(self.data("GCST90475097").stat().st_size))
        self.assertEqual(ready["genome_assembly"], "GRCh38")
        self.assertEqual(ready["file_type"], "GWAS-SSF v1.0")
        self.assertEqual(ready["is_harmonised"], "false")
        self.assertEqual(ready["data_url"], f"https://ftp.ncbi.nlm.nih.gov/dbgap/studies/phs002453/analyses/GIA/{TAR}")
        self.assertEqual(rows["GCST90479999"]["status"], "error")
        self.assertTrue(rows["GCST90479999"]["error"].startswith("not converted:"))

    def test_12_manifest_is_accepted_by_the_source_inventory(self) -> None:
        path = self.root / "manifests" / f"{STORE_KEY.replace('__', '-')}-download-manifest.tsv"
        rows = read_acquisition_manifest(path)
        self.assertEqual(len(rows), 3)
        self.assertIn("GCST90479999", {row["analysis_id"] for row in rows if row["status"] != "ok"})

        selection = read_candidate_selection(self.candidates, STORE_KEY)
        snapshot = build_snapshot(
            snapshot_id="test-dbgap-phs002453",
            source_collection_id="test-source-collection",
            store_key=STORE_KEY,
            ancestry_group="European",
            manifests=[AcquisitionPass(role="dbgap-phs002453", path=path)],
            candidates=selection,
        )
        self.assertEqual(len(snapshot.rows), 3)
        self.assertEqual({row.analysis_id for row in snapshot.ready_rows}, {"GCST90475097", "GCST90477833"})
        write_snapshot(snapshot, self.root / "inventory.tsv", self.root / "inventory.meta.yaml")
        self.assertTrue((self.root / "inventory.tsv").is_file())

    def test_13_manifest_covers_each_store_key(self) -> None:
        result = run(
            self.root, "manifest", "--candidates", str(self.candidates),
            "--analysis", "GCST90475097,GCST90477833,GCST90479504",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        meta = self.root / "manifests" / f"{META_STORE_KEY.replace('__', '-')}-download-manifest.tsv"
        self.assertTrue(meta.is_file())
        self.assertEqual([row["analysis_id"] for row in self.rows(meta)], ["GCST90479504"])

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def rows(path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle, delimiter="\t"))



class ConverterEdgeCaseTests(unittest.TestCase):
    """The awk program run directly, on rows the fixture tars do not carry."""

    def convert(self, *rows: str) -> subprocess.CompletedProcess[str]:
        text = "\n".join([QUANT_HEADER, *rows]) + "\n"
        return subprocess.run(
            ["awk", "-F", "\t", "-v", "OFS=\t", ACQUIRE.CONVERTER_AWK],
            input=text, capture_output=True, text=True, check=False,
        )

    def p_values(self, *values: str) -> list[str]:
        rows = [
            f"rs{i}\t1\t{100 + i}\tG\tA\tA\t0.1\t338640\t0.1\t0.2\t{value}\t0.9\tNA\tNA\tNA"
            for i, value in enumerate(values)
        ]
        result = self.convert(*rows)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()[1:]
        return [line.split("\t")[7] for line in lines]

    def test_p_value_renders_like_a_python_float(self) -> None:
        values = ["1", "0", "0.35", "6e-04", "1e-09", "1e-300", "2.2e-16", "0.123456789012345"]
        self.assertEqual(self.p_values(*values), [repr(float(value)) for value in values])

    def test_subnormal_p_value_is_not_rounded_to_zero(self) -> None:
        # awk reads these as 0; the source text is kept rather than writing 0.0
        self.assertEqual(self.p_values("1e-321", "5e-324", "0e+00"), ["1e-321", "5e-324", "0.0"])

    def test_row_wider_than_the_header_fails(self) -> None:
        row = "rs1\t1\t100\tG\tA\tA\t0.1\t338640\t0.1\t0.2\t0.5\t0.9\tNA\tNA\tNA\textra"
        result = self.convert(row)
        self.assertEqual(result.returncode, 3)
        self.assertIn("more fields than the header", result.stderr)

    def test_unsafe_member_path_in_a_table_of_contents_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toc = Path(tmp) / f"{TAR}.table_of_contents.txt"
            toc.write_text(
                "-rw-r--r-- user/group 10 2024-01-01 00:00 ../escape.txt.gz\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ACQUIRE.AcquisitionError, "unsafe member path"):
                ACQUIRE.parse_tar_listing(toc)

if __name__ == "__main__":
    unittest.main(verbosity=2)
