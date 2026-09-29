#!/usr/bin/env python3
"""Tests for curation.ukb: ukb-b trait labels and the Showcase resolver.

The contract under test is that a ukb-b queue label parses into its field
title, code, and value (for the coded diagnosis/procedure/medication families
and the general ``title: value`` case), that the Showcase schema resolves a
title to its field with a category path, and that every label yields the
retrieval text and trait context the next pipeline pass will consume.

The suite is hermetic: the Showcase schema is a small in-repo fixture written
into a temporary directory by the test, never the ~4 MiB real download.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation import ukb

SCHEMA_1 = """\
field_id\ttitle\tavailability\tstability\tprivate\tvalue_type\tbase_type\titem_type\tstrata\tinstanced\tarrayed\tsexed\tunits\tmain_category\tencoding_id\tinstance_id\tinstance_min\tinstance_max\tarray_min\tarray_max\tnotes\tdebut\tversion\tnum_participants\titem_count\tshowcase_order\tcost_do\tcost_on\tcost_sc
41202\tDiagnoses - main ICD10\t0\t3\t0\t22\t41\t0\t0\t0\t1\t0\t\t2002\t19\t0\t0\t0\t0\t84\tThis field is a summary of the distinct primary/main diagnosis codes a participant has had recorded across all their hospital inpatient records.\t2013-06-25\t2025-08-29\t448646\t2524659\t140\t0\t1\t0
41204\tDiagnoses - secondary ICD10\t0\t3\t0\t22\t41\t0\t0\t0\t1\t0\t\t2002\t19\t0\t0\t0\t0\t211\tThis field is a summary of the distinct secondary diagnosis codes a participant has had recorded across all their hospital inpatient records.\t2013-06-25\t2025-08-29\t440021\t4441679\t134\t0\t1\t0
41200\tOperative procedures - main OPCS4\t0\t3\t0\t22\t41\t0\t0\t0\t1\t0\t\t2005\t240\t0\t0\t0\t0\t59\tThis field is a summary of the main operation and procedure codes a participant has had recorded across all their hospital inpatient records.\t2013-06-25\t2025-08-29\t409487\t1636009\t118\t0\t1\t0
41210\tOperative procedures - secondary OPCS4\t0\t3\t0\t22\t41\t0\t0\t0\t1\t0\t\t2005\t240\t0\t0\t0\t0\t104\tThis field is a summary of the secondary operation and procedure codes a participant has had recorded across all their hospital inpatient records.\t2013-06-25\t2025-08-29\t397200\t1279554\t110\t0\t1\t0
20003\tTreatment/medication code\t0\t0\t0\t22\t11\t0\t0\t1\t1\t0\t\t100075\t4\t2\t0\t3\t0\t47\tCode for treatment.\t2012-01-12\t2025-08-30\t382887\t1512317\t120\t0\t1\t1
20002\tNon-cancer illness code, self-reported\t0\t0\t0\t22\t11\t0\t3\t1\t1\t0\t\t100074\t6\t2\t0\t3\t0\t36\tCode for non-cancer illness.\t2012-01-12\t2025-08-30\t382731\t2722789\t117\t0\t1\t1
20001\tCancer code, self-reported\t0\t0\t0\t22\t11\t0\t3\t1\t1\t0\t\t100074\t3\t2\t0\t3\t0\t5\tCode for cancer.\t2012-01-12\t2025-08-30\t382801\t689590\t115\t0\t1\t1
20004\tOperation code\t0\t0\t0\t22\t11\t0\t3\t1\t1\t0\t\t100076\t5\t2\t0\t3\t0\t31\tCode for operation.\t2012-01-12\t2025-08-30\t382920\t766786\t113\t0\t1\t1
40006\tType of cancer: ICD10\t0\t2\t0\t21\t41\t0\t0\t1\t0\t0\t\t100092\t19\t9000002\t0\t21\t0\t0\tThe ICD-10 code for the type of cancer.\t2013-06-25\t2025-08-29\t30890\t47044\t79\t0\t1\t0
41248\tDestinations on discharge from hospital (recoded)\t0\t3\t0\t22\t11\t0\t0\t0\t1\t0\t\t2001\t267\t0\t0\t0\t0\t11\tThis field is a summary of the distinct destination on discharge codes a participant has had recorded across all their hospital inpatient records.\t2013-06-25\t2025-08-29\t7364\t7394\t86\t0\t1\t0
6150\tVascular/heart problems diagnosed by doctor\t0\t0\t0\t22\t11\t0\t0\t1\t1\t0\t\t100044\t100605\t2\t0\t2\t0\t3\tACE touchscreen question "Has a doctor ever told you that you have had any of the following conditions?"\t2009-08-04\t2025-08-30\t457723\t2196078\t258\t0\t1\t1
48\tWaist circumference\t0\t0\t0\t31\t11\t0\t0\t1\t1\t0\tcm\t100046\t0\t2\t0\t1\t0\t0\tWaist circumference was measured using a Seca 200 device.\t2006-03-14\t2025-08-30\t502252\t502252\t77\t0\t1\t1
"""

SCHEMA_3 = """\
category_id\ttitle\tavailability\tgroup_type\tdescript\tnotes
91\tHealth outcomes\t0\t1\t
100091\tExternally sourced health outcomes\t0\t1\t
2000\tHospital inpatient\t0\t1\t
2002\tSummary Diagnoses\t0\t1\t
2001\tSummary Administration\t0\t1\t
2005\tSummary Operations\t0\t1\t
100092\tCancer register\t0\t1\t
100000\tAssessment centre\t0\t1\t
100071\tVerbal interview\t0\t1\t
100074\tMedical conditions\t0\t1\t
100075\tMedications\t0\t1\t
100076\tOperations\t0\t1\t
100044\tCardiovascular\t0\t1\t
100046\tBody size measures\t0\t1\t
"""

SCHEMA_13 = """\
parent_id\tchild_id\tshowcase_order
91\t100091\t100
100091\t2000\t110
100091\t100092\t120
2000\t2002\t130
2000\t2001\t131
2000\t2005\t132
100000\t100071\t200
100071\t100074\t210
100071\t100075\t211
100071\t100076\t212
100071\t100044\t213
100000\t100046\t230
"""


def write_fixture_schema(directory: Path) -> Path:
    """Write the fixture Showcase TSVs under ``directory`` and return it."""
    (directory / "schema-1.tsv").write_text(SCHEMA_1, encoding="utf-8")
    (directory / "schema-3.tsv").write_text(SCHEMA_3, encoding="utf-8")
    (directory / "schema-13.tsv").write_text(SCHEMA_13, encoding="utf-8")
    return directory


class ParseTraitLabelTest(unittest.TestCase):
    """The label families, codes, and value split (issue #185)."""

    def assert_parses(
        self,
        label: str,
        *,
        family: str,
        title: str,
        code: str = "",
        system: str = "",
        value: str | None = None,
    ) -> ukb.UkbLabel:
        parsed = ukb.parse_trait_label(label)
        self.assertEqual(parsed.family, family)
        self.assertEqual(parsed.title, title)
        self.assertEqual(parsed.code, code)
        self.assertEqual(parsed.code_system, system)
        if value is not None:
            self.assertEqual(parsed.value, value)
        return parsed

    def test_icd10_families(self) -> None:
        self.assert_parses(
            "diagnoses - main icd10: c20 malignant neoplasm of rectum",
            family=ukb.FAMILY_DIAGNOSES_MAIN_ICD10,
            title="diagnoses - main icd10",
            code="c20",
            system="icd10",
            value="malignant neoplasm of rectum",
        )
        self.assert_parses(
            "diagnoses - secondary icd10: b34.9 viral infection, unspecified",
            family=ukb.FAMILY_DIAGNOSES_SECONDARY_ICD10,
            title="diagnoses - secondary icd10",
            code="b34.9",
            system="icd10",
            value="viral infection, unspecified",
        )

    def test_opcs_families_alias_to_opcs4(self) -> None:
        self.assert_parses(
            "operative procedures - main opcs: a52.1 therapeutic lumbar epidural injection",
            family=ukb.FAMILY_PROCEDURES_MAIN_OPCS,
            title="operative procedures - main opcs4",
            code="a52.1",
            system="opcs4",
            value="therapeutic lumbar epidural injection",
        )
        self.assert_parses(
            "operative procedures - secondary opcs: a55.9 unspecified diagnostic spinal puncture",
            family=ukb.FAMILY_PROCEDURES_SECONDARY_OPCS,
            title="operative procedures - secondary opcs4",
            code="a55.9",
            system="opcs4",
            value="unspecified diagnostic spinal puncture",
        )

    def test_code_families_without_a_leading_code(self) -> None:
        self.assert_parses(
            "treatment/medication code: adalat 5mg capsule",
            family=ukb.FAMILY_TREATMENT_MEDICATION,
            title="treatment/medication code",
            value="adalat 5mg capsule",
        )
        self.assert_parses(
            "non-cancer illness code, self-reported: allergy or anaphylactic reaction to drug",
            family=ukb.FAMILY_NON_CANCER_ILLNESS,
            title="non-cancer illness code, self-reported",
            value="allergy or anaphylactic reaction to drug",
        )
        self.assert_parses(
            "cancer code, self-reported: breast cancer",
            family=ukb.FAMILY_CANCER_CODE_SELF_REPORTED,
            title="cancer code, self-reported",
            value="breast cancer",
        )
        self.assert_parses(
            "operation code: anal surgery",
            family=ukb.FAMILY_OPERATION_CODE,
            title="operation code",
            value="anal surgery",
        )

    def test_type_of_cancer_family(self) -> None:
        self.assert_parses(
            "type of cancer: icd10: c18.7 sigmoid colon",
            family=ukb.FAMILY_TYPE_OF_CANCER,
            title="type of cancer: icd10",
            code="c18.7",
            system="icd10",
            value="sigmoid colon",
        )

    def test_general_title_value_case(self) -> None:
        self.assert_parses(
            "illness, injury, bereavement, stress in last 2 years: serious illness, injury or assault to yourself",
            family=ukb.FAMILY_GENERAL,
            title="illness, injury, bereavement, stress in last 2 years",
            value="serious illness, injury or assault to yourself",
        )

    def test_recoded_title_is_stripped_for_the_lookup_key(self) -> None:
        self.assert_parses(
            "destinations on discharge from hospital (recoded): transfer within nhs provider",
            family=ukb.FAMILY_GENERAL,
            title="destinations on discharge from hospital",
            value="transfer within nhs provider",
        )

    def test_bare_label_has_no_title_or_code(self) -> None:
        parsed = self.assert_parses(
            "waist circumference",
            family=ukb.FAMILY_GENERAL,
            title="",
        )
        self.assertEqual(parsed.value, "waist circumference")

    def test_blank_label(self) -> None:
        parsed = ukb.parse_trait_label("   ")
        self.assertEqual(parsed.raw, "")
        self.assertEqual(parsed.family, ukb.FAMILY_GENERAL)

    def test_icd10_code_extraction_is_family_scoped(self) -> None:
        self.assertEqual(
            ukb.icd10_code_from_label(
                "diagnoses - main icd10: c20 malignant neoplasm of rectum"
            ),
            "c20",
        )
        # An opcs code is not an ICD-10 code; a b12-like token outside the
        # code families never fires.
        self.assertEqual(
            ukb.icd10_code_from_label(
                "operative procedures - main opcs: a52.1 therapeutic lumbar epidural injection"
            ),
            "",
        )
        self.assertEqual(ukb.icd10_code_from_label("vitamin b12 intake"), "")


class ShowcaseResolverTest(unittest.TestCase):
    """Title matching, coded-vs-not, and the retrieval/context split."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.schema_dir = write_fixture_schema(Path(self.temp_dir.name))
        self.resolver = ukb.ShowcaseResolver.from_directory(self.schema_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_icd10_label_resolves_to_its_field(self) -> None:
        field = self.resolver.resolve(
            "diagnoses - main icd10: c20 malignant neoplasm of rectum"
        )
        self.assertIsNotNone(field)
        self.assertEqual(field.field_id, "41202")
        self.assertEqual(field.schema_title, "Diagnoses - main ICD10")
        self.assertEqual(field.encoding_id, "19")
        self.assertTrue(field.is_coded)
        # Coded field: retrieve on the value without the code, and the notes
        # are boilerplate, so the context is deliberately empty.
        self.assertEqual(field.retrieval_text, "malignant neoplasm of rectum")
        self.assertEqual(field.trait_context, "")

    def test_type_of_cancer_title_contains_a_colon(self) -> None:
        field = self.resolver.resolve(
            "type of cancer: icd10: c34.1 upper lobe, bronchus or lung"
        )
        self.assertEqual(field.field_id, "40006")
        self.assertTrue(field.is_coded)
        self.assertEqual(field.retrieval_text, "upper lobe, bronchus or lung")
        self.assertEqual(field.category_path, ("Health outcomes", "Externally sourced health outcomes", "Cancer register"))

    def test_opcs_alias_resolves_against_the_schema(self) -> None:
        field = self.resolver.resolve(
            "operative procedures - main opcs: a52.1 therapeutic lumbar epidural injection"
        )
        self.assertEqual(field.field_id, "41200")
        self.assertTrue(field.is_coded)

    def test_recoded_title_resolves_to_the_recoded_field(self) -> None:
        field = self.resolver.resolve(
            "destinations on discharge from hospital (recoded): transfer within nhs provider"
        )
        self.assertEqual(field.field_id, "41248")

    def test_categorical_question_field_is_coded(self) -> None:
        # 6150 is a categorical (value_type 22) field: the value is the
        # participant's answer, which is the phenotype to retrieve on.
        field = self.resolver.resolve(
            "vascular/heart problems diagnosed by doctor: high blood pressure"
        )
        self.assertTrue(field.is_coded)
        self.assertEqual(field.retrieval_text, "high blood pressure")
        self.assertEqual(field.trait_context, "")

    def test_bare_measurement_label_resolves_with_context(self) -> None:
        field = self.resolver.resolve("waist circumference")
        self.assertEqual(field.field_id, "48")
        self.assertFalse(field.is_coded)
        self.assertEqual(field.retrieval_text, "waist circumference")
        self.assertEqual(
            field.category_path,
            ("Assessment centre", "Body size measures"),
        )
        self.assertIn("Waist circumference was measured", field.trait_context)
        self.assertIn("units: cm", field.trait_context)
        self.assertIn("field 48 'Waist circumference'", field.trait_context)

    def test_continuous_title_value_label_is_not_coded(self) -> None:
        field = self.resolver.resolve("waist circumference: 102.3")
        self.assertFalse(field.is_coded)
        self.assertEqual(field.retrieval_text, "waist circumference: 102.3")
        self.assertIn("units: cm", field.trait_context)

    def test_unresolved_title_passes_the_label_through(self) -> None:
        field = self.resolver.resolve("qwertyuiop asdfghjkl")
        self.assertIsNotNone(field)
        self.assertEqual(field.field_id, "")
        self.assertEqual(field.category_path, ())
        self.assertFalse(field.is_coded)
        self.assertEqual(field.retrieval_text, "qwertyuiop asdfghjkl")
        self.assertEqual(field.trait_context, "")

    def test_blank_label_is_none(self) -> None:
        self.assertIsNone(self.resolver.resolve(""))
        self.assertIsNone(self.resolver.resolve("   "))

    def test_missing_schema_file_fails_loudly(self) -> None:
        (self.schema_dir / "schema-13.tsv").unlink()
        with self.assertRaises(ValueError):
            ukb.ShowcaseResolver.from_directory(self.schema_dir)


if __name__ == "__main__":
    unittest.main()