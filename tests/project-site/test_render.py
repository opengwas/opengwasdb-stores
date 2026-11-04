#!/usr/bin/env python3
"""The parallel render: pages spread over several Quarto processes, merged back into one site.

Quarto renders a project's pages one after another, about a second each, so the
site took five minutes on a host with hundreds of cores. `project_site.render`
gives each worker its own copy of the staged project, renders one page per
`quarto render <page>` call, and merges the workers' output.

A worker's output holds more than its own pages, and the merge has to know
which copy of a file to believe:

- A page comes from the worker that rendered it. A worker that never rendered
  the home page still writes an `index.html`: Quarto's redirect to the first page
  that worker rendered.
- Every other file (theme, scripts, the resources each call copies) is the
  same in every worker. If it is not, the merge stops rather than pick one.
- The search index and the sitemap list only the pages each worker rendered, so
  they are put back together page by page, each page's part from its renderer.

The merge rules are tested on hand-made worker trees. One test renders a small
real project both ways and compares the two sites file by file, because the
rules above are Quarto's behaviour, not ours, and only a real render shows
whether they still hold.

Run from the repository root:
    python3 tests/project-site/test_render.py
"""

from __future__ import annotations

import filecmp
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from project_site import render

SITE_URL = "https://example.org/site/"

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{urls}</urlset>
"""


def sitemap_url(page_html: str, stamp: str) -> str:
    return (
        "  <url>\n"
        f"    <loc>{SITE_URL}{page_html}</loc>\n"
        f"    <lastmod>{stamp}</lastmod>\n"
        "  </url>\n"
    )


class MergeTest(unittest.TestCase):
    """Two hand-made worker outputs, as Quarto leaves them, merged into one site."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-merge-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.output = self.root / "out"
        self.pages = ["index.qmd", "adr/0001-a.md"]
        # w0 rendered the ADR, w1 the home page.
        self.w0 = self._worker("w0", {
            "adr/0001-a.html": "<p>adr</p>",
            "index.html": "<meta http-equiv=\"refresh\" content=\"0;URL='adr/0001-a.html'\" />",
            "site_libs/quarto.js": "lib",
            "notes.md": "copied resource",
        })
        self.w1 = self._worker("w1", {
            "index.html": "<p>home</p>",
            "site_libs/quarto.js": "lib",
            "notes.md": "copied resource",
        })
        self.parts = [
            render.Part(site=self.w0, pages=["adr/0001-a.md"]),
            render.Part(site=self.w1, pages=["index.qmd"]),
        ]

    def test_a_page_comes_from_the_worker_that_rendered_it(self) -> None:
        render.merge(self.parts, self.pages, self.output)

        self.assertEqual((self.output / "index.html").read_text(), "<p>home</p>")
        self.assertEqual((self.output / "adr/0001-a.html").read_text(), "<p>adr</p>")
        self.assertEqual((self.output / "site_libs/quarto.js").read_text(), "lib")
        self.assertEqual((self.output / "notes.md").read_text(), "copied resource")

    def test_a_shared_file_that_differs_between_workers_stops_the_merge(self) -> None:
        (self.w1 / "site_libs/quarto.js").write_text("other lib")

        with self.assertRaises(render.RenderFailed) as caught:
            render.merge(self.parts, self.pages, self.output)

        self.assertIn("site_libs/quarto.js", str(caught.exception))

    def test_a_page_its_renderer_wrote_no_html_for_stops_the_merge(self) -> None:
        (self.w0 / "adr/0001-a.html").unlink()

        with self.assertRaises(render.RenderFailed) as caught:
            render.merge(self.parts, self.pages, self.output)

        self.assertIn("adr/0001-a.md", str(caught.exception))

    def test_the_search_index_takes_each_page_from_its_renderer_in_page_order(self) -> None:
        adr = [
            {"objectID": "adr/0001-a.html", "href": "adr/0001-a.html", "title": "ADR — one", "text": "a"},
            {"objectID": "adr/0001-a.html#why", "href": "adr/0001-a.html#why", "title": "ADR — one", "text": "b"},
        ]
        home = [{"objectID": "index.html", "href": "index.html", "title": "Home", "text": "c"}]
        (self.w0 / "search.json").write_text(json.dumps(adr), encoding="utf-8")
        (self.w1 / "search.json").write_text(json.dumps(home), encoding="utf-8")

        render.merge(self.parts, self.pages, self.output)

        text = (self.output / "search.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), home + adr)
        self.assertIn("ADR — one", text, "Quarto writes the index as UTF-8, not \\u escapes")

    def test_a_search_entry_for_a_page_its_worker_did_not_render_stops_the_merge(self) -> None:
        stray = [{"objectID": "index.html", "href": "index.html", "title": "Home", "text": "redirect"}]
        (self.w0 / "search.json").write_text(json.dumps(stray), encoding="utf-8")

        with self.assertRaises(render.RenderFailed) as caught:
            render.merge(self.parts, self.pages, self.output)

        self.assertIn("index.html", str(caught.exception))

    def test_the_sitemap_takes_each_page_from_its_renderer_in_page_order(self) -> None:
        (self.w0 / "sitemap.xml").write_text(SITEMAP.format(urls=sitemap_url("adr/0001-a.html", "2026-11-04T10:00:00.000Z")))
        (self.w1 / "sitemap.xml").write_text(SITEMAP.format(urls=sitemap_url("index.html", "2026-11-04T11:00:00.000Z")))

        render.merge(self.parts, self.pages, self.output, site_url=SITE_URL)

        self.assertEqual(
            (self.output / "sitemap.xml").read_text(),
            SITEMAP.format(urls=(
                sitemap_url("index.html", "2026-11-04T11:00:00.000Z")
                + sitemap_url("adr/0001-a.html", "2026-11-04T10:00:00.000Z")
            )),
        )

    def test_a_sitemap_address_for_a_page_its_worker_did_not_render_stops_the_merge(self) -> None:
        # `index.html` is the suffix of more than one address; only the exact
        # address under the site's URL is the home page.
        (self.w0 / "sitemap.xml").write_text(SITEMAP.format(urls=(
            sitemap_url("adr/0001-a.html", "2026-11-04T10:00:00.000Z")
            + sitemap_url("index.html", "2026-11-04T09:00:00.000Z")
        )))

        with self.assertRaises(render.RenderFailed) as caught:
            render.merge(self.parts, self.pages, self.output, site_url=SITE_URL)

        self.assertIn(f"{SITE_URL}index.html", str(caught.exception))

    def test_a_sitemap_laid_out_unlike_quartos_stops_the_merge(self) -> None:
        # Read with a pattern, a sitemap Quarto started writing differently would
        # merge to no pages at all, so a layout it cannot read is an error.
        (self.w0 / "sitemap.xml").write_text(
            '<urlset><url><loc>https://example.org/site/adr/0001-a.html</loc></url></urlset>'
        )

        with self.assertRaises(render.RenderFailed) as caught:
            render.merge(self.parts, self.pages, self.output, site_url=SITE_URL)

        self.assertIn("sitemap.xml", str(caught.exception))

    def test_a_page_written_by_two_workers_stops_the_merge(self) -> None:
        parts = [render.Part(site=self.w0, pages=["index.qmd"]), render.Part(site=self.w1, pages=["index.qmd"])]

        with self.assertRaises(render.RenderFailed) as caught:
            render.merge(parts, ["index.qmd"], self.output)

        self.assertIn("index.qmd", str(caught.exception))

    def _worker(self, name: str, files: dict[str, str]) -> Path:
        site = self.root / name / "_site"
        for relative, text in files.items():
            path = site / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return site


class PageOutputTest(unittest.TestCase):
    def test_a_page_renders_to_html_beside_where_it_sits(self) -> None:
        self.assertEqual(render.page_output("index.qmd"), "index.html")
        self.assertEqual(render.page_output("opengwasdb/adr/0001-x.md"), "opengwasdb/adr/0001-x.html")
        self.assertEqual(render.page_output("reports/r.ipynb"), "reports/r.html")


@unittest.skipUnless(shutil.which("quarto"), "needs Quarto: run in the dev environment")
class ParallelMatchesQuartoTest(unittest.TestCase):
    """A small real project rendered by Quarto alone and in parallel gives the same site.

    The project has what the merge depends on: a sidebar on every page, a home
    page, a resource, and a page a pre-render hook writes, which exists only once
    Quarto has run the hook, so a plan made before that would miss it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-render-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        files = {
            "_quarto.yml": (
                "project:\n"
                "  type: website\n"
                "  pre-render: ./pre-render.sh\n"
                "  resources: [\"data.json\"]\n"
                "website:\n"
                "  title: Test\n"
                "  site-url: \"https://example.org/site/\"\n"
                "  sidebar:\n"
                "    contents:\n"
                "      - index.qmd\n"
                "      - section: Notes\n"
                "        contents: \"notes/*.md\"\n"
                "      - generated.qmd\n"
            ),
            "pre-render.sh": "#!/bin/sh\nprintf -- '---\\ntitle: Generated\\n---\\n\\nWritten by the hook.\\n' > generated.qmd\n",
            "index.qmd": "---\ntitle: Home\n---\n\nThe home page links to [a note](notes/a.md).\n",
            "notes/a.md": "# Note A\n\n## Detail\n\nFirst note, with an em dash — here.\n",
            "notes/b.md": "# Note B\n\nSecond note.\n",
            "data.json": "{\"served\": true}\n",
        }
        for relative, text in files.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.project / "pre-render.sh").chmod(0o755)

    def test_the_parallel_site_is_the_site_quarto_renders_alone(self) -> None:
        alone = self.root / "alone"
        parallel = self.root / "parallel"
        sequential_project = self.root / "sequential-project"
        shutil.copytree(self.project, sequential_project, symlinks=True)
        subprocess.run(
            ["quarto", "render", str(sequential_project), "--output-dir", str(alone)],
            check=True, capture_output=True,
        )
        self.assertTrue((alone / "generated.html").is_file(), "the hook's page is part of the site")

        progress = io.StringIO()
        rendered = render.render_site(self.project, parallel, env=dict(os.environ), jobs=2, out=progress)

        self.assertEqual(rendered, 4)
        self.assertIn("[   4/4]", progress.getvalue())
        self.assertFalse((self.project / "_site").exists(), "the staged project is left as it was")
        alone_files = self._files(alone)
        self.assertEqual(self._files(parallel), alone_files)
        different = [
            relative for relative in alone_files - {"search.json", "sitemap.xml"}
            if not filecmp.cmp(alone / relative, parallel / relative, shallow=False)
        ]
        self.assertEqual(different, [])
        self.assertEqual(self._search(parallel), self._search(alone))
        self.assertEqual(self._locs(parallel), self._locs(alone))

    @staticmethod
    def _files(site: Path) -> set[str]:
        return {path.relative_to(site).as_posix() for path in site.rglob("*") if path.is_file()}

    @staticmethod
    def _search(site: Path) -> list[str]:
        return sorted(json.dumps(entry, sort_keys=True) for entry in json.loads((site / "search.json").read_text()))

    @staticmethod
    def _locs(site: Path) -> list[str]:
        return sorted(re.findall(r"<loc>(.*?)</loc>", (site / "sitemap.xml").read_text()))


if __name__ == "__main__":
    unittest.main()
