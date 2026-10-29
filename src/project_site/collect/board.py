"""The board collector: OpenGWASDB project 1, its epics, sub-issues and milestones (#389).

Its own GraphQL, not the board skill's: this repository does not import the
skill. `fetch` talks to GitHub and `parse` turns the recorded responses into the
file the site reads, so `parse` is tested from a fixture recorded by `fetch`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from project_site.collect.common import CollectError, GhRunner

OWNER = "opengwas"
PROJECT_NUMBER = 1
FIELDS = ("Status", "Workstream", "Next", "Priority")

ISSUE_FIELDS = """
  number title url state closedAt body
  repository { nameWithOwner }
  issueType { name }
  labels(first: 20) { nodes { name } }
  parent { number repository { nameWithOwner } }
  subIssuesSummary { total completed }
"""

PROJECT_QUERY = """
query($owner: String!, $number: Int!, $after: String) {
  organization(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content { __typename ... on Issue { %s } ... on PullRequest { number title url state repository { nameWithOwner } } }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
            }
          }
        }
      }
    }
  }
}
""" % ISSUE_FIELDS

CHILD_FIELDS = """
  number title url state
  repository { nameWithOwner }
  blockedBy(first: 50) { pageInfo { hasNextPage endCursor } nodes { number state repository { nameWithOwner } } }
"""

# The same two relationships, one page at a time, for what the first look cut off.
SUB_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) { issue(number: $number) {
    subIssues(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { %s } }
  } }
}
""" % CHILD_FIELDS

BLOCKED_BY_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) { issue(number: $number) {
    blockedBy(first: 50, after: $after) { pageInfo { hasNextPage endCursor } nodes { number state repository { nameWithOwner } } }
  } }
}
"""


def issue_key(repository: dict[str, Any], number: int) -> str:
    """`repo#n`, the key the epic record uses; every repository is under the one owner."""
    return f"{repository['nameWithOwner'].split('/')[-1]}#{number}"


def is_epic(issue: dict[str, Any]) -> bool:
    """An Epic by issue type, by `epic` label, or by an `Epic:` title, as the board skill reads it."""
    if is_milestone(issue):
        return False
    labels = {label["name"] for label in (issue.get("labels") or {}).get("nodes", [])}
    return (
        (issue.get("issueType") or {}).get("name") == "Epic"
        or bool(labels & {"complexity:epic", "epic"})
        or issue["title"].lower().startswith("epic:")
    )


def is_milestone(issue: dict[str, Any]) -> bool:
    return (issue.get("issueType") or {}).get("name") == "Milestone" or issue["title"].lower().startswith("milestone:")


def children_query(epics: list[dict[str, Any]]) -> str:
    """One aliased query for every epic's sub-issues and their blocked-by edges."""
    blocks = []
    for i, epic in enumerate(epics):
        owner, name = epic["repository"]["nameWithOwner"].split("/")
        blocks.append(
            f'e{i}: repository(owner: "{owner}", name: "{name}") '
            f'{{ issue(number: {epic["number"]}) {{ number subIssues(first: 100) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {CHILD_FIELDS} }} }} }} }}'
        )
    return "query {\n" + "\n".join(blocks) + "\n}"


def _graphql(gh: GhRunner, query: str, **variables: Any) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables})
    response = json.loads(gh(["api", "graphql", "--input", "-"], body))
    if response.get("errors"):
        raise RuntimeError(f"GraphQL errors: {response['errors']}")
    return response


def fetch(gh: GhRunner) -> dict[str, Any]:
    """The raw responses `parse` reads: every page of project 1, then the epics' children."""
    pages: list[dict[str, Any]] = []
    after = None
    while True:
        page = _graphql(gh, PROJECT_QUERY, owner=OWNER, number=PROJECT_NUMBER, after=after)
        pages.append(page)
        info = page["data"]["organization"]["projectV2"]["items"]["pageInfo"]
        if not info["hasNextPage"]:
            break
        after = info["endCursor"]
    for page in pages:  # a Milestone's checklist and an Epic's purpose are read from the body
        for node in page["data"]["organization"]["projectV2"]["items"]["nodes"]:
            content = node["content"]
            if content.get("__typename") == "Issue" and not (is_milestone(content) or is_epic(content)):
                content.pop("body", None)
    epics = [
        node["content"]
        for page in pages
        for node in page["data"]["organization"]["projectV2"]["items"]["nodes"]
        if node["content"].get("__typename") == "Issue" and is_epic(node["content"])
    ]
    children = _graphql(gh, children_query(epics))["data"] if epics else {}
    for i, epic in enumerate(epics):
        _finish_pages(gh, epic["repository"], epic["number"], children[f"e{i}"]["issue"]["subIssues"])
    return {"project": pages, "epics": [issue_key(e["repository"], e["number"]) for e in epics], "children": children}


def _finish_pages(gh: GhRunner, repository: dict[str, Any], number: int, sub_issues: dict[str, Any]) -> None:
    """Read every further page of an epic's sub-issues, then of each sub-issue's blockers, in place."""
    owner, name = repository["nameWithOwner"].split("/")
    while sub_issues["pageInfo"]["hasNextPage"]:
        more = _graphql(gh, SUB_ISSUES_QUERY, owner=owner, name=name, number=number,
                        after=sub_issues["pageInfo"]["endCursor"])["data"]["repository"]["issue"]["subIssues"]
        sub_issues["nodes"].extend(more["nodes"])
        sub_issues["pageInfo"] = more["pageInfo"]
    for sub in sub_issues["nodes"]:
        edges = sub["blockedBy"]
        sub_owner, sub_name = sub["repository"]["nameWithOwner"].split("/")
        while edges["pageInfo"]["hasNextPage"]:
            more = _graphql(gh, BLOCKED_BY_QUERY, owner=sub_owner, name=sub_name, number=sub["number"],
                            after=edges["pageInfo"]["endCursor"])["data"]["repository"]["issue"]["blockedBy"]
            edges["nodes"].extend(more["nodes"])
            edges["pageInfo"] = more["pageInfo"]


BOX = re.compile(r"\s*- \[( |x|X)\] (.*)")
REF = re.compile(r"(?:\b([\w.-]+)/([\w.-]+))?#(\d+)\b")


def checklist(body: str | None, repo: str) -> list[dict[str, Any]]:
    """The `- [ ]` and `- [x]` lines of an issue body: checked state, text, and refs.

    Only those lines count; a bullet without a box is prose. A bare `#n` is an
    issue of `repo`, the issue that holds the list; a ref into another owner is
    dropped, because the board and the site are about this one's issues.
    """
    lines = []
    for line in (body or "").splitlines():
        match = BOX.fullmatch(line)
        if not match:
            continue
        refs: list[str] = []
        for owner, name, number in REF.findall(match.group(2)):
            if owner and owner != OWNER:
                continue
            ref = f"{name or repo}#{number}"
            if ref not in refs:
                refs.append(ref)
        lines.append({"checked": match.group(1) != " ", "text": match.group(2), "refs": refs})
    return lines


# How much of an Epic's first paragraph the board file keeps: enough for the
# site's epic pages to say what the epic is for, and no more of the body.
PURPOSE_LIMIT = 600

# A paragraph that is not prose: a heading, a fence, a table, a quote, an HTML
# comment, or a list of bullets and checkboxes.
_NOT_PROSE = re.compile(r"^(#{1,6}\s|```|~~~|\||>|<!--|[-*+]\s|\d+[.)]\s)")


def purpose_of(body: str | None) -> str | None:
    """An Epic's purpose: the first non-empty paragraph that is prose, cut at a sentence.

    The body is otherwise dropped, so what a page shows is the epic's own words
    about what it is for, not a summary this repository invented.
    """
    for paragraph in (body or "").split("\n\n"):
        lines = [line for line in paragraph.splitlines() if line.strip()]
        if not lines or _NOT_PROSE.match(lines[0].strip()):
            continue
        text = " ".join(" ".join(lines).split())
        if text:
            return _shorten(text)
    return None


def _shorten(text: str) -> str:
    """`text` at most `PURPOSE_LIMIT` characters, cut at a sentence, else at a word."""
    if len(text) <= PURPOSE_LIMIT:
        return text
    window = text[:PURPOSE_LIMIT]
    sentence = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if sentence > 0:
        return window[: sentence + 1]
    word = window.rfind(" ")
    return (window[:word] if word > 0 else window) + "…"


def _fields(node: dict[str, Any]) -> dict[str, str | None]:
    values = {
        v["field"]["name"]: v.get("name", v.get("text"))
        for v in node["fieldValues"]["nodes"]
        if v and "field" in v
    }
    return {name.lower(): values.get(name) for name in FIELDS}


def _item(content: dict[str, Any], fields: dict[str, str | None]) -> dict[str, Any]:
    summary = content.get("subIssuesSummary") or {}
    return {
        "key": issue_key(content["repository"], content["number"]),
        "title": content["title"],
        "url": content["url"],
        "state": content["state"],
        **fields,
        "sub_issues": {"total": summary.get("total", 0), "completed": summary.get("completed", 0)},
    }


def parse(raw: dict[str, Any]) -> dict[str, Any]:
    """The board file's payload from the responses `fetch` recorded."""
    epic_keys = raw["epics"]
    for i, key in enumerate(epic_keys):
        subs = raw["children"][f"e{i}"]["issue"]["subIssues"]
        if subs["pageInfo"]["hasNextPage"]:
            raise CollectError(f"{key}: sub-issues have more pages than were read")
        for c in subs["nodes"]:
            if c["blockedBy"]["pageInfo"]["hasNextPage"]:
                raise CollectError(f"{key}: blocked-by edges of {issue_key(c['repository'], c['number'])} have more pages than were read")
    children = {
        key: [
            {
                "key": issue_key(c["repository"], c["number"]),
                "title": c["title"],
                "url": c["url"],
                "state": c["state"],
                "blocked_by": [
                    {"key": issue_key(b["repository"], b["number"]), "state": b["state"]}
                    for b in c["blockedBy"]["nodes"]
                ],
            }
            for c in raw["children"][f"e{i}"]["issue"]["subIssues"]["nodes"]
        ]
        for i, key in enumerate(epic_keys)
    }
    epics, milestones, items = [], [], []
    for page in raw["project"]:
        for node in page["data"]["organization"]["projectV2"]["items"]["nodes"]:
            content = node["content"]
            if content.get("__typename") != "Issue":
                continue
            item = _item(content, _fields(node))
            if is_milestone(content):
                repo = content["repository"]["nameWithOwner"].split("/")[-1]
                milestones.append({**item, "lines": checklist(content.get("body"), repo)})
            elif is_epic(content):
                epics.append({**item, "purpose": purpose_of(content.get("body")), "children": children[item["key"]]})
            else:
                items.append(item)
    return {"project": f"{OWNER}/{PROJECT_NUMBER}", "epics": epics, "milestones": milestones, "items": items}


def collect(gh: GhRunner) -> dict[str, Any]:
    return parse(fetch(gh))
