#!/usr/bin/env python3
"""Registry audit for the HM3 Indexed Variant Subset migration (stores #211).

Every registered Release Bundle with a Dense Component -- a Dense or Hybrid
``layout`` -- must declare the default ``hm3`` Indexed Variant Subset, and every
Ragged-only bundle must declare none. The existing Dense-component
bundles were migrated in place by stores#211 (epic #214); this audit is the
executable proof that none was missed. It prints the full in-scope list and
exits non-zero on the first bundle that is neither migrated nor explicitly
opted out.

The audit reads only bundle ``build.yaml`` files and the maintained Reference
Resource declaration under
``resources/reference-resources/<resource_id>/resource.yaml``. It never opens
the large variant list, a Store, or an association body, so it is cheap and
safe to run in CI where the reference root is absent.

An intentional opt-out is a present, empty ``indexed_subsets: []`` -- a
reviewable statement in the bundle that distinguishes "declared none" from "not
migrated". An absent key on a Dense-component bundle is a loud miss. stores#210
owns the generator-facing default/opt-out; if it adopts a different marker, this
audit must adopt that marker too.

Scope: every registered Release Bundle under ``stores/`` -- once the single
home for Release Bundles (#252) -- so a Dense-component bundle that declares
neither a subset nor an explicit opt-out is a miss with nowhere to hide.

    pixi run audit-indexed-subsets
    python3 -m ogstores.audit_indexed_subsets [--registry-root DIR] [--repo-root DIR]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ogstores.paths import REPO_ROOT, STORE_ID_PATTERN

# A registered bundle is a directory named by an opaque OGS- identifier that
# carries a release.yaml.
STORE_ID_RE: re.Pattern[str] = STORE_ID_PATTERN

INDEXED_SUBSETS_KEY = "indexed_subsets"
DENSE_COMPONENT_LAYOUTS: frozenset[str] = frozenset({"dense", "hybrid"})
RAGGED_LAYOUT: str = "ragged"
DEFAULT_SUBSET_NAME: str = "hm3"

# Outcomes that pass.
_OK_OUTCOMES: frozenset[str] = frozenset(
    {"declared", "opt-out", "ragged-clean"}
)
# Outcomes that are a hard audit failure.
_FAIL_OUTCOMES: frozenset[str] = frozenset(
    {"miss", "ragged-declared", "unknown-layout", "malformed"}
)


@dataclass(frozen=True)
class BundleAudit:
    """One registered bundle's classification and declared subsets."""

    store_id: str
    layout: str
    status: str
    dense_component: bool
    declared: tuple[str, ...]
    outcome: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome in _OK_OUTCOMES


@dataclass(frozen=True)
class AuditReport:
    bundles: list[BundleAudit]
    registry_root: Path

    @property
    def failures(self) -> list[BundleAudit]:
        return [b for b in self.bundles if not b.ok]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def in_scope(self) -> list[BundleAudit]:
        return [b for b in self.bundles if b.dense_component]

    @property
    def ragged(self) -> list[BundleAudit]:
        return [b for b in self.bundles if b.layout == RAGGED_LAYOUT]


def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None


def _registered_bundle_ids(registry_root: Path) -> list[str]:
    if not registry_root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in registry_root.iterdir()
        if entry.is_dir()
        and STORE_ID_RE.fullmatch(entry.name) is not None
        and (entry / "release.yaml").is_file()
    )


def _declaration_names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name)
    return names


def _resource_errors(
    declaration: Any, repo_root: Path, store_id: str
) -> list[str]:
    """Statically check one declaration against its maintained resource.

    Reads only ``resource.yaml``; never hashes the variant list. The runtime
    preflight (``ogstores.resources``) owns the exhaustive byte check.
    """
    errors: list[str] = []
    if not isinstance(declaration, dict):
        return [f"{store_id}: indexed_subsets entry is not a mapping"]

    resource_id = declaration.get("resource_id")
    variant_list = declaration.get("variant_list")
    reference_assembly = declaration.get("reference_assembly")
    checksum = declaration.get("checksum")

    for key, value in (
        ("resource_id", resource_id),
        ("variant_list", variant_list),
        ("reference_assembly", reference_assembly),
    ):
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{store_id}: indexed_subsets entry requires string {key!r}")

    if errors:
        return errors

    resource_path = (
        repo_root / "resources" / "reference-resources" / resource_id / "resource.yaml"
    )
    if not resource_path.is_file():
        errors.append(
            f"{store_id}: resource_id {resource_id!r} names no maintained "
            f"Reference Resource ({resource_path} does not exist)"
        )
        return errors

    document = _load_yaml(resource_path)
    if not isinstance(document, dict):
        errors.append(f"{store_id}: {resource_path} is missing or not a mapping")
        return errors

    if document.get("kind") != "variant_list":
        errors.append(
            f"{store_id}: resource_id {resource_id!r} kind is "
            f"{document.get('kind')!r}, not 'variant_list'"
        )
    if document.get("genome_build") != reference_assembly:
        errors.append(
            f"{store_id}: declaration reference_assembly {reference_assembly!r} "
            f"differs from {resource_id!r} genome_build "
            f"{document.get('genome_build')!r}"
        )
    if str(document.get("location")) != variant_list:
        errors.append(
            f"{store_id}: declaration variant_list {variant_list!r} differs from "
            f"{resource_id!r} location {document.get('location')!r}"
        )
    checksums = document.get("checksums")
    expected = checksums.get("alid_sha256") if isinstance(checksums, dict) else None
    if checksum is not None and (
        not isinstance(checksum, str)
        or not isinstance(expected, str)
        or checksum.lower() != expected.lower()
    ):
        errors.append(
            f"{store_id}: declaration checksum {checksum!r} differs from "
            f"{resource_id!r} checksums.alid_sha256 {expected!r}"
        )
    return errors


def audit_bundle(store_id: str, registry_root: Path, repo_root: Path) -> BundleAudit:
    """Classify one registered bundle and return its audit row."""
    root = registry_root / store_id
    build = _load_yaml(root / "build.yaml")
    release = _load_yaml(root / "release.yaml")
    status = release.get("status") if isinstance(release, dict) else ""
    status = status if isinstance(status, str) else ""

    if not isinstance(build, dict):
        return BundleAudit(
            store_id=store_id,
            layout="?",
            status=status,
            dense_component=False,
            declared=(),
            outcome="malformed",
            detail="build.yaml is missing or not a mapping",
        )

    layout = build.get("layout")
    layout = layout if isinstance(layout, str) else ""
    raw = build.get(INDEXED_SUBSETS_KEY)
    declared = tuple(_declaration_names(raw))

    if layout in DENSE_COMPONENT_LAYOUTS:
        if DEFAULT_SUBSET_NAME in declared:
            entries = raw if isinstance(raw, list) else []
            declared_entry = next(
                (
                    entry
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("name") == DEFAULT_SUBSET_NAME
                ),
                None,
            )
            errors = _resource_errors(declared_entry, repo_root, store_id)
            if errors:
                return BundleAudit(
                    store_id=store_id,
                    layout=layout,
                    status=status,
                    dense_component=True,
                    declared=declared,
                    outcome="malformed",
                    detail="; ".join(errors),
                )
            return BundleAudit(
                store_id=store_id,
                layout=layout,
                status=status,
                dense_component=True,
                declared=declared,
                outcome="declared",
                detail=DEFAULT_SUBSET_NAME,
            )
        if raw == []:
            return BundleAudit(
                store_id=store_id,
                layout=layout,
                status=status,
                dense_component=True,
                declared=declared,
                outcome="opt-out",
                detail="explicit indexed_subsets: [] (declared none)",
            )
        if declared:
            detail = (
                f"declares {list(declared)} but not {DEFAULT_SUBSET_NAME!r}, and "
                "has no explicit opt-out"
            )
        else:
            detail = f"missing {DEFAULT_SUBSET_NAME!r} and no explicit opt-out"
        return BundleAudit(
            store_id=store_id,
            layout=layout,
            status=status,
            dense_component=True,
            declared=declared,
            outcome="miss",
            detail=detail,
        )

    if layout == RAGGED_LAYOUT:
        if declared:
            return BundleAudit(
                store_id=store_id,
                layout=layout,
                status=status,
                dense_component=False,
                declared=declared,
                outcome="ragged-declared",
                detail=f"Ragged-only bundle declares {list(declared)}",
            )
        return BundleAudit(
            store_id=store_id,
            layout=layout,
            status=status,
            dense_component=False,
            declared=declared,
            outcome="ragged-clean",
            detail="none",
        )

    return BundleAudit(
        store_id=store_id,
        layout=layout or "?",
        status=status,
        dense_component=False,
        declared=declared,
        outcome="unknown-layout",
        detail=f"unrecognised layout {layout!r}",
    )


def audit_registry(registry_root: Path | str, repo_root: Path | str) -> AuditReport:
    """Audit every registered bundle under ``registry_root``, sorted by id."""
    registry = Path(registry_root)
    repo = Path(repo_root)
    return AuditReport(
        bundles=[
            audit_bundle(store_id, registry, repo)
            for store_id in _registered_bundle_ids(registry)
        ],
        registry_root=registry,
    )


_LABELS = {
    "declared": "HM3",
    "opt-out": "OPT-OUT",
    "ragged-clean": "clean",
    "miss": "MISS",
    "ragged-declared": "DECLARED",
    "unknown-layout": "LAYOUT",
    "malformed": "BAD",
}


def render_report(report: AuditReport, repo_root: Path | str) -> str:
    """Render the full in-scope list plus the out-of-scope note."""
    repo = Path(repo_root)
    lines = [
        "Indexed Variant Subset registry audit (stores #211, epic #214)",
        f"registry: {report.registry_root}",
        "",
        f"{'RESULT':<9} {'STORE':<10} {'LAYOUT':<7} {'STATUS':<20} DETAIL",
    ]
    for bundle in report.bundles:
        detail = bundle.detail
        if bundle.outcome == "declared":
            extra = [name for name in bundle.declared if name != DEFAULT_SUBSET_NAME]
            if extra:
                detail = f"{DEFAULT_SUBSET_NAME} (+{', '.join(extra)})"
        lines.append(
            f"{_LABELS.get(bundle.outcome, bundle.outcome):<9} "
            f"{bundle.store_id:<10} {bundle.layout:<7} {bundle.status:<20} {detail}"
        )

    declared = [b for b in report.in_scope if b.outcome == "declared"]
    opted_out = [b for b in report.in_scope if b.outcome == "opt-out"]
    ragged_clean = [b for b in report.ragged if b.outcome == "ragged-clean"]
    lines += [
        "",
        f"{len(declared)}/{len(report.in_scope)} Dense-component bundles declare "
        f"{DEFAULT_SUBSET_NAME}; {len(opted_out)} explicitly opted out.",
        f"{len(ragged_clean)}/{len(report.ragged)} Ragged-only bundles declare none.",
        f"Audited {len(report.bundles)} registered bundles under "
        f"{report.registry_root.relative_to(repo) if report.registry_root.is_relative_to(repo) else report.registry_root}.",
        "Scope: every registered Release Bundle under stores/, the one home for "
        "Release Bundles (#252).",
    ]
    if report.failures:
        lines += ["", "FAILURES:"]
        lines += [f"  - {b.store_id}: {b.detail}" for b in report.failures]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry-root",
        default=None,
        help="directory of registered Release Bundles (default: <repo>/stores)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repository root holding resources/reference-resources (default: this checkout)",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root) if args.repo_root else REPO_ROOT
    registry_root = (
        Path(args.registry_root) if args.registry_root else repo_root / "stores"
    )
    report = audit_registry(registry_root, repo_root)
    print(render_report(report, repo_root))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
