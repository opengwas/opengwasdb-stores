#!/usr/bin/env python3
"""Build and smoke-query a Hybrid gwas-ssf-hybrid release bundle with
OpenGWASDB. Structured like resources/generators/opengwas-gwas-vcf-dense/
build-store.py, since opengwasdb.layouts.hybrid.build:build_hybrid_from_vcf_manifest
shares its manifest shape with the Dense builder (trait_id/file_path/
trait_name/n, plus the opengwasdb#84/#85 source_reader_capability/
source_assembly columns) -- this script's job is the same registry-column ->
builder-column translation, plus supplying the Dense Component's reference
panel (this release's build.yaml `reference_resources` entry with
kind: hybrid_dense_panel).

Read-back verifies both halves of the Hybrid layout actually ran: the Dense
Component (on-panel fill) via a probe Analysis's finite association
statistics, and the Ragged Overflow (off-panel) via HybridBuildResult's own
n_off_panel/n_overflow counts -- a Hybrid build that routes everything
on-panel (or nothing) would silently mean the Overflow path was never
exercised, which this reports as a warning rather than assumes away.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from resources.lib.release_yaml import (  # noqa: E402
    get,
    merge_validation_yaml,
    read_release_yaml,
    read_tsv,
    repo_root,
    require_text,
    resolve_path,
)

from opengwasdb.layouts.hybrid.build import build_hybrid_from_vcf_manifest
from opengwasdb.query import query_store


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--store-dir")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def find_reference_panel(build: dict, root: Path) -> Path:
    for resource in get(build, "reference_resources", default=[]) or []:
        if isinstance(resource, dict) and resource.get("kind") == "hybrid_dense_panel":
            return resolve_path(root, str(resource["location"]))
    raise SystemExit(
        "build.yaml has no reference_resources entry with kind: hybrid_dense_panel "
        "(the Dense Component's reference panel)"
    )


def write_builder_manifest(
    rows: list[dict[str, str]], source_reader_capability: str, source_assembly: str, out_path: Path
) -> None:
    """Translate this registry's analyses.tsv column names into the shape
    opengwasdb.layouts.dense.build_vcf._read_manifest() (shared by the Hybrid
    builder) actually requires."""
    fieldnames = [
        "trait_id", "file_path", "trait_name", "n", "stored_effect_scale",
        "original_sd_method", "original_sd", "assigned_ancestry",
        "trait_ontology_id", "trait_ontology_label",
        "license", "publication_doi", "publication_pmid", "consortium", "first_author",
        "source_reader_capability", "source_assembly",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "trait_id": row["analysis_id"],
                "file_path": row["source_file"],
                "trait_name": row["analysis_label"],
                "n": row["sample_size"],
                "stored_effect_scale": row["stored_effect_scale"],
                "original_sd_method": row["original_sd_method"],
                "original_sd": row.get("original_sd", ""),
                "assigned_ancestry": row.get("assigned_ancestry", ""),
                "trait_ontology_id": row.get("trait_ontology_id", ""),
                "trait_ontology_label": row.get("trait_ontology_label", ""),
                "license": row.get("license", ""),
                "publication_doi": row.get("publication_doi", ""),
                "publication_pmid": row.get("publication_pmid", ""),
                "consortium": row.get("consortium", ""),
                "first_author": row.get("first_author", ""),
                "source_reader_capability": source_reader_capability,
                "source_assembly": source_assembly,
            })


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
    release_dir = Path(args.release_dir).resolve()
    root = repo_root(release_dir)
    release = read_release_yaml(release_dir / "release.yaml")
    build = read_release_yaml(release_dir / "build.yaml")
    all_rows = read_tsv(release_dir / "analyses.tsv")
    excluded_rows = [r for r in all_rows if r.get("exclude_from_build") == "true"]
    rows = [r for r in all_rows if r.get("exclude_from_build") != "true"]
    if not rows:
        raise SystemExit("every analysis in analyses.tsv is excluded from build (exclude_from_build=true)")

    store_dir = (
        Path(args.store_dir).resolve()
        if args.store_dir
        else resolve_path(root, require_text(build, "artifacts", "store_uri"))
    )
    reference_panel = find_reference_panel(build, root)
    source_reader_capability = require_text(build, "source", "source_reader_capability")
    source_assembly = require_text(build, "normalisation", "source_assembly")

    with tempfile.TemporaryDirectory() as tmp:
        builder_manifest_path = Path(tmp) / "manifest.tsv"
        write_builder_manifest(rows, source_reader_capability, source_assembly, builder_manifest_path)
        result = build_hybrid_from_vcf_manifest(
            builder_manifest_path,
            store_dir,
            reference_panel=reference_panel,
            store_id=require_text(release, "store_family_id"),
            release_id=require_text(release, "family_release_id"),
            overwrite=args.overwrite,
        )

    # Read-back: query one known Analysis and confirm finite association
    # statistics, plus that Analytical Metadata (analysis_label, ontology,
    # attribution, case/control counts) actually made it into the built
    # store's own analyses.tsv -- never assumed from the registry manifest
    # alone.
    probe_id = rows[0]["analysis_id"]
    query = query_store(store_dir)
    try:
        assoc = query.analysis(probe_id, observed_only=True)
        analyses_table = query.analyses_table()
    finally:
        query.close()

    n_finite = int(sum(1 for z in assoc["z"] if z == z and abs(z) < float("inf")))
    store_by_id = {r["analysis_id"]: r for r in analyses_table.values()}
    probe_row = store_by_id[probe_id]

    report = {
        "store_uri": str(store_dir),
        "n_analyses": result.n_analyses,
        "n_variants": result.n_variants,
        "n_panel": result.n_panel,
        "n_off_panel": result.n_off_panel,
        "n_overflow_associations": result.n_overflow,
        "n_excluded": len(excluded_rows),
        "probe_analysis_id": probe_id,
        "probe_n_associations": len(assoc["z"]),
        "probe_n_finite_z": n_finite,
        "probe_stored_effect_scale": probe_row.get("stored_effect_scale"),
        "probe_analysis_label": probe_row.get("analysis_label"),
    }
    warnings = [
        f"{row['analysis_id']}: excluded from build -- {row.get('inclusion_reason', '')}"
        for row in excluded_rows
    ]
    if n_finite == 0:
        warnings.append(f"{probe_id}: zero finite association statistics in built store")
    if result.n_off_panel == 0 or result.n_overflow == 0:
        warnings.append(
            f"n_off_panel={result.n_off_panel}, n_overflow_associations={result.n_overflow} "
            "-- the Ragged Overflow component was never exercised by this build"
        )

    # Confirm shared-core columns the manifest actually populated made it into
    # the built store's own analyses.tsv for every Analysis, not just the
    # probe. opengwasdb.layouts.dense.build_vcf._manifest_row_to_analysis (the
    # Analysis-record constructor Hybrid shares with Dense) is not documented
    # to drop any of these, but confirm it rather than assume it -- the same
    # reasoning that caught opengwasdb#83's Ragged-builder gap applies here.
    passthrough_columns = (
        "analysis_label", "trait_ontology_id", "trait_ontology_label",
        "n_cases", "n_controls", "sample_size_kind", "sample_size_scope",
        "original_effect_scale",
    )
    for row in rows:
        store_row = store_by_id.get(row["analysis_id"], {})
        if store_row.get("stored_effect_scale") != row["stored_effect_scale"]:
            warnings.append(
                f"{row['analysis_id']}: built store stored_effect_scale="
                f"{store_row.get('stored_effect_scale')!r} != manifest-resolved "
                f"{row['stored_effect_scale']!r}"
            )
        for column in passthrough_columns:
            if row.get(column) and not store_row.get(column):
                warnings.append(
                    f"{row['analysis_id']}: {column} present in the manifest but missing from "
                    "the built store's analyses.tsv"
                )
        # ancestry_assignment_method is not a passthrough column at all: the
        # shared builder derives it from whether assigned_ancestry is
        # non-empty (af_assigned if so), rather than accepting the manifest's
        # own value -- confirmed directly against a real build, this
        # overwrites source_trusted_no_af with a fabricated af_assigned for
        # every row here, not merely dropping it.
        if (
            row.get("ancestry_assignment_method")
            and store_row.get("ancestry_assignment_method")
            and store_row["ancestry_assignment_method"] != row["ancestry_assignment_method"]
        ):
            warnings.append(
                f"{row['analysis_id']}: built store ancestry_assignment_method="
                f"{store_row['ancestry_assignment_method']!r} != manifest-declared "
                f"{row['ancestry_assignment_method']!r}"
            )

    report_path = release_dir / "sidecars" / "build_report.tsv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=list(report), lineterminator="\n")
        writer.writeheader()
        writer.writerow(report)

    build_status = "passed_with_warnings" if warnings else "passed"
    merge_validation_yaml(
        release_dir / "validation.yaml",
        validator_name="resources/generators/gwas-ssf-hybrid/build-store.py",
        updated_checks={"schema": "passed", "files": build_status, "reader_smoke_test": build_status},
        updated_reports={"build_report": str(report_path.relative_to(release_dir))},
        new_warnings=warnings,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
