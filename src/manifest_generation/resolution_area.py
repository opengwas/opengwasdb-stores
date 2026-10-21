"""The shared Resolution Area: Resolution Records kept once per Source Collection (#284).

ADR 0036: a Resolution Record belongs to the Analysis. It is kept once per
Source Collection, keyed by the resolver's fingerprint, and reused by every
Selection; each bundle's resolution receipt names the records it used. So
Analysis Resolution writes into one area per Source Collection rather than into
each Store Release's Work Root:

    <resolution-root>/<source-collection-id>/
      .lock                     held by the run resolving here, and its resolver
      checkpoint/<settings-digest>/
                                `opengwasdb resolve-analyses`' records directory
        <analysis_id>.json        for one set of resolver settings: its latest
        index.json                record per Analysis, which `--resume` reuses
                                  when the fingerprint still matches; the index
                                  describes the last run only
      records/<analysis_id>/<fingerprint_digest>/<record_sha256>.json
                                the kept Resolution Records, write-once

**The resolver decides reuse.** Each checkpoint is the resolver's own records
directory. It skips an Analysis whose existing successful record carries the
fingerprint digest this run would produce -- the same file, references and
settings -- and re-resolves it otherwise (opengwasdb ADR 0045). A
``controlled_failure`` record is always re-run. Nothing here predicts a
fingerprint.

**The settings digest only routes.** A checkpoint holds one record per
Analysis, so two Selections resolving under different settings would evict
each other's records from a single checkpoint, and each return would
re-resolve, producing a new record (the resolver's timings differ) for an
unchanged fingerprint. So each set of resolver settings -- the references and
their checksums, gates, floors, panels and tool revision, everything
``resolver_argv`` passes except the manifest, output directory and
parallelism -- gets its own checkpoint. The digest is only a routing key: the
resolver still checks every Analysis's full fingerprint on ``--resume``, so a
setting the digest misses causes a re-resolve, never a wrong reuse. Per-Analysis
inputs (the file, method tier, sample size and MAF/INFO requests) are not in
the digest; they are in each record's fingerprint.

**Kept records are never replaced.** A receipt never names a checkpoint file.
After a run, each of its Selection's records is copied into ``records/``,
addressed by its fingerprint digest and its own checksum. A kept file is
written once; keeping different bytes under an existing name is refused.

**One run at a time.** A checkpoint's index is one file and its records are
written in place, so two runs sharing an area would interleave.
:meth:`ResolutionArea.exclusive` takes an exclusive ``flock`` on ``.lock`` and
refuses a second run. The run hands the locked descriptor to the resolver
subprocess, so the lock is held until both have exited: a killed run cannot
let another interleave with its still-running resolver. ``flock`` must be
honoured by the area's filesystem across every host that runs Manifest
Generation (a local filesystem, or NFS with locking). Verifying a candidate
reads only kept records, which never change, so it takes no lock.

**The root is deployment configuration**, resolved like the Artifact Root
(:func:`resolution_root`), never a release config key: two releases over one
Source Collection must find the same area and the same lock.

Store Release Build reads nothing here (``tests/packaging/test_boundary.py``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from manifest_generation.safe_write import SafeWriteError, atomic_write_bytes
from ogstores.paths import repo_config_path

#: The built-in fallback when neither the flag, the environment nor the
#: repository's ``ogstores.yaml`` names a root: beside the candidate workflow's
#: Work Roots under /data/opengwasdb/work.
DEFAULT_RESOLUTION_ROOT: Path = Path("/data/opengwasdb/work/resolution")
RESOLUTION_ROOT_ENV_VAR: str = "OPENGWASDB_RESOLUTION_ROOT"
#: The ``ogstores.yaml`` key, beside ``artifact_root``.
RESOLUTION_ROOT_CONFIG_KEY: str = "resolution_root"

LOCK_FILENAME = ".lock"
CHECKPOINT_DIRNAME = "checkpoint"
RECORDS_DIRNAME = "records"
#: The keys of one receipt entry, as :meth:`KeptRecord.as_receipt_entry` writes it.
RECEIPT_ENTRY_KEYS: frozenset[str] = frozenset({"fingerprint_digest", "record_sha256", "record"})


class ResolutionAreaError(ValueError):
    """A Resolution Area, or a record in it, cannot be used as asked."""


class ResolutionAreaLocked(ResolutionAreaError):
    """Another run, or its resolver, holds the area's lock."""


def resolution_root(
    override: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    repo_root: Path | str | None = None,
) -> Path:
    """Resolve the resolution root, in the Artifact Root's precedence order.

    1. the run's ``--resolution-root``
    2. the ``OPENGWASDB_RESOLUTION_ROOT`` environment variable
    3. the repository's ``ogstores.yaml`` ``resolution_root``
    4. :data:`DEFAULT_RESOLUTION_ROOT`

    A value that is empty or relative, or a malformed ``ogstores.yaml``, is
    refused rather than falling back: a wrong root silently splits a Source
    Collection's records and its lock.
    """
    if override is not None:
        return _absolute(str(override), "--resolution-root")
    env_map = os.environ if env is None else env
    env_value = env_map.get(RESOLUTION_ROOT_ENV_VAR)
    if env_value is not None:
        return _absolute(env_value, RESOLUTION_ROOT_ENV_VAR)
    config = repo_config_path(repo_root)
    if config.is_file():
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        if data is not None and not isinstance(data, dict):
            raise ResolutionAreaError(f"repository config {config} must be a YAML mapping")
        if isinstance(data, dict) and RESOLUTION_ROOT_CONFIG_KEY in data:
            value = data[RESOLUTION_ROOT_CONFIG_KEY]
            if not isinstance(value, str):
                raise ResolutionAreaError(
                    f"repository config {config} '{RESOLUTION_ROOT_CONFIG_KEY}' must be a string"
                )
            return _absolute(value, f"{config} '{RESOLUTION_ROOT_CONFIG_KEY}'")
    return DEFAULT_RESOLUTION_ROOT


def _absolute(value: str, where: str) -> Path:
    path = Path(value.strip())
    if not value.strip() or not path.is_absolute():
        raise ResolutionAreaError(
            f"{where} {value!r} must be an absolute path: every Selection of a Source "
            "Collection must find the same Resolution Area"
        )
    return path


@dataclass(frozen=True)
class KeptRecord:
    """One kept Resolution Record, as a receipt names it."""

    analysis_id: str
    fingerprint_digest: str
    record_sha256: str

    def __post_init__(self) -> None:
        _require_segment(self.analysis_id, "analysis_id")
        _require_hex_digest(self.fingerprint_digest, "fingerprint_digest")
        _require_hex_digest(self.record_sha256, "record_sha256")

    @property
    def relative_path(self) -> str:
        """The record's path within its Resolution Area."""
        return (
            f"{RECORDS_DIRNAME}/{self.analysis_id}/{self.fingerprint_digest}/"
            f"{self.record_sha256}.json"
        )

    def as_receipt_entry(self) -> dict[str, str]:
        return {
            "fingerprint_digest": self.fingerprint_digest,
            "record_sha256": self.record_sha256,
            "record": self.relative_path,
        }

    @classmethod
    def from_receipt_entry(cls, analysis_id: str, entry: Any) -> "KeptRecord":
        """Parse one receipt entry, refusing a malformed or inconsistent one."""
        if not isinstance(entry, Mapping):
            raise ResolutionAreaError(f"{analysis_id}: the receipt's record entry is not a mapping")
        keys = set(entry)
        if keys != RECEIPT_ENTRY_KEYS:
            raise ResolutionAreaError(
                f"{analysis_id}: the receipt's record entry has keys {sorted(keys)}, "
                f"expected {sorted(RECEIPT_ENTRY_KEYS)}"
            )
        kept = cls(
            analysis_id,
            _require_str(entry["fingerprint_digest"], f"{analysis_id}: fingerprint_digest"),
            _require_str(entry["record_sha256"], f"{analysis_id}: record_sha256"),
        )
        if entry["record"] != kept.relative_path:
            raise ResolutionAreaError(
                f"{analysis_id}: the receipt names record {entry['record']!r}, but its "
                f"digest and checksum place it at {kept.relative_path!r}"
            )
        return kept


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ResolutionAreaError(f"{what} is not a string: {value!r}")
    return value


def _require_hex_digest(value: str, what: str) -> str:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ResolutionAreaError(f"{what} {value!r} is not a SHA-256 hex digest")
    return value


def _require_segment(value: str, what: str) -> str:
    """Refuse anything that is not exactly one plain path segment."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or "\0" in value
    ):
        raise ResolutionAreaError(f"{what} {value!r} is not a single path segment")
    return value


@dataclass(frozen=True)
class ResolutionArea:
    """One Source Collection's Resolution Area under the resolution root."""

    root: Path

    @classmethod
    def for_collection(
        cls, resolution_root: Path | str, source_collection_id: str
    ) -> "ResolutionArea":
        segment = _require_segment(str(source_collection_id), "source_collection_id")
        return cls(Path(resolution_root) / segment)

    @classmethod
    def from_receipt(cls, value: Any) -> "ResolutionArea":
        """The area a receipt names, refusing a missing or relative one."""
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ResolutionAreaError(
                f"the resolution receipt's resolution_area {value!r} is not an absolute path"
            )
        return cls(Path(value))

    def checkpoint_dir(self, settings_digest: str) -> Path:
        """The resolver's records directory for one set of resolver settings."""
        _require_hex_digest(settings_digest, "settings_digest")
        return self.root / CHECKPOINT_DIRNAME / settings_digest

    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_FILENAME

    def record_path(self, kept: KeptRecord) -> Path:
        return self.root / kept.relative_path

    @contextmanager
    def exclusive(self, holder: str) -> Iterator[int]:
        """Hold the area's lock for one run, or refuse at once if it is held.

        Yields the locked descriptor. Pass it to the resolver subprocess
        (``pass_fds``) so the lock outlives a killed run while its resolver is
        still writing: an ``flock`` belongs to the open file description, and is
        released only when every descriptor sharing it is closed. The lock file
        is never removed. The holder writes who it is into the file, and a
        refused run quotes it.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o664)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder_text = os.pread(fd, 4096, 0).decode("utf-8", "replace").strip()
                raise ResolutionAreaLocked(
                    f"Resolution Area {self.root} is locked by another run or its resolver "
                    f"({holder_text or 'no holder recorded'}); two runs must not resolve "
                    "into one Source Collection's area at once. Wait for it to finish, "
                    "then re-run."
                ) from None
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps(_holder_record(holder)).encode("utf-8") + b"\n", 0)
            yield fd
        finally:
            # Closing, never LOCK_UN: an unlock would release a resolver that
            # still shares the descriptor.
            os.close(fd)

    def keep(self, checkpoint_dir: Path, analysis_id: str) -> KeptRecord:
        """Keep one checkpoint record; return what a receipt names.

        Call this only while holding :meth:`exclusive`, after the run's records
        were accounted. The record is copied byte for byte under the fingerprint
        digest it carries, through an exclusive temporary and a rename, with the
        directory synced. An existing kept file must hold the same bytes; a
        difference is refused, never overwritten.
        """
        source = checkpoint_dir / f"{_require_segment(analysis_id, 'analysis_id')}.json"
        data = source.read_bytes()
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResolutionAreaError(f"{source} is not a resolver record: {exc}") from exc
        fingerprints = record.get("fingerprints") if isinstance(record, dict) else None
        digest = fingerprints.get("fingerprint_digest") if isinstance(fingerprints, dict) else None
        if not isinstance(digest, str):
            raise ResolutionAreaError(f"{source} carries no fingerprint digest")
        kept = KeptRecord(analysis_id, digest, hashlib.sha256(data).hexdigest())
        target = self.record_path(kept)
        if target.exists():
            if target.read_bytes() != data:
                raise ResolutionAreaError(
                    f"kept Resolution Record {target} exists and its bytes differ from "
                    f"{source}; a kept record is never replaced"
                )
            return kept
        try:
            atomic_write_bytes(target, data, self.root)
        except SafeWriteError as exc:
            raise ResolutionAreaError(f"cannot keep {source}: {exc}") from exc
        return kept

    def read_kept(self, kept: KeptRecord) -> bytes:
        """The bytes of one kept record, refused if missing or not what it was bound as."""
        path = self.record_path(kept)
        if not path.is_file():
            raise ResolutionAreaError(f"{kept.analysis_id}: kept Resolution Record is missing ({path})")
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != kept.record_sha256:
            raise ResolutionAreaError(
                f"{kept.analysis_id}: kept Resolution Record {path} has sha256 {actual} but "
                f"the resolution receipt bound record_sha256 {kept.record_sha256}; the record "
                "was edited after it was kept"
            )
        return data


def _holder_record(holder: str) -> Mapping[str, Any]:
    return {
        "holder": holder,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


__all__ = [
    "CHECKPOINT_DIRNAME",
    "DEFAULT_RESOLUTION_ROOT",
    "LOCK_FILENAME",
    "RECEIPT_ENTRY_KEYS",
    "RECORDS_DIRNAME",
    "RESOLUTION_ROOT_CONFIG_KEY",
    "RESOLUTION_ROOT_ENV_VAR",
    "KeptRecord",
    "ResolutionArea",
    "ResolutionAreaError",
    "ResolutionAreaLocked",
    "resolution_root",
]
