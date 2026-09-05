#!/usr/bin/env python3
"""Build and smoke-query a dense opengwas-gwas-vcf release bundle with
OpenGWASDB. Structured like resources/generators/gwas-ssf-ragged/build-store.py,
but simpler: dense has no sparse-region sidecar to cross-check against, so
the read-back just queries one known Analysis and confirms finite
association statistics plus correct passthrough Analytical Metadata.

opengwasdb.layouts.dense.build_vcf.build_dense_from_vcf_manifest() requires
its own manifest column names (trait_id/file_path/trait_name/n --
OpenGWASDB's older internal convention), distinct from this registry's
analyses.tsv (analysis_id/source_file/analysis_label/sample_size). This
script's whole job is that translation -- see the field-by-field mapping in
main() below.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import resource
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from opengwasdb.layouts.dense.build_vcf import (  # noqa: E402
    build_dense_from_vcf_manifest,
)
from opengwasdb.query import query_store  # noqa: E402
from opengwasdb.validation import validate_store  # noqa: E402

from resources.lib.release_yaml import (  # noqa: E402
    merge_validation_yaml,
    read_release_yaml,
    read_tsv,
    repo_root,
    require_text,
    resolve_path,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--store-dir")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args()


def write_builder_manifest(rows: list[dict[str, str]], out_path: Path) -> None:
    """Translate this registry's analyses.tsv column names into the shape
    opengwasdb.layouts.dense.build_vcf._read_manifest() actually requires."""
    ancestry_columns = sorted({
        column
        for row in rows
        for column in row
        if column.startswith("ancestry_prop_")
    })
    fieldnames = [
        "trait_id", "file_path", "trait_name", "n", "stored_effect_scale",
        "source_reader_capability", "source_assembly",
        "original_sd_method", "original_sd", "assigned_ancestry",
        "ancestry_assignment_method", "original_effect_scale",
        "sample_size_kind", "sample_size_scope", "n_cases", "n_controls",
        "trait_ontology_id", "trait_ontology_label",
        "license", "publication_doi", "publication_pmid", "consortium", "first_author",
        *ancestry_columns,
    ]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            if row.get("exclude_from_build") == "true":
                continue
            writer.writerow({
                "trait_id": row["analysis_id"],
                "file_path": row["source_file"],
                "trait_name": row["analysis_label"],
                "n": row["sample_size"],
                "stored_effect_scale": row["stored_effect_scale"],
                "source_reader_capability": row.get("source_reader_capability", ""),
                "source_assembly": row.get("source_genome_build", ""),
                "original_sd_method": row["original_sd_method"],
                "original_sd": row.get("original_sd", ""),
                "assigned_ancestry": row.get("assigned_ancestry", ""),
                "ancestry_assignment_method": row.get("ancestry_assignment_method", ""),
                "original_effect_scale": row.get("original_effect_scale", ""),
                "sample_size_kind": row.get("sample_size_kind", ""),
                "sample_size_scope": row.get("sample_size_scope", ""),
                "n_cases": row.get("n_cases", ""),
                "n_controls": row.get("n_controls", ""),
                "trait_ontology_id": row.get("trait_ontology_id", ""),
                "trait_ontology_label": row.get("trait_ontology_label", ""),
                "license": row.get("license", ""),
                "publication_doi": row.get("publication_doi", ""),
                "publication_pmid": row.get("publication_pmid", ""),
                "consortium": row.get("consortium", ""),
                "first_author": row.get("first_author", ""),
                **{column: row.get(column, "") for column in ancestry_columns},
            })


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def first_probe(rows: list[dict[str, str]], scale: str) -> str:
    return next((row["analysis_id"] for row in rows if row["stored_effect_scale"] == scale), "")


def metadata_mismatch_errors(
    rows: list[dict[str, str]], store_by_id: dict[str, dict[str, str]]
) -> list[str]:
    """Return exact interpretation-bearing manifest/Store mismatches."""
    errors = []
    for row in rows:
        analysis_id = row["analysis_id"]
        store_row = store_by_id.get(analysis_id)
        if store_row is None:
            errors.append(f"{analysis_id}: missing from built Store analyses.tsv")
            continue
        metadata_columns = (
            "analysis_label", "trait_ontology_id", "trait_ontology_label",
            "stored_effect_scale", "sample_size_kind", "sample_size_scope",
            "sample_size", "n_cases", "n_controls", "assigned_ancestry",
            "ancestry_assignment_method", "original_effect_scale", "original_sd",
            "original_sd_method",
            *(column for column in row if column.startswith("ancestry_prop_")),
        )
        for column in metadata_columns:
            if store_row.get(column, "") != row.get(column, ""):
                errors.append(
                    f"{analysis_id}: built {column}={store_row.get(column)!r} "
                    f"!= manifest {row.get(column)!r}"
                )
    return errors


def main() -> None:
    # opengwasdb reports its build phases through the logging module; without a
    # handler those records are discarded and the build log holds only this
    # script's own output. Records go to stderr so the JSON report on stdout
    # stays machine-readable.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    release_dir = Path(args.release_dir).resolve()
    root = repo_root(release_dir)
    release = read_release_yaml(release_dir / "release.yaml")
    build = read_release_yaml(release_dir / "build.yaml")
    rows = [row for row in read_tsv(release_dir / "analyses.tsv") if row.get("exclude_from_build") != "true"]

    store_dir = (
        Path(args.store_dir).resolve()
        if args.store_dir
        else resolve_path(root, require_text(build, "artifacts", "store_uri"))
    )

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        builder_manifest_path = Path(tmp) / "manifest.tsv"
        write_builder_manifest(rows, builder_manifest_path)
        result = build_dense_from_vcf_manifest(
            builder_manifest_path,
            store_dir,
            store_id=require_text(release, "store_family_id"),
            release_id=require_text(release, "family_release_id"),
            overwrite=args.overwrite,
            n_workers=args.workers,
        )
    build_wall_seconds = time.monotonic() - started

    # Read-back: query one known Analysis and confirm finite association
    # statistics, plus that the *resolved* stored_effect_scale (not a
    # VCF-header re-derivation -- opengwasdb#14) and passthrough
    # Analytical Metadata (analysis_label, ADR 0034) actually made it in.
    binary_probe_id = first_probe(rows, "log_or")
    quantitative_probe_id = next(
        (row["analysis_id"] for row in rows if row["stored_effect_scale"] != "log_or"),
        "",
    )
    probe_ids = [probe for probe in (binary_probe_id, quantitative_probe_id) if probe]
    query = query_store(store_dir)
    try:
        associations = {
            probe: query.analysis(probe, observed_only=True)
            for probe in probe_ids
        }
        analyses_table = query.analyses_table()
    finally:
        query.close()

    finite_by_probe = {
        probe: int(sum(1 for z in assoc["z"] if math.isfinite(z)))
        for probe, assoc in associations.items()
    }
    store_by_id = {r["analysis_id"]: r for r in analyses_table.values()}
    store_validation = validate_store(store_dir)

    report = {
        "store_uri": str(store_dir),
        "n_analyses": result.n_analyses,
        "n_variants": result.n_variants,
        "source_download_bytes": sum(Path(row["source_file"]).stat().st_size for row in rows),
        "store_bytes": directory_size(store_dir),
        "build_wall_seconds": f"{build_wall_seconds:.3f}",
        "peak_rss_kib": max(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        ),
        "binary_probe_analysis_id": binary_probe_id,
        "binary_probe_n_associations": len(associations.get(binary_probe_id, {}).get("z", [])),
        "binary_probe_n_finite_z": finite_by_probe.get(binary_probe_id, 0),
        "quantitative_probe_analysis_id": quantitative_probe_id,
        "quantitative_probe_n_associations": len(
            associations.get(quantitative_probe_id, {}).get("z", [])
        ),
        "quantitative_probe_n_finite_z": finite_by_probe.get(quantitative_probe_id, 0),
        "store_validation_status": "passed" if store_validation.ok else "failed",
    }
    failures = []
    for probe_id, n_finite in finite_by_probe.items():
        if n_finite == 0:
            failures.append(f"{probe_id}: zero finite association statistics in built store")
    failures.extend(f"store validation: {error}" for error in store_validation.errors)

    # Confirm shared-core columns the manifest actually populated (resolved
    # stored_effect_scale -- opengwasdb#14 -- plus analysis_label/
    # trait_ontology_id/label, ADR 0034) made it into the built store's own
    # analyses.tsv for every Analysis, not just the probe. Dense's builder is
    # documented to carry these through (unlike Ragged's -- opengwasdb#83),
    # but confirm it rather than assume it.
    failures.extend(metadata_mismatch_errors(rows, store_by_id))

    report_path = release_dir / "sidecars" / "build_report.tsv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=list(report), lineterminator="\n")
        writer.writeheader()
        writer.writerow(report)

    build_status = "failed" if failures else "passed"
    merge_validation_yaml(
        release_dir / "validation.yaml",
        validator_name="resources/generators/opengwas-gwas-vcf-dense/build-store.py",
        updated_checks={
            "schema": "passed",
            "files": build_status,
            "reader_smoke_test": build_status,
            "store": build_status,
        },
        updated_reports={"build_report": str(report_path.relative_to(release_dir))},
        new_warnings=failures,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    if failures:
        raise SystemExit("Store build validation failed:\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
