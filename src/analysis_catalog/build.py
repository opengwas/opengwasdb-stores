"""Generate the Analysis Catalog from the Release Bundles of a registry (ADR 0039).

`build_catalog` is the one entry point. It reads every registered bundle through
the Release Bundle contract, decides each one under the serving policy, and
publishes a version under `<artifact-root>/catalog/` unless that version is
already there. It never opens a Store (ADR 0023).
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ogstores import bundle as release_bundle
from ogstores.paths import is_valid_store_id

from analysis_catalog import database, publish, report
from analysis_catalog.policy import Decision, decide


class CatalogError(RuntimeError):
    """The catalog cannot be built, and nothing was written."""


@dataclass(frozen=True)
class BuildResult:
    version: str
    # A new version directory was written.
    published: bool
    # `current` was moved to this version.
    repointed: bool
    path: Path
    decisions: tuple[Decision, ...]


def build_catalog(
    registry: Path | str,
    artifact_root: Path | str,
    *,
    built_at: str | None = None,
) -> BuildResult:
    registry, artifact_root = Path(registry), Path(artifact_root)
    commit = registry_commit(registry)
    decisions = tuple(
        decide(release_bundle.load(store_id, registry_root=registry), registry)
        for store_id in registered_store_ids(registry)
    )
    version = publish.compute_version(decisions, registry)
    final = publish.versions_dir(artifact_root) / version
    if final.is_dir():
        repointed = publish.current_target(artifact_root) != version
        if repointed:
            publish.swap_current(artifact_root, version)
        return BuildResult(version, False, repointed, final, decisions)

    built_at = built_at or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    previous = publish.current_target(artifact_root)
    partial = final.with_name(final.name + publish.PARTIAL_SUFFIX)
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    try:
        _write_version(
            partial, decisions, version=version, built_at=built_at, registry_commit=commit,
            previous=(publish.versions_dir(artifact_root) / previous, previous) if previous else None,
        )
        partial.rename(final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    publish.swap_current(artifact_root, version)
    publish.prune(artifact_root)
    return BuildResult(version, True, True, final, decisions)


def _write_version(
    directory: Path,
    decisions: tuple[Decision, ...],
    *,
    version: str,
    built_at: str,
    registry_commit: str | None,
    previous: tuple[Path, str] | None,
) -> None:
    path = directory / database.DATABASE
    database.write_database(
        path, decisions, version=version, built_at=built_at, registry_commit=registry_commit
    )
    change = None
    if previous is not None:
        connection = sqlite3.connect(path)
        try:
            change = report.compare(
                connection, previous[0] / database.DATABASE, previous[1], directory / report.CHANGES_TSV
            )
        finally:
            connection.close()
    database.write_exports(directory)
    (directory / "report.md").write_text(
        report.render(
            list(decisions), version=version, built_at=built_at,
            registry_commit=registry_commit, change=change,
        ),
        encoding="utf-8",
    )


def registered_store_ids(registry: Path) -> list[str]:
    """Every `OGS-` directory of the registry: each is a registered Store Release."""
    if not registry.is_dir():
        raise CatalogError(f"registry {registry} is not a directory")
    return sorted(p.name for p in registry.iterdir() if p.is_dir() and is_valid_store_id(p.name))


def registry_commit(registry: Path) -> str | None:
    """The commit the registry is at, or None when it is not in a git repository.

    Refuses when the registry has uncommitted changes: the recorded commit
    would then not describe the bundles that were read.
    """
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(registry), *args], capture_output=True, text=True)

    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return None
    dirty = git("status", "--porcelain", "--untracked-files=all", "--", ".")
    if dirty.returncode != 0:
        raise CatalogError(f"cannot read the git status of {registry}: {dirty.stderr.strip()}")
    if dirty.stdout.strip():
        raise CatalogError(
            f"{registry} has uncommitted changes, so the recorded registry commit would be false; "
            "commit them or build from a clean checkout:\n" + dirty.stdout.rstrip()
        )
    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        raise CatalogError(f"cannot read the commit of {registry}: {head.stderr.strip()}")
    return head.stdout.strip()
