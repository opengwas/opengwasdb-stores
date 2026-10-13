"""Read/write audit hook injected into the resolver process (no env install).

The wrapper puts this directory first on ``PYTHONPATH`` and sets
``OPENGWASDB_PILOT_GUARD`` to a JSON config. Python imports ``sitecustomize`` at
startup, so the hook is active before the resolver reads anything.

Allowed:
  * interpreter/library reads (``.py``/``.pyc``/``.so``/``.pyd``, the Python
    prefix, ``/proc`` and ``/dev``);
  * reads of the explicitly allowlisted reference panels and the 15 source
    bodies;
  * writes only inside the fresh output root.

Denied (raises ``PermissionError``): reads of any other source/data path
(including ``.gz``/``.bgz``/``.bcf``/``.vcf``) and any write outside the root.
Counts are written to the configured log for the receipt.
"""

from __future__ import annotations

import atexit
import json
import os
import site
import sys

_CONFIG = json.loads(os.environ.get("OPENGWASDB_PILOT_GUARD", "{}"))
_ALLOW_READ = {os.path.realpath(p) for p in _CONFIG.get("allow_read", [])}
_ROOT = os.path.realpath(_CONFIG["allow_write_root"]) if _CONFIG.get("allow_write_root") else None
_LOG = _CONFIG.get("log")
_ENFORCE_READS = bool(_CONFIG.get("enforce_reads"))
_LIBRARY_SUFFIXES = (".py", ".pyc", ".pyo", ".so", ".pyd", ".dylib")
_DATA_SUFFIXES = (".gz", ".bgz", ".bcf", ".vcf", ".bgen", ".pgen", ".npz")
_COUNTS = {"allowed_reads": 0, "denied_reads": 0, "allowed_writes": 0, "denied_writes": 0, "denied": []}


def _under_root(path: str) -> bool:
    return bool(_ROOT) and (path == _ROOT or path.startswith(_ROOT + os.sep))


def _is_library(path: str) -> bool:
    if path.endswith(_LIBRARY_SUFFIXES):
        return True
    try:
        site_prefixes = tuple(site.getsitepackages()) + (site.getusersitepackages(),)
    except Exception:  # pragma: no cover - non-standard interpreters
        site_prefixes = ()
    prefixes = (
        sys.prefix,
        sys.base_prefix,
        *site_prefixes,
        "/usr/lib",
        "/usr/share",
        "/usr/local/lib",
        "/lib",
        "/etc",
        "/proc",
        "/dev",
        "/sys",
        "/snap",
    )
    return any(path.startswith(prefix) for prefix in prefixes)


def _audit(event: str, args: tuple) -> None:
    if event == "open":
        raw, mode, flags = args
        if isinstance(raw, int):
            return
        path = os.path.realpath(os.fsdecode(raw))
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing:
            if _under_root(path):
                _COUNTS["allowed_writes"] += 1
                return
            _COUNTS["denied_writes"] += 1
            _COUNTS["denied"].append(["write", path])
            raise PermissionError(f"pilot guard: write outside output root: {path}")
        if not _ENFORCE_READS or _is_library(path) or path in _ALLOW_READ:
            _COUNTS["allowed_reads"] += 1
            return
        _COUNTS["denied_reads"] += 1
        _COUNTS["denied"].append(["read", path])
        raise PermissionError(f"pilot guard: read outside allowlist: {path}")
    elif event in ("os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod", "os.truncate", "os.link", "os.symlink"):
        for raw in args[:2]:
            if raw is None or isinstance(raw, int):
                continue
            path = os.path.realpath(os.fsdecode(raw))
            if not _under_root(path):
                _COUNTS["denied_writes"] += 1
                _COUNTS["denied"].append([event, path])
                raise PermissionError(f"pilot guard: mutation outside output root: {path}")


def _dump() -> None:
    if _LOG:
        try:
            with open(_LOG, "w", encoding="utf-8") as handle:
                json.dump(_COUNTS, handle, indent=2, sort_keys=True)
        except OSError:
            pass


atexit.register(_dump)
sys.addaudithook(_audit)
