#!/usr/bin/env python3
"""The Planned changes section of the Structure and health page, from a fixture `work.json` (#422).

The fixture is a hand-written literal in the shape `docs/spec/project-site.md`
documents, so every expected value is read off it. One test renders the page with
Quarto; it is skipped without it.

Run from the repository root:
    python3 tests/project-site-architecture/test_plan_page.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_architecture_pages import architecture

from project_site import pages

QUARTO = shutil.which("quarto")
URL = "https://github.com/opengwas/opengwasdb/issues/{}"
ENGINE = {"ref": "main", "commit": "e" * 40}


def comp(cid: str, name: str, repo: str = "engine", new: bool = False) -> dict:
    return {"id": cid, "name": name, "repo": None if new else repo, "new": new}


def entry(number: int, state: str, plan: dict, components: list[dict], checks: list[dict]) -> dict:
    return {"ticket": f"opengwasdb#{number}", "epic": "opengwasdb#58", "title": f"Ticket {number}", "url": URL.format(number),
            "state": state, "plan": {"adds": [], "moves": [], "drops": [], "absorbs": [], **plan},
            "components": components, "checks": checks}


OPEN = entry(
    350, "open",
    {"adds": [{"paths": ["opengwasdb/encoding/association.py"], "component": "enc"}],
     "drops": [{"from": "dense", "to": "enc"}], "absorbs": ["encoding/codec.py:932", "layouts/ragged/zarr_csr.py:551"]},
    [comp("dense", "Dense layout"), comp("enc", "Store Encoding")], [])
CLOSED = entry(
    361, "closed",
    {"adds": [{"paths": ["opengwasdb/fresh/a.py", "opengwasdb/enc/gone.py"], "component": "fresh", "name": "Fresh one"}],
     "moves": [{"paths": ["opengwasdb/build/eaf.py"], "to": "enc"}],
     "drops": [{"from": "dense", "to": "enc"}]},
    [comp("dense", "Dense layout"), comp("enc", "Store Encoding"), comp("fresh", "Fresh one", new=True)],
    [{"kind": "adds", "path": "opengwasdb/fresh/a.py", "component": "fresh", "held": True, "why": "claimed by `fresh`"},
     {"kind": "adds", "path": "opengwasdb/enc/gone.py", "component": "fresh", "held": False, "why": "no component claims it"},
     {"kind": "moves", "path": "opengwasdb/build/eaf.py", "component": "enc", "held": None, "why": "the engine's model is missing at main (eeeeeeeeeeee)"},
     {"kind": "drops", "from": "dense", "to": "enc", "imports": 4, "held": False, "why": "4 imports from `dense` into `enc` remain"}])
INVALID = entry(362, "open", {"invalid": "unknown key `removes`"}, [], [])
INVALID["plan"] = {"invalid": "unknown key `removes`"}


def work(*plans: dict | None, key: bool = True) -> dict:
    doc = {"source": "work", "available": True, "collected_at": "2026-11-02T12:00:00Z", "error": None, "schema_version": 1,
           "inputs": {"board": {"collected_at": "x"}, "architecture": {"collected_at": "x", "engine": ENGINE,
                                                                       "registry": {"ref": "HEAD", "commit": "r" * 40}}},
           "owners": [], "components": [], "signals": [], "meets": [], "unmeasured": {}, "waits": [], "tickets": {},
           "branches": {"sources": [], "list": []}, "summary": {}}
    if key:
        doc["plans"] = list(plans)
    return doc


def planned(doc: dict | None) -> str:
    page = pages.render_structure({"architecture": architecture(), "work": doc})
    return page.split("## Planned changes", 1)[1] if "## Planned changes" in page else ""


def dots(text: str) -> list[str]:
    return re.findall(r"```\{dot\}\n(.*?)\n```", text, re.S)


class PlannedChangesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = planned(work(OPEN, CLOSED, INVALID))

    def test_the_section_is_on_the_structure_page_with_one_diagram_per_plan(self) -> None:
        self.assertEqual(len(dots(self.text)), 2)

    def test_added_and_moved_items_are_marked_and_dropped_edges_dashed(self) -> None:
        open_dot, closed_dot = dots(self.text)
        self.assertIn("+ opengwasdb/encoding/association.py", open_dot)
        self.assertIn('"dense" -> "enc" [style=dashed', open_dot)
        self.assertIn("→ opengwasdb/build/eaf.py", closed_dot)
        self.assertIn("+ opengwasdb/fresh/a.py", closed_dot)
        self.assertIn('"dense" -> "enc" [style=dashed', closed_dot)
        self.assertNotIn("→", open_dot)

    def test_a_new_component_is_drawn_dashed_and_called_new(self) -> None:
        closed_dot = dots(self.text)[1]
        fresh = next(line for line in closed_dot.splitlines() if line.startswith('"fresh" ['))
        self.assertIn("Fresh one", fresh)
        self.assertIn("new", fresh)
        self.assertIn("dashed", fresh)
        enc = next(line for line in closed_dot.splitlines() if line.startswith('"enc" ['))
        self.assertNotIn("dashed", enc)

    def test_an_open_plan_is_planned_with_its_absorbs_listed_and_nothing_checked(self) -> None:
        block = self.text.split("opengwasdb#350", 1)[1].split("###", 1)[0]
        self.assertIn("**Planned**", block)
        self.assertIn("`encoding/codec.py:932`", block)
        self.assertIn("`layouts/ragged/zarr_csr.py:551`", block)
        self.assertNotIn("held", block)

    def test_a_closed_plan_shows_held_not_held_and_unknown_per_item(self) -> None:
        block = self.text.split("opengwasdb#361", 1)[1].split("###", 1)[0]
        rows = [line for line in block.splitlines() if line.startswith("- ")]
        self.assertEqual([r.split(" — ")[0] for r in rows], ["- **held**", "- **not held**", "- **unknown**", "- **not held**"])
        self.assertIn("`opengwasdb/enc/gone.py`", rows[1])
        self.assertIn("no component claims it", rows[1])
        self.assertIn("4 imports from", rows[3])
        self.assertIn(pages._code("the engine's model is missing at main (eeeeeeeeeeee)"), rows[2])

    def test_unmet_items_are_a_warning_that_says_the_ticket_may_have_closed_on_an_unmerged_branch(self) -> None:
        block = self.text.split("opengwasdb#361", 1)[1].split("###", 1)[0]
        self.assertIn("**Warning**", block)
        self.assertIn("2 items", block)
        self.assertIn("integration branch", block)

    def test_an_invalid_plan_is_shown_with_its_reason(self) -> None:
        block = self.text.split("opengwasdb#362", 1)[1]
        self.assertIn("**Invalid plan**", block)
        self.assertIn(pages._code("unknown key `removes`"), block)

    def test_facts_go_through_the_code_helper(self) -> None:
        self.assertIn(pages._code("opengwasdb/encoding/association.py"), self.text)
        self.assertIn(pages._code("dense"), self.text)
        hostile = entry(363, "open", {"absorbs": ["<script>x</script> | `y`"]}, [], [])
        text = planned(work(hostile))
        self.assertNotIn("<script>", text)
        self.assertIn(pages._code("<script>x</script> | `y`"), text)

    def test_it_says_the_plans_are_written_by_hand_in_tickets_and_the_checks_are_measured(self) -> None:
        self.assertIn("written by hand", self.text)
        self.assertIn("measured", self.text)

    def test_no_plans_is_said_not_left_blank(self) -> None:
        self.assertIn("No ticket carries an `architecture` block", planned(work()))

    def test_a_work_file_without_the_plans_key_is_not_called_empty(self) -> None:
        text = planned(work(key=False))
        self.assertNotIn("No ticket carries", text)
        self.assertIn("predates planned changes", text)

    def test_unavailable_work_is_the_sites_notice(self) -> None:
        text = planned({"source": "work", "available": False, "collected_at": None, "error": "CollectError: board is unavailable"})
        self.assertIn("Work data unavailable", text)
        self.assertIn("board is unavailable", text)

    def test_a_component_id_cannot_inject_a_diagram_statement(self) -> None:
        hostile = 'fresh"; injected [label="UNTRUSTED"]; "tail'
        plan = entry(364, "open", {"adds": [{"paths": ['a"b\\c.py'], "component": hostile, "name": 'N"x'}]},
                     [{"id": hostile, "name": 'N"x', "repo": None, "new": True}], [])
        source = dots(planned(work(plan)))[0]
        self.assertIn('"fresh\\"; injected [label=\\"UNTRUSTED\\"]; \\"tail" [', source)
        self.assertEqual(sum(1 for line in source.splitlines() if "UNTRUSTED" in line), 1)

    def test_the_warning_names_the_ref_of_the_repository_the_item_was_read_in(self) -> None:
        registry = entry(365, "closed", {"adds": [{"paths": ["src/x.py"], "component": "pages"}]}, [comp("pages", "Pages", "registry")],
                         [{"kind": "adds", "path": "src/x.py", "component": "pages", "held": False, "why": "no such file", "repo": "registry"}])
        block = planned(work(registry)).split("opengwasdb#365", 1)[1]
        self.assertIn("registry `HEAD`, `rrrrrrrrrrrr`", block)
        self.assertNotIn("engine `main`", block)
        engine = planned(work(CLOSED)).split("opengwasdb#361", 1)[1]
        self.assertIn("engine `main`, `eeeeeeeeeeee`", engine)


@unittest.skipUnless(QUARTO, "Quarto is not on PATH; the render assertion needs it")
class QuartoRenderTests(unittest.TestCase):
    def test_the_page_renders_with_the_plan_diagrams(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="plan-page-"))
        self.addCleanup(shutil.rmtree, root, True)
        docs = root / "docs"
        docs.mkdir()
        (docs / "_quarto.yml").write_text("project:\n  type: website\n", encoding="utf-8")
        page = pages.render_structure({"architecture": architecture(), "work": work(OPEN, CLOSED)})
        (docs / "index.qmd").write_text(pages._page("Structure", page), encoding="utf-8")
        subprocess.run([QUARTO, "render", str(docs), "--output-dir", str(root / "out")], check=True, capture_output=True, text=True)
        html = (root / "out" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Planned changes", html)
        self.assertIn("Fresh one", html)


if __name__ == "__main__":
    unittest.main()
