#!/usr/bin/env python3
"""Subset a real BESD source, build a Ragged Store from it via opengwasdb's
existing `build_ragged_from_besd()`, and emit the release bundle (issue #67).

Unlike this registry's GWAS-VCF/GWAS-SSF/FinnGen generators, there is no
per-Analysis metadata resolution here: BESD carries no case/control counts,
sample size, or effect-scale concept, and `build_ragged_from_besd()` derives
one Analysis per probe straight from the `.epi` file. So this script is a
single pass -- subset, build, validate, report -- rather than the staged
discover/select/derive/emit/accept pipeline heavier Source Formats need.

Run from the repository root:
    python3 resources/generators/eqtlgen-besd-ragged/generate.py \
        --config=families/eqtlgen-cis-pilot/generators/config-pilot-10.yaml
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from resources.lib.release_yaml import (  # noqa: E402
    get,
    merge_validation_yaml,
    read_release_yaml,
    repo_root,
    require_text,
    resolve_path,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from subset_besd import write_besd_subset  # noqa: E402

from opengwasdb.layouts.ragged.build_besd import build_ragged_from_besd  # noqa: E402
from opengwasdb.query import query_store  # noqa: E402
from opengwasdb.validation import validate_store  # noqa: E402


def _dump_yaml(value: object, indent: int = 0) -> list[str]:
    """Write the same block-style YAML subset `release_yaml.parse_yaml_subset`
    reads back (2-space indents, `null`/quoted-or-bare scalars, `- ` block
    sequences) -- this repository's Python generators deliberately avoid a
    PyYAML dependency (see `release_yaml.py`'s own module docstring)."""
    pad = " " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, dict) and val:
                lines.append(f"{pad}{key}:")
                lines.extend(_dump_yaml(val, indent + 2))
            elif isinstance(val, list) and val:
                lines.append(f"{pad}{key}:")
                for item in val:
                    if isinstance(item, dict):
                        item_lines = _dump_yaml(item, indent + 4)
                        lines.append(f"{pad}  - " + item_lines[0].strip())
                        lines.extend(item_lines[1:])
                    else:
                        lines.append(f"{pad}  - {_dump_scalar(item)}")
            elif isinstance(val, dict | list):  # empty
                lines.append(f"{pad}{key}: {'{}' if isinstance(val, dict) else '[]'}")
            else:
                lines.append(f"{pad}{key}: {_dump_scalar(val)}")
    return lines


def _dump_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text == "" or any(c in text for c in ":#\n") or text != text.strip():
        return json.dumps(text)
    return text


def write_yaml_file(value: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_dump_yaml(value)) + "\n", encoding="utf-8")


def build_yaml_document(
    *,
    store_family_id: str,
    family_release_id: str,
    association_coverage: str,
    source_genome_build: str,
    source_dir: Path,
    artifact_root: str,
    artifact_subdir: str,
    store_dir: Path,
) -> dict:
    """This release's executable `build.yaml` (issue #97, ADR 0022).

    Factorised out of `main()` so the emitted schema can be asserted against a
    migrated release's `build.yaml` without running a build.
    """
    return {
        "store_family_id": store_family_id,
        "family_release_id": family_release_id,
        "store_layout": "ragged-observed",
        "completion_state": "observed-only",
        "build": {
            "command": "build-ragged-besd",
            "arguments": {"store-id": store_family_id, "release-id": family_release_id},
        },
        "source": {
            "root": str(source_dir),
            "analyses": "analyses.tsv",
            "source_format": "besd",
            "source_reader_capability": None,
        },
        "normalisation": {
            "target_reference_assembly": "GRCh38",
            "liftover": "hg19-to-hg38" if source_genome_build != "hg38" else "none",
        },
        # docs/release-metadata-schema.md marks effects.stored_effect_scale
        # required, but BESD/molecular-QTL Analyses carry no effect-scale
        # concept for build_ragged_from_besd() to declare (see the
        # analyses.tsv comment above) -- recorded as null rather than
        # silently omitting the whole block, the same "never fabricate,
        # never silently drop" choice checks.schema=not_run makes below.
        "effects": {"stored_effect_scale": None},
        "shape": {"association_coverage": association_coverage},
        "validation": {"required": True},
        "artifacts": {
            "artifact_root": artifact_root,
            "release_subdir": artifact_subdir,
            "source_dir": str(source_dir),
            "store_uri": str(store_dir),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


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
    config_path = Path(args.config).resolve()
    root = repo_root(config_path.parent)
    cfg = read_release_yaml(config_path)

    store_family_id = require_text(cfg, "store_family_id")
    family_release_id = require_text(cfg, "family_release_id")
    besd_prefix = require_text(cfg, "source", "besd_prefix")
    source_genome_build = require_text(cfg, "source", "source_genome_build")
    tissue = require_text(cfg, "source", "tissue")
    probe_ids = [str(p) for p in get(cfg, "selection", "probe_ids", default=[])]
    if not probe_ids:
        raise SystemExit("config selection.probe_ids is empty")

    release_dir = resolve_path(root, require_text(cfg, "output", "release_dir"))
    artifact_root = require_text(cfg, "output", "artifact_root")
    artifact_subdir = require_text(cfg, "output", "artifact_subdir")
    artifact_dir = Path(artifact_root) / artifact_subdir
    source_dir = artifact_dir / "source"
    store_dir = artifact_dir / "store" / f"ragged__{store_family_id}__{family_release_id}"

    release_dir.mkdir(parents=True, exist_ok=True)
    (release_dir / "sidecars").mkdir(parents=True, exist_ok=True)

    # ── 1. Subset the real BESD source into artifact storage ──────────────────
    print(f"Subsetting {besd_prefix} -> {source_dir} ({len(probe_ids)} probes) ...")
    subset_prefix = source_dir / family_release_id
    subset_result = write_besd_subset(besd_prefix, subset_prefix, probe_ids)
    print(
        f"Subset: {subset_result.n_probes} probes, {subset_result.n_snps} SNPs, "
        f"{subset_result.n_associations} associations"
    )

    # ── 2. Build the real Ragged Store ─────────────────────────────────────────
    print(f"Building Ragged Store at {store_dir} ...")
    build_result = build_ragged_from_besd(
        subset_prefix,
        store_dir,
        store_id=store_family_id,
        release_id=family_release_id,
        tissue=tissue,
        source_build=source_genome_build,
        overwrite=args.overwrite,
    )

    # ── 3. Copy the store's own analyses.tsv into the release bundle for
    #        provenance/inspection. opengwasdb.model.analyses.validate_analyses()
    #        (this registry's usual pre-build "schema" gate for a
    #        registry-authored manifest, e.g. gwas-ssf-ragged/gwas-ssf-hybrid's
    #        analyses.tsv before it is handed to opengwasdb) does not apply here:
    #        build_ragged_from_besd() derives Analyses on the fly from the
    #        source .epi file, so there is no registry-authored, pre-build
    #        manifest for that check to validate in the first place -- BESD
    #        molecular-QTL Analyses carry no case/control counts, sample size,
    #        or effect-scale concept for REQUIRED_COLUMNS to require, and
    #        `build_ragged_from_besd` never supplies `stored_effect_scale`,
    #        `sample_size_kind`, `sample_size_scope`, `sample_size`,
    #        `original_effect_scale`, or `ancestry_assignment_method` to the
    #        `molecular_analysis()` records it constructs -- verified by
    #        reading that function directly, not assumed. The correct
    #        acceptance gate for a *built* store is opengwasdb's own
    #        `validate_store()` (step 4 below), which does tolerate these
    #        columns being blank.
    analyses_path = release_dir / "analyses.tsv"
    analyses_path.write_bytes((store_dir / "analyses.tsv").read_bytes())

    # ── 4. Validate the built store itself ─────────────────────────────────────
    store_validation = validate_store(store_dir)

    # ── 5. Read-back smoke test: two probes, including one deliberately
    #        MHC-adjacent (TNF) locus ──────────────────────────────────────────
    probe_analysis_ids = [f"{pid}::{tissue}" for pid in probe_ids]
    query = query_store(store_dir)
    try:
        smoke_probes = probe_analysis_ids[:2]
        smoke_results = {pid: query.analysis(pid, observed_only=True) for pid in smoke_probes}
    finally:
        query.close()

    warnings: list[str] = []
    if not store_validation.ok:
        warnings.extend(f"store validation: {e}" for e in store_validation.errors)
    for pid, assoc in smoke_results.items():
        n_finite = int(sum(1 for z in assoc["z"] if z == z and abs(z) < float("inf")))
        if n_finite == 0:
            warnings.append(f"{pid}: zero finite association statistics in built store")

    # ── 6. Sidecars ─────────────────────────────────────────────────────────────
    selection_path = release_dir / "sidecars" / "selection.tsv"
    with selection_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, delimiter="\t", lineterminator="\n",
            fieldnames=["selection_rank", "analysis_id", "probe_id", "tissue"],
        )
        writer.writeheader()
        for rank, pid in enumerate(probe_ids, start=1):
            writer.writerow({
                "selection_rank": rank, "analysis_id": f"{pid}::{tissue}",
                "probe_id": pid, "tissue": tissue,
            })

    report = {
        "store_uri": str(store_dir),
        "n_probes": build_result.n_analyses,
        "n_variants": build_result.n_variants,
        "n_associations": build_result.n_associations,
        "source_genome_build": source_genome_build,
        "target_reference_assembly": "GRCh38",
        "smoke_probe_ids": smoke_probes,
        "smoke_n_associations": {pid: len(a["z"]) for pid, a in smoke_results.items()},
    }
    report_path = release_dir / "sidecars" / "build_report.tsv"
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", lineterminator="\n", fieldnames=list(report))
        writer.writeheader()
        writer.writerow({k: json.dumps(v) if isinstance(v, dict | list) else v for k, v in report.items()})

    # ── 7. release.yaml / build.yaml / validation.yaml ─────────────────────────
    from datetime import UTC, datetime

    from resources.lib.build_environment import capture as capture_build_environment

    write_yaml_file(
        {
            "metadata_schema_version": 1,
            "store_family_id": store_family_id,
            "family_release_id": family_release_id,
            # "built" once the store has actually been built and passes
            # opengwasdb's own validate_store() (this script always builds
            # before writing release.yaml, so this reflects the just-completed
            # build's outcome, not an aspirational default); "candidate"
            # otherwise, matching this registry's documented release lifecycle.
            "status": "built" if store_validation.ok else "candidate",
            "source_collection_id": require_text(cfg, "source_collection_id"),
            "source_snapshot_id": require_text(cfg, "source", "source_snapshot_id"),
            "source_snapshot": {
                "besd_prefix": besd_prefix,
                "source_genome_build": source_genome_build,
            },
            "release_kind": require_text(cfg, "release_kind"),
            "association_coverage": require_text(cfg, "association_coverage"),
            "description": require_text(cfg, "description"),
            "created_at": datetime.now(UTC).isoformat(),
            "generator": {
                "name": "resources/generators/eqtlgen-besd-ragged/generate.py",
                "command": f"python3 resources/generators/eqtlgen-besd-ragged/generate.py --config={args.config}",
            },
            "build_environment": capture_build_environment(root),
            "sidecars": {"selection": "sidecars/selection.tsv", "build_report": "sidecars/build_report.tsv"},
            "notes": get(cfg, "notes", default="") or "",
        },
        release_dir / "release.yaml",
    )
    write_yaml_file(
        build_yaml_document(
            store_family_id=store_family_id,
            family_release_id=family_release_id,
            association_coverage=require_text(cfg, "association_coverage"),
            source_genome_build=source_genome_build,
            source_dir=source_dir,
            artifact_root=artifact_root,
            artifact_subdir=artifact_subdir,
            store_dir=store_dir,
        ),
        release_dir / "build.yaml",
    )

    build_status = "passed_with_warnings" if warnings else "passed"
    merge_validation_yaml(
        release_dir / "validation.yaml",
        validator_name="resources/generators/eqtlgen-besd-ragged/generate.py",
        updated_checks={
            # not_run, not fabricated "passed": opengwasdb.model.analyses.
            # validate_analyses() validates a registry-authored, pre-build
            # manifest, which BESD has none of (see the comment above the
            # analyses.tsv copy) -- there is nothing for this check to run
            # against for this Source Format.
            "schema": "not_run",
            "files": build_status,
            "reader_smoke_test": build_status,
            "store": "passed" if store_validation.ok else "failed",
        },
        updated_reports={"build_report": "sidecars/build_report.tsv", "selection": "sidecars/selection.tsv"},
        new_warnings=warnings,
    )

    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
