"""LD panels as rows of `ld-resources.tsv`: read from `resource.yaml` declarations (#540).

An LD panel is a Reference Resource, not a record: `resources/reference-resources/
<resource_id>/resource.yaml` with `kind: ld_panel` declares it, and the registry
lists it beside the LD Stores (docs/spec/ld-store-registry.md, ADR 0042). One
declaration gives one row per ancestry it declares, each with the id its
`ld_ids` map gives that ancestry. Nothing here opens a panel directory: the
counts are in the declaration, measured on the host by
`tools/ld-panel/measure_panel.py`, and a count the declaration does not hold is
`None`, an empty cell, never `0`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ogstores.resources import ReferenceResourceError, load_resource_declaration

from ld_registry.ids import is_valid_ld_id
from ld_registry.rows import Value, keyed, panel_representation

PANEL_KIND = "ld_panel"
#: The `provenance` column of a panel row: it is built from genotypes.
PANEL_PROVENANCE = "genotypes"
RESOURCES = Path("resources", "reference-resources")
DECLARATION = "resource.yaml"


class PanelError(ValueError):
    """A panel declaration the registry cannot list."""


def _ancestries(where: Path, document: Mapping[str, Any]) -> list[str]:
    declared = document.get("ancestry")
    ancestries = [declared] if isinstance(declared, str) else list(declared or [])
    if not ancestries:
        raise PanelError(f"{where}: ancestry is required: an ld_panel declares at least one")
    return ancestries


def _location(document: Mapping[str, Any], ancestry: str, several: bool) -> str | None:
    location = document.get("location")
    if location is None:
        return None
    if several:
        return f"{location}/{ancestry}"
    if document.get("ancestry_dir"):
        alias = f" (-> {document['physical_directory']})" if document.get("physical_directory") else ""
        return f"{location}/{document['ancestry_dir']}{alias}"
    return str(location)


def _check_keys(where: Path, field: str, mapping: Mapping[str, Any], ancestries: list[str]) -> None:
    """A per-ancestry mapping names every declared ancestry and no other: a misspelling is an error, not an empty cell."""
    missing = [a for a in ancestries if a not in mapping]
    extra = [str(a) for a in mapping if a not in ancestries]
    if missing or extra:
        problems = ([f"has no entry for ancestry {', '.join(missing)}"] if missing else []) + (
            [f"names {', '.join(extra)}, which the panel does not declare"] if extra else []
        )
        raise PanelError(f"{where}: {field} {' and '.join(problems)}")


def _rows(where: Path, document: Mapping[str, Any]) -> list[dict[str, Value]]:
    for field in ("resource_id", "used_for", "licence"):
        if not document.get(field):
            raise PanelError(f"{where}: {field} is required for an ld_panel")
    ancestries = _ancestries(where, document)
    several = len(ancestries) > 1
    ld = document.get("ld") or {}
    source = document.get("source") or {}
    ids = document.get("ld_ids") or {}
    _check_keys(where, "ld_ids", ids, ancestries)
    for ancestry in ancestries:
        if not is_valid_ld_id(ids[ancestry]):
            raise PanelError(f"{where}: ld_ids {ancestry}: {ids[ancestry]!r} does not match 'OGL-\\d{{5}}' (ADR 0042)")

    def declared(section: Mapping[str, Any], field: str) -> Mapping[str, Any]:
        """A declared count as a mapping from ancestry to value; an absent one is empty, so each cell is None."""
        value = section.get(field)
        name = f"{'ld' if section is ld else 'source'}.{field}"
        if value is None:
            return {}
        if isinstance(value, Mapping):
            _check_keys(where, name, value, ancestries)
            return value
        if several:
            raise PanelError(f"{where}: {name} is one value for {len(ancestries)} ancestries; declare it per ancestry")
        return {ancestries[0]: value}

    n_individuals = declared(source, "n_individuals")
    n_variants, n_blocks, size = declared(ld, "n_variants"), declared(ld, "n_blocks"), declared(ld, "bytes")
    resource_id = document["resource_id"]
    return [
        keyed(
            {
                "ld_id": ids[ancestry],
                "label": f"{resource_id} ({ancestry})" if several else resource_id,
                "kind": PANEL_KIND,
                "provenance": PANEL_PROVENANCE,
                "status": document.get("status"),
                "used_for": document.get("used_for"),
                "ancestry": ancestry,
                "source_panel": source.get("cohort"),
                "n": n_individuals.get(ancestry),
                "reference_assembly": document.get("genome_build"),
                "representation": panel_representation(ld),
                "n_variants": n_variants.get(ancestry),
                "n_blocks": n_blocks.get(ancestry),
                "store_bytes": size.get(ancestry),
                "source": source.get("cohort"),
                "licence": document.get("licence"),
                "location": _location(document, ancestry, several),
                "record": (RESOURCES / resource_id / DECLARATION).as_posix(),
                "notes": document.get("index_notes"),
            }
        )
        for ancestry in ancestries
    ]


def panel_rows(resources_root: Path | str) -> list[dict[str, Value]]:
    """Every `ld_panel` declaration's rows under `resources_root` (`<repo>/resources/reference-resources`), in id order.

    A declaration the registry cannot list is refused, naming its file.
    """
    directory = Path(resources_root)
    if directory.parts[-2:] != RESOURCES.parts:
        raise PanelError(f"{directory}: is not a '{RESOURCES.as_posix()}' directory")
    repo_root = directory.parents[1]
    found: list[dict[str, Value]] = []
    where_by_id: dict[str, Path] = {}
    for entry in sorted(directory.iterdir()) if directory.is_dir() else []:
        where = entry / DECLARATION
        if not where.is_file():
            continue
        try:
            document = load_resource_declaration(entry.name, repo_root)
        except ReferenceResourceError as exc:
            raise PanelError(str(exc)) from exc
        if document.get("kind") != PANEL_KIND:
            continue
        for row in _rows(where, document):
            ld_id = str(row["ld_id"])
            if ld_id in where_by_id:
                raise PanelError(f"{where}: {ld_id} is also {where_by_id[ld_id]}'s; ids are assigned once")
            where_by_id[ld_id] = where
            found.append(row)
    return sorted(found, key=lambda row: str(row["ld_id"]))
