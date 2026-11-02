"""The architecture pages: Pipeline, and Structure and health (#418, epic #416).

Both are written from `architecture.json`, the architecture collector's file
(`docs/spec/project-site.md`). Like every page of `pages.py` they read
`available` first and show the site's usual notice when the source could not be
read, and every figure traces to one collector field.

The diagrams are Quarto `{dot}` cells, which Quarto draws at render time with
its own bundled Graphviz. A `{dot}` cell is a diagram, not code: it needs no
kernel and no new dependency, so `pages.py`'s promise of no executable code
cells still holds, and nothing generated is ever committed.

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
        lines.append("")
        placed = _adrs_for(data, {c["id"] for c in members})
        if placed:
            lines += ["ADRs:", ""]
            lines += [f"- {_adr_link(a, members)}" for a in placed]
            lines.append("")
        else:
            lines += ["_No ADR is placed on these components._", ""]
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


# ---------------------------------------------------------------------------
# Text parts
# ---------------------------------------------------------------------------


def _hand_and_measured(*, hand: str, measured: str) -> list[str]:
    return [
        f"> **Written by hand:** {hand}. Each is in a repository's `docs/architecture/components.yaml`.",
        ">",
        f"> **Measured:** {measured}. Nothing measured is written by hand, so it cannot drift from the code.",
        "",
    ]


def _engine_notice(data: Document) -> list[str]:
    engine = data.get("engine") or {}
    if engine.get("model"):
        return []
    return [
        f"> **The engine has no architecture model yet**: no architecture model at `{engine.get('ref')}` "
        f"(commit `{str(engine.get('commit'))[:12]}`) of `{engine.get('repo')}`. Its components, layers and "
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
    note = "" if adr.get("status") in (None, "current") else f" _{adr['status']}_"
    via = sorted({adr["components"][c["id"]] for c in members if c["id"] in adr["components"]})
    return f"[{pages.escape(str(adr['title']))}]({adr['url']}) ({adr['repo']}; placed by {' and '.join(via)}){note}"


def _as_of(data: Document) -> str:
    registry, engine = data.get("registry") or {}, data.get("engine") or {}
    return (
        f"_Read from this repository at `{registry.get('ref')}` (`{str(registry.get('commit'))[:12]}`) and the engine at "
        f"`{engine.get('ref')}` (`{str(engine.get('commit'))[:12]}`); collected {pages._cell(data.get('collected_at'))}._"
    )


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
        lines += [f"- Unclaimed: `{path}`" for path in unclaimed]
        lines += [f"- Stale: `{c['path']}` (claimed by `{c['component']}`)" for c in stale]
        if unclaimed or stale:
            lines.append("")
    by_id = {c["id"]: c for c in components}
    upward = [e for e in edges if e.get("upward")]
    if (data.get("engine") or {}).get("model"):
        lines += ["### The engine's layering", ""]
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
    lines.append(
        f"{summary.get('registry_to_engine_imports', 0)} imports from this repository into the engine, "
        f"across {summary.get('registry_to_engine_edges', 0)} component pairs."
    )
    lines.append("")
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
