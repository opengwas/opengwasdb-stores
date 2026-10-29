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
from collections.abc import Mapping
from dataclasses import dataclass

# A target with its own scheme (`https:`, `mailto:`), a protocol-relative or
# root-absolute path, or nothing but a fragment is not a relative path into the
# tree, so it is never rewritten.
_NOT_RELATIVE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//|/|#|$)")

# `[text](target)` and `![alt](target "title")`. The destination pattern stops
# at a parenthesis, so `../src/a(b).py` is not read as a link at all; those are
# reported as unreadable rather than silently left alone.
_MARKDOWN_LINK = re.compile(r"(\]\()([^()\s]+)((?:[ \t]+\"[^\"]*\")?\))")
_READABLE_TARGET = re.compile(r"^([^()\s]+)(?:[ \t]+\"[^\"]*\")?$")
_MAYBE_LINK = re.compile(r"\]\(")
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


def rewrite_target(
    target: str,
    *,
    page: str,
    repo_url: str,
    commit: str,
    rendered: Mapping[str, str] | None = None,
) -> str:
    """The GitHub blob URL for a target outside the docs tree, or `target` unchanged.

    `page` is the page's own path relative to the docs root, so a target is
    resolved the way a browser would resolve it. A target that resolves inside
    the tree stays relative (Quarto rewrites `x.md` to `x.html`); one that leaves
    the tree is replaced by GitHub at `commit`, keeping any fragment or query.
    `rendered` maps a staged page that changed name (a `.md` staged as `.qmd` for
    a Mermaid cell) to the path it is published at, so a link to the source
    reaches the page.
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
    if rendered and resolved in rendered:
        return f"{posixpath.relpath(rendered[resolved], posixpath.dirname(page) or '.')}{suffix}"
    return target


def _unreadable_targets(line: str) -> list[str]:
    """Link destinations on the line that the link pattern cannot read.

    A destination may hold its own parentheses, as `../src/a(b).py` does. The
    pattern stops at the first one, so such a link is left alone; it is reported
    here instead of disappearing, because a silently unchanged link is how a
    reader ends up on a 404 nobody knows about.
    """
    found: list[str] = []
    for start in (match.end() for match in _MAYBE_LINK.finditer(line)):
        depth, index = 1, start
        while index < len(line) and depth:
            if line[index] == "(":
                depth += 1
            elif line[index] == ")":
                depth -= 1
            index += 1
        if depth:
            found.append(line[start:].rstrip("\n"))
        else:
            found.append(line[start:index - 1])
    return [target for target in found if _READABLE_TARGET.match(target) is None]


def _rewrite_page_line(
    line: str,
    *,
    page: str,
    repo_url: str,
    commit: str,
    targets: list[Target],
    rendered: Mapping[str, str] | None,
    unparsed: list[str],
) -> str:
    def replace(match: re.Match[str]) -> str:
        target = match.group(2)
        url = rewrite_target(target, page=page, repo_url=repo_url, commit=commit, rendered=rendered)
        if url == target:
            return match.group(0)
        targets.append(Target(target=target, url=url, repo_path=_repo_path(url, repo_url, commit)))
        return f"{match.group(1)}{url}{match.group(3)}"

    before = line
    line = _MARKDOWN_LINK.sub(replace, line)
    line = _HTML_ATTRIBUTE.sub(replace, line)
    if "](" in before:
        unparsed.extend(_unreadable_targets(before))
    return line


def _repo_path(url: str, repo_url: str, commit: str) -> str | None:
    prefix = f"{repo_url}/blob/{commit}/"
    if not url.startswith(prefix):
        return None
    path, _ = _split_suffix(url[len(prefix):])
    return path


def rewrite_document(
    text: str,
    *,
    page: str,
    repo_url: str,
    commit: str,
    rendered: Mapping[str, str] | None = None,
) -> tuple[str, list[Target], list[str]]:
    """Rewrite one page's outbound links, leaving fenced code blocks alone.

    Returns the new text, every target that was rewritten, and every destination
    the link pattern could not read.
    """
    targets: list[Target] = []
    unparsed: list[str] = []
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
        out.append(
            _rewrite_page_line(
                line,
                page=page,
                repo_url=repo_url,
                commit=commit,
                targets=targets,
                rendered=rendered,
                unparsed=unparsed,
            )
        )
    return "".join(out), targets, unparsed


def convert_mermaid_fences(text: str) -> str:
    """Spell a plain ```` ```mermaid ```` fence as the ```` ```{mermaid} ```` cell Quarto renders."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _MERMAID_FENCE.match(line.rstrip("\n"))
        out.append(f"{match.group(1)}```{{mermaid}}\n" if match else line)
    return "".join(out)
