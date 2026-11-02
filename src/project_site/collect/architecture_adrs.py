"""Place each ADR on the architecture (#418).

An ADR belongs to a component for one of two reasons, and no other:

- **code**: it cites code the component claims, by a file path, a dotted module
  name (`opengwasdb.store.paths`, `project_site.pages`) or a GitHub link to a file.
  A bare suffix such as `store/paths.py` counts only when exactly one tracked file
  ends with it; an ambiguous one is skipped, never guessed;
- **title**: a component's `name` appears in the ADR's title, case-insensitive,
  at the start of a word and with no boundary at its end ("Analysis Catalog" places
  "Analysis Catalogue ..."; "Query" does not match inside "subquery").

A component's name that appears only in the body, however often, places
nothing: counting words rewarded long ADRs and wrongly placed ones that merely
discuss a neighbour. An ADR placed nowhere is still listed, with no components,
so the page can say which decisions the map cannot reach.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from project_site.collect import architecture_model as model_rules

ADR_PATH = re.compile(r"docs/adr/(\d{4})-.*\.md$")
PATH_CITE = re.compile(r"(?<![\w./:-])((?:[\w.-]+/)+[\w.-]+\.(?:py|smk|sh|toml))\b")
URL_CITE = re.compile(
    r"github\.com/opengwas/(opengwasdb(?:-stores)?)/(?:blob|tree)/[^/\s]+/([^\s#)?\]>`'\"]+)"
)
REPO_OF_URL = {"opengwasdb": "engine", "opengwasdb-stores": "registry"}
GITHUB = {"engine": "opengwas/opengwasdb", "registry": "opengwas/opengwasdb-stores"}


def place(
    trees: Mapping[str, Any],
    claims: Mapping[str, Sequence[tuple[str, str]]],
    components: Sequence[Mapping[str, Any]],
    modules: Mapping[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Every ADR of both repositories, with `components` as `{component id: "code" | "title"}`."""
    packages = sorted({module.split(".")[0] for module in modules})
    dotted = re.compile(r"\b((?:" + "|".join(map(re.escape, packages)) + r")(?:\.[A-Za-z_0-9]+)+)") if packages else None
    names = {
        c["id"]: re.compile(r"(?<!\w)" + re.escape(c["name"]), re.IGNORECASE) for c in components
    }
    adrs: list[dict[str, Any]] = []
    for repo in ("registry", "engine"):
        tree = trees[repo]
        paths = sorted(f for f in tree.files if ADR_PATH.search(f))
        texts = tree.read(paths)
        for path in paths:
            text = texts.get(path, "")
            title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path)
            placed = {
                cid: "code"
                for found_repo, found in _cited_files(text, repo, trees, modules, dotted)
                if (cid := model_rules.owner(claims[found_repo], found))
            }
            for cid, pattern in names.items():
                if cid not in placed and pattern.search(title):
                    placed[cid] = "title"
            adrs.append(
                {
                    "repo": repo,
                    "number": ADR_PATH.search(path).group(1),  # type: ignore[union-attr]
                    "path": path,
                    "title": title,
                    "status": _status(text),
                    "url": f"https://github.com/{GITHUB[repo]}/blob/main/{path}",
                    "components": dict(sorted(placed.items())),
                }
            )
    return adrs


def _status(text: str) -> str:
    head = "\n".join(text.splitlines()[:25])
    if re.search(r"[Ss]uperseded by", head):
        return "superseded"
    if re.search(r"[Aa]mended (?:by|in)|[Aa]mendment", head):
        return "amended"
    return "current"


def _cited_files(
    text: str, home: str, trees: Mapping[str, Any], modules: Mapping[str, tuple[str, str]], dotted: re.Pattern | None
) -> set[tuple[str, str]]:
    """The `(repository, file)` pairs a text cites, by dotted module, GitHub link or path."""
    found: set[tuple[str, str]] = set()
    for name in dotted.findall(text) if dotted else []:
        parts = name.split(".")
        while parts and ".".join(parts) not in modules:
            parts.pop()
        if parts:
            found.add(modules[".".join(parts)])
    for slug, path in URL_CITE.findall(text):
        repo = REPO_OF_URL[slug]
        if path in set(trees[repo].files):
            found.add((repo, path))
    order = [home, *[r for r in trees if r != home]]
    for raw in PATH_CITE.findall(text):
        for repo in order:
            if raw in set(trees[repo].files):
                found.add((repo, raw))
                break
        else:
            hits = [(repo, f) for repo in order for f in trees[repo].files if f.endswith("/" + raw)]
            if len(hits) == 1:
                found.add(hits[0])
    return found
