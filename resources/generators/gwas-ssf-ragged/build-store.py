#!/usr/bin/env python3
"""Build and smoke-query a ragged GWAS-SSF release bundle with OpenGWASDB."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from resources.lib.release_yaml import (  # noqa: E402
    merge_validation_yaml,
    read_release_yaml,
    read_tsv,
    repo_root,
    require_text,
    resolve_path,
)

from opengwasdb.layouts.ragged.build_ssf import build_ragged_from_ssf
from opengwasdb.layouts.ragged.zarr_csr import RaggedCSRReader
from opengwasdb.model.manifest import StoreManifest


def read_store_analyses(store_dir: Path) -> dict[str, dict[str, str]]:
    """Read a built ragged store's own analyses.tsv (ADR 0034, opengwasdb#69:
    no separate Trait axis reader exists any more -- analyses.tsv at the
    store root is the sole record), keyed by analysis_id."""
    with (store_dir / "analyses.tsv").open(newline="", encoding="utf-8") as fh:
        return {row["analysis_id"]: row for row in csv.DictReader(fh, delimiter="\t")}


def shared_core_passthrough_warnings(
    manifest_rows: list[dict[str, str]], store_analyses: dict[str, dict[str, str]]
) -> list[str]:
    """Confirm shared-core columns the registry manifest actually populated
    (analysis_label, trait_ontology_id/label) made it into the built store's
    own analyses.tsv, rather than trusting that a successful build implies a
    complete one -- opengwasdb#83 found opengwasdb.layouts.ragged.build_ssf
    silently drops these (and several pre-ADR-0034 fields) with no error."""
    warnings = []
    for row in manifest_rows:
        store_row = store_analyses.get(row["analysis_id"], {})
        for column in ("analysis_label", "trait_ontology_id", "trait_ontology_label"):
            if row.get(column) and not store_row.get(column):
                warnings.append(
                    f"{row['analysis_id']}: {column} present in the manifest but missing from the "
                    "built store's analyses.tsv (see opengwasdb#83)"
                )
    return warnings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--store-dir")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def variant_lookup(store: Path) -> dict[int, tuple[str, int]]:
    out: dict[int, tuple[str, int]] = {}
    with gzip.open(store / "variants.tsv.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            out[int(fields[2])] = (fields[0], int(fields[1]))
    return out


def first_complete_manifest_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    missing = [r["analysis_id"] for r in rows if not r.get("checksum") or not r.get("size_bytes")]
    if missing:
        preview = ", ".join(missing[:10])
        raise SystemExit(
            f"{path} still has {len(missing)} rows without filtered-file checksums: {preview}"
        )
    return rows


def associations_in_region(
    reader: RaggedCSRReader,
    variants: dict[int, tuple[str, int]],
    analysis_index: int,
    chromosome: str,
    start: int,
    end: int,
) -> dict[str, object]:
    assoc = reader.get_analysis(analysis_index)
    hits = [
        (int(vi), float(z), float(se))
        for vi, z, se in zip(assoc.variant_index, assoc.z, assoc.se)
        if variants[int(vi)][0] == chromosome and start <= variants[int(vi)][1] <= end
    ]
    top = max(hits, key=lambda row: abs(row[1]), default=None)
    if top is None:
        return {
            "observed": 0,
            "top_variant": "",
            "top_position": "",
            "top_z": "",
            "top_se": "",
        }
    top_chrom, top_pos = variants[top[0]]
    return {
        "observed": len(hits),
        "top_variant": f"{top_chrom}:{top_pos}",
        "top_position": top_pos,
        "top_z": round(top[1], 4),
        "top_se": round(top[2], 6),
    }


def choose_region(regions: list[dict[str, str]], kind: str) -> dict[str, str] | None:
    candidates = [
        row for row in regions
        if row.get("region_kind") == kind and int(row.get("n_variants_retained") or 0) > 0
    ]
    return candidates[0] if candidates else None


def du_bytes(path: Path) -> int:
    result = subprocess.run(
        ["du", "-sb", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    return int(result.stdout.split()[0])


_STATUS_RANK = {"not_run": 0, "passed": 1, "passed_with_warnings": 2, "failed": 3}


def read_previous_checks_and_warnings(path: Path) -> tuple[dict[str, str], list[str]]:
    """Read `checks.*` and `warnings` from an existing validation.yaml.

    This intentionally only understands the flat/one-level-nested shape this
    pipeline writes (see write_yaml_file() in generate.R), so that the
    ancestry/effect_scale/sd_estimation checks and warnings recorded by the
    generator's `--mode=effect-scale` stage are preserved rather than
    clobbered when the build step re-writes validation.yaml.
    """
    if not path.exists():
        return {}, []
    checks: dict[str, str] = {}
    warnings: list[str] = []
    section = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line == "checks:":
            section = "checks"
            continue
        if raw_line.startswith("warnings:"):
            section = "warnings"
            continue
        if raw_line and not raw_line.startswith(" "):
            section = None
            continue
        if section == "checks" and ":" in raw_line:
            key, value = raw_line.strip().split(":", 1)
            checks[key.strip()] = value.strip().strip("'\"")
        elif section == "warnings" and raw_line.strip().startswith("- "):
            warnings.append(raw_line.strip()[2:].strip().strip("'\""))
    return checks, warnings


def write_validation_yaml(path: Path, report_path: Path, status: str, warnings: list[str]) -> None:
    previous_checks, previous_warnings = read_previous_checks_and_warnings(path)
    ancestry_check = previous_checks.get("ancestry", "not_run")
    effect_scale_check = previous_checks.get("effect_scale", "not_run")
    sd_estimation_check = previous_checks.get("sd_estimation", "not_run")
    all_warnings = list(dict.fromkeys([*previous_warnings, *warnings]))

    build_checks = {"schema": "passed", "files": "passed", "reader_smoke_test": "passed", "sparse_regions": "passed"}
    ranked = [status, ancestry_check, effect_scale_check, sd_estimation_check, *build_checks.values()]
    overall_status = max(ranked, key=lambda value: _STATUS_RANK.get(value, 0))

    warning_lines = "\n".join(f"  - {warning}" for warning in all_warnings) if all_warnings else "[]"
    validated_at = datetime.now(UTC).isoformat()
    path.write_text(
        "\n".join([
            f"status: {overall_status}",
            f"validated_at: {validated_at}",
            "validator:",
            "  name: resources/generators/gwas-ssf-ragged/build-store.py",
            "  version: null",
            "checks:",
            f"  schema: {build_checks['schema']}",
            f"  files: {build_checks['files']}",
            f"  reader_smoke_test: {build_checks['reader_smoke_test']}",
            f"  ancestry: {ancestry_check}",
            f"  effect_scale: {effect_scale_check}",
            f"  sd_estimation: {sd_estimation_check}",
            f"  sparse_regions: {build_checks['sparse_regions']}",
            "reports:",
            f"  build_report: {report_path.relative_to(path.parent)}",
            f"warnings: {warning_lines}",
            "errors: []",
            "",
        ]),
        encoding="utf-8",
    )


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
    analyses_path = release_dir / "analyses.tsv"
    rows = first_complete_manifest_rows(analyses_path)

    filtered_dir = resolve_path(root, require_text(build, "artifacts", "filtered_dir"))
    if not filtered_dir.exists():
        raise SystemExit(f"Filtered directory does not exist: {filtered_dir}")
    store_id = require_text(release, "store_key") or (
        f"{require_text(release, 'store_family_id')}__{require_text(release, 'family_release_id')}"
    )
    store_dir = (
        Path(args.store_dir).resolve()
        if args.store_dir
        else resolve_path(root, require_text(build, "artifacts", "store_uri"))
    )
    # opengwasdb's StoredEffectScale enum now uses this registry's own
    # vocabulary directly (sd/log_or/log_hazard) -- no translation needed.
    stored_effect_scale = require_text(build, "effects", "stored_effect_scale") or "sd"

    build_started = time.perf_counter()
    result = build_ragged_from_ssf(
        analyses_path,
        filtered_dir,
        store_dir,
        store_id=store_id,
        release_id=require_text(release, "family_release_id"),
        stored_effect_scale=stored_effect_scale,
        overwrite=args.overwrite,
    )
    build_seconds = round(time.perf_counter() - build_started, 3)

    manifest = StoreManifest.load(store_dir)
    reader = RaggedCSRReader(store_dir)
    variants = variant_lookup(store_dir)
    trait_by_id = read_store_analyses(store_dir)
    regions = read_tsv(release_dir / "sidecars" / "sparse_regions.tsv")
    filter_summary = read_tsv(release_dir / "sidecars" / "filter_summary.tsv")

    cis_region = choose_region(regions, "cis")
    trans_region = choose_region(regions, "significant_trans") or choose_region(regions, "suggestive_trans")
    if cis_region is None and trans_region is None:
        raise SystemExit("No non-empty cis or trans region found in sparse_regions.tsv")

    # Gene-target-less Store Families (e.g. small-molecule metabolomics) have
    # no cis window by design (docs/release-metadata-schema.md's documented
    # family-shape convention) and so no "cis" rows in sparse_regions.tsv --
    # the cis smoke test only runs when a cis region actually exists.
    cis_fields: dict[str, object] = {}
    expected_associations = sum(int(row["retained_rows"]) for row in filter_summary if row.get("status") == "ok")
    warnings = shared_core_passthrough_warnings(rows, trait_by_id)
    if cis_region is not None:
        cis_trait = trait_by_id[cis_region["analysis_id"]]
        cis_query = associations_in_region(
            reader,
            variants,
            int(cis_trait["analysis_index"]),
            cis_region["chromosome"],
            int(cis_region["start"]),
            int(cis_region["end"]),
        )
        cis_expected = int(cis_region.get("n_variants_retained") or 0)
        if cis_query["observed"] != cis_expected:
            warnings.append(f"cis query observed {cis_query['observed']} associations but sidecar records {cis_expected}")
        cis_fields = {
            "cis_analysis_id": cis_region["analysis_id"],
            "cis_region_id": cis_region["region_id"],
            "cis_region": f"{cis_region['chromosome']}:{cis_region['start']}-{cis_region['end']}",
            "cis_sidecar_associations": cis_expected,
            "cis_query_associations": cis_query["observed"],
            "cis_top_variant": cis_query["top_variant"],
            "cis_top_z": cis_query["top_z"],
        }

    trans_fields: dict[str, object] = {}
    if trans_region is not None:
        trans_trait = trait_by_id[trans_region["analysis_id"]]
        trans_query = associations_in_region(
            reader,
            variants,
            int(trans_trait["analysis_index"]),
            trans_region["chromosome"],
            int(trans_region["start"]),
            int(trans_region["end"]),
        )
        trans_expected = int(trans_region.get("n_variants_retained") or 0)
        if trans_query["observed"] != trans_expected:
            warnings.append(f"trans query observed {trans_query['observed']} associations but sidecar records {trans_expected}")
        trans_fields = {
            "trans_analysis_id": trans_region["analysis_id"],
            "trans_region_id": trans_region["region_id"],
            "trans_region_kind": trans_region["region_kind"],
            "trans_region": f"{trans_region['chromosome']}:{trans_region['start']}-{trans_region['end']}",
            "trans_sidecar_associations": trans_expected,
            "trans_query_associations": trans_query["observed"],
            "trans_top_variant": trans_query["top_variant"],
            "trans_top_z": trans_query["top_z"],
        }

    if result.n_associations != expected_associations:
        warnings.append(
            f"store has {result.n_associations} associations but filter summary records {expected_associations}"
        )

    report = {
        "store_uri": require_text(build, "artifacts", "store_uri"),
        "filtered_uri": require_text(build, "artifacts", "filtered_dir"),
        "store_bytes": du_bytes(store_dir),
        "filtered_bytes": du_bytes(filtered_dir),
        "download_filter_seconds": round(
            sum(float(row["total_seconds"]) for row in filter_summary if row.get("status") == "ok"),
            3,
        ),
        "build_seconds": build_seconds,
        "end_to_end_seconds": round(
            sum(float(row["total_seconds"]) for row in filter_summary if row.get("status") == "ok")
            + build_seconds,
            3,
        ),
        "layout": manifest.primary_layout.value,
        "coverage": manifest.association_coverage.value,
        "completion_state": manifest.completion_state.value,
        "n_manifest_rows": len(rows),
        "n_analyses": result.n_analyses,
        "n_variants": result.n_variants,
        "n_associations": result.n_associations,
        "filter_summary_retained_rows": expected_associations,
        **cis_fields,
        **trans_fields,
        "warnings": "; ".join(warnings) if warnings else "none",
    }
    report_path = release_dir / "sidecars" / "build_report.tsv"
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=list(report), lineterminator="\n")
        writer.writeheader()
        writer.writerow(report)
    build_status = "passed_with_warnings" if warnings else "passed"
    merge_validation_yaml(
        release_dir / "validation.yaml",
        validator_name="resources/generators/gwas-ssf-ragged/build-store.py",
        default_checks={"ancestry": "not_run", "effect_scale": "not_run", "sd_estimation": "not_run"},
        updated_checks={
            "schema": "passed", "files": "passed", "reader_smoke_test": "passed", "sparse_regions": build_status,
        },
        updated_reports={"build_report": str(report_path.relative_to(release_dir))},
        new_warnings=warnings,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
