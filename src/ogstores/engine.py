"""Which opengwasdb engine this is, and which one the lock expects (#253).

This is the one place that answers "which opengwasdb" for the repository:

* `identify(executable)` reads the commit and version an executable's own
  install records (its `direct_url.json` and `METADATA`), so a record names the
  engine that ran rather than whichever `opengwasdb` the current interpreter
  happens to import;
* `in_process()` answers the same question for code that imports opengwasdb
  and calls it in this process, where the imported engine *is* the one that ran;
* `locked_commit()` reads the commit `pixi.lock` resolved. The lock is the only
  place the commit is written: `pixi.toml` follows opengwasdb's `main`, and
  `pixi run engine-update` moves the lock;
* `mismatch(identity)` is the refusal both workflows give an engine that is not
  the locked one, in the same words.

An identity that cannot be proven is `UNAVAILABLE`, never a guess.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from ogstores import paths

UNAVAILABLE: str = "unavailable"
_HEX_40_OR_64: re.Pattern[str] = re.compile(r"\A(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")

OPENGWASDB_GIT_URL: str = "https://github.com/opengwas/opengwasdb"

#: The lock the repository's environments are installed from.
LOCK_PATH: Path = paths.REPO_ROOT / "pixi.lock"


class EnginePinError(RuntimeError):
    """`pixi.lock` does not name exactly one opengwasdb commit."""


@dataclass(frozen=True)
class EngineIdentity:
    """An opengwasdb engine: where it runs from, its commit and its version."""

    executable: str
    commit: str
    version: str

    def is_provable(self) -> bool:
        """True when the commit is an exact hash read from the install itself."""
        return is_exact_commit_hash(self.commit)


def is_exact_commit_hash(s: str | None) -> bool:
    """Return True if s is an exact 40-character or 64-character hex commit SHA."""
    return bool(isinstance(s, str) and _HEX_40_OR_64.fullmatch(s.strip()))


def find_executable(cmd: str = "opengwasdb", env: Mapping[str, str] | None = None) -> str:
    """Return the absolute path of the opengwasdb executable or `UNAVAILABLE`."""
    path_val = env.get("PATH") if env is not None else None
    resolved = shutil.which(cmd, path=path_val)
    if resolved:
        return str(Path(resolved).resolve())
    cmd_p = Path(cmd)
    if cmd_p.is_file():
        return str(cmd_p.resolve())
    return UNAVAILABLE


def commit_from_direct_url(direct_url: Any) -> str | None:
    """The exact commit a parsed `direct_url.json` records, or None."""
    if not isinstance(direct_url, dict):
        return None
    vcs_info = direct_url.get("vcs_info")
    if isinstance(vcs_info, dict) and is_exact_commit_hash(vcs_info.get("commit_id")):
        return str(vcs_info["commit_id"]).strip()
    if is_exact_commit_hash(direct_url.get("commit_id")):
        return str(direct_url["commit_id"]).strip()
    return None


def _commit_from_direct_url_file(direct_url_p: Path) -> str | None:
    try:
        return commit_from_direct_url(json.loads(direct_url_p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _version_from_metadata_file(metadata_p: Path) -> str | None:
    """Read `Version:` from a dist-info METADATA file, or None."""
    try:
        for line in metadata_p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip() or None
    except OSError:
        return None
    return None


def _metadata_roots(executable: str, env: Mapping[str, str] | None = None) -> list[Path]:
    """Candidate install-prefix roots to read `opengwasdb*.dist-info` metadata from.

    An explicit executable is trusted over ambient process metadata: a record
    must name the engine it actually ran, not whichever `opengwasdb` happens to
    be importable by the current interpreter.
    """
    exe_p = Path(executable).resolve()
    roots = [exe_p.parents[1] if len(exe_p.parents) > 1 else exe_p.parent, exe_p.parent]
    for name in ("PYTHONPATH", "CONDA_PREFIX", "VIRTUAL_ENV"):
        value = env.get(name) if env else None
        if not value:
            continue
        entries = value.split(os.pathsep) if name == "PYTHONPATH" else [value]
        roots.extend(Path(entry.strip()).resolve() for entry in entries if entry.strip())
    return roots


def _dist_info_files(root: Path, filename: str) -> list[str]:
    """The `opengwasdb*.dist-info/<filename>` files under one install root."""
    patterns = [
        root / "lib" / "python*" / "site-packages" / "opengwasdb*.dist-info" / filename,
        root / "Lib" / "site-packages" / "opengwasdb*.dist-info" / filename,
        root / "opengwasdb*.dist-info" / filename,
    ]
    return [match for pattern in patterns for match in glob.glob(str(pattern))]


def executable_commit(executable: str, env: Mapping[str, str] | None = None) -> str:
    """The exact commit `executable`'s own install records, or `UNAVAILABLE`."""
    if not executable or executable == UNAVAILABLE:
        return UNAVAILABLE
    for root in _metadata_roots(executable, env):
        for match in _dist_info_files(root, "direct_url.json"):
            commit = _commit_from_direct_url_file(Path(match))
            if commit:
                return commit
    return UNAVAILABLE


def executable_version(executable: str, env: Mapping[str, str] | None = None) -> str:
    """The version `executable`'s own install records, or `UNAVAILABLE`."""
    if not executable or executable == UNAVAILABLE:
        return UNAVAILABLE
    for root in _metadata_roots(executable, env):
        for match in _dist_info_files(root, "METADATA"):
            version = _version_from_metadata_file(Path(match))
            if version:
                return version
    return UNAVAILABLE


def identify(executable: str | None = None, env: Mapping[str, str] | None = None) -> EngineIdentity:
    """The engine `executable` runs (the one on `PATH` when it is None).

    Commit and version both come from that executable's own install, never one
    from it and the other from the current interpreter.
    """
    resolved = executable if executable is not None else find_executable(env=env)
    return EngineIdentity(
        executable=resolved,
        commit=executable_commit(resolved, env),
        version=executable_version(resolved, env),
    )


def in_process() -> EngineIdentity:
    """The engine this interpreter imports, for code that runs opengwasdb in-process.

    Use `identify` for anything run as a subprocess: the interpreter's own
    install says nothing about an executable found on `PATH`.
    """
    import importlib.metadata as metadata

    try:
        dist = metadata.distribution("opengwasdb")
    except metadata.PackageNotFoundError:
        return EngineIdentity(executable="in-process", commit=UNAVAILABLE, version=UNAVAILABLE)
    try:
        commit = commit_from_direct_url(json.loads(dist.read_text("direct_url.json") or "null"))
    except ValueError:
        commit = None
    return EngineIdentity(
        executable="in-process",
        commit=commit or UNAVAILABLE,
        version=str(dist.version or "").strip() or UNAVAILABLE,
    )


def _is_engine_url(spec: Any) -> bool:
    """True for a lock reference to the opengwasdb repository itself, exactly."""
    if not isinstance(spec, str) or not spec.startswith("git+"):
        return False
    parts = urlsplit(spec[len("git+"):])
    return (
        parts.scheme == "https"
        and parts.netloc == "github.com"
        and parts.path.removesuffix(".git") == "/opengwas/opengwasdb"
    )


def _lock_engine_references(document: Any) -> list[str]:
    """Every opengwasdb reference the parsed lock carries: per environment, and package."""
    references: list[str] = []
    for environment in (document.get("environments") or {}).values():
        for packages in (environment.get("packages") or {}).values():
            references.extend(
                entry["pypi"] for entry in packages if _is_engine_url(entry.get("pypi"))
            )
    references.extend(
        entry["pypi"] for entry in document.get("packages") or [] if _is_engine_url(entry.get("pypi"))
    )
    return references


def lock_pin_revisions(lock: Path) -> set[str]:
    """The opengwasdb commits `lock` resolves, one per distinct reference.

    Every opengwasdb entry is parsed, per environment and per package. A
    reference without an exact 40-hex commit fragment is reported as itself so
    that the caller's "exactly one" check refuses it rather than skipping it.
    """
    document = yaml.load(lock.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader) or {}
    commits: set[str] = set()
    for reference in _lock_engine_references(document):
        fragment = urlsplit(reference[len("git+"):]).fragment
        commits.add(fragment if re.fullmatch(r"[0-9a-f]{40}", fragment) else reference)
    return commits


def locked_commit(lock: Path | None = None) -> str:
    """The one opengwasdb commit `pixi.lock` resolves; refuses none, several or a malformed one."""
    lock_p = lock if lock is not None else LOCK_PATH
    commits = lock_pin_revisions(lock_p)
    if len(commits) != 1 or not is_exact_commit_hash(next(iter(commits))):
        raise EnginePinError(
            f"{lock_p} must resolve exactly one opengwasdb commit, but it resolves "
            f"{len(commits)}: {sorted(commits)}. Run `pixi run engine-update` to relock."
        )
    return commits.pop()


def mismatch(identity: EngineIdentity, expected: str) -> str | None:
    """Why `identity` is not the locked engine `expected`, or None when it is.

    Both workflows, Manifest Generation and Store Release Build, refuse with
    this message, so an operator sees one explanation whichever one stopped.
    """
    if identity.executable == UNAVAILABLE:
        return (
            "no opengwasdb executable was found on PATH. Run through `pixi run`, which "
            "puts the environment's locked engine there."
        )
    if identity.is_provable() and identity.commit == expected:
        return None
    return (
        f"the opengwasdb at {identity.executable!r} reports commit {identity.commit!r}, "
        f"which is not the commit pixi.lock resolves ({expected}). A run must use the "
        "locked engine and prove it from the executable's own install metadata. Run "
        "`pixi install` so the environment matches the lock, or `pixi run engine-update` "
        "to move the lock; never substitute another executable on PATH."
    )


__all__ = [
    "EngineIdentity",
    "EnginePinError",
    "LOCK_PATH",
    "OPENGWASDB_GIT_URL",
    "UNAVAILABLE",
    "commit_from_direct_url",
    "executable_commit",
    "executable_version",
    "find_executable",
    "identify",
    "in_process",
    "is_exact_commit_hash",
    "lock_pin_revisions",
    "locked_commit",
    "mismatch",
]
