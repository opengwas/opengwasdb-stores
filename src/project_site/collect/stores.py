"""The Stores collector: each registered Store Release's facts, through `ogstores.bundle` (#389).

The registry is the checkout being rendered (`stores/`), not `/data`. A
Release Bundle is read as `ogstores.bundle.load` reads it and no Store is
opened. A fact the bundle cannot state exactly (a `trial` that is not a
boolean, a coverage outside the vocabulary) is `null` with the reason in
`problems`: absence stays distinct from a value, and one bad Store stops
nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ogstores import bundle as release_bundle
from ogstores import paths

from project_site.collect.common import CollectError


def default_registry() -> Path:
    return paths.REPO_ROOT / "stores"


def registered_store_ids(registry: Path) -> list[str]:
    return sorted(p.name for p in registry.iterdir() if p.is_dir() and paths.is_valid_store_id(p.name))


def _mapping(document: Any) -> dict[str, Any]:
    return document if isinstance(document, dict) else {}


def _store(registry: Path, store_id: str) -> dict[str, Any]:
    bundle = release_bundle.load(store_id, registry_root=registry)
    release, build = _mapping(bundle.release), _mapping(bundle.build)
    validation = _mapping(bundle.validation)
    problems: list[str] = []

    def exact(fact: Callable[[], Any]) -> Any:
        try:
            return fact()
        except ValueError as exc:
            problems.append(str(exc))
            return None

    for name, document in (("release.yaml", release), ("build.yaml", build), ("validation.yaml", validation)):
        for key in ("__yaml_error__", "__read_error__", "__document_error__"):
            if key in document:
                problems.append(f"{name}: {document[key]}")
    observed = validation.get("observed")
    return {
        "store_id": store_id,
        "label": release.get("label"),
        "status": release.get("status"),
        "trial": exact(lambda: bundle.trial),
        "layout": build.get("layout"),
        "completion_state": build.get("completion_state"),
        "association_coverage": exact(lambda: bundle.association_coverage),
        "access_posture": release.get("access_posture"),
        "derived_from": release.get("derived_from"),
        "superseded_by": release.get("superseded_by"),
        "created_at": release.get("created_at"),
        "accepted_at": release.get("accepted_at"),
        "validated_at": validation.get("validated_at"),
        "validation_status": validation.get("status"),
        "observed": observed if isinstance(observed, dict) else None,
        "description": release.get("description"),
        "notes": release.get("notes"),
        "problems": problems,
    }


def collect(registry: Path | None = None) -> dict[str, Any]:
    registry = default_registry() if registry is None else Path(registry)
    if not registry.is_dir():
        raise CollectError(f"registry {registry} is not a directory")
    return {"registry": str(registry), "stores": [_store(registry, i) for i in registered_store_ids(registry)]}
