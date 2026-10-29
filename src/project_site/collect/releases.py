"""The Releases collector: both repositories' tags and GitHub Releases (#389).

Neither repository has a tag or a Release yet, until each merges to `main` and
tags every merge (#386); an empty list is the answer, not an error.
"""

from __future__ import annotations

import json
from typing import Any

from project_site.collect.common import GhRunner

OWNER = "opengwas"
REPOS = ("opengwasdb", "opengwasdb-stores")


def _rows(gh: GhRunner, repo: str, kind: str) -> list[dict[str, Any]]:
    out = gh(["api", f"repos/{OWNER}/{repo}/{kind}", "--paginate", "--jq", ".[]"], None)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def collect(gh: GhRunner) -> dict[str, Any]:
    repos = []
    for repo in REPOS:
        repos.append({
            "repo": repo,
            "tags": [{"name": t["name"], "commit": t["commit"]["sha"]} for t in _rows(gh, repo, "tags")],
            "releases": [
                {
                    "tag": r["tag_name"], "name": r["name"], "published_at": r["published_at"],
                    "draft": r["draft"], "prerelease": r["prerelease"], "url": r["html_url"],
                }
                for r in _rows(gh, repo, "releases")
            ],
        })
    return {"repos": repos}
