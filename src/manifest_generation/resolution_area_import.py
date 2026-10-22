#!/usr/bin/env python3
"""Seed a Source Collection's Resolution Area from pre-#284 records (#290).

Before #284 a run kept its Resolution Records in its own Work Root
(``<work-root>/<store-id>/resolver/records/<analysis_id>.json``). Those records
still carry the resolver's fingerprint, and the fingerprint is what decides
reuse: if the file, references, settings and tool revision are unchanged, the
resolver resumes the record instead of measuring the Analysis again. #287's
spec documents copying such files into the checkpoint the run will use; this
module is the tool that does it, with the two facts a manual copy can get
wrong made loud:

- the record's ``analysis_id`` must be its file's name, and its fingerprint
  digest must be a SHA-256 hex digest, so a stray or truncated file is refused;
- a checkpoint file is written atomically and never replaced with different
  bytes, and the kept copy goes through :meth:`ResolutionArea.keep`, so the
  area's write-once rule is preserved.

It imports records only; it does not build the resolver's ``index.json``, which
the next ``resolve`` run writes for its own Selection. A record outside the
Selection is harmless -- the resolver checks each Analysis's full fingerprint
and only the manifest's rows are accounted -- but a record outside the Source
Collection is not: before seeding, every record is checked against the
collection's checksummed intent (its frozen Source Metadata Table and
Acquisition Manifest) and refused by name if it does not belong, if its
fingerprint does not digest to the value it stores, if the file identity it was
resolved against is not the one the collection acquired, or if the run's own
``index.json`` (and the receipt beside it) does not name it (#290 review).

Usage:
    python3 -m manifest_generation.resolution_area_import \\
        --config resources/generators/dbgap-mvp/release-eur.yaml \\
        --source /data/opengwasdb/work/epic232/221/resolve-EUR-.../records
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from manifest_generation.candidate_workflow import (
    declared_setting_checksums,
    load_candidate_configuration,
    recompute_fingerprint_digest,
    resolver_settings,
    resolver_settings_digest,
)
from manifest_generation.freeze import CollectionIntent, FreezeError, load_collection_intent
from manifest_generation.resolution_area import (
    ResolutionArea,
    ResolutionAreaError,
    resolution_root,
)


class ResolutionImportError(ValueError):
    """A pre-#284 record cannot be seeded into the Resolution Area as asked."""


#: The record schema this tool can seed: the resolver's ``record_schema_version``.
RECORD_SCHEMA_VERSION: int = 1

#: The fingerprint fields a seedable record must carry: its self-digest, and the
#: source identity the collection's Acquisition Manifest must corroborate.
REQUIRED_FINGERPRINTS: tuple[str, ...] = (
    "fingerprint_digest",
    "source_file",
    "source_recorded_sha256",
    "source_recorded_bytes",
)


@dataclass(frozen=True)
class RunEvidence:
    """What a run's own record proves about the records in its Work Root.

    ``entries`` are the run's ``index.json`` rows, keyed by Analysis: each names
    the record file, its status and the measurements the run reported.
    ``record_digests`` is the receipt's per-record sha256 (a v1 pipeline receipt
    carries them), and ``statuses`` the receipt's per-record statuses. At least
    one of the two must corroborate the index, and a record the index does not
    name is not the run's.
    """

    index_path: Path
    receipt_path: Path
    entries: Mapping[str, Mapping[str, object]]
    record_digests: Mapping[str, str]
    statuses: Mapping[str, str]


@dataclass(frozen=True)
class ImportResult:
    """What one import did, per source directory."""

    imported: int = 0
    skipped: int = 0
    kept: Mapping[str, object] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.imported + self.skipped


def import_records(
    source: Path,
    area: ResolutionArea,
    *,
    settings_digest: str,
    intent: CollectionIntent,
    analysis_ids: Iterable[str] | None = None,
    holder: str = "manifest_generation.resolution_area_import",
) -> ImportResult:
    """Copy ``source``'s ``<analysis_id>.json`` records into ``area`` and keep them.

    ``settings_digest`` routes the records to the checkpoint the release's
    :func:`resolver_settings` will use, and ``intent`` is the Source
    Collection's checksummed intent every record must belong to. Only records
    whose Analysis is in ``analysis_ids`` (all of them when ``None``) are
    imported. Re-importing the same bytes is a skip; different bytes under one
    name are refused.
    """
    source = Path(source)
    if not source.is_dir():
        raise ResolutionImportError(f"record directory not found: {source}")
    wanted = None if analysis_ids is None else set(analysis_ids)
    evidence = _run_evidence(source)
    with area.exclusive(holder):
        checkpoint = area.checkpoint_dir(settings_digest)
        checkpoint.mkdir(parents=True, exist_ok=True)
        imported = skipped = 0
        kept: dict[str, object] = {}
        for path in sorted(source.glob("*.json")):
            if path.name == "index.json":
                continue  # the last run's index, never a record
            analysis_id = path.stem
            if wanted is not None and analysis_id not in wanted:
                continue
            data = path.read_bytes()
            document = _load_record(path, data, analysis_id, intent)
            digest = _fingerprint_digest(path, document)
            _check_bound(path, document, analysis_id, intent)
            _check_evidence(path, data, document, evidence)
            target = checkpoint / f"{analysis_id}.json"
            if target.exists():
                if target.read_bytes() != data:
                    raise ResolutionImportError(
                        f"checkpoint record {target} already exists with different bytes; "
                        "a checkpoint record is never replaced"
                    )
                skipped += 1
            else:
                _write_once(target, data)
                imported += 1
            kept[analysis_id] = area.keep(checkpoint, analysis_id)
            if kept[analysis_id].fingerprint_digest != digest:  # pragma: no cover - defensive
                raise ResolutionImportError(
                    f"{path}: the record's fingerprint digest changed while it was copied"
                )
    return ImportResult(imported=imported, skipped=skipped, kept=kept)


def _load_record(
    path: Path, data: bytes, analysis_id: str, intent: CollectionIntent
) -> Mapping[str, object]:
    """The record's JSON, after the schema and collection-membership gates."""
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolutionImportError(f"{path} is not a resolver record: {exc}") from exc
    if not isinstance(document, dict):
        raise ResolutionImportError(f"{path} is not a resolver record: not a JSON mapping")
    recorded = document.get("analysis_id")
    if recorded != analysis_id:
        raise ResolutionImportError(
            f"{path} carries analysis_id {recorded!r}, not the file's {analysis_id!r}"
        )
    if analysis_id not in intent.source_metadata_table:
        raise ResolutionImportError(
            f"{path}: {analysis_id} is not an Analysis of the Source Collection's intended "
            f"membership ({intent.source_metadata_table.path.name})"
        )
    schema = document.get("record_schema_version")
    if type(schema) is not int or schema != RECORD_SCHEMA_VERSION:
        raise ResolutionImportError(
            f"{path} records record_schema_version {schema!r}; this tool seeds only "
            f"{RECORD_SCHEMA_VERSION}"
        )
    status = document.get("status")
    if status != "success":
        raise ResolutionImportError(
            f"{path} records status {status!r}; only a successful Resolution Record can be "
            "seeded (a failed one cannot be resumed, so it would claim a measurement it has not)"
        )
    return document


def _fingerprint_digest(path: Path, document: Mapping[str, object]) -> str:
    """The record's stored fingerprint digest, after proving it digests its own inputs."""
    fingerprints = document.get("fingerprints")
    if not isinstance(fingerprints, Mapping):
        raise ResolutionImportError(f"{path} carries no fingerprint block")
    absent = [key for key in REQUIRED_FINGERPRINTS if not fingerprints.get(key)]
    if absent:
        raise ResolutionImportError(
            f"{path} fingerprint block carries no {', '.join(absent)}; a record without them "
            "cannot be resumed and cannot be bound to a file"
        )
    digest = fingerprints["fingerprint_digest"]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ResolutionImportError(
            f"{path} carries no fingerprint digest; a record without one cannot be resumed"
        )
    recomputed = recompute_fingerprint_digest(fingerprints)
    if recomputed != digest:
        raise ResolutionImportError(
            f"{path} stores fingerprint digest {digest}, but its own fingerprint inputs digest "
            f"to {recomputed}; the record was edited after the resolver wrote it"
        )
    return digest


def _check_bound(
    path: Path,
    document: Mapping[str, object],
    analysis_id: str,
    intent: CollectionIntent,
) -> None:
    """The record's source identity, checked against what the collection acquired.

    The fingerprint binds the record to one file. That file must be the one the
    collection's own Acquisition Manifest acquired for this Analysis; the
    storage checksum the resolver measured is what the binding is proved with.
    """
    fingerprints = document["fingerprints"]
    entry = intent.acquired(analysis_id)
    if entry is None or not entry.ready:
        raise ResolutionImportError(
            f"{path}: the Source Collection records no acquired file for {analysis_id}"
        )
    recorded = {
        "source_file": entry.location,
        "source_recorded_sha256": entry.sha256,
        "source_recorded_bytes": entry.bytes,
    }
    for key, expected in recorded.items():
        actual = fingerprints.get(key)
        if actual != expected:
            raise ResolutionImportError(
                f"{path}: fingerprint {key} is {actual!r}, but the Source Collection acquired "
                f"{expected!r}; this record was not resolved against the acquired file"
            )


def _record_measurements(document: Mapping[str, object]) -> dict[str, object]:
    """The measurements the run's index summarises, derived from the record.

    It mirrors the resolver's own resumed-summary fields, so an index entry is
    comparable: an edited record no longer matches the run that wrote it.
    """
    ancestry = document.get("ancestry")
    ancestry = ancestry if isinstance(ancestry, Mapping) else {}
    phenotype = document.get("phenotype_sd")
    phenotype = phenotype if isinstance(phenotype, Mapping) else {}
    estimate = phenotype.get("estimate")
    estimate = estimate if isinstance(estimate, Mapping) else {}
    fingerprints = document["fingerprints"]
    resolution = fingerprints.get("resolution_config")
    resolution = resolution if isinstance(resolution, Mapping) else {}
    return {
        "assigned_ancestry": ancestry.get("assigned_ancestry") or None,
        "gate_reason": ancestry.get("gate_reason") or None,
        "original_sd": estimate.get("sd"),
        "original_sd_method": resolution.get("original_sd_method"),
        "status": document.get("status"),
        "record_file": f"{document['analysis_id']}.json",
    }


def _check_evidence(
    path: Path,
    data: bytes,
    document: Mapping[str, object],
    evidence: RunEvidence,
) -> None:
    """Bind the record's bytes and measurements to what the run recorded (#290 review).

    The run's index names the record and reports its measurements; the receipt
    binds either that index or each record's sha256. A record whose bytes or
    measurements disagree with them did not come from that run.
    """
    analysis_id = str(document["analysis_id"])
    if evidence.record_digests:
        recorded = evidence.record_digests.get(analysis_id)
        if recorded is None:
            raise ResolutionImportError(
                f"{path}: the run's receipt carries no record_digests entry for {analysis_id}, "
                "so this record is not the run's"
            )
        actual = hashlib.sha256(data).hexdigest()
        if actual != recorded:
            raise ResolutionImportError(
                f"{path}: the record's sha256 is {actual}, but the run's receipt bound "
                f"{recorded}; the record was edited after the run wrote it"
            )
    named = evidence.entries.get(analysis_id)
    if named is None:  # pragma: no cover - _run_evidence refuses an empty index, checked again below
        raise ResolutionImportError(
            f"{path}: the run's index.json does not name {analysis_id}, so this record is not "
            "the run's"
        )
    for key, actual in _record_measurements(document).items():
        expected = named.get(key)
        if actual != expected:
            raise ResolutionImportError(
                f"{path}: the record's {key} is {actual!r}, but the run's index.json recorded "
                f"{expected!r}; the record was edited after the run wrote it"
            )
    if evidence.statuses:
        recorded = evidence.statuses.get(analysis_id)
        if recorded != document.get("status"):
            raise ResolutionImportError(
                f"{path}: the run's receipt recorded status {recorded!r} for {analysis_id}, not "
                f"{document.get('status')!r}"
            )


def _run_evidence(source: Path) -> RunEvidence:
    """The run's index and its sibling ``receipt/``, or a refusal.

    The #221 runs kept their records in a Work Root and their receipt beside it
    (``../receipt/``), not beside the records. Both must exist and corroborate
    each other: absence is not "nothing to check" (#290 review).
    """
    index_path = source / "index.json"
    if not index_path.is_file():
        raise ResolutionImportError(
            f"{source} has no index.json, so nothing proves which records the run wrote; a "
            "record directory without its run's index cannot be seeded"
        )
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolutionImportError(f"{index_path} is not a resolver index: {exc}") from exc
    if not isinstance(index, dict) or not isinstance(index.get("analyses"), list):
        raise ResolutionImportError(f"{index_path} is not a resolver index: no analyses list")
    entries: dict[str, Mapping[str, object]] = {}
    for entry in index["analyses"]:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("analysis_id"), str):
            raise ResolutionImportError(f"{index_path} names an Analysis without an analysis_id")
        entries[entry["analysis_id"]] = entry
    if not entries:
        raise ResolutionImportError(
            f"{index_path} names no Analysis, so no record in {source} can be proved the run's"
        )

    receipt_path = source.parent / "receipt" / "resolution-receipt.json"
    if not receipt_path.is_file():
        raise ResolutionImportError(
            f"{source}: the run's receipt {receipt_path} is missing, so the index cannot be "
            "corroborated and nothing proves the records are the run's"
        )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolutionImportError(f"{receipt_path} is not a run receipt: {exc}") from exc
    if not isinstance(receipt, Mapping):
        raise ResolutionImportError(f"{receipt_path} is not a run receipt: not a JSON mapping")

    digests = receipt.get("record_digests")
    record_digests: dict[str, str] = {}
    if digests is not None:
        if not isinstance(digests, Mapping):
            raise ResolutionImportError(f"{receipt_path}: record_digests is not a mapping")
        for analysis_id, digest in digests.items():
            if not isinstance(digest, str) or len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ResolutionImportError(
                    f"{receipt_path}: record_digests[{analysis_id!r}] is not a lower-case sha256"
                )
            record_digests[str(analysis_id)] = digest

    records = receipt.get("records")
    recorded_index = records.get("index") if isinstance(records, Mapping) else None
    statuses: dict[str, str] = {}
    if isinstance(records, Mapping) and isinstance(records.get("statuses"), Mapping):
        statuses = {
            str(analysis_id): str(status)
            for analysis_id, status in records["statuses"].items()
        }
    if recorded_index is None and not record_digests:
        raise ResolutionImportError(
            f"{receipt_path} records neither the run's index nor its records' digests, so it "
            "corroborates nothing"
        )
    if recorded_index is not None and recorded_index != index:
        raise ResolutionImportError(
            f"{receipt_path} records a different index than {index_path}; this record directory "
            "is not the one the run recorded"
        )
    return RunEvidence(
        index_path=index_path,
        receipt_path=receipt_path,
        entries=entries,
        record_digests=record_digests,
        statuses=statuses,
    )


def _write_once(target: Path, data: bytes) -> None:
    """Write ``data`` through an exclusive temporary and a rename, never over a peer."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".importing")
    if temporary.exists():
        temporary.unlink()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, target)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--config", required=True,
        help="a release config of the Source Collection; its resolver settings choose the checkpoint",
    )
    parser.add_argument(
        "--source", required=True, action="append",
        help="a pre-#284 records directory (repeatable)",
    )
    parser.add_argument("--resolution-root", help="override the deployment resolution root")
    parser.add_argument(
        "--analysis", action="append",
        help="import only these Analysis ids (repeatable); default: every record",
    )
    parser.add_argument("--repo-root", help="repository root (default: the installed package's)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        from manifest_generation import REPO_ROOT

        repo_root = Path(args.repo_root) if args.repo_root else REPO_ROOT
        config = load_candidate_configuration(Path(args.config), repo_root)
        intent = load_collection_intent(config.base.inventory_provenance_path)
        area = ResolutionArea.for_collection(
            resolution_root(args.resolution_root, repo_root=repo_root),
            config.base.source_collection_id,
        )
        # The chain a release declares need not be on every host: its
        # declaration, not its bytes, composes this routing key (#292).
        digest = resolver_settings_digest(
            resolver_settings(config), declared_checksums=declared_setting_checksums(config)
        )
        total_imported = total_skipped = 0
        for source in args.source:
            result = import_records(
                Path(source), area, settings_digest=digest, intent=intent,
                analysis_ids=args.analysis,
            )
            total_imported += result.imported
            total_skipped += result.skipped
            print(
                f"imported {result.imported}, skipped {result.skipped} from {source} "
                f"into {area.checkpoint_dir(digest)}"
            )
        print(
            f"Resolution Area {area.root}: {total_imported} imported, {total_skipped} already present "
            f"(checkpoint {digest})"
        )
        return 0
    except (ResolutionImportError, ResolutionAreaError, FreezeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
