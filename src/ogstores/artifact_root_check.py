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
   holds the release's lock. The lock is probed as the entry point's dry run
   probes it (`release.lock_holder`): a non-blocking shared lock, taken and
   released at once, which creates and writes nothing.

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
otherwise, and 2 when it refuses: an Artifact Root that is not a directory or
cannot be read, a registry that is missing or holds no bundle, a status
vocabulary it does not know, or a report path under the Artifact Root. The
report records the registry's git revision and whether its tree was dirty.

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
import subprocess
import sys
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

from ogstores import bundle as bundle_module
from ogstores import paths, release
from ogstores.readiness import discover_bundles

DEFAULT_REGISTRY_ROOT: Path = paths.REPO_ROOT / "stores"


class Category(IntEnum):
    """A finding's category: its number, its report name (`key`) and its title."""

    title: str

    def __new__(cls, number: int, title: str) -> "Category":
        member = int.__new__(cls, number)
        member._value_ = number
        member.title = title
        return member

    REGISTERED_BUT_MISSING = 1, "registered but missing"
    UNREGISTERED = 2, "on disk but unregistered"
    STATUS_DISAGREEMENT = 3, "status disagreement"
    BROKEN_BY_LABEL_LINK = 4, "broken by-label link"
    STRAY_STATE = 5, "stray state"

    @property
    def key(self) -> str:
        return self.name.lower()

    @property
    def fails(self) -> bool:
        """Categories 1 and 4 fail the check; a status disagreement can be normal."""
        return self in (Category.REGISTERED_BUT_MISSING, Category.BROKEN_BY_LABEL_LINK)


class CheckRefused(RuntimeError):
    """The check cannot give a meaningful answer; it exits 2 and writes no report."""


# The bundle module's Release Status vocabulary, split by what each says about
# the Store. A status in none of the three would be reported as nothing, so a
# vocabulary the split does not cover refuses the check (`require_status_split`).
BUILT_STATUSES: frozenset[str] = frozenset({"built", "validated"})
UNBUILT_STATUSES: frozenset[str] = frozenset({"candidate", "accepted"})
RETIRED_STATUSES: frozenset[str] = frozenset({"superseded", "withdrawn"})


def require_status_split() -> None:
    """Raise unless the three status sets are exactly `bundle.VALID_STATUSES`."""
    split = BUILT_STATUSES | UNBUILT_STATUSES | RETIRED_STATUSES
    valid = frozenset(bundle_module.VALID_STATUSES)
    if split != valid:
        raise CheckRefused(
            "the Artifact Root check does not know what these Release Statuses say about a Store: "
            f"{sorted(valid - split)}; it splits only {sorted(split)}"
        )


require_status_split()


@dataclass(frozen=True)
class Finding:
    category: Category
    store_id: str | None
    detail: str
    release_status: str | None = None
    #: A path under `<root>/<id>`, never one outside it.
    path: str | None = None
    #: A `by-label` link's label (category 4).
    label: str | None = None

    def sort_key(self) -> tuple[int, str, str]:
        # Stable: one release's stray state keeps the order it was found in.
        return (self.category, self.store_id or "~", self.label or "")


@dataclass(frozen=True)
class CheckResult:
    artifact_root: Path
    registry_revision: str | None
    registry_dirty: bool | None
    n_bundles: int
    n_release_dirs: int
    findings: tuple[Finding, ...]

    @property
    def exit_status(self) -> int:
        return 1 if any(f.category.fails for f in self.findings) else 0


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
    live_run = f"a run of {store_id} is in progress ({holder})" if holder is not None else None
    findings = []
    marker = paths.publication_marker(store_id, root=root)
    if _present(marker):
        if live_run is not None:
            detail = f"{live_run} and is publishing; it finishes its own publication"
        else:
            detail = f"a publication is pending, and `pixi run release {store_id}` finishes it"
        findings.append(Finding(Category.STRAY_STATE, store_id, f"{paths.PUBLICATION_MARKER} exists: {detail}", status, str(marker)))
    for staged in _staged(store_id, root):
        note = f"; {live_run}" if live_run is not None else ""
        findings.append(Finding(Category.STRAY_STATE, store_id, f"{staged.name} exists: a Store is staged{note}", status, str(staged)))
    return findings


def _label_findings(root: Path) -> list[Finding]:
    by_label = paths.by_label_dir(root=root)
    if not by_label.is_dir():
        return []
    findings = []
    for entry in sorted(by_label.iterdir()):
        if not entry.is_symlink() or entry.exists():
            continue
        # `root` is resolved; resolve the target's directory too, so a link that
        # spells the root another way (relative, absolute, through a link) matches.
        raw = by_label / os.readlink(entry)
        target = Path(os.path.realpath(raw.parent)) / raw.name
        inside = target.parent == root and paths.is_valid_store_id(target.name)
        store_id = target.name if inside else None
        where = f"its target {store_id} is missing" if inside else "its target, outside the Artifact Root's release directories, is missing"
        findings.append(Finding(Category.BROKEN_BY_LABEL_LINK, store_id, f"label `{entry.name}`: {where}", label=entry.name))
    return findings


def _git(registry: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(registry), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def registry_revision(registry: Path) -> tuple[str | None, bool | None]:
    """The registry's `git rev-parse HEAD`, and whether its tree differs from it; None outside git."""
    head = _git(registry, "rev-parse", "HEAD")
    if head is None:
        return None, None
    status = _git(registry, "status", "--porcelain", "--untracked-files=normal", "--", ".")
    return head.strip(), (None if status is None else bool(status.strip()))


def check_artifact_root(root: Path | str, *, registry_root: Path | str | None = None) -> CheckResult:
    """Compare the registry's bundles with the Artifact Root; read-only.

    Raises `CheckRefused` when the answer would mean nothing: an unknown status
    vocabulary, a registry that is missing or holds no bundle, or an `OSError`
    while reading the registry or the root.
    """
    require_status_split()
    registry = Path(registry_root) if registry_root is not None else DEFAULT_REGISTRY_ROOT
    if not registry.is_dir():
        raise CheckRefused(f"the registry {registry} is not a directory; nothing was checked")
    root = Path(root).resolve()
    try:
        loaded = discover_bundles(registry)
        if not loaded:
            raise CheckRefused(
                f"the registry {registry} holds no Release Bundle; every release directory "
                "would read as unregistered, so nothing was checked"
            )
        revision, dirty = registry_revision(registry)
        findings = _findings(root, loaded)
        n_release_dirs = len(_release_dirs(root))
    except OSError as exc:
        raise CheckRefused(f"reading the registry or the Artifact Root failed: {exc}; nothing was reported") from exc
    return CheckResult(
        artifact_root=root,
        registry_revision=revision,
        registry_dirty=dirty,
        n_bundles=len(loaded),
        n_release_dirs=n_release_dirs,
        findings=tuple(sorted(findings, key=Finding.sort_key)),
    )


def _findings(root: Path, loaded: list) -> list[Finding]:
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
            findings.append(Finding(Category.REGISTERED_BUT_MISSING, b.store_id, f"Release Status `{status}`, but {where}", status, str(store)))
        elif present and status in UNBUILT_STATUSES | RETIRED_STATUSES:
            findings.append(Finding(Category.STATUS_DISAGREEMENT, b.store_id, f"the Store is present while the Release Status is `{status}`", status, str(store)))

    for store_id in on_disk:
        if store_id in by_id:
            continue
        store = paths.store_path(store_id, root=root)
        holds = "holds a Store" if _present(store) else "holds no Store"
        retired = "; the id is retired" if store_id in paths.RETIRED_STORE_IDS else ""
        findings.append(Finding(Category.UNREGISTERED, store_id, f"no Release Bundle; the directory {holds}{retired}", path=str(paths.store_dir(store_id, root=root))))

    findings.extend(_label_findings(root))

    for store_id in on_disk:
        b = by_id.get(store_id)
        findings.extend(_stray_state(store_id, root, b.status if b else None))
    return findings


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def _utc(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def report_document(result: CheckResult, *, now: dt.datetime, host: str) -> dict[str, Any]:
    counts = {category.key: 0 for category in Category}
    for f in result.findings:
        counts[f.category.key] += 1
    findings = []
    for f in result.findings:
        row: dict[str, Any] = {"category": int(f.category), "name": f.category.key, "store_id": f.store_id}
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
        "registry_revision": result.registry_revision,
        "registry_dirty": result.registry_dirty,
        "bundles": result.n_bundles,
        "release_directories": result.n_release_dirs,
        "exit_status": result.exit_status,
        "failing_categories": [int(c) for c in Category if c.fails],
        "counts": counts,
        "findings": findings,
    }


def _revision(doc: dict[str, Any]) -> str:
    if doc["registry_revision"] is None:
        return "unknown (the registry is not a git checkout)"
    state = {True: "with uncommitted changes", False: "clean", None: "state unknown"}[doc["registry_dirty"]]
    return f"`{doc['registry_revision']}` ({state})"


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
        f"- Registry revision: {_revision(doc)}",
        f"- Release Bundles: {doc['bundles']}; release directories: {doc['release_directories']}",
        f"- Result: {verdict}, exit status {doc['exit_status']} (categories 1 and 4 fail the check)",
        "",
        "| # | Category | Findings |",
        "|---:|:---|---:|",
    ]
    for category in Category:
        lines.append(f"| {int(category)} | {category.title} | {doc['counts'][category.key]} |")
    lines += ["", "## Findings", ""]
    if not doc["findings"]:
        lines.append("None.")
    else:
        lines += ["| # | Category | Store | Release Status | Finding |", "|---:|:---|:---|:---|:---|"]
        for f in doc["findings"]:
            store = f"`{f['store_id']}`" if f["store_id"] else "-"
            status = f"`{f['release_status']}`" if f.get("release_status") else "-"
            lines.append(
                f"| {f['category']} | {Category(f['category']).title} | {store} | {status} | {_cell(f['detail'])} |"
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

    root = paths.artifact_root(args.artifact_root).resolve()
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

    try:
        result = check_artifact_root(root, registry_root=args.registry_root)
    except CheckRefused as exc:
        print(f"check-artifact-root: {exc}", file=sys.stderr)
        return 2
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
