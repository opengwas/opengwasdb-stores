"""Acquisition for a region-filtered release: filter, record, then delete the original (#300).

ADR 0036, clarified by #300: a release whose config declares a region policy
(``release_config.region_policy``) is acquired filtered, for scale. Each
downloaded original is filtered to the policy with the shared region filter
(``manifest_generation.region_filter``), which always keeps the QC panel; the
filtered file is what the Acquisition Manifest lists, and its lineage records
the original's location and sha256, the filter tool and the policy's
parameters. The original is deleted only after that row is durably recorded:
a failure to record leaves it in place.

The input is the Acquisition Manifest of the downloaded originals, as a
provider's downloader and manifest writer produce it. Each original's sha256 is
checked against that manifest before it is filtered. An Analysis acquisition
could not download stays an explained absence. A cis policy needs every
Analysis's target span, from the Source Metadata Table.

Re-running resumes, but only a run that is this one: before anything is
skipped, filtered or deleted, every Analysis the output manifest already
records must still be a ready original of this run, filtered from that
original (its location and sha256), to this run's region policy, QC panel
and target, into a filtered file that is still there with its recorded size
and sha256. Any difference refuses the run, naming each Analysis, and deletes
nothing. A recorded Analysis whose original survived an interrupted run then
has that original deleted.

``<manifest-stem>.meta.yaml`` (``acquisition_manifest.pass_lineage_path``)
records the run: the originals' manifest, the tool, the policy and each
filtered file's counts. Freeze checksums it into the inventory's provenance.

Usage (the release config names the policy, its QC panel and the Source
Metadata Table whose target spans a cis policy reads):
    python3 -m manifest_generation.region_filtered_acquisition \\
        --config <release config> --originals <originals' Acquisition Manifest> \\
        --out-dir <filtered files> --out <Acquisition Manifest> [--workers N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from manifest_generation import REPO_ROOT, region_filter
from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    AcquisitionEntry,
    AcquisitionManifest,
    load_acquisition_manifest,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import write_connector
from manifest_generation.region_filter import (
    QcPanel,
    RegionFilterError,
    RegionPolicy,
    TargetSpan,
    filter_file,
    load_qc_panel,
)
from manifest_generation.release_config import ReleaseConfig, load_release_config
from manifest_generation.source_metadata_table import load_source_metadata_table
from manifest_generation.source_inventory import InventoryError, atomic_write, sha256_file

#: The columns this acquisition writes.
COLUMNS: tuple[str, ...] = (
    "source_analysis_id", "status", "location", "bytes", "sha256", "source_url",
    "sidecar_location", "sidecar_url", "sidecar_bytes", "error",
    "original_location", "original_sha256", "reformat_tool", "region_policy",
)

TOOL_MODULE: str = "manifest_generation.region_filter"


class RegionFilteredAcquisitionError(InventoryError):
    """A region-filtered acquisition could not filter or record a file."""


def filter_tool() -> str:
    """The filter's identity in lineage: its module and the sha256 of its source."""
    return f"{TOOL_MODULE} sha256:{sha256_file(Path(region_filter.__file__))}"


def policy_record(policy: RegionPolicy, qc_panel: QcPanel, target: TargetSpan | None) -> dict[str, Any]:
    """The region-policy parameters one filtered file's lineage records."""
    record: dict[str, Any] = {
        "region_policy_id": policy.region_policy_id,
        "association_coverage": policy.association_coverage,
        "trans_flank_bp": policy.trans_flank_bp,
        "significant_p": policy.significant_p,
        "suggestive_p": policy.suggestive_p,
        "suggestive_lead_min_distance_bp": policy.suggestive_lead_min_distance_bp,
        "qc_panel": {"resource_id": qc_panel.resource_id, "sha256": qc_panel.sha256},
    }
    if policy.association_coverage == "cis_plus_signals":
        record["cis_flank_bp"] = policy.cis_flank_bp
        if target is not None:
            record["target"] = asdict(target)
    return record


def acquire_region_filtered(
    originals: AcquisitionManifest,
    *,
    policy: RegionPolicy,
    qc_panel: QcPanel,
    targets: Mapping[str, TargetSpan],
    out_dir: Path,
    manifest_path: Path,
    workers: int = 1,
) -> AcquisitionManifest:
    """Filter every ready original into ``out_dir``, recording each before deleting its original."""
    out_dir, manifest_path = Path(out_dir), Path(manifest_path)
    ready = [entry for entry in originals.entries if entry.ready]
    if policy.association_coverage == "cis_plus_signals":
        untargeted = [entry.source_analysis_id for entry in ready if entry.source_analysis_id not in targets]
        if untargeted:
            raise RegionFilteredAcquisitionError(
                f"region policy {policy.region_policy_id} keeps a cis flank, but "
                f"{len(untargeted)} Analysis/Analyses have no target span in the Source Metadata "
                f"Table: {', '.join(untargeted[:10])}"
            )

    rows: dict[str, dict[str, str]] = {
        entry.source_analysis_id: _carried(entry) for entry in originals.entries if not entry.ready
    }
    counts: dict[str, dict[str, int]] = {}
    if manifest_path.is_file():
        recorded = load_acquisition_manifest(manifest_path)
        resumed = {
            entry.source_analysis_id: dict(row)
            for entry, row in zip(recorded.entries, recorded.rows)
            if entry.lineage is not None
        }
        by_id = {entry.source_analysis_id: entry for entry in ready}
        problems = [
            f"{analysis_id}: {problem}"
            for analysis_id, row in resumed.items()
            for problem in _resume_problems(
                by_id.get(analysis_id), row,
                policy_record(policy, qc_panel, targets.get(analysis_id)),
            )
        ]
        if problems:
            raise RegionFilteredAcquisitionError(
                f"cannot resume the acquisition recorded in {manifest_path}; nothing was deleted:\n  - "
                + "\n  - ".join(problems)
            )
        rows.update(resumed)
        previous = pass_lineage_path(manifest_path)
        if previous.is_file():
            counts.update((yaml.safe_load(previous.read_text(encoding="utf-8")) or {}).get("filtered") or {})
        # Every recorded Analysis is this run's: an original an interrupted run
        # left behind may now go.
        for analysis_id in resumed:
            Path(by_id[analysis_id].location).unlink(missing_ok=True)

    tool = filter_tool()
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        (entry, out_dir / f"{entry.source_analysis_id}.filtered.tsv.gz", policy, qc_panel,
         targets.get(entry.source_analysis_id))
        for entry in ready
        if entry.source_analysis_id not in rows
    ]
    order = [entry.source_analysis_id for entry in originals.entries]

    def record(entry: AcquisitionEntry, filtered: Path, original_sha256: str, summary) -> None:
        rows[entry.source_analysis_id] = {
            **_carried(entry),
            "status": "ok",
            "location": str(filtered),
            "bytes": str(filtered.stat().st_size),
            "sha256": sha256_file(filtered),
            "error": "",
            "original_location": entry.location,
            "original_sha256": original_sha256,
            "reformat_tool": tool,
            "region_policy": json.dumps(
                policy_record(policy, qc_panel, targets.get(entry.source_analysis_id)),
                sort_keys=True, separators=(",", ":"),
            ),
        }
        counts[entry.source_analysis_id] = asdict(summary)
        _write_durably(manifest_path, [rows[analysis_id] for analysis_id in order if analysis_id in rows])
        _write_run_record(manifest_path, originals, tool, policy, qc_panel, counts)
        # Recorded and synced: only now may the original go.
        Path(entry.location).unlink()

    if workers > 1 and len(tasks) > 1:
        with Pool(min(workers, len(tasks))) as pool:
            for result in pool.imap_unordered(_filter_one, tasks):
                record(*result)
    else:
        for task in tasks:
            record(*_filter_one(task))
    if not rows:
        raise RegionFilteredAcquisitionError("the originals' Acquisition Manifest has no rows to acquire")
    _write_durably(manifest_path, [rows[analysis_id] for analysis_id in order if analysis_id in rows])
    return load_acquisition_manifest(manifest_path)


def _filter_one(task):
    entry, destination, policy, qc_panel, target = task
    original = Path(entry.location)
    original_sha256 = sha256_file(original)
    if original_sha256 != entry.sha256:
        raise RegionFilteredAcquisitionError(
            f"{entry.source_analysis_id}: original {original} has sha256 {original_sha256}, not the "
            f"{entry.sha256} its Acquisition Manifest recorded; it was not filtered or deleted"
        )
    try:
        summary = filter_file(original, destination, policy, qc_panel, target=target)
    except RegionFilterError as exc:
        raise RegionFilteredAcquisitionError(f"{entry.source_analysis_id}: {exc}") from exc
    return entry, destination, original_sha256, summary


def _carried(entry: AcquisitionEntry) -> dict[str, str]:
    """The originals' row facts a filtered row keeps: identity, provenance and sidecar."""
    return {
        "source_analysis_id": entry.source_analysis_id,
        "status": entry.status,
        "source_url": entry.source_url,
        "sidecar_location": entry.sidecar_location,
        "sidecar_url": entry.sidecar_url,
        "sidecar_bytes": "" if entry.sidecar_bytes is None else str(entry.sidecar_bytes),
        "error": entry.error,
    }


def _resume_problems(
    entry: AcquisitionEntry | None, row: Mapping[str, str], expected_policy: Mapping[str, Any]
) -> list[str]:
    """Why an Analysis an earlier run recorded is not this run's, if it is not."""
    if entry is None:
        return ["recorded, but not a ready original in this run's originals' Acquisition Manifest"]
    problems = []
    filtered = Path(row["location"])
    if not filtered.is_file():
        problems.append(f"its recorded filtered file {filtered} is missing")
    elif str(filtered.stat().st_size) != row["bytes"] or sha256_file(filtered) != row["sha256"]:
        problems.append(f"its filtered file {filtered} is not the size and sha256 recorded")
    if (row.get("original_location"), row.get("original_sha256")) != (entry.location, entry.sha256):
        problems.append(
            f"it was filtered from {row.get('original_location')} (sha256 {row.get('original_sha256')}), "
            f"not this run's original {entry.location} (sha256 {entry.sha256})"
        )
    original = Path(entry.location)
    if original.is_file() and sha256_file(original) != entry.sha256:
        problems.append(f"its original {original} no longer has sha256 {entry.sha256}")
    recorded_policy = json.loads(row["region_policy"]) if row.get("region_policy") else {}
    differing = sorted(
        key for key in set(recorded_policy) | set(expected_policy)
        if recorded_policy.get(key) != expected_policy.get(key)
    )
    if differing:
        problems.append(
            "it was filtered to another region policy: "
            + "; ".join(f"{key} {recorded_policy.get(key)!r}, not {expected_policy.get(key)!r}" for key in differing)
        )
    return problems


def _write_durably(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    """Write the manifest and sync it and its directory, so a recorded row survives a crash."""
    write_connector(path, ACQUISITION_MANIFEST, COLUMNS, rows)
    with path.open("rb") as fh:
        os.fsync(fh.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_run_record(
    manifest_path: Path,
    originals: AcquisitionManifest,
    tool: str,
    policy: RegionPolicy,
    qc_panel: QcPanel,
    counts: Mapping[str, Mapping[str, int]],
) -> None:
    document = {
        "acquisition_manifest": str(manifest_path),
        "written_by": "manifest_generation.region_filtered_acquisition",
        "originals": {"path": str(originals.path), "sha256": originals.sha256},
        "filter_tool": tool,
        "region_policy": policy_record(policy, qc_panel, None),
        "filtered": {analysis_id: dict(counts[analysis_id]) for analysis_id in sorted(counts)},
    }
    atomic_write(
        pass_lineage_path(manifest_path),
        yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100),
    )


def target_spans(config: ReleaseConfig) -> dict[str, TargetSpan]:
    """Each Analysis's target span from the Source Metadata Table the release config names."""
    if config.connectors is None:
        raise RegionFilteredAcquisitionError(
            f"{config.path}: a cis region policy reads target spans from the Source Metadata "
            "Table, and source_collection.connectors names none"
        )
    table = load_source_metadata_table(config.connectors.source_metadata_table)
    return {
        row["source_analysis_id"]: TargetSpan(
            row["target_chromosome"], int(row["target_start"]), int(row["target_end"])
        )
        for row in table.rows
        if row.get("target_chromosome")
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path, help="the release config naming the region policy")
    parser.add_argument("--originals", required=True, type=Path,
                        help="the Acquisition Manifest of the downloaded originals")
    parser.add_argument("--out-dir", required=True, type=Path, help="where the filtered files go")
    parser.add_argument("--out", required=True, type=Path, help="the Acquisition Manifest to write")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        config = load_release_config(args.config, REPO_ROOT)
        if config.region_policy is None:
            raise RegionFilteredAcquisitionError(f"{args.config} declares no region_policy")
        resource = config.reference_resources[config.qc_panel_resource_id]
        manifest = acquire_region_filtered(
            load_acquisition_manifest(args.originals),
            policy=config.region_policy,
            qc_panel=load_qc_panel(Path(resource.location), resource.resource_id),
            targets=target_spans(config) if config.region_policy.cis_flank_bp is not None else {},
            out_dir=args.out_dir,
            manifest_path=args.out,
            workers=args.workers,
        )
    except (InventoryError, RegionFilterError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Acquisition Manifest {args.out}: {manifest.readiness_counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
