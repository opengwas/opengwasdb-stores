#!/usr/bin/env python3
"""Links that leave a docs tree point at GitHub, not at a path the site cannot serve (#388).

A docs page links to its siblings relatively (`adr/0001-x.md`), and those stay
relative so the site resolves them. A page that links to `../src/`,
`../../CONTRIBUTING.md` or `../resources/` names a path outside `docs/`, which
the site never serves; on the published site such a link would 404. The rewrite
turns each of them into the GitHub blob URL of the commit being rendered, so the
link keeps working. It never edits a source file: it runs over the staged copy.

Run from the repository root:
    python3 tests/project-site/test_links.py
"""

from __future__ import annotations

import unittest

from project_site.links import convert_mermaid_fences, rewrite_document, rewrite_target

# Literal expectations: one commit, one repository URL, both written out here
# rather than recomputed from the module under test.
COMMIT = "0123456789abcdef0123456789abcdef01234567"
STORES = "https://github.com/opengwas/opengwasdb-stores"
ENGINE = "https://github.com/opengwas/opengwasdb"


class RewriteTargetTest(unittest.TestCase):
    def test_a_path_outside_docs_becomes_a_github_blob_url(self) -> None:
        self.assertEqual(
            rewrite_target("../src/ogstores/bundle.py", page="notes.md", repo_url=STORES, commit=COMMIT),
            f"{STORES}/blob/{COMMIT}/src/ogstores/bundle.py",
        )

    def test_a_path_two_levels_outside_docs_becomes_a_github_blob_url(self) -> None:
        self.assertEqual(
            rewrite_target("../../CONTRIBUTING.md", page="adr/0001-x.md", repo_url=STORES, commit=COMMIT),
            f"{STORES}/blob/{COMMIT}/CONTRIBUTING.md",
        )

    def test_a_path_inside_docs_stays_relative(self) -> None:
        self.assertEqual(
            rewrite_target("adr/0001-x.md", page="notes.md", repo_url=STORES, commit=COMMIT),
            "adr/0001-x.md",
        )

    def test_a_fragment_on_a_docs_path_stays_relative(self) -> None:
        self.assertEqual(
            rewrite_target("adr/0001-x.md#decision", page="notes.md", repo_url=STORES, commit=COMMIT),
            "adr/0001-x.md#decision",
        )

    def test_a_fragment_on_an_outside_path_is_kept(self) -> None:
        self.assertEqual(
            rewrite_target("../src/ogstores/bundle.py#L10", page="notes.md", repo_url=STORES, commit=COMMIT),
            f"{STORES}/blob/{COMMIT}/src/ogstores/bundle.py#L10",
        )

    def test_an_absolute_url_is_untouched(self) -> None:
        for target in ("https://example.org/x", "mailto:a@b", "//cdn.example.org/x", "#anchor"):
            with self.subTest(target=target):
                self.assertEqual(rewrite_target(target, page="notes.md", repo_url=STORES, commit=COMMIT), target)

    def test_an_engine_page_uses_the_engine_repository(self) -> None:
        self.assertEqual(
            rewrite_target("../CONTEXT.md", page="getting-started.md", repo_url=ENGINE, commit=COMMIT),
            f"{ENGINE}/blob/{COMMIT}/CONTEXT.md",
        )

    def test_a_link_to_a_page_staged_as_qmd_goes_to_its_rendered_page(self) -> None:
        # `site.md` is served as `site.html` because Quarto needs a `.qmd` for a
        # Mermaid cell; a link to it must reach the rendered page, not the source.
        rendered = {"site.md": "site.html"}

        self.assertEqual(
            rewrite_target("site.md", page="notes.md", repo_url=STORES, commit=COMMIT, rendered=rendered),
            "site.html",
        )
        self.assertEqual(
            rewrite_target("../site.md#publishing", page="adr/0001-x.md", repo_url=STORES, commit=COMMIT, rendered=rendered),
            "../site.html#publishing",
        )
        self.assertEqual(
            rewrite_target("adr/0001-x.md", page="notes.md", repo_url=STORES, commit=COMMIT, rendered=rendered),
            "adr/0001-x.md",
        )


class RewriteDocumentTest(unittest.TestCase):
    def test_markdown_links_and_html_hrefs_are_rewritten_outside_code_fences(self) -> None:
        text = (
            "# Notes\n"
            "\n"
            "See [the bundle](../src/ogstores/bundle.py) and the ADR [here](adr/0001-x.md).\n"
            "An <a href=\"../CONTRIBUTING.md\">html link</a>.\n"
            "\n"
            "```bash\n"
            "cat ../src/ogstores/bundle.py\n"
            "```\n"
        )
        rewritten, targets, unparsed = rewrite_document(text, page="notes.md", repo_url=STORES, commit=COMMIT)

        self.assertEqual(unparsed, [])
        self.assertIn(f"[the bundle]({STORES}/blob/{COMMIT}/src/ogstores/bundle.py)", rewritten)
        self.assertIn("[here](adr/0001-x.md)", rewritten)
        self.assertIn(f'<a href="{STORES}/blob/{COMMIT}/CONTRIBUTING.md">', rewritten)
        self.assertIn("cat ../src/ogstores/bundle.py", rewritten)
        self.assertEqual(
            [target.repo_path for target in targets if target.repo_path],
            ["src/ogstores/bundle.py", "CONTRIBUTING.md"],
        )

    def test_a_target_the_link_pattern_cannot_read_is_reported(self) -> None:
        text = "A [odd path](../src/a(b).py) and a [plain one](../src/plain.py).\n"
        rewritten, targets, unparsed = rewrite_document(text, page="notes.md", repo_url=STORES, commit=COMMIT)

        self.assertEqual(unparsed, ["../src/a(b).py"])
        self.assertIn("[odd path](../src/a(b).py)", rewritten)
        self.assertIn(f"[plain one]({STORES}/blob/{COMMIT}/src/plain.py)", rewritten)
        self.assertEqual([target.target for target in targets], ["../src/plain.py"])

    def test_a_title_on_a_link_is_not_reported_as_unreadable(self) -> None:
        text = "A [titled](../src/x.py \"the module\").\n"
        rewritten, targets, unparsed = rewrite_document(text, page="notes.md", repo_url=STORES, commit=COMMIT)

        self.assertEqual(unparsed, [])
        self.assertIn(f"[titled]({STORES}/blob/{COMMIT}/src/x.py \"the module\")", rewritten)


class MermaidFenceTest(unittest.TestCase):
    def test_a_plain_mermaid_fence_becomes_a_quarto_mermaid_cell(self) -> None:
        self.assertEqual(
            convert_mermaid_fences("```mermaid\ngraph TD; A-->B;\n```\n"),
            "```{mermaid}\ngraph TD; A-->B;\n```\n",
        )

    def test_another_fence_is_untouched(self) -> None:
        text = "```bash\nquarto render\n```\n"
        self.assertEqual(convert_mermaid_fences(text), text)


if __name__ == "__main__":
    unittest.main()
