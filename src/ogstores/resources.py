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

It also re-hashes the Column Map a bundle carries against its Build Recipe's
binding before a build (#292): `column_maps/<file>` must still have the bound
sha256, or the run is refused naming the map, and `stage_column_map` copies the
verified bytes to the path `plan()` passes as `--column-map`.

It also holds the capability guard of the Observed-View Check (stores #306): a
release that completes its own Store is refused on a runtime without
`opengwasdb check-observed-view`, before anything is written.

See docs/adr/0034-indexed-variant-subsets-in-release-planning.md,
docs/adr/0037-reference-completion-replaces-its-store.md and
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
from ogstores import paths, run, runtime
from ogstores.plan import (
    INDEXED_SUBSET_COMMAND,
    OBSERVED_VIEW_CHECK_COMMAND,
    IndexedSubsetDeclaration,
    indexed_subset_declarations,
)


class ReferenceResourceError(RuntimeError):
    """A declared subset's maintained Reference Resource is not the one used."""


class ColumnMapChecksumError(ReferenceResourceError):
    """A bundle's carried Column Map no longer has the checksum its Build Recipe binds."""


class IndexedSubsetRuntimeError(RuntimeError):
    """A declared subset needs an opengwasdb runtime this environment lacks."""


class ObservedViewCheckRuntimeError(RuntimeError):
    """A single-id completion needs an opengwasdb runtime that has the Observed-View Check."""


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
    return _probe_command(INDEXED_SUBSET_COMMAND, executable)


def probe_opengwasdb_observed_view_capability(executable: str | None = None) -> bool:
    """Return True when `opengwasdb` provides the `check-observed-view` command."""
    return _probe_command(OBSERVED_VIEW_CHECK_COMMAND, executable)


def _probe_command(command: str, executable: str | None) -> bool:
    resolved = executable if executable is not None else run.get_opengwasdb_executable()
    if not resolved or resolved == run.UNAVAILABLE:
        return False
    try:
        result = subprocess.run(
            [str(resolved), command, "--help"],
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
            "pinned current-format runtime (stores#218, coordinating stores#197); install "
            "the tracked lock with `pixi install` and re-run rather than substituting an "
            "executable on PATH."
        )

    for store_id, declarations in declarations_by_store:
        for declaration in declarations:
            try:
                verify_variant_list_resource(declaration, repo_root=repo_root)
            except ReferenceResourceError as exc:
                raise ReferenceResourceError(
                    f"Release {store_id}: {exc}"
                ) from exc


def verify_column_map(loaded: bundle_module.Bundle) -> Path | None:
    """The carried Column Map's path once its bytes match the binding; ``None`` when none is carried.

    A built-in map is carried nowhere, so there is nothing to re-hash: its
    checksum is the pinned package's, which `bundle.check()` compared.
    """
    try:
        binding = bundle_module.column_map_binding(loaded.build)
    except ValueError as exc:
        raise ColumnMapChecksumError(f"Release {loaded.store_id}: {exc}") from exc
    if binding is None or binding.file is None:
        return None
    path = Path(loaded.root) / bundle_module.column_map_path(binding.file)
    if not path.is_file() or path.is_symlink():
        raise ColumnMapChecksumError(
            f"Release {loaded.store_id}: Column Map {binding.name!r} is bound to "
            f"{bundle_module.column_map_path(binding.file)}, which the bundle does not carry "
            "as a regular file"
        )
    actual = _sha256_file(path)
    if actual != binding.sha256:
        raise ColumnMapChecksumError(
            f"Release {loaded.store_id}: Column Map {binding.name!r} "
            f"({bundle_module.column_map_path(binding.file)}) has sha256 {actual}, but "
            f"build.yaml binds {binding.sha256}; a build never reads a map that is not the "
            "one its Resolution Records were made with"
        )
    return path


def verify_staged_column_map(
    loaded: bundle_module.Bundle, artifact_root: Path | str, inputs: Iterable[Path | str]
) -> None:
    """Refuse a build step whose staged Column Map no longer has the bound checksum.

    Snakemake treats an existing staged copy as up to date, so a resumed or
    retried build could read a copy changed since it was staged. A step that
    lists the staged map among its inputs is checked here, immediately before
    it runs: the bytes it will read must be the binding's (#292).
    """
    binding = bundle_module.column_map_binding(loaded.build)
    if binding is None or binding.file is None:
        return
    staged = paths.column_map_path(loaded.store_id, binding.file, root=artifact_root)
    if staged not in {Path(item) for item in inputs}:
        return
    actual = _sha256_file(staged) if staged.is_file() and not staged.is_symlink() else None
    if actual != binding.sha256:
        raise ColumnMapChecksumError(
            f"Release {loaded.store_id}: the staged Column Map {binding.name!r} ({staged}) "
            f"{'has sha256 ' + actual if actual else 'is not a regular file'}, but build.yaml binds "
            f"{binding.sha256}; delete it to stage the bundle's copy again"
        )


def verify_column_maps_for_releases(
    scheduled: Iterable[tuple[str, str]], *, registry_root: Path | str
) -> None:
    """Refuse a run whose scheduled bundle carries a Column Map that is not the one it binds.

    `scheduled` is the Snakemake dry run's `[(artifact_root, store_id)]`. A
    release with no carried map returns at once (#292).
    """
    for _root, store_id in scheduled:
        verify_column_map(bundle_module.load(store_id, registry_root=registry_root))


def stage_column_map(loaded: bundle_module.Bundle, destination: Path | str) -> Path:
    """Copy the verified carried map to `destination`, the path `plan()` passes to the build.

    The bytes are re-hashed here, at the moment of the copy, so a map edited
    after the run's check is still refused by name.
    """
    source = verify_column_map(loaded)
    if source is None:
        raise ColumnMapChecksumError(f"Release {loaded.store_id} binds no carried Column Map to stage")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    temporary.write_bytes(source.read_bytes())
    if _sha256_file(temporary) != bundle_module.column_map_binding(loaded.build).sha256:  # type: ignore[union-attr]
        temporary.unlink()
        raise ColumnMapChecksumError(
            f"Release {loaded.store_id}: Column Map {source} changed while it was staged"
        )
    temporary.replace(destination)
    return destination


def verify_observed_view_check_for_releases(
    scheduled: Iterable[tuple[str, str]],
    *,
    registry_root: Path | str,
    executable: str | None = None,
) -> None:
    """Refuse a single-id completion on a runtime without `check-observed-view`.

    `scheduled` is the Snakemake dry run's `[(artifact_root, store_id)]`. A
    release that completes its own Store plans the Observed-View Check before
    its completion phase publishes (ADR 0037), so the runtime must provide it.
    A run with no such release returns immediately, so a two-id or
    Observed-Only release is never gated on this command (stores #306).
    """
    needing: list[str] = []
    for _root, store_id in scheduled:
        if bundle_module.load(store_id, registry_root=registry_root).completes_own_store:
            needing.append(store_id)
    if not needing:
        return

    resolved = executable if executable is not None else run.get_opengwasdb_executable()
    if not probe_opengwasdb_observed_view_capability(resolved):
        raise ObservedViewCheckRuntimeError(
            f"Release(s) {', '.join(sorted(needing))} complete their own Store, whose swap is "
            f"gated on the Observed-View Check: an opengwasdb runtime providing "
            f"`{OBSERVED_VIEW_CHECK_COMMAND}` (opengwas/opengwasdb#289). The resolved "
            f"opengwasdb is {resolved!r}. The repository's pinned runtime "
            f"({runtime.PINNED_OPENGWASDB_REVISION[:8]}) provides it: install the tracked lock "
            "with `pixi install` and re-run rather than substituting an executable on PATH."
        )


__all__ = [
    "ColumnMapChecksumError",
    "IndexedSubsetRuntimeError",
    "ObservedViewCheckRuntimeError",
    "ReferenceResourceError",
    "load_resource_declaration",
    "probe_opengwasdb_observed_view_capability",
    "probe_opengwasdb_subset_capability",
    "stage_column_map",
    "verify_column_map",
    "verify_column_maps_for_releases",
    "verify_staged_column_map",
    "verify_indexed_subsets_for_releases",
    "verify_observed_view_check_for_releases",
    "verify_variant_list_resource",
]
