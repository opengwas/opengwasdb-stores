"""Fixture board, architecture and branch documents for the Work map's tests (#419).

Small literals in the shape `docs/spec/project-site.md` documents, so a test's
expected values can be written by hand from them. Components: `enc` claims
`opengwasdb/encoding/`, `query` `opengwasdb/query/`, `store` `opengwasdb/store/`
(engine), `pages` claims `src/project_site/` (registry).
"""

from __future__ import annotations

from project_site.collect import work

ENGINE, STORES = "opengwasdb", "opengwasdb-stores"


def ticket(key: str, *, cited: list[str] | None = None, blocked_by: list[str] | None = None, state: str = "OPEN") -> dict:
    return {"key": key, "title": f"Ticket {key}", "url": f"https://example.org/{key}", "state": state,
            "cited": cited or [], "blocked_by": [{"key": b, "state": "OPEN"} for b in blocked_by or []]}


def epic(key: str, status: str, children: list[dict], *, cited: list[str] | None = None, state: str = "OPEN") -> dict:
    return {"key": key, "title": f"Epic: {key}", "url": f"https://example.org/{key}", "state": state, "status": status,
            "workstream": "Engineering", "cited": cited or [], "children": children}


def board(*epics: dict) -> dict:
    return {"source": "board", "available": True, "collected_at": "2026-11-02T12:00:00Z", "error": None,
            "project": "opengwas/1", "epics": list(epics), "milestones": [], "items": []}


def component(cid: str, name: str, repo: str, paths: list[str]) -> dict:
    return {"id": cid, "name": name, "repo": repo, "paths": paths}


ARCHITECTURE = {
    "source": "architecture", "available": True, "collected_at": "2026-11-02T12:00:00Z", "error": None,
    "engine": {"ref": "main", "commit": "e" * 40}, "registry": {"ref": "HEAD", "commit": "r" * 40},
    "components": [
        component("enc", "Store Encoding", "engine", ["opengwasdb/encoding/"]),
        component("query", "Query", "engine", ["opengwasdb/query/"]),
        component("store", "Store envelope", "engine", ["opengwasdb/store/"]),
        component("pages", "Site pages", "registry", ["src/project_site/"]),
    ],
}


def branch(name: str, files: list[str], *, repo: str = ENGINE, epic: str | None = None, ticket: str | None = None) -> dict:
    return {"repo": repo, "name": name, "ahead": 1, "committed": "2026-11-01T10:00:00Z", "files": files,
            "epic": epic, "ticket": ticket, "local": False}


def branch_set(*found: dict, repo: str = ENGINE, fetch: dict | None = None) -> dict:
    return {"repo": repo, "git_dir": f"/git/{repo}", "main": "main", "days": 21, "fetch": fetch, "branches": list(found)}


def compute(board_doc: dict, *sets: dict) -> dict:
    return work.compute(board_doc, ARCHITECTURE, list(sets))


def states(result: dict, component_id: str) -> dict[tuple[str, str], str]:
    """`(owner, item)` -> state of every signal on one component."""
    return {(s["owner"], s["item"]): s["state"] for s in result["signals"] if s["component"] == component_id}
