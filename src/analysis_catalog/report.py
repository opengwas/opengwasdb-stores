"""`report.md`: every registered Store Release's decision, and what changed (ADR 0039).

The change is read from the previous `current` catalog by SQL, so the report
cannot disagree with what the catalog serves. The summary report (#325) renders
beside it.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from analysis_catalog import BUILDER_VERSION
from analysis_catalog.columns import ANALYSIS_COLUMNS
from analysis_catalog.policy import EXCLUDED, LISTED, PLACEHOLDER, REFUSED, Decision

# A rebuild after a big Store arrives could add millions of ids. `changes.tsv`
# holds every one; the report shows counts and this many of each.
LIST_LIMIT = 20
CHANGES_TSV = "changes.tsv"


@dataclass
class Change:
    """What differs from the previous version, by Analysis id."""

    previous_version: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[tuple[str, list[str]]] = field(default_factory=list)
    n_added: int = 0
    n_removed: int = 0
    n_changed: int = 0
    decisions: list[str] = field(default_factory=list)


def compare(
    connection: sqlite3.Connection, previous: Path, previous_version: str, changes_path: Path
) -> Change:
    """Compare the new database, open on `connection`, with the previous version's.

    Every change goes to `changes_path` (`id`, `change`), in id order.
    """
    connection.execute("ATTACH DATABASE ? AS previous", (f"file:{previous}?mode=ro",))
    try:
        shared = [
            column for column in ANALYSIS_COLUMNS
            if column != "id" and column in _columns(connection, "previous", "analyses")
        ]
        differs = " OR ".join(f"n.{c} IS NOT p.{c}" for c in shared) or "0"
        change = Change(previous_version)
        change.n_added = connection.execute(
            "SELECT COUNT(*) FROM main.analyses WHERE id NOT IN (SELECT id FROM previous.analyses)"
        ).fetchone()[0]
        change.n_removed = connection.execute(
            "SELECT COUNT(*) FROM previous.analyses WHERE id NOT IN (SELECT id FROM main.analyses)"
        ).fetchone()[0]
        change.n_changed = connection.execute(
            f"SELECT COUNT(*) FROM main.analyses n JOIN previous.analyses p USING (id) WHERE {differs}"
        ).fetchone()[0]
        change.added = [r[0] for r in connection.execute(
            "SELECT id FROM main.analyses WHERE id NOT IN (SELECT id FROM previous.analyses) "
            "ORDER BY id LIMIT ?", (LIST_LIMIT,))]
        change.removed = [r[0] for r in connection.execute(
            "SELECT id FROM previous.analyses WHERE id NOT IN (SELECT id FROM main.analyses) "
            "ORDER BY id LIMIT ?", (LIST_LIMIT,))]
        with open(changes_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["id", "change"])
            writer.writerows(connection.execute(
                "SELECT id, 'added' FROM main.analyses WHERE id NOT IN (SELECT id FROM previous.analyses) "
                "UNION ALL SELECT id, 'removed' FROM previous.analyses "
                "WHERE id NOT IN (SELECT id FROM main.analyses) "
                "UNION ALL SELECT n.id, 'changed' FROM main.analyses n JOIN previous.analyses p USING (id) "
                f"WHERE {differs} ORDER BY 1"
            ))
        pairs = ", ".join(f"n.{c}, p.{c}" for c in shared)
        for row in connection.execute(
            f"SELECT n.id, {pairs} FROM main.analyses n JOIN previous.analyses p USING (id) "
            f"WHERE {differs} ORDER BY n.id LIMIT ?", (LIST_LIMIT,)
        ):
            values = row[1:]
            names = [c for i, c in enumerate(shared) if values[2 * i] != values[2 * i + 1]]
            change.changed.append((row[0], names))
        change.decisions = [
            f"{store}: {old_decision} ({old_reason}) -> {new_decision} ({new_reason})"
            for store, old_decision, old_reason, new_decision, new_reason in connection.execute(
                "SELECT n.store_id, p.decision, p.reason, n.decision, n.reason "
                "FROM main.selection n JOIN previous.selection p USING (store_id) "
                "WHERE n.decision IS NOT p.decision OR n.reason IS NOT p.reason ORDER BY n.store_id"
            )
        ]
        for store, decision, reason in connection.execute(
            "SELECT store_id, decision, reason FROM main.selection "
            "WHERE store_id NOT IN (SELECT store_id FROM previous.selection) ORDER BY store_id"
        ):
            change.decisions.append(f"{store}: new, {decision} ({reason})")
        for (store,) in connection.execute(
            "SELECT store_id FROM previous.selection "
            "WHERE store_id NOT IN (SELECT store_id FROM main.selection) ORDER BY store_id"
        ):
            change.decisions.append(f"{store}: no longer registered")
        return change
    finally:
        connection.commit()
        connection.execute("DETACH DATABASE previous")


def _columns(connection: sqlite3.Connection, schema: str, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA {schema}.table_info({table})")}


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render(
    decisions: list[Decision],
    *,
    version: str,
    built_at: str,
    registry_commit: str | None,
    change: Change | None,
) -> str:
    counts = {name: sum(d.decision == name for d in decisions) for name in (LISTED, PLACEHOLDER, EXCLUDED, REFUSED)}
    lines = [
        f"# Analysis Catalog {version}",
        "",
        f"Built {built_at} from registry commit {registry_commit or 'none (the registry is not a git checkout)'}, "
        f"builder version {BUILDER_VERSION}.",
        "",
        "## Selection",
        "",
        f"{len(decisions)} registered, {counts[LISTED]} listed, {counts[PLACEHOLDER]} placeholder, "
        f"{counts[EXCLUDED]} excluded, {counts[REFUSED]} refused.",
        "",
    ]
    if not counts[LISTED]:
        lines += [
            f"No Store Release is listed: {len(decisions)} registered, 0 listed, "
            f"{counts[PLACEHOLDER]} placeholder, {counts[EXCLUDED]} excluded, {counts[REFUSED]} refused. "
            "A Store Release is listed only when it is `validated`, not a Trial Store Release, not "
            "`embargoed`, and its Release Manifest count matches its Validation Record.",
            "",
        ]
    lines += ["| Store | Decision | Reason |", "|---|---|---|"]
    lines += [f"| {d.store_id} | {d.decision} | {_cell(d.reason)} |" for d in decisions]
    lines += ["", "## Change since the previous version", ""]
    if change is None:
        lines += ["No previous version: nothing to compare.", ""]
        return "\n".join(lines)
    lines += [
        f"Previous version: {change.previous_version}",
        "",
        f"Analyses added: {change.n_added}",
        f"Analyses removed: {change.n_removed}",
        f"Analyses with changed metadata: {change.n_changed}",
        "",
        f"Every changed id is in `{CHANGES_TSV}` beside this report; the lists below are a sample.",
        "",
    ]
    for heading, count, entries in (
        ("Added", change.n_added, [f"- {i}" for i in change.added]),
        ("Removed", change.n_removed, [f"- {i}" for i in change.removed]),
        ("Changed", change.n_changed, [f"- {i} ({', '.join(c)})" for i, c in change.changed]),
    ):
        if not entries:
            continue
        lines += [f"### {heading}", ""] + entries
        if count > len(entries):
            lines.append(f"- … and {count - len(entries)} more, all in `{CHANGES_TSV}`")
        lines.append("")
    if change.decisions:
        lines += ["### Store Release decisions changed", ""] + [f"- {d}" for d in change.decisions] + [""]
    return "\n".join(lines)
