"""The architecture pages: Pipeline, Structure and health (#418) and the Work map (#419), epic #416.

Both are written from `architecture.json`, the architecture collector's file
(`docs/spec/project-site.md`). Like every page of `pages.py` they read
`available` first and show the site's usual notice when the source could not be
read, and every figure traces to one collector field.

The diagrams are Quarto `{dot}` cells, which Quarto draws at render time with
its own bundled Graphviz. A `{dot}` cell is a diagram, not code: it needs no
kernel and no new dependency, so `pages.py`'s promise of no executable code
cells still holds, and nothing generated is ever committed.

The Work map is written from `work.json`, the work collector's file, and is
the one page that is not about the code alone: it says where live work meets,
so it states when it was measured and that branches existing only on a build
host are not shown (the `conflicts` command, run there, shows them).

Each page states what is written by hand (the models: stages, layers, claims,
the context and containers) and what is measured (code size, imports, ADR
placement, gaps), so a reader knows which part can drift and which cannot.
"""

from __future__ import annotations

import html
import textwrap
from collections import defaultdict
from itertools import pairwise
from typing import Any, Mapping

from project_site import pages

Document = Mapping[str, Any]

UPWARD = "#c0392b"
GREY = "#7f8c8d"
HEAD = (
    'graph [fontname="Arial" bgcolor="transparent" pad="0.2" nodesep="0.35" ranksep="0.55"];\n'
    'node [fontname="Arial" shape=box style="rounded,filled" fillcolor="white" fontcolor="black" margin="0.15,0.08"];\n'
    f'edge [fontname="Arial" fontsize=9 arrowsize=0.6 color="{GREY}" fontcolor="{GREY}"];\n'
)

PIPELINE_INTRO = (
    "The path data takes through the system, from a source file to a query: the stages, the "
    "components that do each one in both repositories, and the architecture decisions (ADRs) "
    "that govern them."
)
STRUCTURE_INTRO = (
    "How the system fits together at three levels (C4): who and what it talks to, its "
    "containers, and the components inside the engine and this repository, with the imports "
    "between them."
)


def render_pipeline(documents: Mapping[str, Document | None]) -> str:
    """The landing page: each stage with its components and their ADRs."""
    lines = [pages.GENERATED, "", PIPELINE_INTRO, "", "See also [Structure and health](architecture-structure.qmd).", ""]
    data = pages.available(documents, "architecture")
    if data is None:
        return "\n".join(lines + pages.notices(documents, ("architecture",))).rstrip() + "\n"
    lines += _hand_and_measured(
        hand="the stages and their order, which stages each component serves, each component's name, "
        "summary and the code paths it claims",
        measured="each component's code size, the ADRs placed on it (by the code an ADR cites and by "
        "component names in its title), and the engine commit read",
    )
    lines += _engine_notice(data)
    lines += ["## The stages", ""]
    components = [c for c in data.get("components") or [] if isinstance(c, Mapping)]
    stages = list((data.get("model") or {}).get("stages") or [])
    ordered = [s for s in stages if s["id"] != "cross"] + [s for s in stages if s["id"] == "cross"]
    for number, stage in enumerate(ordered, 1):
        members = [c for c in components if stage["id"] in c.get("stages", [])]
        lines += [f"### {number}. {pages.escape(str(stage['name']))}", "", pages.escape(str(stage.get("summary") or "")), ""]
        if not members:
            lines += ["_No component serves this stage._", ""]
            continue
        for c in members:
            lines.append(f"- **{pages.escape(str(c['name']))}** ({c['repo']}; {_size(c)}): {pages.escape(str(c['summary']))}")
            lines += [f"  - {_adr_link(a, [c])}" for a in _adrs_for(data, {c["id"]})]
        lines.append("")
    lines += ["## ADRs the map cannot place", ""]
    unplaced = [a for a in data.get("adrs") or [] if not a["components"]]
    summary = data.get("summary") or {}
    lines.append(
        f"{pages._figure(summary.get('adrs_placed'))} of {pages._figure(summary.get('adrs_total'))} ADRs are placed on a "
        "component. These cite no code a component claims and name none in their title:"
    )
    lines.append("")
    lines += [f"- [{pages.escape(str(a['title']))}]({a['url']}) ({a['repo']})" for a in unplaced] or ["_None._"]
    lines += ["", _as_of(data), ""]
    return "\n".join(lines).rstrip() + "\n"


def render_structure(documents: Mapping[str, Document | None]) -> str:
    """The structure of both repositories, and the measured health of the claims and layering."""
    lines = [pages.GENERATED, "", STRUCTURE_INTRO, "", "See also the [Pipeline](architecture.qmd).", ""]
    data = pages.available(documents, "architecture")
    if data is None:
        return "\n".join(lines + pages.notices(documents, ("architecture",))).rstrip() + "\n"
    lines += _hand_and_measured(
        hand="the system context, the containers and how they relate, the engine's layers (the "
        "intended layering, a decision rather than a measurement) and the code each component claims",
        measured="the imports between components (counts, read from the code of both repositories), "
        "which of them go against the layering, the seam between the repositories, and the gaps in the claims",
    )
    lines += _engine_notice(data)
    model = data.get("model") or {}
    components = [c for c in data.get("components") or [] if isinstance(c, Mapping)]
    edges = [e for e in data.get("edges") or [] if isinstance(e, Mapping)]
    engine_model = bool((data.get("engine") or {}).get("model"))
    summary = data.get("summary") or {}

    lines += ["## System context", "", _cell(_context(model)), "## Containers", "", _cell(_containers(model))]
    if engine_model:
        lines += [
            "## The engine by layer",
            "",
            "Each row is a layer, the top layer first; a component may import from its own layer or "
            f"below. <span style=\"color:{UPWARD}\">Red</span> arrows are imports that go **against** "
            "that layering, labelled with their import count. Dashed arrows are imports within a "
            "layer. Imports that follow the layering are not drawn.",
            "",
            _cell(_engine(components, edges, model)),
        ]
    lines += [
        "## This repository, and its seam with the engine",
        "",
        "Numbers on the arrows into the engine are imports of engine modules.",
        "",
        _cell(_registry(components, edges, model, engine_model)),
    ]
    lines += _health(data, components, edges, summary)
    lines += [_as_of(data), ""]
    return "\n".join(lines).rstrip() + "\n"


WORK_INTRO = (
    "Which work is changing which part of the system right now, and where two owners' work meets on "
    "one component. An owner is an epic, or a branch or ticket that is on no epic of the board."
)
STATE_ORDER = ("building", "active", "waiting", "scope", "planned", "paused", "stale")
STATE_MEANING = (
    ("building", "a branch's diff against the merge-base with `main`"),
    ("active", "a file cited by an open ticket of an Active epic"),
    ("waiting", "a file cited by an open ticket that waits for another open ticket"),
    ("scope", "a file cited by an epic's body"),
    ("planned", "a file cited by an open ticket of a Todo epic"),
    ("paused", "a file cited by an open ticket of a Paused epic"),
    ("stale", "a branch whose epic or ticket is closed"),
)


def render_work(documents: Mapping[str, Document | None]) -> str:
    """The Work map: tiles, where work meets, the components by owner, and the branches."""
    lines = [pages.GENERATED, "", WORK_INTRO, "", "See also the [Pipeline](architecture.qmd).", ""]
    data = pages.available(documents, "work")
    if data is None:
        return "\n".join(lines + pages.notices(documents, ("work",))).rstrip() + "\n"
    lines += _hand_and_measured(
        hand="which code each component owns (the claims in each repository's `docs/architecture/components.yaml`), "
        "the epics and tickets on the board and the blocking edges between them, and the files a ticket's text cites",
        where="The claims are in the repositories, the rest on the project board.",
        measured="the files each live branch changes, which component each file and each cited file falls on, "
        "the state of each piece of work there, and the meets between owners",
    )
    lines += _measured_at(data)
    lines += _fetch_failures(data)
    summary = data.get("summary") or {}
    lines += [
        "| Owners | Meets | Coordinated | Uncoordinated | Branches |",
        "|---:|---:|---:|---:|---:|",
        "| " + " | ".join(
            pages._figure(summary.get(k)) for k in ("owners", "meets", "coordinated", "uncoordinated", "branches")
        ) + " |",
        "",
    ]
    owners = {o["key"]: o for o in data.get("owners") or [] if isinstance(o, Mapping)}
    names = {c["id"]: c["name"] for c in data.get("components") or [] if isinstance(c, Mapping)}
    lines += ["## Where work meets", ""]
    lines += _meets(data, owners, names)
    lines += ["## Components by owner", ""]
    lines += _matrix(data, owners, names)
    lines += ["## Branches", ""]
    lines += _branches(data, owners, names)
    lines += ["## What the states mean", ""]
    lines += [f"- **{state}**: {meaning}" for state, meaning in STATE_MEANING]
    lines += [
        "",
        "A meet is two or more owners with `building` or `active` work on one component. It is "
        "**coordinated** only when a blocking edge links two specific tickets of the owners, in either "
        "direction; sharing an epic, or an edge between other tickets of the same epics, does not count.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _measured_at(data: Document) -> list[str]:
    inputs = data.get("inputs") or {}
    architecture = inputs.get("architecture") or {}
    engine = architecture.get("engine") or {}
    days = next((s.get("days") for s in (data.get("branches") or {}).get("sources") or [] if s.get("days")), None)
    return [
        f"Measured {pages._cell(data.get('collected_at'))}, from the board (collected {pages._cell((inputs.get('board') or {}).get('collected_at'))}) "
        f"and the claims at engine {pages._code(engine.get('ref'))} ({pages._code(str(engine.get('commit'))[:12])}). "
        f"Branches are pushed ones ahead of `main` whose newest commit is within {pages._figure(days)} days; branches that "
        "exist only on a build host are not shown. On that host, `pixi run architecture conflicts --branch NAME --local GIT_DIR` includes them.",
        "",
    ]


def _fetch_failures(data: Document) -> list[str]:
    lines = []
    for source in (data.get("branches") or {}).get("sources") or []:
        fetch = source.get("fetch")
        if isinstance(fetch, Mapping) and fetch.get("ok") is False:
            lines += [
                f"> **The branch fetch for {pages.escape(str(source.get('repo')))} failed**: {pages._cell(fetch.get('error'))}. "
                "The branches shown are the ones already there, so they may be out of date.",
                "",
            ]
    return lines


def _owner(key: str, owners: Mapping[str, Mapping[str, Any]]) -> str:
    found = owners.get(key) or {}
    link = pages._link(key, found.get("url"))
    title = found.get("title")
    return f"{link} {pages.escape(str(title))}" if title and title != key else link


def _meets(data: Document, owners: Mapping[str, Any], names: Mapping[str, str]) -> list[str]:
    meets = [m for m in data.get("meets") or [] if isinstance(m, Mapping)]
    if not meets:
        return ["No two owners have building or active work on the same component.", ""]
    signals = [s for s in data.get("signals") or [] if isinstance(s, Mapping)]
    lines: list[str] = []
    for meet in sorted(meets, key=lambda m: (m.get("coordinated") is True, str(m.get("component")))):
        cid = meet["component"]
        lines += [f"### {pages.escape(str(names.get(cid, cid)))}", ""]
        for owner in meet["owners"]:
            lines.append(f"- {_owner(owner, owners)}")
            for item in meet["items"].get(owner, []):
                found = [s for s in signals if s["component"] == cid and s["owner"] == owner and s["item"] == item
                         and s["state"] in ("building", "active")]
                detail = "; ".join(f"{s['state']}: " + ", ".join(pages._code(w) for w in s["what"]) for s in found)
                lines.append(f"  - {pages.escape(item)} ({detail})")
        lines.append("")
        for pair in meet.get("pairs") or []:
            a, b = pair["owners"]
            if pair.get("coordinated"):
                edges = "; ".join(f"{pages.escape(e['waiting'])} waits for {pages.escape(e['on'])}" for e in pair["edges"])
                lines.append(f"**Coordinated**: {edges}.")
            else:
                mine, theirs = (", ".join(pages.escape(i) for i in meet["items"].get(o, [])) for o in (a, b))
                lines.append(f"**Uncoordinated**: no blocking edge links {mine} ({pages.escape(a)}) and {theirs} ({pages.escape(b)}).")
            lines.append("")
    return lines


def _matrix(data: Document, owners: Mapping[str, Any], names: Mapping[str, str]) -> list[str]:
    signals = [s for s in data.get("signals") or [] if isinstance(s, Mapping)]
    columns = sorted({s["owner"] for s in signals})
    if not columns:
        return ["No component has any work on it.", ""]
    lines = [
        "| Component | " + " | ".join(pages._link(o, (owners.get(o) or {}).get("url")) for o in columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    for cid in sorted({s["component"] for s in signals}):
        row = []
        for owner in columns:
            states = {s["state"] for s in signals if s["component"] == cid and s["owner"] == owner}
            row.append(", ".join(s for s in STATE_ORDER if s in states))
        lines.append(f"| {pages.escape(str(names.get(cid, cid)))} | " + " | ".join(row) + " |")
    return lines + [""]


def _branches(data: Document, owners: Mapping[str, Any], names: Mapping[str, str]) -> list[str]:
    listed = [b for b in (data.get("branches") or {}).get("list") or [] if isinstance(b, Mapping)]
    if not listed:
        return ["No branch is ahead of `main` within the window.", ""]
    lines = ["| Branch | Repository | Ahead | Newest commit | Files | Ticket | Owner | Components |", "|---|---|---:|---|---:|---|---|---|"]
    for b in listed:
        lines.append(
            f"| {pages._code(b['name'])} | {pages.escape(str(b['repo']))} | {pages._figure(b.get('ahead'))} | "
            f"{pages._cell(b.get('committed'))} | {pages._figure(len(b.get('files') or []))} | {pages.escape(str(b.get('item')))} | "
            f"{pages.escape(str(b.get('owner')))} | {pages.escape(', '.join(names.get(c, c) for c in b.get('components') or []))} |"
        )
    return lines + [""]


# ---------------------------------------------------------------------------
# Text parts
# ---------------------------------------------------------------------------


def _hand_and_measured(*, hand: str, measured: str, where: str = "Each is in a repository's `docs/architecture/components.yaml`.") -> list[str]:
    return [
        f"> **Written by hand:** {hand}. {where}",
        ">",
        f"> **Measured:** {measured}. Nothing measured is written by hand, so it cannot drift from the code.",
        "",
    ]


def _engine_notice(data: Document) -> list[str]:
    engine = data.get("engine") or {}
    lines = [
        f"Read from engine {pages._code(engine.get('ref'))} at {pages._code(str(engine.get('commit'))[:12])}, "
        f"committed {pages._cell(engine.get('committed'))}.",
        "",
    ]
    refresh = engine.get("refresh")
    if isinstance(refresh, Mapping) and refresh.get("ok") is False:
        lines += [
            f"> **The engine refresh failed**: {pages._cell(refresh.get('error'))}. "
            f"This is the {pages._code(engine.get('ref'))} this clone already had, so it may be behind.",
            "",
        ]
    if engine.get("model"):
        return lines
    return lines + [
        f"> **The engine has no architecture model yet**: no architecture model at {pages._code(engine.get('ref'))} "
        f"(commit {pages._code(str(engine.get('commit'))[:12])}) of {pages._code(engine.get('repo'))}. Its components, layers and "
        "imports are not drawn; this repository's are.",
        "",
    ]


def _size(component: Mapping[str, Any]) -> str:
    if component.get("files"):
        return f"{component['files']:,} files, {component['lines']:,} lines"
    return f"{component.get('tracked', 0):,} tracked files"


def _adrs_for(data: Document, ids: set[str]) -> list[Mapping[str, Any]]:
    return [a for a in data.get("adrs") or [] if ids & set(a["components"])]


def _adr_link(adr: Mapping[str, Any], members: list[Mapping[str, Any]]) -> str:
    """An ADR as a link, with how it was placed on `members` (code it cites, or a name in its title)."""
    note = "" if adr.get("status") in (None, "current") else f" _{adr['status']}_"
    via = sorted({adr["components"][c["id"]] for c in members if c["id"] in adr["components"]})
    return f"[{pages.escape(str(adr['title']))}]({adr['url']}) ({adr['repo']}; placed by {' and '.join(via)}){note}"


def _as_of(data: Document) -> str:
    registry, engine = data.get("registry") or {}, data.get("engine") or {}
    return (
        f"Read from this repository at {pages._code(registry.get('ref'))} ({pages._code(str(registry.get('commit'))[:12])}) and the engine at "
        f"{pages._code(engine.get('ref'))} ({pages._code(str(engine.get('commit'))[:12])}); collected {pages._cell(data.get('collected_at'))}."
    )


def _seam(summary: Mapping[str, Any]) -> str:
    """The seam's import count, and the component pairs when the engine's model says which they are (#421).

    The count is measured from the names, so it stands with or without the
    engine's model; the pairs are resolved through the engine's claims, and an
    unknown number of them is said to be unknown, never 0.
    """
    imports = f"{pages._figure(summary.get('registry_to_engine_imports'))} imports from this repository into the engine"
    pairs = summary.get("registry_to_engine_edges")
    if pairs is None:
        return imports + "; the component pairs cannot be drawn until the engine's model is at the ref read."
    return imports + f", across {pages._figure(pairs)} component pairs."


def _baseline(baseline: Any) -> list[str]:
    """The engine-import baseline's size, and what this repository makes, as one line (#421).

    The size is the committed file's entry count, which the collector read from
    this repository; a file that is not there is unknown, never zero.
    """
    if not isinstance(baseline, Mapping):
        return []
    entries, measured = baseline.get("entries"), baseline.get("measured")
    if not isinstance(entries, int) or isinstance(entries, bool):
        return [
            f"**The engine-import baseline is unknown**: {pages._cell(baseline.get('note'))}. "
            f"This repository makes {pages._figure(measured)} imports of engine modules.",
            "",
        ]
    return [
        f"**The engine-import baseline** ({pages._code(baseline.get('path'))}) lists "
        f"{pages._figure(entries)} imports of engine modules, one per importing file and imported name; "
        f"this repository makes {pages._figure(measured)} of them.",
        "",
    ]


def _health(data: Document, components: list, edges: list, summary: Mapping[str, Any]) -> list[str]:
    lines = ["## Health", ""]
    gaps = data.get("gaps") or {}
    names = {"registry": "This repository", "engine": "The engine"}
    for repo in ("registry", "engine"):
        found = gaps.get(repo) or {}
        unclaimed, stale = found.get("unclaimed") or [], found.get("stale") or []
        lines.append(f"### {names[repo]}: claims")
        lines.append("")
        if repo == "engine" and not (data.get("engine") or {}).get("model"):
            lines += ["No model to check the engine's files against.", ""]
            continue
        lines.append(f"{len(unclaimed)} code files no component claims; {len(stale)} claims that match no tracked file.")
        lines.append("")
        lines += [f"- Unclaimed: {pages._code(path)}" for path in unclaimed]
        lines += [f"- Stale: {pages._code(c['path'])} (claimed by {pages._code(c['component'])})" for c in stale]
        if unclaimed or stale:
            lines.append("")
    by_id = {c["id"]: c for c in components}
    upward = [e for e in edges if e.get("upward")]
    lines += ["### The engine's layering", ""]
    if not (data.get("engine") or {}).get("model"):
        lines += [
            "The engine's layering cannot be measured until its model is at the ref read: a component "
            "without a layer has no upward for an import to cross, so there is no figure to give.",
            "",
        ]
    else:
        lines.append(
            f"{summary.get('upward_edges', 0)} edges ({summary.get('upward_imports', 0)} imports) go against the layering, "
            f"of {sum(1 for e in edges if e.get('scope') == 'engine')} edges between engine components."
        )
        lines.append("")
        if upward:
            lines += ["| From | To | Imports |", "|---|---|---:|"]
            for e in sorted(upward, key=lambda e: -e["imports"]):
                a, b = by_id[e["from"]], by_id[e["to"]]
                lines.append(f"| {pages.escape(a['name'])} (layer {a['layer']}) | {pages.escape(b['name'])} (layer {b['layer']}) | {e['imports']} |")
            lines.append("")
    lines += ["### The seam between the repositories", ""]
    lines.append(_seam(summary))
    lines.append("")
    lines += _baseline(data.get("baseline"))
    lines += ["### ADRs", ""]
    lines.append(f"{summary.get('adrs_placed', 0)} of {summary.get('adrs_total', 0)} ADRs are placed on a component.")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Diagrams: Graphviz source for Quarto's {dot} cells
# ---------------------------------------------------------------------------


def _cell(source: str) -> str:
    return f"```{{dot}}\n//| fig-width: 9\n{source}\n```\n"


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _label(name: str, tech: str | None, summary: str | None, width: int = 30) -> str:
    rows = [f'<FONT POINT-SIZE="12"><B>{_esc(name)}</B></FONT>']
    if tech:
        rows.append(f'<FONT POINT-SIZE="9">[{_esc(tech)}]</FONT>')
    if summary:
        rows.extend(f'<FONT POINT-SIZE="9">{_esc(line)}</FONT>' for line in textwrap.wrap(summary, width)[:4])
    return "<" + "<BR/>".join(rows) + ">"


def _context(model: Mapping[str, Any]) -> str:
    inside = {c["id"] for c in model.get("containers") or []}
    system = model.get("system") or {}
    lines = ["digraph context {", HEAD, "rankdir=TB;"]
    for p in model.get("people") or []:
        lines.append(f'"{p["id"]}" [label={_label(p["name"], "Person", p.get("summary"))} fillcolor="#dbe9f7"];')
    lines.append(f'"system" [label={_label(system.get("name", "System"), "Software system", system.get("summary"), 36)} fillcolor="#c5dcf2"];')
    for x in model.get("external") or []:
        lines.append(f'"{x["id"]}" [label={_label(x["name"], "External system", x.get("summary"))} fillcolor="#eeeeee"];')
    seen = set()
    for a, b, text in model.get("container_relations") or []:
        a, b = ("system" if a in inside else a), ("system" if b in inside else b)
        if a != b and (a, b) not in seen:
            seen.add((a, b))
            lines.append(f'"{a}" -> "{b}" [label="{_esc(text)}"];')
    lines.append("}")
    return "\n".join(lines)


def _containers(model: Mapping[str, Any]) -> str:
    system = model.get("system") or {}
    lines = ["digraph containers {", HEAD, "rankdir=TB; compound=true;"]
    for p in model.get("people") or []:
        lines.append(f'"{p["id"]}" [label={_label(p["name"], "Person", None)} fillcolor="#dbe9f7"];')
    lines.append(f'subgraph cluster_system {{ label="{_esc(system.get("name", "System"))}"; fontname="Arial"; fontsize=11; margin=16; color="{GREY}";')
    for c in model.get("containers") or []:
        shape = ' shape=cylinder style="filled"' if c.get("repo") is None else ""
        lines.append(f'"{c["id"]}" [{shape.strip()} label={_label(c["name"], c.get("tech"), c.get("summary"), 28)} fillcolor="#c5dcf2"];')
    lines.append("}")
    for x in model.get("external") or []:
        lines.append(f'"{x["id"]}" [label={_label(x["name"], "External", None)} fillcolor="#eeeeee"];')
    for a, b, text in model.get("container_relations") or []:
        lines.append(f'"{a}" -> "{b}" [label="{_esc(textwrap.fill(text, 22))}"];')
    lines.append("}")
    return "\n".join(lines)


def _node(component: Mapping[str, Any]) -> str:
    sub = _size(component)
    return (
        f'"{component["id"]}" [label=<<FONT POINT-SIZE="12"><B>{_esc(component["name"])}</B></FONT><BR/>'
        f'<FONT POINT-SIZE="9">{_esc(sub)}</FONT>>];'
    )


def _engine(components: list, edges: list, model: Mapping[str, Any]) -> str:
    """The engine's components, one row per layer, with the imports that go against the layering."""
    engine = [c for c in components if c["repo"] == "engine"]
    layers = model.get("layers") or []
    by_id = {c["id"]: c for c in engine}
    lines = ["digraph engine {", HEAD, "rankdir=TB; newrank=true; ranksep=0.45;"]
    for layer in layers:
        lines.append(
            f'"L{layer["layer"]}" [shape=plaintext style="" fontcolor="{GREY}" '
            f'label=<<FONT POINT-SIZE="10">{layer["layer"]} · {_esc(str(layer["name"]).upper())}</FONT>>];'
        )
    for upper, lower in pairwise(layers):
        lines.append(f'"L{upper["layer"]}" -> "L{lower["layer"]}" [style=invis];')
    for layer in layers:
        members = [c["id"] for c in engine if c["layer"] == layer["layer"]]
        lines.append("{ rank=same; " + f'"L{layer["layer"]}"; ' + "".join(f'"{i}"; ' for i in members) + "}")
    lines += [_node(c) for c in engine]
    # An invisible spine from each layer to the next keeps the rows in order.
    for upper, lower in pairwise(layers):
        top = next((c["id"] for c in engine if c["layer"] == upper["layer"]), None)
        bottom = next((c["id"] for c in engine if c["layer"] == lower["layer"]), None)
        if top and bottom:
            lines.append(f'"{top}" -> "{bottom}" [style=invis weight=0];')
    for e in edges:
        if e.get("scope") != "engine" or e["from"] not in by_id or e["to"] not in by_id:
            continue
        a, b, n = by_id[e["from"]], by_id[e["to"]], e["imports"]
        if e.get("upward"):
            lines.append(
                f'"{e["from"]}" -> "{e["to"]}" [color="{UPWARD}" fontcolor="{UPWARD}" penwidth=1.4 constraint=false '
                f'label=" {n}" tooltip="{_esc(a["name"])} imports {_esc(b["name"])}, a higher layer ({n})"];'
            )
        elif a["layer"] == b["layer"]:
            lines.append(f'"{e["from"]}" -> "{e["to"]}" [style=dashed constraint=false label=" {n}"];')
    lines.append("}")
    return "\n".join(lines)


def _registry(components: list, edges: list, model: Mapping[str, Any], engine_model: bool) -> str:
    """This repository's components by container, and the imports they make into the engine."""
    mine = [c for c in components if c["repo"] == "registry"]
    containers = {c["id"]: c for c in model.get("containers") or []}
    by_container: dict[str, list[str]] = defaultdict(list)
    for c in mine:
        by_container[c["container"]].append(c["id"])
    by_id = {c["id"]: c for c in mine}
    lines = ["digraph registry {", HEAD, "rankdir=TB; newrank=true;"]
    for container, ids in by_container.items():
        name = containers.get(container, {}).get("name", container)
        lines.append(f'subgraph cluster_{container} {{ label="{_esc(name)}"; fontname="Arial"; fontsize=11; margin=12; color="{GREY}";')
        lines += [_node(by_id[i]) for i in ids]
        lines.append("}")
    engine_ids = {c["id"] for c in components if c["repo"] == "engine"}
    lines.append(
        '"engine" [fillcolor="#eeeeee" label='
        + _label("Engine (opengwasdb)", "imported directly",
                 f"{len({e['to'] for e in edges if e.get('scope') == 'registry_to_engine'})} of {len(engine_ids)} engine components"
                 if engine_model else "no architecture model yet", 28)
        + "];"
    )
    for e in edges:
        if e.get("scope") == "registry" and e["from"] in by_id and e["to"] in by_id:
            lines.append(f'"{e["from"]}" -> "{e["to"]}";')
    seam: dict[str, int] = defaultdict(int)
    for e in edges:
        if e.get("scope") == "registry_to_engine" and e["from"] in by_id:
            seam[e["from"]] += e["imports"]
    for source, n in sorted(seam.items()):
        lines.append(f'"{source}" -> "engine" [label="{n}" penwidth={0.6 + min(n, 30) / 30:.2f}];')
    lines.append("}")
    return "\n".join(lines)
