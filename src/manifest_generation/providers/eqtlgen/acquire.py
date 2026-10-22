#!/usr/bin/env python3
"""Acquire an eQTLGen Selection's BESD subset and write the shared Acquisition Manifest (#291, ADR 0036).

eQTLGen's cis-eQTL statistics are one genome-wide BESD file set
(``/data/opengwasdb/raw/eqtlgen/eqtlgen-sparse``, ~19k probes, ~2 GB), and a
release takes the probes its Selection names. ``build-ragged-besd`` demands that
a file set hold exactly the probes it is built for, so acquisition writes a
standalone ``.besd``/``.epi``/``.esi`` set per Selection with
``subset_besd.write_besd_subset`` -- a structural change a reader cannot do for
itself (ADR 0036, clarified by #300) -- and records what it produced as a
canonical **Acquisition Manifest** (``manifest_generation.acquisition_manifest``):
one row per selected Analysis, every row claiming the one file set.

A row's ``location`` is the subset's prefix, its ``source_probe_id`` the probe it
reads, and its ``members`` each member file's size and sha256. ``bytes`` and
``sha256`` are the ``.besd`` member's, so they equal what the resolver
fingerprints; there is no composite checksum. Every row carries its lineage: the
original file set's prefix, the sha256 of its ``.besd`` member, and the tool, with
the sha256 of the writer that made the subset. Beside the manifest this writes
``<manifest-stem>.meta.yaml``, the pass lineage freeze checksums into the Source
Inventory's provenance: the Selection, the probes in the order written, every
original member's size and sha256, the subset's counts and the run's settings.

The Selection is the release config's, read from the same Source Metadata Table
the release freezes (``source_collection.connectors.source_metadata_table``), so
acquisition and freeze cannot disagree about membership. The source directory is
read-only; the subset is written under ``--out-dir``.

``--adopt-prefix`` records a file set that already exists -- one an earlier run of
this writer produced, kept where it is -- instead of writing one. The Selection's
subset is written to a temporary directory and the existing set is adopted only if
every member has the sha256 the writer produces now; a set that differs is refused
by name, so an adopted set is the writer's output, never a lookalike.

A re-run resumes only the run it is: when the manifest at ``--out`` exists, the
subset's members must still be the bytes it records, and are then kept rather than
rewritten. A member that differs is refused by name and nothing is rewritten: a
recorded checksum is identity evidence, not a field a later run may overwrite.

Usage:
    python3 -m manifest_generation.providers.eqtlgen.acquire \\
        --config resources/generators/eqtlgen-cis-pilot/release-pilot-10.yaml \\
        --source-prefix /data/opengwasdb/raw/eqtlgen/eqtlgen-sparse \\
        --out-dir <subset dir> --name pilot-10 --out <acquisition manifest>
    python3 -m manifest_generation.providers.eqtlgen.acquire \\
        --config <release config> --source-prefix <genome-wide file set> \\
        --adopt-prefix <existing subset> --out <acquisition manifest>
"""

from __future__ import annotations

import argparse
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    BESD_MEMBERS,
    COLUMNS,
    AcquisitionManifestError,
    load_acquisition_manifest,
    members_text,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import FreezeError, write_connector
from manifest_generation.providers.eqtlgen.subset_besd import write_besd_subset
from manifest_generation.selection import SelectionError, load_selection, select
from manifest_generation.source_inventory import atomic_write, sha256_file
from manifest_generation.source_metadata_table import load_source_metadata_table

#: The writer a subset's lineage names: the module that changes the file set.
TOOL_MODULE = "manifest_generation.providers.eqtlgen.subset_besd"
READY_STATUS = "ok"
#: The magic, 15 reserved uint32s and the uint64 ``val_num`` that open a SPARSE_FILE_TYPE_3 ``.besd``.
BESD_HEADER_BYTES = 4 + 15 * 4 + 8
COLUMNS_WRITTEN: tuple[str, ...] = tuple(
    column for column in COLUMNS
    if column in {
        "source_analysis_id", "status", "location", "bytes", "sha256",
        "original_location", "original_sha256", "reformat_tool",
        "source_probe_id", "members",
    }
)


class AcquisitionError(FreezeError):
    """eQTLGen acquisition cannot produce the Selection's file set, or will not overwrite its record."""


def member_identity(prefix: Path) -> dict[str, dict[str, Any]]:
    """Each member file's size and sha256: ``{".besd": {"bytes", "sha256"}, ...}``."""
    identity: dict[str, dict[str, Any]] = {}
    for suffix in BESD_MEMBERS:
        member = Path(f"{prefix}{suffix}")
        if not member.is_file():
            raise AcquisitionError(f"BESD member file not found: {member}")
        identity[suffix] = {"bytes": member.stat().st_size, "sha256": sha256_file(member)}
    return identity


def file_set_counts(prefix: Path) -> dict[str, int]:
    """A file set's probes, SNPs and associations, from its ``.epi`` and ``.esi`` lines and ``.besd`` header.

    ``val_num`` (the uint64 after the magic and 15 reserved words) counts each
    association twice, a beta and an SE.
    """
    with open(f"{prefix}.besd", "rb") as fh:
        val_num = struct.unpack("<Q", fh.read(BESD_HEADER_BYTES)[BESD_HEADER_BYTES - 8:])[0]
    return {
        "probes": _line_count(Path(f"{prefix}.epi")),
        "snps": _line_count(Path(f"{prefix}.esi")),
        "associations": val_num // 2,
    }


def _line_count(path: Path) -> int:
    with open(path, "rb") as fh:
        return sum(1 for line in fh if line.strip())


def tool_identity() -> str:
    """The tool and its version: the subset writer's module name and the sha256 of its source."""
    from manifest_generation.providers.eqtlgen import subset_besd

    return f"{TOOL_MODULE} sha256:{sha256_file(Path(subset_besd.__file__))}"


def acquire(
    *,
    config: Path,
    source_prefix: Path,
    out_dir: Path | None,
    name: str | None,
    out: Path,
    adopt_prefix: Path | None = None,
) -> Path:
    """Subset ``source_prefix`` to the config's Selection under ``out_dir`` and write the manifest at ``out``.

    With ``adopt_prefix``, record that existing file set instead, if it is what the
    subset writer produces for the Selection.
    """
    config, source_prefix, out = Path(config), Path(source_prefix), Path(out)
    if (adopt_prefix is None) == (out_dir is None or name is None):
        raise AcquisitionError("name either --adopt-prefix, or --out-dir with --name")
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    connectors = ((document or {}).get("source_collection") or {}).get("connectors") or {}
    table_path = connectors.get("source_metadata_table")
    if not table_path:
        raise AcquisitionError(
            f"{config} names no source_collection.connectors.source_metadata_table to select from"
        )
    table = load_source_metadata_table(Path(table_path))
    selection = load_selection(config)
    rows = select(table, selection)
    blank = [row["source_analysis_id"] for row in rows if not row.get("source_probe_id")]
    if blank:
        raise AcquisitionError(
            f"{len(blank)} selected Analysis/Analyses have no source_probe_id: {', '.join(blank[:10])}"
        )
    probes = [row["source_probe_id"] for row in rows]
    prefix = Path(adopt_prefix) if adopt_prefix is not None else Path(out_dir) / name

    original = member_identity(source_prefix)
    tool = tool_identity()
    if out.is_file():
        members = _resume_members(
            out, prefix, rows, source_prefix, original, tool, selection.as_dict()
        )
    elif adopt_prefix is not None:
        members = _adopt(source_prefix, prefix, probes)
        print(f"Adopted {prefix}: its members are what the subset writer produces for the Selection")
    else:
        result = write_besd_subset(source_prefix, prefix, probes)
        members = member_identity(prefix)
        print(
            f"Subset {prefix}: {result.n_probes} probes, {result.n_snps} SNPs, "
            f"{result.n_associations} associations"
        )
    data = members[".besd"]
    members_cell = members_text(members)
    manifest_rows = [
        {
            "source_analysis_id": row["source_analysis_id"],
            "status": READY_STATUS,
            "location": str(prefix),
            "bytes": str(data["bytes"]),
            "sha256": data["sha256"],
            "original_location": str(source_prefix),
            "original_sha256": original[".besd"]["sha256"],
            "reformat_tool": tool,
            "source_probe_id": row["source_probe_id"],
            "members": members_cell,
        }
        for row in rows
    ]
    write_connector(out, ACQUISITION_MANIFEST, COLUMNS_WRITTEN, manifest_rows)
    load_acquisition_manifest(out)
    _write_pass_lineage(out, config, selection.as_dict(), probes, source_prefix, original, prefix, members, tool)
    return out


def _adopt(
    source_prefix: Path, existing: Path, probes: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """The identity of ``existing``, once it is shown to be the subset writer's output for ``probes``."""
    held = member_identity(existing)
    with tempfile.TemporaryDirectory(prefix="eqtlgen-adopt-") as scratch:
        write_besd_subset(source_prefix, Path(scratch) / "subset", list(probes))
        fresh = member_identity(Path(scratch) / "subset")
    differs = [
        f"{suffix} (existing sha256 {held[suffix]['sha256']}, writer's {fresh[suffix]['sha256']})"
        for suffix in BESD_MEMBERS
        if held[suffix] != fresh[suffix]
    ]
    if differs:
        raise AcquisitionError(
            f"{existing} is not the subset writer's output for this Selection: {', '.join(differs)}"
        )
    return held


def _resume_members(
    out: Path,
    prefix: Path,
    rows: Sequence[Mapping[str, str]],
    source_prefix: Path,
    original: Mapping[str, Mapping[str, Any]],
    tool: str,
    selection: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """The subset a manifest already at ``out`` recorded, if it is still on disk and this run's.

    This run's means the same Selection, the same original (its prefix and every
    member's sha256) and the same tool: retained output is never relabelled with a
    new original or tool, so any difference is refused before anything is written.
    """
    recorded = load_acquisition_manifest(out)
    wanted = {row["source_analysis_id"]: row["source_probe_id"] for row in rows}
    held = {entry.source_analysis_id: entry.source_probe_id for entry in recorded.entries}
    if held != wanted:
        raise AcquisitionError(
            f"{out} records a different Selection ({len(held)} Analyses, not {len(wanted)}); "
            "a recorded manifest is not rewritten: write the new one elsewhere"
        )
    first = recorded.entries[0]
    if first.location != str(prefix):
        raise AcquisitionError(f"{out} records its file set at {first.location}, not {prefix}")
    _lineage_problems(out, first, source_prefix, original, tool, selection)
    actual = member_identity(prefix)
    changed = [
        f"{suffix} (recorded sha256 {first.members[suffix]['sha256']}, on disk {actual[suffix]['sha256']})"
        for suffix in BESD_MEMBERS
        if actual[suffix] != first.members[suffix]
    ]
    if changed:
        raise AcquisitionError(
            f"member file(s) of {prefix} no longer match {out}: {', '.join(changed)}; "
            "the recorded checksums stand and nothing was rewritten"
        )
    return actual


def _lineage_problems(
    out: Path,
    first: Any,
    source_prefix: Path,
    original: Mapping[str, Mapping[str, Any]],
    tool: str,
    selection: Mapping[str, Any],
) -> None:
    """Refuse, by name, a run whose original, tool or Selection is not the one the record names."""
    lineage = first.lineage
    problems: list[str] = []
    if lineage is None:
        problems.append("it records no lineage")
    else:
        if lineage.original_location != str(source_prefix):
            problems.append(
                f"original prefix is {source_prefix}, recorded {lineage.original_location}"
            )
        if lineage.original_sha256 != original[".besd"]["sha256"]:
            problems.append(
                f"original .besd sha256 is {original['.besd']['sha256']}, recorded {lineage.original_sha256}"
            )
        if lineage.reformat_tool != tool:
            problems.append(f"tool is {tool}, recorded {lineage.reformat_tool}")
    sidecar = pass_lineage_path(out)
    record = yaml.safe_load(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else None
    if not isinstance(record, dict):
        problems.append(f"its pass lineage {sidecar} is missing")
    else:
        held = ((record.get("original") or {}).get("members")) or {}
        for suffix in BESD_MEMBERS:
            if held.get(suffix) != dict(original[suffix]):
                problems.append(f"original {suffix} is {dict(original[suffix])}, recorded {held.get(suffix)}")
        if record.get("selection") != dict(selection):
            problems.append(f"Selection is {dict(selection)}, recorded {record.get('selection')}")
    if problems:
        raise AcquisitionError(
            f"{out} was written from another original, tool or Selection: {'; '.join(problems)}; "
            "the recorded lineage stands and nothing was rewritten"
        )


def _write_pass_lineage(
    out: Path,
    config: Path,
    selection: Mapping[str, Any],
    probes: Sequence[str],
    source_prefix: Path,
    original: Mapping[str, Mapping[str, Any]],
    prefix: Path,
    members: Mapping[str, Mapping[str, Any]],
    tool: str,
) -> Path:
    sidecar = pass_lineage_path(out)
    document = {
        "acquisition_manifest": str(out),
        "sha256": sha256_file(out),
        "rows": len(probes),
        "readiness_counts": {READY_STATUS: len(probes)},
        "written_by": __spec__.name,
        "release_config": str(config),
        "selection": dict(selection),
        "probes": list(probes),
        "original": {
            "prefix": str(source_prefix),
            "members": {k: dict(v) for k, v in original.items()},
            "counts": file_set_counts(source_prefix),
        },
        "subset": {
            "prefix": str(prefix),
            "members": {k: dict(v) for k, v in members.items()},
            "counts": file_set_counts(prefix),
        },
        "reformat_tool": tool,
    }
    atomic_write(
        sidecar, yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100)
    )
    return sidecar


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", required=True, type=Path, help="the release config naming the Selection")
    parser.add_argument("--source-prefix", required=True, type=Path, help="the full BESD file set's prefix")
    parser.add_argument("--out-dir", type=Path, help="where the subset is written")
    parser.add_argument("--name", help="the subset's file name stem under --out-dir")
    parser.add_argument(
        "--adopt-prefix", type=Path,
        help="record this existing file set, if it is the subset writer's output, instead of writing one",
    )
    parser.add_argument("--out", required=True, type=Path, help="the Acquisition Manifest to write")
    args = parser.parse_args(argv)
    try:
        acquire(
            config=args.config, source_prefix=args.source_prefix, out_dir=args.out_dir,
            name=args.name, out=args.out, adopt_prefix=args.adopt_prefix,
        )
    except (FreezeError, SelectionError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Acquisition Manifest {args.out} written for {args.config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
