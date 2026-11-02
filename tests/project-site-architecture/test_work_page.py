#!/usr/bin/env python3
"""The Work map page, written from a fixture `work.json` (#419).

The fixture is a hand-written literal in the shape `docs/spec/project-site.md`
documents, so every expected figure is read off it. One test renders the page with
Quarto; it is skipped without it.

Run from the repository root:
    python3 tests/project-site-architecture/test_work_page.py
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from project_site import pages

QUARTO = shutil.which("quarto")
MEASURED = "2026-11-02T12:00:00Z"
URL = "https://github.com/opengwas/{}/issues/{}"


def owner(key: str, title: str, status: str) -> dict:
    repo, number = key.split("#")
    return {"key": key, "kind": "epic", "title": title, "url": URL.format(repo, number), "status": status, "workstream": None}


def signal(component: str, who: str, item: str, via: str, state: str, what: list[str]) -> dict:
    return {"component": component, "owner": who, "item": item, "via": via, "state": state, "what": what}


def branch(name: str, repo: str, who: str, item: str, components: list[str], files: int) -> dict:
    return {"repo": repo, "name": name, "ahead": 2, "committed": "2026-10-29T10:00:00Z", "files": [f"f{i}.py" for i in range(files)],
            "epic": None, "ticket": None, "local": False, "owner": who, "item": item, "components": components}


def work(*, registry_fetch: dict | None = None) -> dict:
    return {
        "source": "work", "available": True, "collected_at": MEASURED, "error": None, "schema_version": 1,
        "inputs": {"board": {"collected_at": "2026-11-02T11:59:00Z"},
                   "architecture": {"collected_at": "2026-11-02T11:59:30Z", "engine": {"ref": "main", "commit": "e" * 40},
                                    "registry": {"ref": "HEAD", "commit": "r" * 40}}},
        "owners": [owner("opengwasdb#347", "Encoding rework", "Active"), owner("opengwasdb#58", "Hybrid layout", "Active"),
                   owner("opengwasdb-stores#232", "Phase 2", "Active")],
        "components": [{"id": "enc", "name": "Store Encoding", "repo": "engine"}, {"id": "query", "name": "Query", "repo": "engine"},
                       {"id": "store", "name": "Store envelope", "repo": "engine"}],
        "signals": [
            signal("enc", "opengwasdb#347", "opengwasdb#338", "branch", "building", ["opengwasdb:orch/347/338"]),
            signal("enc", "opengwasdb#58", "opengwasdb#350", "cited", "active", ["opengwasdb/encoding/se_range.py"]),
            signal("query", "opengwasdb#58", "opengwasdb#57", "cited", "active", ["opengwasdb/query/facade.py"]),
            signal("query", "opengwasdb-stores#232", "opengwasdb-stores#240", "branch", "building", ["opengwasdb:orch/232/240"]),
            signal("query", "opengwasdb-stores#232", "opengwasdb-stores#241", "cited", "waiting", ["opengwasdb/query/x.py"]),
        ],
        "unmeasured": {},
        "meets": [
            {"component": "store", "owners": ["opengwasdb#58", "opengwasdb#347"],
             "items": {"opengwasdb#58": ["branch:opengwasdb:orch/58-deepen-modules"], "opengwasdb#347": ["opengwasdb#338"]},
             "pairs": [{"owners": ["opengwasdb#58", "opengwasdb#347"], "coordinated": False, "coordination": "unlinkable",
                        "edges": [], "epic_branches": ["opengwasdb:orch/58-deepen-modules"]}],
             "coordinated": False, "coordination": "unlinkable"},
            {"component": "enc", "owners": ["opengwasdb#347", "opengwasdb#58"],
             "items": {"opengwasdb#347": ["opengwasdb#338"], "opengwasdb#58": ["opengwasdb#350"]},
             "pairs": [{"owners": ["opengwasdb#347", "opengwasdb#58"], "coordinated": True, "coordination": "coordinated",
                        "edges": [{"waiting": "opengwasdb#338", "on": "opengwasdb#350"}]}], "coordinated": True, "coordination": "coordinated"},
            {"component": "query", "owners": ["opengwasdb#58", "opengwasdb-stores#232"],
             "items": {"opengwasdb#58": ["opengwasdb#57"], "opengwasdb-stores#232": ["opengwasdb-stores#240"]},
             "pairs": [{"owners": ["opengwasdb#58", "opengwasdb-stores#232"], "coordinated": False, "coordination": "uncoordinated",
                        "edges": [], "epic_branches": []}], "coordinated": False, "coordination": "uncoordinated"},
        ],
        "waits": [{"item": "opengwasdb#338", "on": "opengwasdb#350"}],
        "tickets": {},
        "branches": {
            "sources": [
                {"repo": "opengwasdb", "git_dir": "/clone/engine.git", "main": "main", "days": 21, "fetch": {"ok": True, "error": None}, "local": False, "count": 2},
                {"repo": "opengwasdb-stores", "git_dir": "/checkout", "main": "origin/main", "days": 21, "fetch": registry_fetch, "local": False, "count": 0},
            ],
            "list": [branch("orch/232/240", "opengwasdb", "opengwasdb-stores#232", "opengwasdb-stores#240", ["query"], 3),
                     branch("orch/347/338", "opengwasdb", "opengwasdb#347", "opengwasdb#338", ["enc"], 44)],
        },
        "summary": {"owners": 3, "meets": 3, "coordinated": 1, "uncoordinated": 1, "unlinkable": 1, "branches": 2},
    }


def render(document: dict | None) -> str:
    return pages.render_work({"work": document})


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


class WorkPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = render(work())

    def test_the_tiles_are_the_summary_figures(self) -> None:
        lines = self.page.splitlines()
        head = lines.index("| Owners | Meets | Coordinated | Uncoordinated | Unlinkable | Branches |")
        self.assertEqual(cells(lines[head + 2]), ["3", "3", "1", "1", "1", "2"])

    def test_each_meet_lists_its_owners_items_and_coordination(self) -> None:
        self.assertIn("### Store Encoding", self.page)
        enc = self.page.split("### Store Encoding", 1)[1].split("###", 1)[0]
        self.assertIn("[opengwasdb#347](https://github.com/opengwas/opengwasdb/issues/347)", enc)
        self.assertIn("Encoding rework", enc)
        self.assertIn("opengwasdb#338", enc)
        self.assertIn("**Coordinated**: opengwasdb#338 waits for opengwasdb#350", enc)
        query = self.page.split("### Query", 1)[1].split("###", 1)[0]
        self.assertIn("**Uncoordinated**", query)
        self.assertIn("no blocking edge links", query)

    def test_a_meet_with_an_epic_branch_says_why_it_cannot_be_linked_and_is_not_called_uncoordinated(self) -> None:
        store = self.page.split("### Store envelope", 1)[1].split("###", 1)[0]
        self.assertIn("**Unlinkable**", store)
        self.assertIn("`opengwasdb:orch/58-deepen-modules` belongs to an epic as a whole, so no blocking edge can link it to a single ticket", store)
        self.assertNotIn("Uncoordinated", store)

    def test_engine_side_work_that_was_not_measured_is_said_on_the_page(self) -> None:
        document = work()
        document["unmeasured"] = {"engine": "docs/architecture/components.yaml is not in opengwas/opengwasdb at main (abc)"}
        page = render(document)
        self.assertIn("**Engine-side work was not measured**", page)
        self.assertIn("is not in opengwas/opengwasdb at main (abc)", page)
        self.assertIn("meets shown are this repository's only", page)
        self.assertNotIn("Engine-side work was not measured", self.page)

    def test_uncoordinated_meets_come_first(self) -> None:
        self.assertLess(self.page.index("### Query"), self.page.index("### Store Encoding"))

    def test_the_matrix_has_a_row_per_component_and_a_column_per_owner(self) -> None:
        lines = self.page.splitlines()
        head = next(i for i, line in enumerate(lines) if line.startswith("| Component |"))
        header = cells(lines[head])
        self.assertEqual([h.split("](")[0].lstrip("[") for h in header[1:]],
                         ["opengwasdb#347", "opengwasdb#58", "opengwasdb-stores#232"])
        rows = {cells(line)[0]: cells(line)[1:] for line in lines[head + 2:] if line.startswith("|")}
        self.assertEqual(rows["Store Encoding"], ["building", "active", ""])
        self.assertEqual(rows["Query"], ["", "active", "building, waiting"])

    def test_it_says_when_it_was_measured_and_that_build_host_branches_are_not_shown(self) -> None:
        self.assertIn("Measured 2026-11-02T12:00:00Z", self.page)
        self.assertIn("exist only on a build host are not shown", self.page)

    def test_it_says_what_is_written_by_hand_and_what_is_measured(self) -> None:
        self.assertIn("**Written by hand:**", self.page)
        self.assertIn("**Measured:**", self.page)

    def test_the_branches_are_listed_with_their_files_and_owner(self) -> None:
        lines = [cells(line) for line in self.page.splitlines() if line.startswith("| `orch/")]
        self.assertEqual(
            [(r[0], r[1], r[2], r[4]) for r in lines],
            [("`orch/232/240`", "opengwasdb", "2", "3"), ("`orch/347/338`", "opengwasdb", "2", "44")],
        )

    def test_a_failed_branch_fetch_is_a_visible_line_with_gits_error(self) -> None:
        page = render(work(registry_fetch={"ok": False, "error": "fatal: could not read from remote"}))
        self.assertIn("**The branch fetch for opengwasdb-stores failed**", page)
        self.assertIn("fatal: could not read from remote", page)
        self.assertNotIn("The branch fetch for opengwasdb failed", page)

    def test_an_unavailable_work_source_is_the_sites_usual_notice(self) -> None:
        page = render({"source": "work", "available": False, "collected_at": MEASURED, "error": "CollectError: board is unavailable"})
        self.assertIn("Work data unavailable", page)
        self.assertIn("board is unavailable", page)
        self.assertNotIn("| Owners |", page)

    def test_a_work_file_that_was_never_written_is_a_notice(self) -> None:
        self.assertIn("`work.json` was not written", render(None))

    def test_no_fact_reaches_the_page_as_raw_html(self) -> None:
        document = work()
        document["owners"][0]["title"] = "<script>x</script> | `y`"
        page = render(document)
        self.assertNotIn("<script>", page)


@unittest.skipUnless(QUARTO, "Quarto is not on PATH; the render assertion needs it")
class QuartoRenderTests(unittest.TestCase):
    def test_the_page_renders_with_its_tables(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="work-page-"))
        self.addCleanup(shutil.rmtree, root, True)
        docs = root / "docs"
        docs.mkdir()
        (docs / "_quarto.yml").write_text("project:\n  type: website\n", encoding="utf-8")
        (docs / "index.qmd").write_text(pages._page("Work map", render(work())), encoding="utf-8")
        subprocess.run([QUARTO, "render", str(docs), "--output-dir", str(root / "out")], check=True, capture_output=True, text=True)
        html = (root / "out" / "index.html").read_text(encoding="utf-8")
        self.assertGreaterEqual(html.count("<table"), 3)
        self.assertIn("Store Encoding", html)


if __name__ == "__main__":
    unittest.main()
