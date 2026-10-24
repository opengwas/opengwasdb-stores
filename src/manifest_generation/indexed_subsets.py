"""Default Indexed Variant Subset declarations for generated bundles (stores #210).

Epic opengwas/opengwasdb-stores#214 makes the maintained HapMap3 GRCh38 Indexed
Variant Subset the default derived artifact of every future Dense or Hybrid
Store Release. This module is the generator-side half of that decision: it
resolves the `indexed_subsets` block every Dense and Hybrid `build.yaml` writer
emits, from the single maintained Reference Resource declaration at
`resources/reference-resources/hapmap3-hg38/resource.yaml` (stores #208). No
generator copies the list path, assembly or checksum.

The default is a default, not a requirement. An operator can still omit it
deliberately on a Dense or Hybrid release by setting the generator config's
`indexed_subsets` to an explicit empty list together with a written
`indexed_subsets_opt_out` reason; the reason is carried into `release.yaml`
notes so the omission is reviewable. An empty list with no reason, a malformed
entry, or any declaration on a Ragged release is a generator error rather than
a silent omission (CONTRIBUTING's fail-loud rule).

Ragged releases have no Dense Component, so `resolve_indexed_subsets` returns
`None` for them and the writer emits no `indexed_subsets` key at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

#: Top-level `build.yaml` key #209 defines and the generator config key #210 adds.
INDEXED_SUBSETS_KEY: str = "indexed_subsets"
#: Generator-config key recording why a Dense/Hybrid release deliberately omits
#: the default. It is not a Build Recipe field.
INDEXED_SUBSETS_OPT_OUT_KEY: str = "indexed_subsets_opt_out"
#: Layouts with a self-contained Dense Component; Ragged has none (ADR 0061).
INDEXED_SUBSET_LAYOUTS: frozenset[str] = frozenset({"dense", "hybrid"})
DEFAULT_INDEXED_SUBSET_NAME: str = "hm3"
DEFAULT_INDEXED_SUBSET_RESOURCE_ID: str = "hapmap3-hg38"
_REQUIRED_ENTRY_KEYS: tuple[str, ...] = (
    "name",
    "resource_id",
    "variant_list",
    "reference_assembly",
)


class IndexedSubsetConfigError(ValueError):
    """A generator config's `indexed_subsets` block cannot be honoured."""


def _repo_root(repo_root: Path | str | None) -> Path:
    from manifest_generation import REPO_ROOT

    return Path(repo_root) if repo_root is not None else REPO_ROOT


def load_variant_list_resource(
    resource_id: str, repo_root: Path | str | None = None
) -> dict[str, Any]:
    """Load one maintained `kind: variant_list` Reference Resource declaration.

    The generator only reads the declaration, never the list itself: the list is
    a large external artifact the run-time verifier hashes before a build.
    """
    path = (
        _repo_root(repo_root)
        / "resources"
        / "reference-resources"
        / resource_id
        / "resource.yaml"
    )
    if not path.is_file():
        raise IndexedSubsetConfigError(
            f"resource_id {resource_id!r} names no maintained Reference Resource: "
            f"{path} does not exist"
        )
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise IndexedSubsetConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(document, Mapping):
        raise IndexedSubsetConfigError(f"{path}: declaration is not a mapping")
    if document.get("kind") != "variant_list":
        raise IndexedSubsetConfigError(
            f"resource_id {resource_id!r} has kind {document.get('kind')!r}, "
            "not 'variant_list'"
        )
    return dict(document)


def default_hm3_declaration(repo_root: Path | str | None = None) -> dict[str, str]:
    """The default `hm3` declaration, sourced from the maintained resource."""
    document = load_variant_list_resource(DEFAULT_INDEXED_SUBSET_RESOURCE_ID, repo_root)
    location = document.get("location")
    assembly = document.get("genome_build")
    checksums = document.get("checksums")
    checksum = (
        checksums.get("alid_sha256") if isinstance(checksums, Mapping) else None
    )
    missing = [
        name
        for name, value in (
            ("location", location),
            ("genome_build", assembly),
            ("checksums.alid_sha256", checksum),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise IndexedSubsetConfigError(
            f"{DEFAULT_INDEXED_SUBSET_RESOURCE_ID}/resource.yaml is missing "
            f"{missing}; the default Indexed Variant Subset cannot be declared"
        )
    return {
        "name": DEFAULT_INDEXED_SUBSET_NAME,
        "resource_id": DEFAULT_INDEXED_SUBSET_RESOURCE_ID,
        "variant_list": str(location),
        "reference_assembly": str(assembly),
        "checksum": str(checksum),
    }


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _validate_entry(entry: object, where: str) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise IndexedSubsetConfigError(f"{where} must be a mapping")
    for key in _REQUIRED_ENTRY_KEYS:
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            raise IndexedSubsetConfigError(f"{where} requires a non-empty string {key!r}")
    checksum = entry.get("checksum")
    if checksum is not None and not _is_sha256(checksum):
        raise IndexedSubsetConfigError(
            f"{where} checksum must be a 64-character hex sha256, got {checksum!r}"
        )
    return dict(entry)


def resolve_indexed_subsets(
    document: Mapping[str, Any],
    layout: str,
    repo_root: Path | str | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Resolve a generator config's `indexed_subsets` for one release.

    Returns ``(declarations, note)``:

    * ``None`` declarations means the writer must emit no ``indexed_subsets``
      key at all (a Ragged release).
    * ``[]`` means the writer emits the explicit empty list of a reviewable
      opt-out, and ``note`` carries the recorded reason for ``release.yaml``.
    * a non-empty list is the declaration(s) to emit, defaulting to the
      maintained ``hm3`` entry when the config says nothing.

    A Dense/Hybrid config that empties the list without a reason, or names a
    malformed entry, is a generator error; a Ragged release may never declare
    one.
    """
    present = INDEXED_SUBSETS_KEY in document
    declared = document.get(INDEXED_SUBSETS_KEY)
    reason = document.get(INDEXED_SUBSETS_OPT_OUT_KEY)

    if present and declared is None:
        raise IndexedSubsetConfigError(
            f"{INDEXED_SUBSETS_KEY} must be a list of declarations, not null; omit "
            "the key to accept the default or set [] to opt out"
        )

    if layout not in INDEXED_SUBSET_LAYOUTS:
        if present and declared:
            raise IndexedSubsetConfigError(
                f"{INDEXED_SUBSETS_KEY} may be declared only for "
                f"{sorted(INDEXED_SUBSET_LAYOUTS)} layout, not {layout!r}; a Ragged "
                "Store Release has no Dense Component to index"
            )
        if reason is not None and str(reason).strip():
            raise IndexedSubsetConfigError(
                f"{INDEXED_SUBSETS_OPT_OUT_KEY} is only meaningful for a Dense or "
                f"Hybrid release, not {layout!r}"
            )
        return None, None

    if not present:
        if reason is not None and str(reason).strip():
            raise IndexedSubsetConfigError(
                f"{INDEXED_SUBSETS_OPT_OUT_KEY} needs an explicit "
                f"{INDEXED_SUBSETS_KEY}: [] to omit the default"
            )
        return [default_hm3_declaration(repo_root)], None

    if not isinstance(declared, list):
        raise IndexedSubsetConfigError(
            f"{INDEXED_SUBSETS_KEY} must be a list of declarations, "
            f"not {type(declared).__name__}"
        )

    if not declared:
        if not isinstance(reason, str) or not reason.strip():
            raise IndexedSubsetConfigError(
                f"{INDEXED_SUBSETS_KEY}: [] deliberately omits the default Indexed "
                f"Variant Subset; record why with a non-empty "
                f"{INDEXED_SUBSETS_OPT_OUT_KEY} reason"
            )
        return [], reason.strip()

    if reason is not None and str(reason).strip():
        raise IndexedSubsetConfigError(
            f"{INDEXED_SUBSETS_OPT_OUT_KEY} contradicts a non-empty "
            f"{INDEXED_SUBSETS_KEY}"
        )
    return [
        _validate_entry(entry, f"{INDEXED_SUBSETS_KEY}[{index}]")
        for index, entry in enumerate(declared)
    ], None


__all__ = [
    "DEFAULT_INDEXED_SUBSET_NAME",
    "DEFAULT_INDEXED_SUBSET_RESOURCE_ID",
    "INDEXED_SUBSETS_KEY",
    "INDEXED_SUBSETS_OPT_OUT_KEY",
    "INDEXED_SUBSET_LAYOUTS",
    "IndexedSubsetConfigError",
    "default_hm3_declaration",
    "load_variant_list_resource",
    "resolve_indexed_subsets",
]
