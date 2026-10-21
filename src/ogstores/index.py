"""Render the master list from Release Bundles.

`stores.tsv`, `STORES.md`, each bundle's `summary.yaml`, and both `by-label/`
symlink trees are generated, committed, and verified in CI by regenerating and
failing on a dirty tree.

This module reads git, never the artifact root -- which is the whole
constraint, and a narrower one than it sounds. Measurements are welcome in
the index; they just have to reach it through the bundle. Analysis count and
descriptive fields come from `analyses.tsv`; `register` writes Store
measurements and the validate verdict into `validation.yaml`. Git records both,
so only facts that change without a commit stay out.

The `build_command` column is derived by `plan()`, so the published command
is derived rather than maintained. It is the build phase's first step, so a
single-id release names the command that builds its Store rather than the one
that then completes it (ADR 0037). `store_uri` is likewise derived: it is the
pure function of the identifier `<artifact-root>/<store-id>/store.opengwasdb`
(ADR 0022), with no migration-note fallback (ADR 0030).

The `completion_state` column reports the completion state of the Store users
can query, not the bundle's declaration (#307). `register` records it as
`observed.completion_state` in the Validation Record of each publication, so a
single-id release reads `observed_only` while completion runs and
`reference_completed` after the swap. A record written before that field
carries none, and the row keeps the declared value, which for an Observed-Only
Store and for a two-id child is the state the record's Store was published in.
A release that completes its own Store is the exception: its earlier
publication can only have been the Observed-Only Store, so its row reports
`observed_only` (ADR 0037). When a row's published state is `observed_only`
while its bundle declares `reference_completed`, `STORES.md` marks the cell
`observed_only (completion pending)`; `stores.tsv` keeps the bare state so a
machine reader still sees a documented value. A record that contradicts its
bundle in any other way is refused rather than published.

See docs/spec/store-release-workflow.md and ADRs 0022, 0023, 0028, 0030.
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from typing import Any

import yaml

from ogstores import bundle, paths
from ogstores.bundle import Bundle
from ogstores.plan import BUILD_PHASE, plan

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# The completion states a Store can be published in, the only values the
# register-written `observed.completion_state` may take (#307, ADR 0037).
PUBLISHED_COMPLETION_STATES: tuple[str, ...] = ("observed_only", "reference_completed")

COLUMNS: tuple[str, ...] = (
    "store_id",
    "label",
    "layout",
    "completion_state",
    "status",
    "derived_from",
    "store_uri",
    "created_at",
    "opengwasdb_rev",
    "generator_command",
    "build_command",
    "format_version",
    "n_analyses",
    "n_manifest",
    "n_excluded",
    "n_variants",
    "n_associations",
    "store_bytes",
    "build_elapsed_s",
    "validate_status",
    "first_author",
    "publication_pmid",
    "tissue",
    "context",
    "assigned_ancestry",
    "sample_size",
    "source_url",
)


def render_stores_row(
    bundle_obj: Bundle,
    artifact_root: Path | str | None = None,
    repo_root: Path | str | None = None,
) -> dict[str, str]:
    """Derive one canonical row for stores.tsv from a Release Bundle without opening any Store (ADR 0023)."""
    paths.require_valid_store_id(bundle_obj.store_id)

    store_id = bundle_obj.store_id
    label = bundle_obj.label or ""
    layout = bundle_obj.layout or ""
    declared_completion_state = bundle_obj.completion_state or ""
    status = bundle_obj.status or ""
    derived_from = bundle_obj.derived_from or ""
    membership_summary = bundle.summarise(bundle_obj)

    # store_uri is a pure function of the identifier: <artifact-root>/<store-id>/
    # store.opengwasdb (ADRs 0022 and 0030). The one-time migration-note
    # fallback (release.yaml `store_uri` and `migration.previous_store_uri`) is
    # removed: a Store's published location is where it must live under the flat
    # opaque-id layout, resolved from deployment configuration, never a legacy
    # family-first path from a superseded layout (issue #137). Whether the bytes
    # on disk have been physically moved yet is a separate operational fact
    # tracked by its own issue and is not something this derived column may
    # paper over.
    art_root = Path(artifact_root) if artifact_root else paths.artifact_root()
    store_uri = str(paths.store_path(store_id, root=art_root))

    created_at = str(bundle_obj.release.get("created_at") or "")

    # The standalone build_environment block was removed from release.yaml
    # (issue #136). The opengwasdb revision the master list publishes is the one
    # the Validation Record's register-written build_environment names, so there
    # is a single source rather than a hand-maintained generation-time copy.
    opengwasdb_rev = (
        (bundle_obj.validation.get("build_environment", {}).get("opengwasdb_commit") if bundle_obj.validation else "")
        or ""
    )

    gen = bundle_obj.release.get("generator")
    if isinstance(gen, dict):
        commands = gen.get("commands") or []
        generator_command = " ; ".join(
            str(command) for command in commands if isinstance(command, str)
        )
    elif isinstance(gen, str):
        generator_command = gen
    else:
        generator_command = ""

    # build_command derived purely from plan(): the build phase's first step,
    # which is `steps[0]` for every bundle whose plan is one phase. The
    # `analyses` token resolves to the derived build manifest under the artifact
    # root, so the rendered command already names the filtered manifest the
    # builder consumes (ADR 0025).
    steps = plan(bundle_obj, artifact_root=artifact_root)
    build_steps = [step for step in steps if step.phase == BUILD_PHASE] or steps
    build_command = " ".join(build_steps[0].argv) if build_steps else ""

    # Observed columns extracted exclusively from validation.yaml in git. The
    # values live in the register-written `observed` block and nowhere else: a
    # pre-seam record that kept them at the top level is not read (issue #135).
    # Every record carries the block after migration, so no compatibility
    # fallback remains and an unrecorded measurement is published as absent.
    val = bundle_obj.validation or {}
    obs = val.get("observed", {})

    # The published Store's completion state (#307), from the record `register`
    # wrote at publication. Never from build.yaml's declaration, which under
    # single-id completion reads `reference_completed` while the Observed-Only
    # Store is what is published.
    if "completion_state" in obs:
        recorded_completion_state = obs["completion_state"]
        if recorded_completion_state not in PUBLISHED_COMPLETION_STATES:
            raise ValueError(
                f"{store_id}: validation.yaml records observed.completion_state "
                f"{recorded_completion_state!r}, not one of the documented states "
                f"{sorted(PUBLISHED_COMPLETION_STATES)}"
            )
        # The record and the declaration differ only while a release that
        # completes its own Store sits between its two publications (ADR 0037).
        # Anything else -- a two-id child whose record claims an Observed-Only
        # Store, say -- contradicts the bundle, so it is refused rather than
        # rendered as a pending completion.
        if recorded_completion_state != declared_completion_state and not (
            bundle.is_completion_pending(bundle_obj, recorded_completion_state)
        ):
            raise ValueError(
                f"{store_id}: validation.yaml records observed.completion_state "
                f"{recorded_completion_state!r} but the bundle declares "
                f"{declared_completion_state!r}, and only a release that completes "
                "its own Store can have an Observed-Only Store published under its id"
            )
        completion_state = str(recorded_completion_state)
    elif bundle_obj.completes_own_store and isinstance(obs, dict) and obs:
        # A record that records a publication but predates #307 wrote no
        # `completion_state`. ADR 0037's one amendment lets an accepted
        # `observed_only` bundle gain a `complete` block, so such a bundle can
        # declare `reference_completed` while its published Store is still the
        # Observed-Only one. No `register` on `dev` ever wrote a field-less
        # record for a completed single-id Store: #304 and #307 land together.
        # Reporting the declaration would claim a completed Store that does not
        # exist, so the record's own publication is what the row says.
        completion_state = "observed_only"
    else:
        # Two-id children and Observed-Only releases published before #307: the
        # declaration is the state their Store was published in, so those rows
        # render byte for byte as they did.
        completion_state = declared_completion_state

    format_version = obs.get("format_version")
    format_version_str = str(format_version) if format_version else ""

    # Membership counts describe bundle membership, so they are derived from
    # the table rather than copied from build-time observations. Issue #135's
    # observed-only contract still applies to every actual Store measurement.
    # n_analyses is the admitted (buildable) membership; n_manifest and
    # n_excluded keep the manifest rows and excluded rows visible instead of
    # counting them as built Analyses (issue #217). n_manifest is a selection
    # count, not the intended Source Inventory size.
    n_analyses_str = str(membership_summary["n_analyses"])
    n_manifest_str = str(membership_summary["n_manifest"])
    n_excluded_str = str(membership_summary["n_excluded"])

    n_variants = obs.get("n_variants")
    n_variants_str = str(n_variants) if n_variants != "" and n_variants is not None else ""

    n_associations = obs.get("n_associations")
    n_associations_str = str(n_associations) if n_associations != "" and n_associations is not None else ""

    store_bytes = obs.get("store_bytes")
    store_bytes_str = str(store_bytes) if store_bytes != "" and store_bytes is not None else ""

    build_elapsed_s = obs.get("build_elapsed_s")
    build_elapsed_s_str = str(build_elapsed_s) if build_elapsed_s != "" and build_elapsed_s is not None else ""

    # The Validation Record's own `status` is the release-level verdict (issue
    # #124). A per-check entry such as `checks.store` describes one check and
    # must never override a record that failed overall -- a record can read
    # `status: failed` while an individual check passed, and that is exactly
    # what the record is for. Empty when there is no Validation Record.
    validate_status_str = str(val.get("status") or "")

    return {
        "store_id": store_id,
        "label": label,
        "layout": layout,
        "completion_state": completion_state,
        "status": status,
        "derived_from": derived_from,
        "store_uri": store_uri,
        "created_at": created_at,
        "opengwasdb_rev": opengwasdb_rev,
        "generator_command": generator_command,
        "build_command": build_command,
        "format_version": format_version_str,
        "n_analyses": n_analyses_str,
        "n_manifest": n_manifest_str,
        "n_excluded": n_excluded_str,
        "n_variants": n_variants_str,
        "n_associations": n_associations_str,
        "store_bytes": store_bytes_str,
        "build_elapsed_s": build_elapsed_s_str,
        "validate_status": validate_status_str,
        "first_author": str(membership_summary["first_author"]),
        "publication_pmid": str(membership_summary["publication_pmid"]),
        "tissue": str(membership_summary["tissue"]),
        "context": str(membership_summary["context"]),
        "assigned_ancestry": str(membership_summary["assigned_ancestry"]),
        "sample_size": str(membership_summary["sample_size"]),
        "source_url": str(membership_summary["source_url"]),
    }


def render_stores_tsv(
    bundles: list[Bundle],
    artifact_root: Path | str | None = None,
    repo_root: Path | str | None = None,
) -> str:
    """Render the canonical stores.tsv from a list of bundles."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for b in sorted(bundles, key=lambda x: x.store_id):
        row = render_stores_row(b, artifact_root=artifact_root, repo_root=repo_root)
        writer.writerow(row)
    return output.getvalue()


def _format_cell(val: Any) -> str:
    """Format markdown table cell values."""
    if val is None or val == "":
        return "-"
    try:
        return f"{int(val):,}"
    except (ValueError, TypeError):
        return str(val)


def tolerated_gaps_by_store(
    bundles: list[Bundle],
    registry_root: Path | str,
) -> dict[str, tuple[bundle.ToleratedGap, ...]]:
    """Compute each bundle's tolerated suppressions via check()'s second channel.

    The index reads git and Release Bundle files only; ``check()`` honours the
    same seam (it never opens a Store), so running it here adds no artifact
    access. Only bundles with a non-empty tolerated gap are returned, so a
    registry with none renders no gap section (issue #142).
    """
    gaps: dict[str, tuple[bundle.ToleratedGap, ...]] = {}
    for b in bundles:
        tolerated = tuple(bundle.check(b, registry_root=registry_root).tolerated)
        if tolerated:
            gaps[b.store_id] = tolerated
    return gaps


def render_stores_md(
    bundles: list[Bundle],
    rows: list[dict[str, str]],
    tolerated_gaps: dict[str, tuple[bundle.ToleratedGap, ...]] | None = None,
) -> str:
    """Render human-readable STORES.md table."""
    # A row carries only its own columns, so the bundle the pending marker needs
    # comes from the same list, looked up by id.
    bundles_by_id = {b.store_id: b for b in bundles}
    lines: list[str] = [
        "# OpenGWASDB Store Releases",
        "",
        "Generated master list of Store Releases in this registry.",
        "",
        "> Generated from Release Bundles in `stores/`. Do not edit by hand; regenerate with `pixi run index`.",
        "",
        "| Store ID | Label | Layout | Completion | Status | Format | Analyses (admitted) | Excluded | Variants | Associations | Validated |",
        "|:---|:---|:---|:---|:---|:---|:---|---:|---:|---:|:---|",
    ]

    for row in rows:
        sid = f"`{row['store_id']}`"
        lbl = row["label"] or "-"
        lay = row["layout"] or "-"
        comp = row["completion_state"] or "-"
        # The same predicate readiness uses for the interim state (#307).
        bundle_obj = bundles_by_id.get(row["store_id"])
        if bundle_obj is not None and bundle.is_completion_pending(
            bundle_obj, row["completion_state"]
        ):
            comp += " (completion pending)"
        st = row["status"] or "-"
        fmt = row["format_version"] or "-"
        n_ana = _format_cell(row["n_analyses"])
        n_exc = _format_cell(row["n_excluded"])
        n_var = _format_cell(row["n_variants"])
        n_assoc = _format_cell(row["n_associations"])
        val_st = row["validate_status"] or "-"

        lines.append(f"| {sid} | {lbl} | {lay} | {comp} | {st} | {fmt} | {n_ana} | {n_exc} | {n_var} | {n_assoc} | {val_st} |")

    lines.extend([
        "",
        "## Derived membership summaries",
        "",
        "Every value below is derived from the Release Bundle's `analyses.tsv`; `NA` means the column is absent or has an empty value.",
        "",
        "| Store ID | Author | Publication PMID | Tissue | Context | Population | Sample size | Download source |",
        "|:---|:---|:---|:---|:---|:---|:---|:---|",
    ])
    for row in rows:
        lines.append(
            "| "
            + " | ".join([
                f"`{row['store_id']}`",
                row["first_author"],
                row["publication_pmid"],
                row["tissue"],
                row["context"],
                row["assigned_ancestry"],
                row["sample_size"],
                row["source_url"],
            ])
            + " |"
        )

    if tolerated_gaps:
        lines.extend([
            "",
            "## Tolerated gaps",
            "",
            "These bundles pass the Release Bundle gate only because a named "
            "exemption tolerates known-missing required Analysis values. The "
            "values are genuinely unavailable and left blank rather than "
            "fabricated (issue #134); the tolerance is the visible remainder, "
            "not a clean pass.",
            "",
            "| Store ID | Tolerated gap |",
            "|:---|:---|",
        ])
        for row in rows:
            gaps = (tolerated_gaps or {}).get(row["store_id"])
            if not gaps:
                continue
            for gap in gaps:
                lines.append(
                    f"| `{row['store_id']}` | {gap.count} {gap.detail} "
                    f"(tolerated under {gap.citation}) |"
                )

    lines.append("")
    return "\n".join(lines)


def write_bundle_summary(bundle_obj: Bundle) -> Path:
    """Write the generated summary beside the membership table."""
    summary_path = bundle_obj.root / "summary.yaml"
    content = yaml.safe_dump(
        bundle.summarise(bundle_obj),
        sort_keys=False,
        allow_unicode=True,
    )
    summary_path.write_text(content, encoding="utf-8")
    return summary_path


def generate_by_label_symlinks(
    bundles: list[Bundle],
    registry_root: Path | str | None = None,
    artifact_root: Path | str | None = None,
) -> None:
    """Generate stores/by-label/ and <artifact_root>/by-label/ symlink trees."""
    resolved_registry_root = (
        Path(registry_root).resolve()
        if registry_root
        else (REPO_ROOT / "stores").resolve()
    )

    # 1. stores/by-label/
    stores_by_label_dir = resolved_registry_root / "by-label"
    stores_by_label_dir.mkdir(parents=True, exist_ok=True)

    # Clean existing symlinks in stores/by-label/
    for item in stores_by_label_dir.iterdir():
        if item.is_symlink() or item.is_file():
            item.unlink()

    for b in bundles:
        if b.label and b.store_id:
            link_p = stores_by_label_dir / b.label
            target_rel = Path("..") / b.store_id
            link_p.symlink_to(target_rel)

    # 2. <artifact_root>/by-label/ (if artifact_root directory exists)
    if artifact_root:
        resolved_artifact_root = Path(artifact_root).resolve()
        if resolved_artifact_root.is_dir():
            art_by_label_dir = resolved_artifact_root / "by-label"
            art_by_label_dir.mkdir(parents=True, exist_ok=True)
            for item in art_by_label_dir.iterdir():
                if item.is_symlink() or item.is_file():
                    item.unlink()
            for b in bundles:
                if b.label and b.store_id:
                    link_p = art_by_label_dir / b.label
                    target_rel = Path("..") / b.store_id
                    link_p.symlink_to(target_rel)


def generate_index(
    registry_root: Path | str | None = None,
    repo_root: Path | str | None = None,
    artifact_root: Path | str | None = None,
) -> tuple[Path, Path]:
    """Generate master views, bundle summaries, and by-label symlinks.

    Strict seam compliance: reads git, never opens or inspects the artifact root.
    """
    resolved_repo_root = Path(repo_root).resolve() if repo_root else REPO_ROOT.resolve()
    resolved_registry_root = (
        Path(registry_root).resolve()
        if registry_root
        else (resolved_repo_root / "stores").resolve()
    )

    # Discover all store directories in registry_root
    store_ids = sorted([
        d.name for d in resolved_registry_root.iterdir()
        if d.is_dir() and paths.is_valid_store_id(d.name) and (d / "release.yaml").is_file()
    ]) if resolved_registry_root.is_dir() else []

    bundles = [bundle.load(sid, registry_root=resolved_registry_root) for sid in store_ids]
    for bundle_obj in bundles:
        write_bundle_summary(bundle_obj)
    tolerated = tolerated_gaps_by_store(bundles, resolved_registry_root)
    # Resolve the artifact root once, from configuration rather than any Build
    # Recipe (issue #126), and render both derived views under it.
    resolved_artifact_root = Path(artifact_root) if artifact_root else paths.artifact_root()
    rows = [render_stores_row(b, artifact_root=resolved_artifact_root, repo_root=resolved_repo_root) for b in bundles]

    # Write stores.tsv
    tsv_content = render_stores_tsv(bundles, artifact_root=resolved_artifact_root, repo_root=resolved_repo_root)
    stores_tsv_path = resolved_repo_root / "stores.tsv"
    stores_tsv_path.write_text(tsv_content, encoding="utf-8")

    # Write STORES.md
    md_content = render_stores_md(bundles, rows, tolerated_gaps=tolerated)
    stores_md_path = resolved_repo_root / "STORES.md"
    stores_md_path.write_text(md_content, encoding="utf-8")

    # Generate by-label trees
    generate_by_label_symlinks(
        bundles,
        registry_root=resolved_registry_root,
        artifact_root=Path(artifact_root) if artifact_root else None,
    )

    # Delete docs/store-catalog.md as subsumed
    store_catalog_p = resolved_repo_root / "docs" / "store-catalog.md"
    if store_catalog_p.is_file():
        store_catalog_p.unlink()

    return stores_tsv_path, stores_md_path


build_index = generate_index
regenerate_index = generate_index

__all__ = [
    "COLUMNS",
    "build_index",
    "generate_by_label_symlinks",
    "generate_index",
    "regenerate_index",
    "render_stores_md",
    "render_stores_row",
    "render_stores_tsv",
    "tolerated_gaps_by_store",
    "write_bundle_summary",
]
