"""Point the links that leave a docs tree at GitHub, and make Mermaid fences render (#388).

The site is rooted at `docs/`, so a page's relative links to its siblings keep
working. A page that links to `../src/`, `../../CONTRIBUTING.md` or
`../resources/` names a path the site never serves: on the published site that
link would 404. Each of those becomes the GitHub blob URL of the commit being
rendered (`https://github.com/<owner>/<repo>/blob/<commit>/<path>`), for the
registry or for the engine, whichever tree the page came from.

These functions run over a staged copy of the documentation, never over a source
file, which is what keeps `docs/` a set of files a human can still read on GitHub.

Mermaid: GitHub renders a plain ```` ```mermaid ```` fence. Quarto only renders a
`{mermaid}` cell, so the same pre-render pass spells the fence the way Quarto
reads it; both tools then show the diagram.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

# A target with its own scheme (`https:`, `mailto:`), a protocol-relative or
# root-absolute path, or nothing but a fragment is not a relative path into the
# tree, so it is never rewritten.
_NOT_RELATIVE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//|/|#|$)")

# `[text](target)` and `![alt](target "title")`, without nested parentheses,
# which no page in either tree uses in a link destination.
_MARKDOWN_LINK = re.compile(r"(\]\()([^()\s]+)((?:[ \t]+\"[^\"]*\")?\))")
_HTML_ATTRIBUTE = re.compile(r"(\b(?:href|src)=\")([^\"]*)(\")")
_MERMAID_FENCE = re.compile(r"^([ \t]*)```[ \t]*mermaid[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")


@dataclass(frozen=True)
class Target:
    """One link the rewrite looked at.

    `url` is what replaces `target`; `repo_path` is the repository-relative path
    the rewritten link points at, or None when the target stayed relative.
    """

    target: str
    url: str
    repo_path: str | None


def _split_suffix(target: str) -> tuple[str, str]:
    """Split a link destination into its path and any `#fragment` or `?query`."""
    cut = len(target)
    for mark in ("#", "?"):
        at = target.find(mark)
        if at != -1:
            cut = min(cut, at)
    return target[:cut], target[cut:]


def rewrite_target(target: str, *, page: str, repo_url: str, commit: str) -> str:
    """The GitHub blob URL for a target outside the docs tree, or `target` unchanged.

    `page` is the page's own path relative to the docs root, so a target is
    resolved the way a browser would resolve it. A target that resolves inside
    the tree stays relative (Quarto rewrites `x.md` to `x.html`); one that leaves
    the tree is replaced by GitHub at `commit`, keeping any fragment or query.
    """
    if _NOT_RELATIVE.match(target):
        return target
    path, suffix = _split_suffix(target)
    if not path:
        return target
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(page), path))
    if resolved == ".." or resolved.startswith("../"):
        # The docs tree is `docs/`, so one level up is the repository root.
        repo_path = posixpath.normpath(posixpath.join("docs", resolved))
        if repo_path == ".." or repo_path.startswith("../"):
            # It leaves the repository too; GitHub cannot serve it either.
            return target
        return f"{repo_url}/blob/{commit}/{repo_path}{suffix}"
    return target


def _rewrite_page_line(
    line: str, *, page: str, repo_url: str, commit: str, targets: list[Target]
) -> str:
    def replace(match: re.Match[str]) -> str:
        target = match.group(2)
        url = rewrite_target(target, page=page, repo_url=repo_url, commit=commit)
        if url == target:
            return match.group(0)
        targets.append(Target(target=target, url=url, repo_path=_repo_path(url, repo_url, commit)))
        return f"{match.group(1)}{url}{match.group(3)}"

    line = _MARKDOWN_LINK.sub(replace, line)
    return _HTML_ATTRIBUTE.sub(replace, line)


def _repo_path(url: str, repo_url: str, commit: str) -> str | None:
    prefix = f"{repo_url}/blob/{commit}/"
    if not url.startswith(prefix):
        return None
    path, _ = _split_suffix(url[len(prefix):])
    return path


def rewrite_document(
    text: str, *, page: str, repo_url: str, commit: str
) -> tuple[str, list[Target]]:
    """Rewrite one page's outbound links, leaving fenced code blocks alone.

    Returns the new text and every target that was rewritten.
    """
    targets: list[Target] = []
    out: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        out.append(_rewrite_page_line(line, page=page, repo_url=repo_url, commit=commit, targets=targets))
    return "".join(out), targets


def convert_mermaid_fences(text: str) -> str:
    """Spell a plain ```` ```mermaid ```` fence as the ```` ```{mermaid} ```` cell Quarto renders."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _MERMAID_FENCE.match(line.rstrip("\n"))
        out.append(f"{match.group(1)}```{{mermaid}}\n" if match else line)
    return "".join(out)
