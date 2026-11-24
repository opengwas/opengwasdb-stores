"""The Artifact Root check: do the Store Registry and the Artifact Root agree? (#510)

A read-only comparison of the committed Release Bundles with what is under the
Artifact Root on this host. It reports five categories of finding:

1. **Registered but missing** -- a bundle whose Release Status describes a
   built Store (`built`, `validated`) with no Store at its path,
   `<root>/<id>/store.opengwasdb`.
2. **Unregistered** -- a release directory `<root>/<id>` with no bundle.
3. **Status disagreement** -- a Store present while its bundle says it is not
   built (`candidate`, `accepted`), or is retired (`superseded`, `withdrawn`).
   Reported, not judged: a release that completes its own Store is `accepted`
   while its Observed-Only Store is published (ADR 0037).
4. **Broken `by-label` link** -- a link under `<root>/by-label/` whose target is
   missing.
5. **Stray state** -- a pending publication (`publication.json`) or a staged
   Store (`store.opengwasdb.partial*`, `.store.opengwasdb.partial*`), described
   as the release workflow's refusals describe them, and whether a live run
   holds the release's lock.

A Store is present when its path exists (`lstat`), the test the release
workflow's own refusal of a published release makes
(`run.refuse_rebuilding_published_releases`). A release that completes its own
Store is published twice under one id (ADR 0037), both times at that one path,
so the test is the same for it.

It never writes under the Artifact Root, never changes a bundle, and refuses
to write its report under the Artifact Root. The report records the UTC time,
the host, the Artifact Root and each finding, and no path outside
`<root>/<id>`: a `by-label` link is named by its label and its target's Store
id, and a lock by its holder. It exits 1 when it finds category 1 or 4, 0
otherwise, and 2 when it refuses.

    pixi run check-artifact-root [--artifact-root PATH] [--registry-root stores] \
        [--json docs/artifact-root-check.json] [--markdown docs/artifact-root-check.md]

See docs/spec/store-release-workflow.md, "The Artifact Root check".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ogstores import bundle as bundle_module
from ogstores import paths, release
from ogstores.bundle import Bundle
from ogstores.readiness import discover_bundles

REGISTERED_BUT_MISSING = 1
UNREGISTERED = 2
STATUS_DISAGREEMENT = 3
BROKEN_BY_LABEL_LINK = 4
STRAY_STATE = 5

CATEGORY_NAMES: dict[int, str] = {
    REGISTERED_BUT_MISSING: "registered_but_missing",
    UNREGISTERED: "unregistered",
    STATUS_DISAGREEMENT: "status_disagreement",
    BROKEN_BY_LABEL_LINK: "broken_by_label_link",
    STRAY_STATE: "stray_state",
}
CATEGORY_TITLES: dict[int, str] = {
    REGISTERED_BUT_MISSING: "registered but missing",
    UNREGISTERED: "on disk but unregistered",
    STATUS_DISAGREEMENT: "status disagreement",
    BROKEN_BY_LABEL_LINK: "broken by-label link",
    STRAY_STATE: "stray state",
}
#: The categories that fail the check. A status disagreement can be normal.
FAILING_CATEGORIES: frozenset[int] = frozenset({REGISTERED_BUT_MISSING, BROKEN_BY_LABEL_LINK})

# The bundle module's Release Status vocabulary, split by what each says about
# the Store. The assertion makes a new status a loud failure here, not a gap.
BUILT_STATUSES: frozenset[str] = frozenset({"built", "validated"})
UNBUILT_STATUSES: frozenset[str] = frozenset({"candidate", "accepted"})
RETIRED_STATUSES: frozenset[str] = frozenset({"superseded", "withdrawn"})
assert BUILT_STATUSES | UNBUILT_STATUSES | RETIRED_STATUSES == bundle_module.VALID_STATUSES


@dataclass(frozen=True)
class Finding:
    category: int
    store_id: str | None
    detail: str
    release_status: str | None = None
    #: A path under `<root>/<id>`, never one outside it.
    path: str | None = None
    #: A `by-label` link's label (category 4).
    label: str | None = None

    @property
    def name(self) -> str:
        return CATEGORY_NAMES[self.category]

    def sort_key(self) -> tuple[int, str, str]:
        # Stable: one release's stray state keeps the order it was found in.
        return (self.category, self.store_id or "~", self.label or "")


@dataclass(frozen=True)
class CheckResult:
    artifact_root: Path
    n_bundles: int
    n_release_dirs: int
    findings: tuple[Finding, ...]

    @property
    def exit_status(self) -> int:
        return 1 if any(f.category in FAILING_CATEGORIES for f in self.findings) else 0


def _present(path: Path) -> bool:
    return os.path.lexists(path)


def _release_dirs(root: Path) -> list[str]:
    """The Store Release ids with a directory under the Artifact Root."""
    return sorted(
        entry.name
        for entry in os.scandir(root)
        if paths.is_valid_store_id(entry.name) and entry.is_dir(follow_symlinks=False)
    )


def _staged(store_id: str, root: Path) -> list[Path]:
    """A release's staged Store and the files staging leaves beside it (`relocate_store`'s refusal)."""
    staged = paths.partial_store_path(store_id, root=root)
    directory = staged.parent
    return sorted(directory.glob(f"{staged.name}*")) + sorted(directory.glob(f".{staged.name}*"))


def _stray_state(store_id: str, root: Path, status: str | None) -> list[Finding]:
    holder = release.lock_holder(store_id, root)
    run_note = f"; a run of {store_id} is in progress ({holder})" if holder is not None else ""
    findings = []
    marker = paths.publication_marker(store_id, root=root)
    if _present(marker):
        if holder is not None:
            detail = f"a run of {store_id} is in progress ({holder}) and is publishing; it finishes its own publication"
        else:
            detail = f"a publication is pending, and `pixi run release {store_id}` finishes it"
        findings.append(Finding(STRAY_STATE, store_id, f"{paths.PUBLICATION_MARKER} exists: {detail}", status, str(marker)))
    for staged in _staged(store_id, root):
        findings.append(Finding(STRAY_STATE, store_id, f"{staged.name} exists: a Store is staged{run_note}", status, str(staged)))
    return findings


def _label_findings(root: Path) -> list[Finding]:
    by_label = paths.by_label_dir(root=root)
    if not by_label.is_dir():
        return []
    findings = []
    for entry in sorted(by_label.iterdir()):
        if not entry.is_symlink() or entry.exists():
            continue
        target = Path(os.path.normpath(by_label / os.readlink(entry)))
        inside = target.parent == root and paths.is_valid_store_id(target.name)
        store_id = target.name if inside else None
        where = f"its target {store_id} is missing" if inside else "its target, outside the Artifact Root's release directories, is missing"
        findings.append(Finding(BROKEN_BY_LABEL_LINK, store_id, f"label `{entry.name}`: {where}", label=entry.name))
    return findings


def check_artifact_root(
    root: Path | str,
    *,
    registry_root: Path | str | None = None,
    bundles: Sequence[Bundle] | None = None,
) -> CheckResult:
    """Compare the registry's bundles with the Artifact Root; read-only."""
    root = Path(root)
    loaded = list(bundles) if bundles is not None else discover_bundles(registry_root)
    by_id = {b.store_id: b for b in loaded}
    on_disk = _release_dirs(root)
    findings: list[Finding] = []

    for b in loaded:
        status = b.status
        store = paths.store_path(b.store_id, root=root)
        present = _present(store)
        if status in BUILT_STATUSES and not present:
            directory = paths.store_dir(b.store_id, root=root)
            where = "its release directory holds no Store" if _present(directory) else "it has no release directory"
            findings.append(Finding(REGISTERED_BUT_MISSING, b.store_id, f"Release Status `{status}`, but {where}", status, str(store)))
        elif present and status in UNBUILT_STATUSES | RETIRED_STATUSES:
            findings.append(Finding(STATUS_DISAGREEMENT, b.store_id, f"the Store is present while the Release Status is `{status}`", status, str(store)))

    for store_id in on_disk:
        if store_id in by_id:
            continue
        store = paths.store_path(store_id, root=root)
        holds = "holds a Store" if _present(store) else "holds no Store"
        retired = "; the id is retired" if store_id in paths.RETIRED_STORE_IDS else ""
        findings.append(Finding(UNREGISTERED, store_id, f"no Release Bundle; the directory {holds}{retired}", path=str(paths.store_dir(store_id, root=root))))

    findings.extend(_label_findings(root))

    for store_id in on_disk:
        b = by_id.get(store_id)
        findings.extend(_stray_state(store_id, root, b.status if b else None))

    return CheckResult(
        artifact_root=root,
        n_bundles=len(loaded),
        n_release_dirs=len(on_disk),
        findings=tuple(sorted(findings, key=Finding.sort_key)),
    )


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def _utc(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def report_document(result: CheckResult, *, now: dt.datetime, host: str) -> dict[str, Any]:
    counts = {name: 0 for name in CATEGORY_NAMES.values()}
    for f in result.findings:
        counts[f.name] += 1
    findings = []
    for f in result.findings:
        row: dict[str, Any] = {"category": f.category, "name": f.name, "store_id": f.store_id}
        if f.release_status is not None:
            row["release_status"] = f.release_status
        if f.label is not None:
            row["label"] = f.label
        if f.path is not None:
            row["path"] = f.path
        row["detail"] = f.detail
        findings.append(row)
    return {
        "check": "artifact-root-check",
        "checked_at": _utc(now),
        "host": host,
        "artifact_root": str(result.artifact_root),
        "bundles": result.n_bundles,
        "release_directories": result.n_release_dirs,
        "exit_status": result.exit_status,
        "failing_categories": sorted(FAILING_CATEGORIES),
        "counts": counts,
        "findings": findings,
    }


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(doc: dict[str, Any]) -> str:
    verdict = "fails" if doc["exit_status"] else "passes"
    lines = [
        "# Artifact Root check",
        "",
        "Generated by `pixi run check-artifact-root` (`ogstores.artifact_root_check`) from",
        "the committed Release Bundles and the Artifact Root on the host below. Do not",
        "edit by hand; the same run writes `docs/artifact-root-check.json`. It is read-only:",
        "fixing a finding (rebuild, withdraw, finish a publication) is the operator's decision.",
        "",
        f"- Checked at: {doc['checked_at']}",
        f"- Host: `{doc['host']}`",
        f"- Artifact Root: `{doc['artifact_root']}`",
        f"- Release Bundles: {doc['bundles']}; release directories: {doc['release_directories']}",
        f"- Result: {verdict}, exit status {doc['exit_status']} (categories 1 and 4 fail the check)",
        "",
        "| # | Category | Findings |",
        "|---:|:---|---:|",
    ]
    for number, name in CATEGORY_NAMES.items():
        lines.append(f"| {number} | {CATEGORY_TITLES[number]} | {doc['counts'][name]} |")
    lines += ["", "## Findings", ""]
    if not doc["findings"]:
        lines.append("None.")
    else:
        lines += ["| # | Category | Store | Release Status | Finding |", "|---:|:---|:---|:---|:---|"]
        for f in doc["findings"]:
            store = f"`{f['store_id']}`" if f["store_id"] else "-"
            status = f"`{f['release_status']}`" if f.get("release_status") else "-"
            lines.append(
                f"| {f['category']} | {CATEGORY_TITLES[f['category']]} | {store} | {status} | {_cell(f['detail'])} |"
            )
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _under(path: Path, root: Path) -> bool:
    resolved, base = path.resolve(), root.resolve()
    return resolved == base or base in resolved.parents


def main(argv: list[str] | None = None, *, now: dt.datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-artifact-root", description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifact-root", type=Path, default=None, help="default: ogstores' resolution (paths.artifact_root())")
    parser.add_argument("--registry-root", type=Path, default=None, help="default: the checkout's stores/")
    parser.add_argument("--json", type=Path, default=None, help="write the report as JSON here")
    parser.add_argument("--markdown", type=Path, default=None, help="write the report as Markdown here")
    args = parser.parse_args(argv)

    root = paths.artifact_root(args.artifact_root)
    if not root.is_dir():
        print(f"check-artifact-root: the Artifact Root {root} is not a directory; nothing was checked", file=sys.stderr)
        return 2
    for output in (args.json, args.markdown):
        if output is not None and _under(output, root):
            print(
                f"check-artifact-root: refusing to write {output} under the Artifact Root; "
                "the check is read-only there",
                file=sys.stderr,
            )
            return 2

    result = check_artifact_root(root, registry_root=args.registry_root)
    doc = report_document(result, now=now or dt.datetime.now(dt.timezone.utc), host=socket.gethostname())
    markdown = render_markdown(doc)
    if args.json is not None:
        _write(args.json, json.dumps(doc, indent=2) + "\n")
    if args.markdown is not None:
        _write(args.markdown, markdown)
    print(markdown, end="")
    return result.exit_status


if __name__ == "__main__":
    sys.exit(main())
