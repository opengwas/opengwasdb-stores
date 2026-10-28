"""The Acquisition Manifest: what acquisition produced (issue #283, ADR 0036).

Acquisition writes one row per Analysis it acquired a file for: the file's
location, size and sha256, where it came from, and an optional per-Analysis
metadata sidecar (a GWAS-SSF ``-meta.yaml``). It also writes a row for an
Analysis it tried and could not acquire, with a non-ready ``status`` and the
``error``: that is still an availability fact, recorded so freeze can tell an
explained absence from a silent one. The manifest never records intent; the
Source Metadata Table does.

A plain file is one Analysis's. A BESD file set -- ``.besd``, ``.epi`` and
``.esi`` under one prefix, which holds an Analysis per probe -- is shared: every
Analysis reading it records the prefix as ``location``, its own
``source_probe_id`` and the same ``members`` (each member's size and sha256,
never a composite checksum). The file set's ``bytes`` and ``sha256`` are the
``.besd`` member's, the file that holds the associations, so they equal what
the resolver fingerprints. Sharing is accepted only when the claimants agree on
every member and each probe is claimed once; a member whose checksum differs
between claimants, or a probe claimed twice, is refused by name (#291).

A file acquisition changed carries its lineage, so a Release Bundle traces
back to what the provider published: the original's location and sha256 and
the tool that changed it. Acquisition changes a file in two cases (ADR 0036,
clarified by #300): a structural change a Column Map cannot express, such as
splitting one file into one per Analysis, and filtering it to a release's
region policy, for scale. A region-filtered file's lineage also records the
policy's parameters (``region_policy``, ``manifest_generation.region_filter``),
and its original is deleted once that lineage is recorded.

Loading is a gate: an unknown column or status, a ready file missing its
location, size or checksum, a partial lineage, or a duplicate Analysis or file
is refused, every problem named at once. The readiness vocabulary is
``source_inventory``'s (``READY_STATUSES``, ``KNOWN_READINESS_STATUSES``), so
"ready" keeps one spelling.

Write a manifest with ``connector_tsv.write_connector(path,
ACQUISITION_MANIFEST, columns, rows)``. ``docs/spec/source-connectors.md``
specifies every column.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from manifest_generation.connector_tsv import (
    ConnectorSpec,
    FreezeError,
    raise_problems,
    read_connector,
)
from manifest_generation.region_filter import (
    REGION_POLICY_COVERAGES,
    canonical_association_coverage,
)
from manifest_generation.source_inventory import (
    KNOWN_READINESS_STATUSES,
    READY_STATUSES,
    counts,
    duplicate_values,
    sha256_file,
)

#: Columns every manifest carries. ``source_analysis_id`` and ``status`` have a
#: value on every row; the file columns on every ready row.
REQUIRED_COLUMNS: tuple[str, ...] = ("source_analysis_id", "status", "location", "bytes", "sha256")

#: The file facts a ready row must record.
READY_FIELDS: tuple[str, ...] = ("location", "bytes", "sha256")

#: The lineage of a file acquisition changed: all three, or none. A file whose
#: original was filtered before acquisition checksummed it, and is gone, has
#: no ``original_sha256``; it must then record ``original_bytes`` (#300).
LINEAGE_COLUMNS: tuple[str, ...] = ("original_location", "original_sha256", "reformat_tool")

#: Lineage a file may add: the original's size, and for a region-filtered file
#: the region policy it was filtered to, one canonical JSON object.
OPTIONAL_LINEAGE_COLUMNS: tuple[str, ...] = ("original_bytes", "region_policy")

#: A BESD file set several Analyses share (#291): the probe each Analysis reads
#: and the members' sizes and checksums, one canonical JSON object. Both or neither.
FILE_SET_COLUMNS: tuple[str, ...] = ("source_probe_id", "members")

#: The member suffixes of a BESD file set, and the one that holds the associations.
BESD_MEMBERS: tuple[str, ...] = (".besd", ".epi", ".esi")
BESD_DATA_MEMBER: str = ".besd"

OPTIONAL_COLUMNS: tuple[str, ...] = (
    "source_url",
    "sidecar_location",
    "sidecar_url",
    "sidecar_bytes",
    "error",
) + LINEAGE_COLUMNS + OPTIONAL_LINEAGE_COLUMNS + FILE_SET_COLUMNS

#: Every canonical column, in the order a writer emits them.
COLUMNS: tuple[str, ...] = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

# No leading zero, so a size survives the round trip to an integer and back.
_SIZE = re.compile(r"\A(?:0|[1-9]\d*)\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class AcquisitionManifestError(FreezeError):
    """An Acquisition Manifest is not usable as the record of what was acquired."""


#: The file contract the shared connector reader and writer apply.
ACQUISITION_MANIFEST = ConnectorSpec(
    what="Acquisition Manifest",
    columns=COLUMNS,
    required_columns=REQUIRED_COLUMNS,
    error=AcquisitionManifestError,
)


def pass_lineage_path(manifest_path: Path) -> Path:
    """Where an acquisition writer records the passes a manifest came from: ``<stem>.meta.yaml``."""
    manifest_path = Path(manifest_path)
    return manifest_path.with_name(f"{manifest_path.name.removesuffix('.tsv')}.meta.yaml")


@dataclass(frozen=True)
class Lineage:
    """Where a changed file came from, and what changed it.

    ``original_sha256`` is empty only for an original filtered before it was
    checksummed, which ``original_bytes`` then identifies. ``region_policy``
    is the parsed policy a region-filtered file was filtered to.
    """

    original_location: str
    original_sha256: str
    reformat_tool: str
    original_bytes: int | None = None
    region_policy: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "original_location": self.original_location,
            "original_sha256": self.original_sha256,
            "reformat_tool": self.reformat_tool,
        }
        if self.original_bytes is not None:
            document["original_bytes"] = self.original_bytes
        if self.region_policy is not None:
            document["region_policy"] = dict(self.region_policy)
        return document


@dataclass(frozen=True)
class AcquisitionEntry:
    """One Analysis's acquisition outcome: its file, or why there is none.

    ``bytes`` and ``sidecar_bytes`` are ``None`` when acquisition recorded no
    size -- absence, never zero.
    """

    source_analysis_id: str
    status: str
    location: str
    bytes: int | None
    sha256: str
    source_url: str = ""
    sidecar_location: str = ""
    sidecar_url: str = ""
    sidecar_bytes: int | None = None
    error: str = ""
    lineage: Lineage | None = None
    #: The probe a BESD Analysis reads in the file set ``location`` is the prefix of.
    source_probe_id: str = ""
    #: A BESD file set's members -> ``{"bytes": int, "sha256": str}``; ``None`` for a plain file.
    members: Mapping[str, Mapping[str, Any]] | None = None

    @property
    def ready(self) -> bool:
        return self.status in READY_STATUSES

    @property
    def files(self) -> tuple[str, ...]:
        """Every local path this entry claims alone: a file set's prefix is shared, so it is not one."""
        paths = (self.location, self.sidecar_location)
        return tuple(path for path in paths if path and not (self.members and path == self.location))


@dataclass(frozen=True)
class AcquisitionManifest:
    """A loaded, validated Acquisition Manifest, its rows as written, and its identity."""

    path: Path
    sha256: str
    bytes: int
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]
    entries: tuple[AcquisitionEntry, ...]
    _by_id: Mapping[str, AcquisitionEntry] = field(repr=False, compare=False, default_factory=dict)

    def entry(self, analysis_id: str) -> AcquisitionEntry:
        return self._by_id[analysis_id]

    def __contains__(self, analysis_id: object) -> bool:
        return analysis_id in self._by_id

    @property
    def readiness_counts(self) -> dict[str, int]:
        return counts(entry.status for entry in self.entries)


def load_acquisition_manifest(path: Path) -> AcquisitionManifest:
    """Load and validate a canonical Acquisition Manifest, refusing it on any problem.

    Not to be confused with the GWAS Catalog writer's ``read_acquisition_pass``
    (``providers.gwas_catalog.write_acquisition_manifest``), which reads one
    download pass in the downloader's own columns.
    """
    path = Path(path)
    header, numbered = read_connector(path, ACQUISITION_MANIFEST)
    problems: list[str] = []
    entries: list[AcquisitionEntry] = []
    for line_number, row in numbered:
        row_problems = _row_problems(row)
        if row_problems:
            label = row.get("source_analysis_id") or "(no source_analysis_id)"
            problems.extend(f"line {line_number} ({label}): {problem}" for problem in row_problems)
            continue
        entries.append(_entry(row))
    repeated = duplicate_values(row["source_analysis_id"] for _, row in numbered if row["source_analysis_id"])
    if repeated:
        problems.append(f"duplicate source_analysis_id: {', '.join(repeated)}")
    repeated_files = duplicate_values(path for entry in entries for path in entry.files)
    if repeated_files:
        problems.append(f"duplicate file claimed by more than one entry: {', '.join(repeated_files)}")
    problems.extend(_file_set_problems(entries))
    if problems:
        raise_problems(AcquisitionManifestError, f"Acquisition Manifest {path}", problems)
    return AcquisitionManifest(
        path=path,
        sha256=sha256_file(path),
        bytes=path.stat().st_size,
        columns=header,
        rows=tuple(row for _, row in numbered),
        entries=tuple(entries),
        _by_id={entry.source_analysis_id: entry for entry in entries},
    )


# ---------------------------------------------------------------------------
# Per-row rules
# ---------------------------------------------------------------------------


def _row_problems(row: Mapping[str, str]) -> list[str]:
    problems: list[str] = []

    def value(column: str) -> str:
        return row.get(column, "")

    for column in ("source_analysis_id", "status"):
        if not value(column):
            problems.append(f"{column} is empty")
    status = value("status")
    if status and status not in KNOWN_READINESS_STATUSES:
        problems.append(f"status {status!r} is not a known readiness status")
    if status in READY_STATUSES:
        absent = [column for column in READY_FIELDS if not value(column)]
        if absent:
            problems.append(f"a ready file needs {', '.join(absent)}")
    for column in ("bytes", "sidecar_bytes", "original_bytes"):
        if value(column) and not _SIZE.match(value(column)):
            problems.append(f"{column} {value(column)!r} is not a size in bytes")
    for column in ("sha256", "original_sha256"):
        if value(column) and not _SHA256.match(value(column)):
            problems.append(f"{column} {value(column)!r} is not a lower-case sha256 digest")
    if any(value(column) for column in FILE_SET_COLUMNS):
        absent = [column for column in FILE_SET_COLUMNS if not value(column)]
        if absent:
            problems.append(f"a BESD file set needs {', '.join(FILE_SET_COLUMNS)}; {', '.join(absent)} empty")
        elif status not in READY_STATUSES:
            problems.append(f"a file set is recorded on a {status!r} entry with no ready file")
        else:
            problems.extend(_members_problems(row))
    if any(value(column) for column in LINEAGE_COLUMNS + OPTIONAL_LINEAGE_COLUMNS):
        absent = [
            column for column in LINEAGE_COLUMNS
            if not value(column) and not (column == "original_sha256" and value("original_bytes"))
        ]
        if absent:
            problems.append(f"lineage needs {', '.join(LINEAGE_COLUMNS)}; {', '.join(absent)} empty")
        if status not in READY_STATUSES:
            problems.append(f"lineage is recorded on a {status or 'status-less'!r} entry with no ready file")
        if value("region_policy"):
            problems.extend(_region_policy_problems(value("region_policy")))
    return problems


def members_text(members: Mapping[str, Mapping[str, Any]]) -> str:
    """A file set's ``members`` as the manifest records it: one JSON object, sorted keys, no spaces."""
    return json.dumps(members, sort_keys=True, separators=(",", ":"))


def _file_set_problems(entries: list[AcquisitionEntry]) -> list[str]:
    """Why the entries sharing a BESD file set do not agree on it (#291)."""
    problems: list[str] = []
    by_prefix: dict[str, list[AcquisitionEntry]] = {}
    for entry in entries:
        if entry.members is not None:
            by_prefix.setdefault(entry.location, []).append(entry)
    plain = {entry.location for entry in entries if entry.members is None and entry.location}
    for prefix, claimants in by_prefix.items():
        if prefix in plain:
            problems.append(f"duplicate file claimed by more than one entry: {prefix}")
        first = claimants[0]
        for entry in claimants[1:]:
            for suffix in BESD_MEMBERS:
                if entry.members[suffix] != first.members[suffix]:
                    problems.append(
                        f"file set {prefix} member {suffix} differs between "
                        f"{first.source_analysis_id} ({_describe(first.members[suffix])}) and "
                        f"{entry.source_analysis_id} ({_describe(entry.members[suffix])}): "
                        "its sha256 changed"
                    )
        repeated = duplicate_values(entry.source_probe_id for entry in claimants)
        if repeated:
            problems.append(f"duplicate probe claimed twice in file set {prefix}: {', '.join(repeated)}")
    return problems


def _describe(member: Mapping[str, Any]) -> str:
    return f"{member['bytes']} bytes, sha256 {member['sha256']}"


def _members_problems(row: Mapping[str, str]) -> list[str]:
    """Why a row's ``members`` is not a BESD file set's complete record, and its data member's identity."""
    text = row.get("members", "")
    try:
        members = json.loads(text)
    except ValueError:
        return [f"members {text!r} is not JSON"]
    if not isinstance(members, dict) or set(members) != set(BESD_MEMBERS):
        return [f"members must hold exactly {', '.join(BESD_MEMBERS)}; it has {sorted(members) if isinstance(members, dict) else text!r}"]
    problems = []
    for suffix in BESD_MEMBERS:
        member = members[suffix]
        if not (
            isinstance(member, dict)
            and set(member) == {"bytes", "sha256"}
            and type(member["bytes"]) is int and member["bytes"] >= 0
            and isinstance(member["sha256"], str) and _SHA256.match(member["sha256"])
        ):
            problems.append(f"members {suffix} {member!r} is not a size in bytes and a lower-case sha256")
    if problems:
        return problems
    data = members[BESD_DATA_MEMBER]
    if row.get("sha256") != data["sha256"]:
        problems.append(f"sha256 {row.get('sha256')!r} is not the {BESD_DATA_MEMBER} member's {data['sha256']}")
    if row.get("bytes") != str(data["bytes"]):
        problems.append(f"bytes {row.get('bytes')!r} is not the {BESD_DATA_MEMBER} member's {data['bytes']}")
    return problems


def _canonical_policy(policy: Any) -> Any:
    """A recorded region policy read in the current Association Coverage vocabulary (#341).

    An Acquisition Manifest that froze ``cis_plus_signals`` is immutable evidence,
    so the retired spelling is aliased on read, never rewritten; a reader of this
    policy elsewhere sees one vocabulary.
    """
    if isinstance(policy, Mapping) and "association_coverage" in policy:
        return {**policy, "association_coverage": canonical_association_coverage(policy["association_coverage"])}
    return policy


def _region_policy_problems(text: str) -> list[str]:
    """Why a recorded region policy is not a complete one (docs/spec/source-connectors.md).

    It names the policy, its coverage, its flanks and thresholds, the QC panel
    every policy keeps (``resource_id`` and ``sha256``), and for a cis policy
    its flank and the ``target`` span it was applied to.
    """
    try:
        policy = json.loads(text)
    except ValueError:
        return [f"region_policy {text!r} is not JSON"]
    if not isinstance(policy, dict):
        return [f"region_policy {text!r} is not a JSON object"]
    problems = []

    def whole(value: Any) -> bool:
        return type(value) is int and value >= 1

    def probability(value: Any) -> bool:
        return type(value) in (int, float) and 0 < value <= 1

    if not isinstance(policy.get("region_policy_id"), str) or not policy["region_policy_id"]:
        problems.append("region_policy names no region_policy_id")
    # A frozen policy records the spelling its run used, and `cis_plus_signals`
    # is evidence this repository does not rewrite (#341); read it through the
    # one alias so a current value is what the checks below see.
    coverage = canonical_association_coverage(policy.get("association_coverage"))
    if coverage not in REGION_POLICY_COVERAGES:
        problems.append(
            f"region_policy association_coverage {policy.get('association_coverage')!r} is not one "
            f"of {', '.join(REGION_POLICY_COVERAGES)}"
        )
    for key, valid, what in (
        ("trans_flank_bp", whole, "a whole number of bases"),
        ("suggestive_lead_min_distance_bp", whole, "a whole number of bases"),
        ("significant_p", probability, "a p-value in (0, 1]"),
        ("suggestive_p", probability, "a p-value in (0, 1]"),
    ):
        if not valid(policy.get(key)):
            problems.append(f"region_policy {key} {policy.get(key)!r} is not {what}")
    panel = policy.get("qc_panel")
    if not (
        isinstance(panel, dict)
        and isinstance(panel.get("resource_id"), str) and panel["resource_id"]
        and isinstance(panel.get("sha256"), str) and _SHA256.match(panel["sha256"])
    ):
        problems.append(f"region_policy qc_panel {panel!r} names no resource_id and sha256")
    if coverage == "cis_and_signals":
        if not whole(policy.get("cis_flank_bp")):
            problems.append(f"region_policy cis_flank_bp {policy.get('cis_flank_bp')!r} is not a whole number of bases")
        target = policy.get("target")
        if not (
            isinstance(target, dict)
            and isinstance(target.get("chromosome"), str) and target["chromosome"]
            and whole(target.get("start")) and whole(target.get("end"))
        ):
            problems.append(f"region_policy target {target!r} is not a chromosome, start and end")
    elif "cis_flank_bp" in policy or "target" in policy:
        problems.append(f"region_policy cis_flank_bp and target belong to cis_and_signals, not {coverage!r}")
    return problems


def _entry(row: Mapping[str, str]) -> AcquisitionEntry:
    lineage = (
        Lineage(
            *(row.get(column, "") for column in LINEAGE_COLUMNS),
            original_bytes=int(row["original_bytes"]) if row.get("original_bytes") else None,
            region_policy=_canonical_policy(json.loads(row["region_policy"])) if row.get("region_policy") else None,
        )
        if row.get("original_location")
        else None
    )
    return AcquisitionEntry(
        source_analysis_id=row["source_analysis_id"],
        status=row["status"],
        location=row["location"],
        bytes=int(row["bytes"]) if row["bytes"] else None,
        sha256=row["sha256"],
        source_url=row.get("source_url", ""),
        sidecar_location=row.get("sidecar_location", ""),
        sidecar_url=row.get("sidecar_url", ""),
        sidecar_bytes=int(row["sidecar_bytes"]) if row.get("sidecar_bytes") else None,
        error=row.get("error", ""),
        lineage=lineage,
        source_probe_id=row.get("source_probe_id", ""),
        members=json.loads(row["members"]) if row.get("members") else None,
    )
