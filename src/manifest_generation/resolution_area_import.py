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
Selection is harmless: the resolver checks each Analysis's full fingerprint and
only the manifest's rows are accounted.

Usage:
    python3 -m manifest_generation.resolution_area_import \\
        --config resources/generators/dbgap-mvp/release-eur.yaml \\
        --source /data/opengwasdb/work/epic232/221/resolve-EUR-.../records
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from manifest_generation.candidate_workflow import (
    load_candidate_configuration,
    resolver_settings,
    resolver_settings_digest,
)
from manifest_generation.resolution_area import (
    ResolutionArea,
    ResolutionAreaError,
    resolution_root,
)


class ResolutionImportError(ValueError):
    """A pre-#284 record cannot be seeded into the Resolution Area as asked."""


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
    analysis_ids: Iterable[str] | None = None,
    holder: str = "manifest_generation.resolution_area_import",
) -> ImportResult:
    """Copy ``source``'s ``<analysis_id>.json`` records into ``area`` and keep them.

    ``settings_digest`` routes the records to the checkpoint the release's
    :func:`resolver_settings` will use. Only records whose Analysis is in
    ``analysis_ids`` (all of them when ``None``) are imported. Re-importing the
    same bytes is a skip; different bytes under one name are refused.
    """
    source = Path(source)
    if not source.is_dir():
        raise ResolutionImportError(f"record directory not found: {source}")
    wanted = None if analysis_ids is None else set(analysis_ids)
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
            document = _load_record(path, data, analysis_id)
            digest = _fingerprint_digest(path, document)
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


def _load_record(path: Path, data: bytes, analysis_id: str) -> Mapping[str, object]:
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
    return document


def _fingerprint_digest(path: Path, document: Mapping[str, object]) -> str:
    fingerprints = document.get("fingerprints")
    digest = fingerprints.get("fingerprint_digest") if isinstance(fingerprints, Mapping) else None
    if not isinstance(digest, str) or len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ResolutionImportError(
            f"{path} carries no fingerprint digest; a record without one cannot be resumed"
        )
    return digest


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
        area = ResolutionArea.for_collection(
            resolution_root(args.resolution_root, repo_root=repo_root),
            config.base.source_collection_id,
        )
        digest = resolver_settings_digest(resolver_settings(config))
        total_imported = total_skipped = 0
        for source in args.source:
            result = import_records(
                Path(source), area, settings_digest=digest,
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
    except (ResolutionImportError, ResolutionAreaError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
