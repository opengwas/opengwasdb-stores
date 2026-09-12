#!/usr/bin/env python3
"""A frozen FinnGen-shaped bundle through OpenGWASDB's Store envelope and query API.

Issue #103 deleted the `opengwas-gwas-vcf-dense/build-store.py` adapter that used
to drive this build. The two pieces it owned are exercised here against their
surviving shared homes: the builder manifest comes from
`resources/lib/release_manifest.py` (issue #96), the Store from the
`opengwasdb build-dense-vcf` CLI the production workflow invokes, and the
metadata read-back from `workflow/phase.py::store_metadata_mismatches` -- the
check the adapter used to perform by hand.

Run from the repository root:
    pixi run python tests/finngen-r13-pilot/test_store_envelope.py
"""
from __future__ import annotations

import csv
import gzip
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workflow"))

from opengwasdb.query import query_store  # noqa: E402
from opengwasdb.validation import validate_store  # noqa: E402

from phase import store_metadata_mismatches  # noqa: E402
from resources.lib.release_manifest import write_builder_manifest  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OPENGWASDB = shutil.which("opengwasdb")


def write_finngen(path: Path, *, beta: float, se: float) -> None:
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["#chrom", "pos", "ref", "alt", "beta", "sebeta", "af_alt"],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows([
            {"#chrom": "1", "pos": 1000, "ref": "G", "alt": "A", "beta": beta, "sebeta": se, "af_alt": 0.4},
            {"#chrom": "2", "pos": 2000, "ref": "T", "alt": "C", "beta": -beta, "sebeta": se, "af_alt": 0.3},
        ])


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def main() -> None:
    assert OPENGWASDB is not None, "opengwasdb is not on PATH; run inside the `dev` pixi environment"
    with tempfile.TemporaryDirectory(dir=ROOT) as tmp_raw:
        tmp = Path(tmp_raw)
        release = tmp / "release"
        release.mkdir()
        binary_source = tmp / "finngen_R13_BINARY.gz"
        quant_source = tmp / "finngen_R13_QUANT.gz"
        write_finngen(binary_source, beta=0.6, se=0.3)
        write_finngen(quant_source, beta=0.8, se=0.4)
        store = tmp / "store.opengwasdb"

        columns = [
            "analysis_id", "source_analysis_id", "source_label", "analysis_label",
            "trait_ontology_id", "trait_ontology_label", "trait_ontology_mapping_method",
            "source_file", "source_reader_capability", "source_genome_build", "license",
            "publication_doi", "publication_pmid", "consortium", "first_author",
            "source_ancestry_label", "assigned_ancestry", "ancestry_assignment_method",
            "ancestry_prop_EUR", "original_effect_scale", "original_sd", "original_sd_method",
            "stored_effect_scale", "sample_size_kind", "sample_size_scope", "sample_size",
            "n_cases", "n_controls", "exclude_from_build",
        ]
        common = {
            "trait_ontology_id": "",
            "trait_ontology_label": "",
            "trait_ontology_mapping_method": "unmapped",
            "source_reader_capability": "opengwasdb.finngen-r13",
            "source_genome_build": "GRCh38",
            "license": "FinnGen public data",
            "publication_doi": "",
            "publication_pmid": "",
            "consortium": "FinnGen",
            "first_author": "",
            "source_ancestry_label": "Finnish",
            "assigned_ancestry": "EUR",
            "ancestry_assignment_method": "af_assigned",
            "ancestry_prop_EUR": "0.99",
            "sample_size_scope": "analysis_level",
            "exclude_from_build": "",
        }
        rows = [
            {
                **common,
                "analysis_id": "finngen-r13-BINARY",
                "source_analysis_id": "BINARY",
                "source_label": "Binary fixture",
                "analysis_label": "Binary fixture",
                "source_file": str(binary_source),
                "original_effect_scale": "log_or",
                "original_sd": "",
                "original_sd_method": "binary_trait",
                "stored_effect_scale": "log_or",
                "sample_size_kind": "case_control",
                "sample_size": "10000",
                "n_cases": "1000",
                "n_controls": "9000",
            },
            {
                **common,
                "analysis_id": "finngen-r13-QUANT",
                "source_analysis_id": "QUANT",
                "source_label": "Quantitative fixture",
                "analysis_label": "Quantitative fixture",
                "source_file": str(quant_source),
                "original_effect_scale": "sd",
                "original_sd": "2",
                "original_sd_method": "source_provided",
                "stored_effect_scale": "sd",
                "sample_size_kind": "total",
                "sample_size": "10000",
                "n_cases": "",
                "n_controls": "",
            },
        ]
        manifest_path = tmp / "builder-manifest.tsv"
        manifest = write_builder_manifest(rows, manifest_path, layout="dense")

        # The production workflow's build invocation: the shared builder manifest
        # and the opaque `build.arguments` flags, shelled out to the CLI.
        result = subprocess.run(
            [
                OPENGWASDB, "build-dense-vcf", str(manifest_path), str(store),
                "--store-id", "finngen-r13-fixture",
                "--release-id", "fixture-2",
                "--n-workers", "2",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (store / "manifest.json").exists()
        assert (store / "analyses.tsv").exists()
        assert (store / "data.zarr").exists()
        validation = validate_store(store)
        assert validation.ok, validation.errors

        query = query_store(store)
        try:
            binary = query.lookup(["1:1000:A:G"], ["finngen-r13-BINARY"])
            quantitative = query.lookup(["1:1000:A:G"], ["finngen-r13-QUANT"])
            built = {row["analysis_id"]: row for row in query.analyses_table().values()}
        finally:
            query.close()
        assert math.isclose(float(binary["z"][0]), 2.0, rel_tol=1e-3)
        assert math.isclose(float(binary["se"][0]), 0.3, rel_tol=1e-3)
        assert math.isclose(float(quantitative["z"][0]), 2.0, rel_tol=1e-3)
        assert math.isclose(float(quantitative["se"][0]), 0.2, rel_tol=1e-3)

        for analysis_id, source in ((row["analysis_id"], row) for row in rows):
            observed = built[analysis_id]
            for column in (
                "sample_size_kind", "sample_size_scope", "sample_size", "n_cases", "n_controls",
                "assigned_ancestry", "ancestry_assignment_method", "original_effect_scale",
                "original_sd", "original_sd_method", "stored_effect_scale",
            ):
                assert observed[column] == source[column], (
                    f"{analysis_id}: built {column}={observed[column]!r}, expected {source[column]!r}"
                )
            assert math.isclose(float(observed["ancestry_prop_EUR"]), 0.99, rel_tol=1e-9)

        # The read-back the retired adapter performed by hand now lives in the
        # workflow: no interpretation-bearing metadata may differ between the
        # resolved rows/manifest the build was handed and the built Store.
        mismatches = store_metadata_mismatches(rows, manifest.fieldnames, built)
        assert mismatches == [], mismatches
        corrupt = {**built, "finngen-r13-BINARY": {**built["finngen-r13-BINARY"], "n_cases": ""}}
        assert store_metadata_mismatches(rows, manifest.fieldnames, corrupt), (
            "the metadata read-back must flag a built Store that drops n_cases"
        )

    print("store-envelope metadata read-back: passed")


if __name__ == "__main__":
    main()
