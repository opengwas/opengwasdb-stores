"""Run-time verification of declared Indexed Variant Subtests (stores #209).

`plan()` stays pure: it never opens a Reference Resource or a variant list. This
module is the run-time counterpart the release entry point (`ogstores.release`)
and the Snakefile's `onstart` hook call before a run that declares Indexed
Variant Subsets. It does two things, both before any expensive work:

* **Runtime capability.** The declared step runs `opengwasdb
  build-indexed-subset`, which the repository's pinned current-format runtime
  provides (first shipped in opengwas/opengwasdb#264, revision `4819a2ee`; the
  pin adopted by stores#218 is a descendant). The capability is still probed
  before a subset-declaring run: if the resolved `opengwasdb` does not provide
  the subcommand, the run is refused with the required capability and the
  reason, rather than failing hours into a build.

* **Maintained input.** A declaration names a `resource_id`; this resolves
  `resources/reference-resources/<resource_id>/resource.yaml` and requires the
  declaration's `reference_assembly`, `variant_list` and optional `checksum` to
  agree with it, then hashes the list and checks its declared sha256, byte size
  and row count. The exhaustive canonical-ALID and duplicate check remains the
  maintained resource's own verifier (for `hapmap3-hg38`, the sibling
  `verify_hm3_resource.py`); this is the cheap guard the registry can run
  without opening a Store.

See docs/adr/0034-indexed-variant-subsets-in-release-planning.md and
docs/spec/store-release-workflow.md.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from ogstores import bundle as bundle_module
from ogstores import paths, run
from ogstores.plan import (
    INDEXED_SUBSET_COMMAND,
    IndexedSubsetDeclaration,
    indexed_subset_declarations,
)


class ReferenceResourceError(RuntimeError):
    """A declared subset's maintained Reference Resource is not the one used."""


class IndexedSubsetRuntimeError(RuntimeError):
    """A declared subset needs an opengwasdb runtime this environment lacks."""


def _resource_declaration_path(
    resource_id: str, repo_root: Path | str | None = None
) -> Path:
    repo = Path(repo_root) if repo_root is not None else paths.REPO_ROOT
    return repo / "resources" / "reference-resources" / resource_id / "resource.yaml"


def load_resource_declaration(
    resource_id: str, repo_root: Path | str | None = None
) -> dict[str, Any]:
    """Load one maintained Reference Resource declaration, or refuse loudly."""
    declaration_path = _resource_declaration_path(resource_id, repo_root)
    if not declaration_path.is_file():
        raise ReferenceResourceError(
            f"resource_id {resource_id!r} names no maintained Reference Resource: "
            f"{declaration_path} does not exist"
        )
    try:
        document = yaml.safe_load(declaration_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ReferenceResourceError(
            f"{declaration_path}: not valid YAML: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ReferenceResourceError(
            f"{declaration_path}: declaration is not a mapping"
        )
    return document


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _count_nonblank_lines(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def verify_variant_list_resource(
    declaration: IndexedSubsetDeclaration,
    repo_root: Path | str | None = None,
) -> None:
    """Require the declared variant list to be the maintained resource's list.

    Reads the resource declaration and hashes the list; opens no Store and no
    association data. Every mismatch is a refusal, never a silent substitution.
    """
    document = load_resource_declaration(declaration.resource_id, repo_root)

    kind = document.get("kind")
    if kind != "variant_list":
        raise ReferenceResourceError(
            f"Indexed Variant Subset {declaration.name!r} names resource_id "
            f"{declaration.resource_id!r}, whose kind is {kind!r}, not 'variant_list'"
        )
    genome_build = document.get("genome_build")
    if genome_build != declaration.reference_assembly:
        raise ReferenceResourceError(
            f"Indexed Variant Subset {declaration.name!r} declares reference_assembly "
            f"{declaration.reference_assembly!r}, but resource_id "
            f"{declaration.resource_id!r} records genome_build {genome_build!r}"
        )
    declared_location = document.get("location")
    if not isinstance(declared_location, str) or Path(declared_location) != declaration.variant_list:
        raise ReferenceResourceError(
            f"Indexed Variant Subset {declaration.name!r} declares variant_list "
            f"{str(declaration.variant_list)!r}, but resource_id "
            f"{declaration.resource_id!r} records location {declared_location!r}"
        )

    if not declaration.variant_list.is_file():
        raise ReferenceResourceError(
            f"Indexed Variant Subset {declaration.name!r}: maintained variant list "
            f"not found: {declaration.variant_list}"
        )

    checksums = document.get("checksums")
    if not isinstance(checksums, Mapping):
        raise ReferenceResourceError(
            f"{_resource_declaration_path(declaration.resource_id, repo_root)}: "
            "declaration has no 'checksums' mapping"
        )
    expected_sha256 = checksums.get("alid_sha256")
    if not isinstance(expected_sha256, str) or not expected_sha256:
        raise ReferenceResourceError(
            f"resource_id {declaration.resource_id!r} declares no checksums.alid_sha256"
        )
    if (
        declaration.checksum is not None
        and declaration.checksum.lower() != expected_sha256.lower()
    ):
        raise ReferenceResourceError(
            f"Indexed Variant Subset {declaration.name!r} declares checksum "
            f"{declaration.checksum!r}, but resource_id {declaration.resource_id!r} "
            f"records checksums.alid_sha256 {expected_sha256!r}"
        )

    actual_sha256 = _sha256_file(declaration.variant_list)
    if actual_sha256 != expected_sha256.lower():
        raise ReferenceResourceError(
            f"Indexed Variant Subset {declaration.name!r}: {declaration.variant_list} "
            f"SHA-256 mismatch: maintained resource declares {expected_sha256}, "
            f"actual {actual_sha256}"
        )

    expected_bytes = checksums.get("alid_bytes")
    if isinstance(expected_bytes, int) and declaration.variant_list.stat().st_size != expected_bytes:
        raise ReferenceResourceError(
            f"Indexed Variant Subset {declaration.name!r}: {declaration.variant_list} "
            f"byte size mismatch: maintained resource declares {expected_bytes}, "
            f"actual {declaration.variant_list.stat().st_size}"
        )

    expected_rows = document.get("row_count")
    if isinstance(expected_rows, int):
        actual_rows = _count_nonblank_lines(declaration.variant_list)
        if actual_rows != expected_rows:
            raise ReferenceResourceError(
                f"Indexed Variant Subset {declaration.name!r}: {declaration.variant_list} "
                f"row count mismatch: maintained resource declares {expected_rows}, "
                f"actual {actual_rows}"
            )


def probe_opengwasdb_subset_capability(executable: str | None = None) -> bool:
    """Return True when `opengwasdb` provides the `build-indexed-subset` command."""
    resolved = executable if executable is not None else run.get_opengwasdb_executable()
    if not resolved or resolved == run.UNAVAILABLE:
        return False
    try:
        result = subprocess.run(
            [str(resolved), INDEXED_SUBSET_COMMAND, "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def verify_indexed_subsets_for_releases(
    scheduled: Iterable[tuple[str, str]],
    *,
    registry_root: Path | str,
    repo_root: Path | str | None = None,
    executable: str | None = None,
) -> None:
    """Refuse a run that declares subsets without a capable runtime or valid list.

    `scheduled` is the Snakemake dry run's `[(artifact_root, store_id)]`. A run
    with no declared subset returns immediately, so a no-declaration release is
    never gated on the runtime version (stores #209).
    """
    declarations_by_store: list[tuple[str, list[IndexedSubsetDeclaration]]] = []
    for _root, store_id in scheduled:
        loaded = bundle_module.load(store_id, registry_root=registry_root)
        try:
            declarations = indexed_subset_declarations(loaded)
        except ValueError as exc:
            raise ReferenceResourceError(f"Release {store_id}: {exc}") from exc
        if declarations:
            declarations_by_store.append((store_id, declarations))
    if not declarations_by_store:
        return

    resolved = executable if executable is not None else run.get_opengwasdb_executable()
    if not probe_opengwasdb_subset_capability(resolved):
        names = ", ".join(store_id for store_id, _ in declarations_by_store)
        raise IndexedSubsetRuntimeError(
            f"Release(s) {names} declare an Indexed Variant Subset, which needs an "
            "opengwasdb runtime providing `build-indexed-subset` "
            "(opengwas/opengwasdb#264, revision 4819a2ee or later). The resolved "
            f"opengwasdb is {resolved!r}. That capability is part of the repository's "
            "pinned current-format runtime (stores#218); install the tracked lock with "
            "`pixi install` and re-run rather than substituting an executable on PATH."
        )

    for store_id, declarations in declarations_by_store:
        for declaration in declarations:
            try:
                verify_variant_list_resource(declaration, repo_root=repo_root)
            except ReferenceResourceError as exc:
                raise ReferenceResourceError(
                    f"Release {store_id}: {exc}"
                ) from exc


__all__ = [
    "IndexedSubsetRuntimeError",
    "ReferenceResourceError",
    "load_resource_declaration",
    "probe_opengwasdb_subset_capability",
    "verify_indexed_subsets_for_releases",
    "verify_variant_list_resource",
]
