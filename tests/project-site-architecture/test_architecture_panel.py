#!/usr/bin/env python3
"""The component panel on the architecture pages (#435).

Click a component, a box in a diagram or its name in a list or table, and a
panel opens with what the map knows about it. The page carries the panel's
data as JSON, and a short script draws it. The data is what can go wrong
quietly, so it is tested here:

- **What it says.** Each component's code, imports, ADRs and work, from
  `architecture.json` and `work.json` alone.
- **What it does not say.** Work the collectors could not measure is unknown,
  never "none".
- **What it leaks.** The collected data holds host paths, which are never
  published. Only the fields the panel shows are embedded, and no fact can
  become markup or end the page's raw block.

The links that open it are tested on the Markdown the pages write. One test
renders a page with Quarto, to see that the diagram boxes come out as links and
the data survives the render; it is skipped without Quarto.

Run from the repository root:
    python3 tests/project-site-architecture/test_architecture_panel.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_architecture_pages import ENGINE_COMMIT, architecture, dot_cells
from test_work_page import work

from project_site import pages

QUARTO = shutil.which("quarto")
PAGES = (pages.render_pipeline, pages.render_structure, pages.render_work)
DATA_BLOCK = re.compile(r'<script type="application/json" id="arch-panel-data">(.*?)</script>', re.S)


def documents(*, arch: dict | None = None, work_doc: dict | None = None, no_work: bool = False) -> dict:
    doc = arch if arch is not None else architecture()
    doc["components"][0]["paths"] = ["src/manifest_generation/"]
    for component in doc["components"]:
        if component["id"] == "query":
            component["paths"] = ["opengwasdb/query/"]
    return {"architecture": doc, "work": None if no_work else (work_doc if work_doc is not None else work())}


def panel_data(page: str) -> dict:
    found = DATA_BLOCK.findall(page)
    assert len(found) == 1, f"{len(found)} panel data blocks"
    return json.loads(found[0])


class EveryPageTests(unittest.TestCase):
    def test_each_architecture_page_carries_the_panel_once(self) -> None:
        for render in PAGES:
            page = render(documents())
            with self.subTest(page=render.__name__):
                self.assertEqual(page.count('id="arch-panel"'), 1)
                self.assertEqual(len(DATA_BLOCK.findall(page)), 1)
                self.assertEqual(page.count("```{=html}"), 1)

    def test_without_the_architecture_data_there_is_no_panel_and_no_link_to_one(self) -> None:
        docs = documents()
        docs["architecture"] = None
        for render in PAGES:
            page = render(docs)
            with self.subTest(page=render.__name__):
                self.assertNotIn("arch-panel", page)
                self.assertNotIn("#component-", page)


class ContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = panel_data(pages.render_structure(documents()))
        self.components = self.data["components"]

    def test_a_component_says_its_name_summary_repository_container_size_and_stages(self) -> None:
        gen = self.components["gen"]
        self.assertEqual(gen["name"], "Manifest Generation")
        self.assertEqual(gen["summary"], "Manifest Generation summary.")
        self.assertEqual(gen["repo"], "registry")
        self.assertEqual(gen["container"], "Manifest Generation")
        self.assertEqual(gen["size"], "3 files, 100 lines")
        self.assertEqual(gen["stages"], ["Build"])

    def test_its_claimed_paths_link_to_github_at_the_commit_read(self) -> None:
        self.assertEqual(self.components["gen"]["paths"], [[
            "src/manifest_generation/",
            "https://github.com/opengwas/opengwasdb-stores/tree/1111111111111111/src/manifest_generation/",
        ]])
        self.assertEqual(self.components["query"]["paths"], [[
            "opengwasdb/query/", f"https://github.com/opengwas/opengwasdb/tree/{ENGINE_COMMIT}/opengwasdb/query/",
        ]])

    def test_imports_each_way_carry_their_counts_largest_first_and_mark_the_upward_ones(self) -> None:
        store = self.components["store"]
        self.assertEqual(store["out"], [["query", 3, True]])
        self.assertEqual(store["in"], [["gen", 7, False], ["query", 4, False], ["model", 2, True]])
        self.assertEqual(self.components["gen"]["out"], [["store", 7, False], ["model", 1, False]])

    def test_each_adr_says_how_it_was_placed(self) -> None:
        self.assertEqual(self.components["gen"]["adrs"], [{
            "title": "0001. Manifest Generation is one pipeline", "repo": "registry", "status": "current",
            "url": "https://github.com/opengwas/opengwasdb-stores/blob/main/docs/adr/0001-x.md", "via": "cites its code",
        }])
        self.assertEqual([a["via"] for a in self.components["query"]["adrs"]], ["names it in its title"])

    def test_work_is_grouped_by_owner_with_each_items_state_and_what_it_touches(self) -> None:
        self.assertEqual(self.components["query"]["work"], [
            {"owner": "opengwasdb#58", "items": [
                {"state": "active", "item": "opengwasdb#57", "what": ["opengwasdb/query/facade.py"]}]},
            {"owner": "opengwasdb-stores#232", "items": [
                {"state": "building", "item": "opengwasdb-stores#240", "what": ["opengwasdb:orch/232/240"]},
                {"state": "waiting", "item": "opengwasdb-stores#241", "what": ["opengwasdb/query/x.py"]}]},
        ])
        self.assertEqual(self.data["owners"]["opengwasdb#58"], {
            "title": "Hybrid layout", "url": "https://github.com/opengwas/opengwasdb/issues/58", "status": "Active"})
        self.assertIn("building", self.data["states"])

    def test_a_component_no_work_touches_has_none_which_is_a_measurement(self) -> None:
        self.assertEqual(self.components["model"]["work"], [])
        self.assertIsNone(self.components["model"]["work_note"])


class UnknownWorkTests(unittest.TestCase):
    def test_without_the_work_data_every_components_work_is_unknown(self) -> None:
        data = panel_data(pages.render_pipeline(documents(no_work=True)))

        for cid, component in data["components"].items():
            with self.subTest(component=cid):
                self.assertIsNone(component["work"])
                self.assertIn("could not be read", component["work_note"])

    def test_with_the_engine_unmeasured_its_components_work_is_unknown_and_this_repositorys_is_not(self) -> None:
        unmeasured = work()
        unmeasured["unmeasured"] = {"engine": "the engine has no model at main"}
        data = panel_data(pages.render_structure(documents(work_doc=unmeasured)))

        self.assertIsNone(data["components"]["query"]["work"])
        self.assertIn("the engine has no model at main", data["components"]["query"]["work_note"])
        self.assertEqual(data["components"]["gen"]["work"], [])

    def test_without_the_engines_model_imports_into_it_are_said_to_be_unplaced(self) -> None:
        data = panel_data(pages.render_structure(documents(arch=architecture(engine_model=False))))

        self.assertIn("model", data["components"]["gen"]["imports_note"])


class LeakTests(unittest.TestCase):
    def test_no_host_path_in_the_collected_data_reaches_a_page(self) -> None:
        arch, work_doc = architecture(), work()
        arch["registry"]["git_dir"] = "/home/someone/registry-checkout"
        arch["engine"]["clone"] = "/data/opengwasdb/work/site/opengwasdb.git"
        work_doc["branches"]["sources"][0]["git_dir"] = "/home/someone/engine.git"
        docs = documents(arch=arch, work_doc=work_doc)
        for render in PAGES:
            page = render(docs)
            with self.subTest(page=render.__name__):
                for planted in ("/home/someone", "/data/opengwasdb/work/site", "/clone/engine.git", "/checkout"):
                    self.assertNotIn(planted, page)

    def test_a_fact_cannot_become_markup_or_close_the_raw_block_or_the_script(self) -> None:
        hostile = 'Ends </script><b>bold</b> and ```\n``` a fence & more'
        arch = architecture()
        arch["components"][0]["summary"] = hostile
        page = pages.render_structure(documents(arch=arch))

        block = page[page.index("```{=html}"):]
        block = block[:block.index("\n```\n", len("```{=html}")) + 5]
        self.assertNotIn("<b>", block)
        self.assertEqual(block.count("</script>"), 2, "the data's own and the script's, nothing from a fact")
        self.assertEqual(panel_data(page)["components"]["gen"]["summary"], hostile)


class LinkTests(unittest.TestCase):
    def test_each_component_box_in_the_engine_and_registry_diagrams_opens_its_panel(self) -> None:
        cells = dot_cells(pages.render_structure(documents()))
        engine, registry = cells[2], cells[3]
        self.assertIn('URL="#component-query"', engine)
        self.assertIn('class="arch-node arch-c-query"', engine)
        self.assertIn('URL="#component-gen"', registry)

    def test_each_component_on_the_pipeline_opens_its_panel(self) -> None:
        self.assertIn("**[Manifest Generation](#component-gen)**", pages.render_pipeline(documents()))

    def test_the_upward_imports_table_opens_both_ends(self) -> None:
        structure = pages.render_structure(documents())
        self.assertIn("| [Store envelope](#component-store) (layer 1) | [Query](#component-query) (layer 2) | 3 |", structure)

    def test_work_map_names_open_their_panels_and_a_component_the_map_does_not_know_stays_text(self) -> None:
        page = pages.render_work(documents())
        self.assertIn("| [Query](#component-query) |", page)
        self.assertIn("### [Store envelope](#component-store)", page)
        self.assertNotIn("#component-enc", page)
        self.assertIn("### Store Encoding", page)


@unittest.skipUnless(QUARTO, "Quarto is not on PATH; the render assertion needs it")
class QuartoRenderTests(unittest.TestCase):
    def test_the_boxes_are_links_and_the_data_survives_the_render(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="arch-panel-"))
        self.addCleanup(shutil.rmtree, root, True)
        docs = root / "docs"
        docs.mkdir()
        (docs / "_quarto.yml").write_text("project:\n  type: website\n", encoding="utf-8")
        page = pages.render_structure(documents())
        (docs / "index.qmd").write_text(pages._page("Structure", page), encoding="utf-8")
        subprocess.run([QUARTO, "render", str(docs), "--output-dir", str(root / "out")],
                       check=True, capture_output=True, text=True)
        rendered = (root / "out" / "index.html").read_text(encoding="utf-8")

        self.assertRegex(rendered, r'<g id="[^"]*" class="node arch-node arch-c-query">')
        self.assertIn('href="#component-query"', rendered)
        self.assertEqual(panel_data(rendered), panel_data(page))
        self.assertIn('<aside id="arch-panel"', rendered)


if __name__ == "__main__":
    unittest.main()
