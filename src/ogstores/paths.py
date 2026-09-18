"""Artifact layout for OpenGWASDB Store Releases.

Every path is a pure function of the Store Release id alone, because ADR 0022
made the id the directory name under the artifact root as well as in the
registry. Nothing else in this package constructs an artifact path, so a
completed release can resolve its parent's Store from `derived_from` alone,
with no registry lookup.

    <artifact-root>/OGS-00042/
        source/                 acquired or filtered source files
        work/                   checkpoints, scratch
        work/analyses.tsv       derived build manifest: the bundle's analyses.tsv
                                with `exclude_from_build` rows removed
        work/analyses.exclusions.json   audit sidecar naming the dropped rows
        records/<step>.json     one per executed step
        store.opengwasdb        the Store Release
        store.opengwasdb.partial    transient; renamed into place on success
    <artifact-root>/by-label/   generated symlinks

The build manifest is derived, not authored: `manifest.py` materialises it and
`plan()` points every build-phase command at it, so the builder never sees an
excluded audit row (ADR 0025).

The root itself is deployment configuration, not a Release Bundle fact.
`artifact_root()` resolves it from a workflow config override, then
`OPENGWASDB_ARTIFACT_ROOT`, then the repository's `ogstores.yaml`, then
`DEFAULT_ARTIFACT_ROOT` (issue #126). Every other function here takes the root
as an argument, so a caller that resolves once constructs every path under it.

See docs/spec/store-release-workflow.md and ADR 0022.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_ROOT: Path = Path("/data/opengwasdb/stores")
ARTIFACT_ROOT_ENV_VAR: str = "OPENGWASDB_ARTIFACT_ROOT"
REPO_CONFIG_FILENAME: str = "ogstores.yaml"
STORE_ID_PATTERN: re.Pattern[str] = re.compile(r"\AOGS-\d{5}\Z")


def is_valid_store_id(store_id: str) -> bool:
    """Return True if `store_id` conforms to the opaque `OGS-` 5-digit format."""
    return bool(isinstance(store_id, str) and STORE_ID_PATTERN.fullmatch(store_id))


def require_valid_store_id(store_id: str) -> str:
    """Return `store_id` if valid; raise ValueError otherwise."""
    if not is_valid_store_id(store_id):
        raise ValueError(
            f"Invalid store_id format {store_id!r}; must match pattern 'OGS-\\d{{5}}' (ADR 0022)"
        )
    return store_id


def repo_config_path(repo_root: Path | str | None = None) -> Path:
    """Path to the repository configuration file (`ogstores.yaml`)."""
    base = Path(repo_root) if repo_root is not None else REPO_ROOT
    return base / REPO_CONFIG_FILENAME


def artifact_root(
    config_override: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    repo_root: Path | str | None = None,
) -> Path:
    """Resolve the artifact root, in precedence order (issue #126).

    1. workflow config override (Snakemake `--config artifact_root=`)
    2. the `OPENGWASDB_ARTIFACT_ROOT` environment variable
    3. the repository configuration file's `artifact_root` key
    4. `DEFAULT_ARTIFACT_ROOT`

    The artifact root is deployment configuration, not a Release Bundle fact:
    a bundle is immutable once accepted, yet the same bundle must be buildable
    on CI, a developer laptop, and the production host. Reading it from a
    bundle would make the bundle non-portable and hardcode one machine's
    filesystem into the master list's `build_command` (issue #123).

    An existing but malformed config file raises rather than silently falling
    back: a wrong root that looks like a right one is the worst outcome.
    """
    if config_override is not None and str(config_override).strip():
        return Path(config_override)

    env_map = os.environ if env is None else env
    env_value = env_map.get(ARTIFACT_ROOT_ENV_VAR)
    if isinstance(env_value, str) and env_value.strip():
        return Path(env_value)

    config_p = repo_config_path(repo_root)
    if config_p.is_file():
        with open(config_p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is not None and not isinstance(data, dict):
            raise ValueError(
                f"Repository config {config_p} must be a YAML mapping carrying "
                f"an 'artifact_root' key"
            )
        if isinstance(data, dict) and "artifact_root" in data:
            value = data["artifact_root"]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Repository config {config_p} 'artifact_root' must be a "
                    f"non-empty string"
                )
            return Path(value)

    return DEFAULT_ARTIFACT_ROOT


def store_dir(store_id: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Directory for all artifacts of one Store Release: `<root>/<store_id>`."""
    return Path(root) / store_id


def release_root(store_id: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Alias for `store_dir`."""
    return store_dir(store_id, root=root)


def source_dir(store_id: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Directory for acquired or filtered source files: `<root>/<store_id>/source`."""
    return store_dir(store_id, root=root) / "source"


def work_dir(store_id: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Directory for checkpoints and scratch files: `<root>/<store_id>/work`."""
    return store_dir(store_id, root=root) / "work"


def build_manifest_path(store_id: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Derived build manifest: `<root>/<store_id>/work/analyses.tsv`.

    The bundle's `analyses.tsv` filtered of `exclude_from_build` rows. Every
    build-phase command consumes this path, never the bundle row (ADR 0025).
    """
    return work_dir(store_id, root=root) / "analyses.tsv"


def build_manifest_sidecar_path(
    store_id: str, root: Path | str = DEFAULT_ARTIFACT_ROOT
) -> Path:
    """Exclusion audit sidecar: `<root>/<store_id>/work/analyses.exclusions.json`."""
    return work_dir(store_id, root=root) / "analyses.exclusions.json"


def records_dir(store_id: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Directory for Step execution record JSON files: `<root>/<store_id>/records`."""
    return store_dir(store_id, root=root) / "records"


def record_path(store_id: str, step: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Path to one Step execution record: `<root>/<store_id>/records/<step>.json`."""
    return records_dir(store_id, root=root) / f"{step}.json"


def store_path(store_id: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Path to the built Store Release artifact: `<root>/<store_id>/store.opengwasdb`."""
    return store_dir(store_id, root=root) / "store.opengwasdb"


def partial_store_path(store_id: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Transient build destination: `<root>/<store_id>/store.opengwasdb.partial`."""
    return store_dir(store_id, root=root) / "store.opengwasdb.partial"


def by_label_dir(root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Directory for generated by-label symlinks: `<root>/by-label`."""
    return Path(root) / "by-label"


def by_label_link(
    store_id: str, label: str, root: Path | str = DEFAULT_ARTIFACT_ROOT
) -> Path:
    """Path to a label symlink under the artifact root: `<root>/by-label/<label>`."""
    return by_label_dir(root=root) / label


def parent_store_path(derived_from: str, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> Path:
    """Path to a parent release's Store: `<root>/<derived_from>/store.opengwasdb`."""
    return store_path(derived_from, root=root)


__all__ = [
    "ARTIFACT_ROOT_ENV_VAR",
    "DEFAULT_ARTIFACT_ROOT",
    "REPO_CONFIG_FILENAME",
    "REPO_ROOT",
    "STORE_ID_PATTERN",
    "artifact_root",
    "build_manifest_path",
    "build_manifest_sidecar_path",
    "by_label_dir",
    "by_label_link",
    "is_valid_store_id",
    "parent_store_path",
    "partial_store_path",
    "record_path",
    "records_dir",
    "release_root",
    "repo_config_path",
    "require_valid_store_id",
    "source_dir",
    "store_dir",
    "store_path",
    "work_dir",
]
