"""PROTOTYPE (architecture map epic): the page for architecture_prototype.py.

Three views of one model, switched by the URL hash (#c4, #work, #pipeline) and
a floating bar, for reviewers to compare approaches. Diagrams are Graphviz SVG
made here and styled by CSS classes, so they follow the light and dark themes
and need no script to draw.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import textwrap
from collections import defaultdict
from itertools import pairwise

ORG = "opengwas"
ENGINE = "opengwasdb"
VIEWS = [("c4", "C4 views"), ("work", "Work map"), ("pipeline", "Pipeline")]
LIVE = ("building", "active")


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def issue_url(key: str) -> str:
    repo, n = key.split("#")
    return f"https://github.com/{ORG}/{repo}/issues/{n}"


def short(key: str) -> str:
    """`opengwasdb#347` -> `engine #347`; `opengwasdb-stores#232` -> `stores #232`."""
    if key.startswith("branch:"):
        return key.split(":", 2)[2]
    repo, n = key.split("#")
    return f"{'engine' if repo == ENGINE else 'stores'} #{n}"


# --------------------------------------------------------------------------
# Graphviz
# --------------------------------------------------------------------------


def dot_svg(source: str) -> str:
    svg = subprocess.run(["dot", "-Tsvg"], input=source.encode(), capture_output=True,
                         check=True).stdout.decode()
    svg = svg[svg.index("<svg"):]
    svg = re.sub(r"<!--.*?-->\n?", "", svg, flags=re.DOTALL)
    width = re.search(r'width="([\d.]+)pt"', svg)
    w = float(width.group(1)) if width else 800
    svg = re.sub(r'\s(width|height)="[\d.]+pt"', "", svg, count=2)
    svg = svg.replace("<svg", f'<svg style="width:100%;max-width:{w * 1.25:.0f}px" '
                              'role="img"', 1)
    return svg


def label(name: str, tech: str | None, summary: str | None, width: int = 30) -> str:
    """A Graphviz HTML-like label: bold name, [tech], wrapped summary."""
    rows = [f'<FONT POINT-SIZE="12"><B>{esc(name)}</B></FONT>']
    if tech:
        rows.append(f'<FONT POINT-SIZE="9">[{esc(tech)}]</FONT>')
    if summary:
        rows.extend(f'<FONT POINT-SIZE="9">{esc(line)}</FONT>'
                    for line in textwrap.wrap(summary, width)[:4])
    return "<" + "<BR/>".join(rows) + ">"


HEAD = ('graph [fontname="Arial" bgcolor="transparent" pad="0.2" nodesep="0.35" ranksep="0.55"];\n'
        'node [fontname="Arial" shape=box style="rounded,filled" fillcolor="white" margin="0.15,0.08"];\n'
        'edge [fontname="Arial" fontsize=9 arrowsize=0.6];\n')


def context_svg(model: dict) -> str:
    inside = {c["id"] for c in model["containers"]}
    lines = ["digraph context {", HEAD, 'rankdir=TB;']
    for p in model["people"]:
        lines.append(f'"{p["id"]}" [class="person" label={label(p["name"], "Person", p["summary"])}];')
    lines.append(f'"system" [class="system clickable-none" label={label(model["system"]["name"], "Software system", model["system"]["summary"], 36)}];')
    for x in model["external"]:
        lines.append(f'"{x["id"]}" [class="ext" label={label(x["name"], "External system", x["summary"])}];')
    seen = set()
    for a, b, text in model["container_relations"]:
        a2 = "system" if a in inside else a
        b2 = "system" if b in inside else b
        if a2 == b2 or (a2, b2) in seen:
            continue
        seen.add((a2, b2))
        lines.append(f'"{a2}" -> "{b2}" [label="{esc(text)}"];')
    lines.append("}")
    return dot_svg("\n".join(lines))


def container_svg(model: dict) -> str:
    lines = ["digraph containers {", HEAD, "rankdir=TB; compound=true;"]
    for p in model["people"]:
        lines.append(f'"{p["id"]}" [class="person" label={label(p["name"], "Person", None)}];')
    lines.append(f'subgraph cluster_system {{ class="boundary"; label="{esc(model["system"]["name"])}"; fontname="Arial"; fontsize=11; margin=16;')
    for c in model["containers"]:
        shape = ' shape=cylinder style="filled"' if c["id"] == "releases" else ""
        lines.append(f'"{c["id"]}" [class="container{" data" if shape else ""}"{shape} label={label(c["name"], c["tech"], c["summary"], 28)}];')
    lines.append("}")
    for x in model["external"]:
        lines.append(f'"{x["id"]}" [class="ext" label={label(x["name"], "External", None)}];')
    for a, b, text in model["container_relations"]:
        lines.append(f'"{a}" -> "{b}" [label="{esc(textwrap.fill(text, 22))}"];')
    lines.append("}")
    return dot_svg("\n".join(lines))


def component_svg(data: dict, which: str) -> str:
    """`engine`: the engine's components and their imports. `registry`: the
    registry side, with the engine collapsed to the components it imports."""
    model = data["model"]
    comps = {c["id"]: c for c in model["components"]}
    counts = data["check"]["counts"]
    containers = {c["id"]: c for c in model["containers"]}

    def node(cid: str) -> str:
        c = comps[cid]
        n = counts.get(cid, {"files": 0, "lines": 0, "tracked": 0})
        sub = (f'{n["files"]} files · {n["lines"]:,} lines' if n["files"]
               else f'{n["tracked"]:,} tracked files (data)')
        return (f'"{cid}" [id="c-{cid}" class="comp clickable" '
                f'label=<<FONT POINT-SIZE="12"><B>{esc(c["name"])}</B></FONT><BR/>'
                f'<FONT POINT-SIZE="9">{esc(sub)}</FONT>>];')

    lines = [f"digraph {which} {{", HEAD, "rankdir=TB; newrank=true;"]
    edges = data["edges"]
    if which == "engine":
        ids = [c["id"] for c in model["components"] if c["container"] == "engine"]
        layer = {c["id"]: c["layer"] for c in model["components"] if c["container"] == "engine"}
        layers = model["engine_layers"]
        lines.append("ranksep=0.45;")
        # A column of layer names on the left keeps the layers in order.
        for ly in layers:
            lines.append(f'"L{ly["layer"]}" [shape=plaintext style="" class="layer" '
                         f'label=<<FONT POINT-SIZE="10">{ly["layer"]} · {esc(ly["name"]).upper()}</FONT>>];')
        for upper, lower in pairwise(layers):
            lines.append(f'"L{upper["layer"]}" -> "L{lower["layer"]}" [style=invis];')
        for ly in layers:
            members = [i for i in ids if layer[i] == ly["layer"]]
            lines.append("{ rank=same; " + f'"L{ly["layer"]}"; ' + " ".join(f'"{i}";' for i in members) + " }")
        lines += [node(i) for i in ids]
        # Order within the picture: an invisible spine from each layer to the next.
        for upper, lower in pairwise(layers):
            a = next(i for i in ids if layer[i] == upper["layer"])
            b = next(i for i in ids if layer[i] == lower["layer"])
            lines.append(f'"{a}" -> "{b}" [style=invis weight=0];')
        pairs = {(e["from"], e["to"]): e["imports"] for e in edges
                 if e["from"] in ids and e["to"] in ids}
        for (a, b), n in sorted(pairs.items()):
            if layer[b] > layer[a]:
                lines.append(f'"{a}" -> "{b}" [class="up" constraint=false penwidth=1.4 label=" {n}" '
                             f'tooltip="{esc(comps[a]["name"])} imports {esc(comps[b]["name"])}, a higher layer ({n})"];')
            elif layer[b] == layer[a]:
                lines.append(f'"{a}" -> "{b}" [class="same" constraint=false style=dashed label=" {n}"];')
    else:
        reg = [c for c in model["components"] if c["container"] != "engine"]
        by_container = defaultdict(list)
        for c in reg:
            by_container[c["container"]].append(c["id"])
        for cont, ids in by_container.items():
            lines.append(f'subgraph cluster_{cont} {{ class="boundary"; label="{esc(containers[cont]["name"])}"; fontname="Arial"; fontsize=11; margin=12;')
            lines += [node(i) for i in ids]
            lines.append("}")
        engine_ids = {c["id"] for c in model["components"] if c["container"] == "engine"}
        used = sorted({e["to"] for e in edges if e["to"] in engine_ids and e["from"] not in engine_ids})
        lines.append(f'"engine" [class="ext" label={label("Engine (opengwasdb)", "imported directly", f"{len(used)} of {len(engine_ids)} engine components", 28)}];')
        for e in edges:
            a, b = e["from"], e["to"]
            if a in engine_ids:
                continue
            if b in engine_ids:
                continue
            lines.append(f'"{a}" -> "{b}" [class="dep"];')
        cross = defaultdict(int)
        for e in edges:
            if e["from"] not in engine_ids and e["to"] in engine_ids:
                cross[e["from"]] += e["imports"]
        for a, n in sorted(cross.items()):
            lines.append(f'"{a}" -> "engine" [class="dep" label="{n}" penwidth={0.6 + min(n, 30) / 30:.2f}];')
    lines.append("}")
    return dot_svg("\n".join(lines))


# --------------------------------------------------------------------------
# Page parts
# --------------------------------------------------------------------------


def owner_chip(owners: dict, key: str, state: str | None = None) -> str:
    o = owners.get(key, {"title": key, "url": None, "status": ""})
    cls = f"chip {state}" if state else "chip"
    text = esc(short(key))
    title = esc(f'{o["title"]} ({o["status"]})')
    if o.get("url"):
        return f'<a class="{cls}" href="{esc(o["url"])}" target="_blank" rel="noopener" title="{title}">{text}</a>'
    return f'<span class="{cls}" title="{title}">{text}</span>'


def item_chip(item: str, owner: str, branches: dict) -> str:
    if item.startswith("branch:"):
        name = item[7:]
        repo = owner.split("#")[0] if "#" in owner else owner.split(":")[1]
        b = branches.get(f"{repo}:{name}")
        if b and b["remote"]:
            return (f'<a class="item" href="https://github.com/{ORG}/{esc(repo)}/tree/{esc(name)}" '
                    f'target="_blank" rel="noopener">branch {esc(name)}</a>')
        return f'<span class="item" title="local branch on the build host">branch {esc(name)} (local)</span>'
    return f'<a class="item" href="{esc(issue_url(item))}" target="_blank" rel="noopener">{esc(short(item))}</a>'


def comp_button(comps: dict, cid: str) -> str:
    return f'<button class="comp-link" type="button" data-comp="{esc(cid)}">{esc(comps[cid]["name"])}</button>'


def work_view(data: dict) -> str:
    model, work = data["model"], data["work"]
    comps = {c["id"]: c for c in model["components"]}
    owners = work["owners"]
    branches = {f'{b["repo"]}:{b["name"]}': b for b in data["branches"]}
    meets = sorted(work["meets"], key=lambda m: (bool(m["coordinated"]), -len(m["owners"]), m["component"]))
    open_meets = [m for m in meets if not m["coordinated"]]

    rows = []
    for m in meets:
        sides = []
        for k in m["owners"]:
            items = " ".join(item_chip(i, k, branches) for i in m["items"][k][:6])
            more = len(m["items"][k]) - 6
            if more > 0:
                items += f' <span class="more">+{more} more</span>'
            sides.append(f'<div class="side">{owner_chip(owners, k)}<div class="items">{items}</div></div>')
        if m["coordinated"]:
            how = "; ".join(f'{short(c["waiting"])} waits for {short(c["on"])}' for c in m["coordinated"])
            status = f'<span class="pill ok">Coordinated: {esc(how)}</span>'
        else:
            status = '<span class="pill meet">No blocking edge between them</span>'
        rows.append(f'<li class="meet-row"><div class="meet-head">{comp_button(comps, m["component"])}{status}</div>'
                    f'<div class="sides">{"".join(sides)}</div></li>')

    # Matrix: components x owners with live work.
    live_owners = sorted({s["owner"] for s in work["signals"] if s["state"] in LIVE},
                         key=lambda k: ({"Active": 0, "Todo": 1, "Paused": 2}.get(owners[k]["status"], 3), k))
    cell: dict[tuple[str, str], set[str]] = defaultdict(set)
    evidence: dict[tuple[str, str], list[str]] = defaultdict(list)
    planned: dict[str, set[str]] = defaultdict(set)
    for s in work["signals"]:
        key = (s["component"], s["owner"])
        cell[key].add(s["state"])
        ev = s.get("file") or s.get("branch", "")
        evidence[key].append(f'{short(s["item"]) if "#" in s["item"] else s["item"]}: {s["state"]} ({ev})')
        if s["state"] in ("planned", "scope", "waiting", "paused") and s["owner"] not in live_owners:
            planned[s["component"]].add(s["owner"])
    meet_comps = {m["component"]: bool(m["coordinated"]) for m in meets}

    head = "".join(f'<th scope="col" class="own">{owner_chip(owners, k)}<span class="status">{esc(owners[k]["status"])}</span></th>'
                   for k in live_owners)
    body = []
    for cont in model["containers"]:
        ids = [c["id"] for c in model["components"] if c["container"] == cont["id"]]
        if not ids:
            continue
        body.append(f'<tr class="group"><th colspan="{len(live_owners) + 2}" scope="rowgroup">{esc(cont["name"])}</th></tr>')
        for cid in ids:
            flag = ""
            if cid in meet_comps:
                flag = '<span class="flag ok" title="Work meets here, coordinated">●</span>' if meet_comps[cid] \
                    else '<span class="flag meet" title="Work meets here, no blocking edge">●</span>'
            tds = []
            for k in live_owners:
                st = cell.get((cid, k), set())
                if "building" in st:
                    mark, cls = "■", "building"
                elif "active" in st:
                    mark, cls = "●", "active"
                elif st:
                    mark, cls = "○", "quiet"
                else:
                    mark, cls = "", ""
                tip = esc("\n".join(sorted(set(evidence.get((cid, k), [])))[:12])) if mark else ""
                tds.append(f'<td class="m {cls}" title="{tip}">{mark}</td>')
            pl = " ".join(owner_chip(owners, k) for k in sorted(planned.get(cid, ())))
            body.append(f'<tr><th scope="row">{flag}{comp_button(comps, cid)}</th>{"".join(tds)}<td class="planned">{pl}</td></tr>')

    n_live = len(live_owners)
    return f'''
<section class="view" id="view-work" data-view="work" hidden>
  <div class="view-intro">
    <h2>Work map</h2>
    <p>Where open epics, tickets and branches touch the code right now. A component is flagged when two owners have live work on it, and the flag clears when a blocking edge links the two pieces of work.</p>
    <p class="keeps"><strong>What keeps it true:</strong> nothing here is written by hand. It is rebuilt from the board, ticket texts, blocking edges and branches every time the page is generated.</p>
  </div>
  <div class="summary">
    <div><span class="num">{len(meets)}</span> components with live work from two or more owners</div>
    <div><span class="num meet-ink">{len(open_meets)}</span> without a blocking edge between the work</div>
    <div><span class="num">{n_live}</span> owners with live work: epics, issues or unlinked branches</div>
  </div>
  <h3>Where work meets</h3>
  <ul class="meets">{"".join(rows)}</ul>
  <h3>Components × live work</h3>
  <p class="legend"><span class="m building">■</span> a branch with commits in the last {data["window_days"]} days changes it
    <span class="m active">●</span> an open ticket in an Active epic cites its code
    <span class="m quiet">○</span> cited, but planned, waiting on a blocker, or only in an epic's scope. Hover a mark for the evidence.</p>
  <div class="scroll"><table class="matrix">
    <thead><tr><th scope="col">Component</th>{head}<th scope="col">Also cited by</th></tr></thead>
    <tbody>{"".join(body)}</tbody>
  </table></div>
</section>'''


def pipeline_view(data: dict) -> str:
    model, work = data["model"], data["work"]
    comps = {c["id"]: c for c in model["components"]}
    containers = {c["id"]: c for c in model["containers"]}
    owners = work["owners"]
    adrs_by = defaultdict(list)
    for a in data["adrs"]:
        for cid in a["components"]:
            adrs_by[cid].append(a)
    live = defaultdict(lambda: defaultdict(set))
    for s in work["signals"]:
        if s["state"] in LIVE:
            live[s["component"]][s["owner"]].add(s["state"])
    meet_comps = {m["component"]: bool(m["coordinated"]) for m in work["meets"]}

    cards = []
    stages = [s for s in model["stages"] if s["id"] != "cross"] + [s for s in model["stages"] if s["id"] == "cross"]
    for i, st in enumerate(stages, 1):
        ids = [c["id"] for c in model["components"] if st["id"] in c["stages"]]
        chips = []
        for cid in ids:
            repo = containers[comps[cid]["container"]]["repo"]
            tag = "engine" if repo == ENGINE else "stores"
            flag = ""
            if cid in meet_comps:
                flag = ' <span class="flag ok">●</span>' if meet_comps[cid] else ' <span class="flag meet">●</span>'
            chips.append(f'<li><button class="comp-link {tag}" type="button" data-comp="{cid}">'
                         f'<span class="repo">{tag}</span>{esc(comps[cid]["name"])}{flag}</button></li>')
        adrs = {(a["repo"], a["number"]): a for cid in ids for a in adrs_by[cid]}
        adr_items = "".join(
            f'<li><a href="{esc(a["url"])}" target="_blank" rel="noopener"><span class="adr-n">{"" if a["repo"] == ENGINE else "S"}{a["number"]}</span> {esc(re.sub(r"^ADR[- ]?\d+[:.]?\s*", "", a["title"]))}</a>'
            f'{" <em>" + a["status"] + "</em>" if a["status"] != "current" else ""}</li>'
            for _, a in sorted(adrs.items()))
        owns = defaultdict(set)
        for cid in ids:
            for k, sts in live[cid].items():
                owns[k] |= sts
        work_chips = " ".join(owner_chip(owners, k, "building" if "building" in sts else "active")
                              for k, sts in sorted(owns.items()))
        number = f'<span class="step">{i}</span>' if st["id"] != "cross" else '<span class="step">∗</span>'
        cards.append(f'''<li class="stage{" cross" if st["id"] == "cross" else ""}">
  <h3>{number}{esc(st["name"])}</h3>
  <p class="stage-sum">{esc(st["summary"])}</p>
  <ul class="stage-comps">{"".join(chips)}</ul>
  <div class="stage-work"><span class="label">In flight</span> {work_chips or '<span class="none">nothing live</span>'}</div>
  <details><summary><span class="label">ADRs</span> {len(adrs)} placed here</summary><ol class="adrs">{adr_items}</ol></details>
</li>''')
    return f'''
<section class="view" id="view-pipeline" data-view="pipeline" hidden>
  <div class="view-intro">
    <h2>Pipeline</h2>
    <p>The path data takes, from a source file to a query, with the components that do each stage, the ADRs that govern it, and the work in flight there. A proposed evaluation or check attaches to a stage.</p>
    <p class="keeps"><strong>What keeps it true:</strong> each component's stages are written by hand in the model; its code, ADRs and work are measured. Engine and registry components share a stage when they meet there.</p>
  </div>
  <ol class="stages">{"".join(cards)}</ol>
</section>'''


def c4_view(data: dict) -> str:
    model = data["model"]
    return f'''
<section class="view" id="view-c4" data-view="c4">
  <div class="view-intro">
    <h2>C4 views</h2>
    <p>The C4 model's first three levels. Context and Container are written by hand and change rarely. At the Component level the boxes are each component's claimed code and the arrows are the real imports, so that level cannot drift from the code. Click a component for its files, imports, ADRs and work.</p>
    <p class="keeps"><strong>What keeps it true:</strong> the component claims are checked on every build. A file nobody claims, or a claim that matches nothing, is reported below.</p>
  </div>
  <figure class="dg-fig"><figcaption><span class="lvl">Level 1</span> System context</figcaption><div class="dg scroll">{context_svg(model)}</div></figure>
  <figure class="dg-fig"><figcaption><span class="lvl">Level 2</span> Containers</figcaption><div class="dg scroll">{container_svg(model)}</div></figure>
  <figure class="dg-fig"><figcaption><span class="lvl">Level 3</span> Engine components <span class="cap-note">Components sit on the layers the model declares. Red: imports against the layering, with their count. Dashed: imports within a layer. Imports that follow the layering are not drawn; each component's details list them.</span></figcaption><div class="dg scroll">{component_svg(data, "engine")}</div></figure>
  <figure class="dg-fig"><figcaption><span class="lvl">Level 3</span> Registry components <span class="cap-note">Numbers on the arrows into the engine: imports of engine modules.</span></figcaption><div class="dg scroll">{component_svg(data, "registry")}</div></figure>
</section>'''


def method(data: dict) -> str:
    c = data["check"]
    edges = data["edges"]
    comps = {x["id"]: x for x in data["model"]["components"]}
    eng = {k for k, v in comps.items() if v["container"] == "engine"}
    inner = [e for e in edges if e["from"] in eng and e["to"] in eng]
    pairs = {(e["from"], e["to"]) for e in inner}
    mutual = sum(1 for a, b in pairs if (b, a) in pairs) // 2
    layer = {k: v["layer"] for k, v in comps.items() if v["container"] == "engine"}
    up = [e for e in inner if layer[e["to"]] > layer[e["from"]]]
    cross = {e["to"] for e in edges if e["from"] not in eng and e["to"] in eng}
    adrs = data["adrs"]
    placed = [a for a in adrs if a["components"]]
    via = defaultdict(int)
    for a in adrs:
        for v in a["components"].values():
            via[v] += 1
    unclaimed = sum(len(v) for v in c["unclaimed"].values())
    files = sum(c["code_files"].values())
    tickets = sum(len(e["tickets"]) for e in data["epics"])
    local = sum(1 for b in data["branches"] if not b["remote"])
    rev = data["revisions"]
    check = ("every one claimed by exactly one component" if not unclaimed
             else f'<strong class="meet-ink">{unclaimed} claimed by no component</strong>')
    stale = ("no claim that matches nothing" if not c["stale"]
             else f'<strong class="meet-ink">{len(c["stale"])} claims that match nothing</strong>')
    return f'''
<section class="method" aria-labelledby="method-h">
  <h2 id="method-h">How this page was made</h2>
  <dl class="facts">
    <div><dt>Code</dt><dd>{files} source files at <code>origin/main</code> of opengwasdb (<code>{rev[ENGINE][:7]}</code>) and opengwasdb-stores (<code>{rev["opengwasdb-stores"][:7]}</code>): {check}; {stale}.</dd></div>
    <div><dt>Imports</dt><dd>{len(edges)} component-to-component edges from the code's imports. Inside the engine, {len(inner)} edges; {mutual} pairs of components import each other, and through them every engine component can reach every other. Against the declared layering, {len(up)} edges carrying {sum(e["imports"] for e in up)} imports go upward. The registry imports {len(cross)} of the engine's {len(eng)} components directly.</dd></div>
    <div><dt>Decisions</dt><dd>{len(adrs)} ADRs across both repositories; {len(placed)} placed on a component, by the code they cite ({via["code"]}), a glossary term in their title ({via["title"]}), or a term used four or more times ({via["text"]}). The other {len(adrs) - len(placed)} cite neither.</dd></div>
    <div><dt>Work</dt><dd>{len(data["epics"])} open epics on the project board with {tickets} tickets, their blocking edges and the file paths their texts cite; {len(data["branches"])} branches with commits not on <code>main</code> in the last {data["window_days"]} days ({local} of them local to the build host, as orchestrate lanes are).</dd></div>
    <div><dt>Limits</dt><dd>A ticket that names no file shows no work. A branch counts once it has a commit, however old its ticket. A path cited in prose counts the same as one a ticket will change. Glossary terms place an ADR by its subject, so review those placements.</dd></div>
  </dl>
  <p class="gen">Generated {esc(data["generated"])} by <code>src/project_site/architecture_prototype.py</code> from <code>docs/architecture/components.yaml</code>.</p>
</section>'''


def page_data(data: dict) -> dict:
    """What the details drawer needs, and nothing else."""
    model = data["model"]
    containers = {c["id"]: c for c in model["containers"]}
    adrs_by = defaultdict(list)
    for a in data["adrs"]:
        for cid, via in a["components"].items():
            adrs_by[cid].append({"n": a["number"], "repo": a["repo"], "title": a["title"],
                                 "url": a["url"], "status": a["status"], "via": via})
    out_e, in_e = defaultdict(list), defaultdict(list)
    for e in data["edges"]:
        out_e[e["from"]].append([e["to"], e["imports"]])
        in_e[e["to"]].append([e["from"], e["imports"]])
    work = defaultdict(list)
    for s in data["work"]["signals"]:
        work[s["component"]].append({k: s.get(k) for k in ("owner", "item", "via", "state", "file", "branch")})
    comps = {}
    for c in model["components"]:
        repo = containers[c["container"]]["repo"]
        comps[c["id"]] = {
            "name": c["name"], "summary": c["summary"], "container": containers[c["container"]]["name"],
            "repo": repo, "stages": c["stages"], "paths": c["paths"],
            "counts": data["check"]["counts"].get(c["id"], {"files": 0, "lines": 0}),
            "out": sorted(out_e[c["id"]], key=lambda x: -x[1]), "in": sorted(in_e[c["id"]], key=lambda x: -x[1]),
            "adrs": sorted(adrs_by[c["id"]], key=lambda a: (a["repo"], a["n"])), "work": work[c["id"]],
        }
    owners = {k: {"title": v["title"], "url": v["url"], "status": v["status"]}
              for k, v in data["work"]["owners"].items()}
    remote = {f'{b["repo"]}:{b["name"]}': b["remote"] for b in data["branches"]}
    stages = {s["id"]: s["name"] for s in model["stages"]}
    return {"components": comps, "owners": owners, "remote": remote, "stages": stages}


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

STYLE = r"""
/* Layout: a quiet survey sheet. A slim masthead, one view at a time, the method
   at the foot, a details drawer on the right (a sheet from the bottom on phones). */
:root {
  --bg: #f3f5f4; --surface: #ffffff; --ink: #17201c; --muted: #58655f;
  --line: #d4dbd7; --line-strong: #95a69e; --band: #e9eeeb; --ext: #f8f9f6;
  --accent: #145a6e; --accent-soft: #d9eaef; --on-accent: #ffffff;
  --build: #a85208; --build-soft: #fbeedb;
  --active: #2348b8; --active-soft: #e2e9fb;
  --meet: #b0281c; --meet-soft: #fbe4e1;
  --ok: #2c6a4c; --ok-soft: #dff0e6;
  --font-ui: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  --radius: 6px;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #111614; --surface: #19201d; --ink: #e4ebe7; --muted: #9aa8a1;
  --line: #2b3531; --line-strong: #56665e; --band: #161d1a; --ext: #151a18;
  --accent: #66c2d9; --accent-soft: #143139; --on-accent: #0c1214;
  --build: #f2a552; --build-soft: #3a2911;
  --active: #93b1ff; --active-soft: #1b2747;
  --meet: #ff8b7c; --meet-soft: #3c1a16;
  --ok: #82cca5; --ok-soft: #163024; color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #111614; --surface: #19201d; --ink: #e4ebe7; --muted: #9aa8a1;
  --line: #2b3531; --line-strong: #56665e; --band: #161d1a; --ext: #151a18;
  --accent: #66c2d9; --accent-soft: #143139; --on-accent: #0c1214;
  --build: #f2a552; --build-soft: #3a2911;
  --active: #93b1ff; --active-soft: #1b2747;
  --meet: #ff8b7c; --meet-soft: #3c1a16;
  --ok: #82cca5; --ok-soft: #163024; color-scheme: dark; }

body { background: var(--bg); color: var(--ink); font: 15px/1.55 var(--font-ui); }
.wrap { max-width: 1240px; margin: 0 auto; padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 20px 120px; display: grid; gap: 28px; }
a { color: var(--accent); }
code { font-family: var(--font-mono); font-size: 0.88em; }
h1, h2, h3 { text-wrap: balance; margin: 0; line-height: 1.2; }
h2 { font-size: 1.45rem; font-weight: 600; }
h3 { font-size: 1.02rem; font-weight: 600; }
.label, .lvl, .eyebrow { font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-weight: 500; }

.mast { display: grid; gap: 8px; border-bottom: 1px solid var(--line); padding-bottom: 18px; }
.mast h1 { font-size: 1.9rem; font-weight: 600; letter-spacing: -0.01em; }
.mast p { margin: 0; max-width: 70ch; color: var(--muted); }
.mast .eyebrow { color: var(--build); }
.views-nav { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.views-nav a { text-decoration: none; padding: 5px 12px; border: 1px solid var(--line); border-radius: 999px; color: var(--ink); font-size: 0.9rem; }
.views-nav a[aria-current="true"] { background: var(--ink); color: var(--bg); border-color: var(--ink); }

.view { display: grid; gap: 22px; }
.view-intro { display: grid; gap: 6px; max-width: 75ch; }
.view-intro p { margin: 0; }
.keeps { color: var(--muted); font-size: 0.92rem; }
.scroll { overflow-x: auto; min-width: 0; }

.dg-fig { margin: 0; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 14px 14px 18px; display: grid; gap: 10px; min-width: 0; }
.dg-fig figcaption { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; font-weight: 600; }
.cap-note { font-weight: 400; color: var(--muted); font-size: 0.86rem; }
.dg svg { display: block; height: auto; min-width: 560px; margin: 0 auto; }
.dg .graph > polygon { fill: transparent; stroke: none; }
.dg text { fill: var(--ink); font-family: Arial, Helvetica, sans-serif; }
.dg .node path, .dg .node polygon, .dg .node ellipse { fill: var(--surface); stroke: var(--line-strong); }
.dg .node.person path, .dg .node.person polygon { fill: var(--accent-soft); stroke: var(--accent); }
.dg .node.system path, .dg .node.system polygon { fill: var(--accent); stroke: var(--accent); }
.dg .node.system text { fill: var(--on-accent); }
.dg .node.ext path, .dg .node.ext polygon { fill: var(--ext); stroke: var(--line-strong); stroke-dasharray: 4 3; }
.dg .node.data path { fill: var(--band); }
.dg .cluster path, .dg .cluster polygon { fill: var(--band); stroke: var(--line); }
.dg .cluster text { fill: var(--muted); }
.dg .edge path { stroke: var(--line-strong); fill: none; }
.dg .edge polygon { fill: var(--line-strong); stroke: var(--line-strong); }
.dg .edge text { fill: var(--muted); }
.dg .edge.dep path { opacity: 0.75; }
.dg .edge.up path { stroke: var(--meet); }
.dg .edge.up polygon { fill: var(--meet); stroke: var(--meet); }
.dg .edge.up text { fill: var(--meet); font-weight: bold; }
.dg .edge.same path { stroke: var(--build); }
.dg .edge.same polygon { fill: var(--build); stroke: var(--build); }
.dg .edge.same text { fill: var(--build); }
.dg .node.layer text { fill: var(--muted); letter-spacing: 0.06em; }
.dg .node.clickable { cursor: pointer; }
.dg .node.clickable:hover path, .dg .node.clickable:hover polygon,
.dg .node.clickable:focus path, .dg .node.clickable:focus polygon { stroke: var(--accent); stroke-width: 2; }
.dg .node.sel path, .dg .node.sel polygon { stroke: var(--accent); stroke-width: 2.6; fill: var(--accent-soft); }

.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.summary > div { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px; color: var(--muted); font-size: 0.92rem; }
.num { display: block; font-size: 1.8rem; font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; line-height: 1.1; }
.meet-ink { color: var(--meet); }

.meets { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.meet-row { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px; display: grid; gap: 10px; min-width: 0; }
.meet-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.meet-head .comp-link { font-weight: 600; font-size: 1rem; }
.sides { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; }
.side { display: grid; gap: 6px; align-content: start; border-left: 2px solid var(--line); padding-left: 10px; min-width: 0; }
.items { display: flex; flex-wrap: wrap; gap: 4px 8px; font-size: 0.86rem; }
.item { font-family: var(--font-mono); font-size: 0.82rem; color: var(--muted); overflow-wrap: anywhere; }
a.item { color: var(--accent); }
.more { color: var(--muted); font-size: 0.82rem; }

.pill { font-size: 0.8rem; padding: 2px 9px; border-radius: 999px; font-weight: 500; }
.pill.ok { background: var(--ok-soft); color: var(--ok); }
.pill.meet { background: var(--meet-soft); color: var(--meet); }
.chip { display: inline-block; font-family: var(--font-mono); font-size: 0.8rem; padding: 1px 7px; border-radius: 4px;
  border: 1px solid var(--line); color: var(--ink); background: var(--surface); text-decoration: none; white-space: nowrap; }
.chip.building { border-color: var(--build); color: var(--build); background: var(--build-soft); }
.chip.active { border-color: var(--active); color: var(--active); background: var(--active-soft); }
a.chip:hover { border-color: var(--accent); }

.comp-link { font: inherit; color: var(--accent); background: none; border: 0; padding: 0; cursor: pointer; text-align: left; }
.comp-link:hover { text-decoration: underline; }
button:focus-visible, a:focus-visible, summary:focus-visible, .dg .node:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.legend { font-size: 0.88rem; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: baseline; margin: 0; }
.matrix { border-collapse: collapse; background: var(--surface); border: 1px solid var(--line); font-size: 0.9rem; min-width: 100%; }
.matrix th, .matrix td { border-bottom: 1px solid var(--line); padding: 6px 10px; text-align: left; vertical-align: middle; }
.matrix thead th { position: sticky; top: 0; background: var(--surface); font-weight: 500; vertical-align: bottom; }
.matrix th.own { text-align: center; }
.matrix th.own .status { display: block; font-size: 0.7rem; color: var(--muted); font-family: var(--font-ui); }
.matrix tr.group th { background: var(--band); font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-weight: 500; }
.matrix tbody th { font-weight: 400; white-space: nowrap; }
.m { text-align: center !important; font-size: 1rem; font-variant-numeric: tabular-nums; }
.m.building { color: var(--build); }
.m.active { color: var(--active); }
.m.quiet { color: var(--line-strong); }
.flag { font-size: 0.7rem; margin-right: 6px; vertical-align: 1px; }
.flag.meet { color: var(--meet); }
.flag.ok { color: var(--ok); }
.planned { display: flex; flex-wrap: wrap; gap: 4px; min-width: 120px; }

.stages { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; counter-reset: none; }
.stage { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 14px; display: grid; gap: 10px; align-content: start; min-width: 0; }
.stage.cross { grid-column: 1 / -1; background: var(--band); }
.stage h3 { display: flex; align-items: center; gap: 8px; }
.step { display: inline-grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; background: var(--ink); color: var(--bg); font-size: 0.78rem; font-variant-numeric: tabular-nums; }
.stage-sum { margin: 0; color: var(--muted); font-size: 0.88rem; }
.stage-comps { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.stage.cross .stage-comps { grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
.stage-comps .comp-link { display: flex; align-items: baseline; gap: 8px; color: var(--ink); width: 100%; padding: 3px 0; }
.stage-comps .comp-link:hover { color: var(--accent); }
.repo { font-family: var(--font-mono); font-size: 0.68rem; color: var(--muted); border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; flex: none; }
.comp-link.stores .repo { border-style: dashed; }
.stage-work { display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: baseline; font-size: 0.86rem; }
.none { color: var(--muted); }
details summary { cursor: pointer; font-size: 0.88rem; color: var(--muted); }
.adrs { margin: 8px 0 0; padding-left: 0; list-style: none; display: grid; gap: 4px; font-size: 0.84rem; }
.adr-n { font-family: var(--font-mono); color: var(--muted); }
.adrs em { color: var(--muted); font-style: normal; font-size: 0.78rem; }

.method { border-top: 1px solid var(--line); padding-top: 20px; display: grid; gap: 12px; }
.method h2 { font-size: 1.1rem; }
.facts { margin: 0; display: grid; gap: 10px; }
.facts > div { display: grid; grid-template-columns: 110px 1fr; gap: 12px; }
.facts dt { font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); padding-top: 3px; }
.facts dd { margin: 0; max-width: 85ch; min-width: 0; }
.gen { color: var(--muted); font-size: 0.85rem; margin: 0; }
@media (max-width: 560px) { .facts > div { grid-template-columns: 1fr; gap: 2px; } }

.drawer { position: fixed; z-index: 30; top: 0; right: 0; bottom: 0; width: min(420px, 100vw); background: var(--surface);
  border-left: 1px solid var(--line); box-shadow: -8px 0 24px rgba(0,0,0,0.12); overflow-y: auto;
  padding: calc(18px + env(safe-area-inset-top, 0px)) calc(18px + env(safe-area-inset-right, 0px)) calc(90px + env(safe-area-inset-bottom, 0px)) 18px; display: grid; gap: 14px; align-content: start; }
.drawer h2 { font-size: 1.3rem; }
.drawer .close { justify-self: end; font: inherit; font-size: 0.85rem; background: none; border: 1px solid var(--line); border-radius: 999px; padding: 3px 12px; color: var(--ink); cursor: pointer; }
.drawer section { display: grid; gap: 6px; }
.drawer ul { margin: 0; padding-left: 18px; display: grid; gap: 3px; font-size: 0.88rem; }
.drawer .paths li, .drawer .ev { font-family: var(--font-mono); font-size: 0.8rem; overflow-wrap: anywhere; }
.drawer .meta { color: var(--muted); font-size: 0.9rem; margin: 0; }
.drawer .wk { list-style: none; padding: 0; }
.drawer .wk li { display: grid; gap: 2px; border-left: 2px solid var(--line); padding-left: 8px; }
.drawer .st { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; }
.st.building { color: var(--build); } .st.active { color: var(--active); } .st.planned, .st.scope, .st.waiting, .st.paused, .st.stale { color: var(--muted); }
@media (max-width: 640px) {
  .drawer { top: auto; left: 0; width: 100%; max-height: 72vh; border-left: 0; border-top: 1px solid var(--line); border-radius: 12px 12px 0 0;
    padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
}

.proto-bar { position: fixed; z-index: 40; left: 50%; transform: translateX(-50%); bottom: calc(16px + env(safe-area-inset-bottom, 0px));
  display: flex; align-items: center; gap: 4px; padding: 5px; border-radius: 999px; background: #1b1f24; color: #f5f7f8;
  box-shadow: 0 6px 22px rgba(0,0,0,0.35); font: 500 0.88rem/1 var(--font-ui); border: 1px solid #3a4048; }
.proto-bar button { font: inherit; background: transparent; color: inherit; border: 0; border-radius: 999px; width: 34px; height: 34px; cursor: pointer; }
.proto-bar button:hover { background: #2f353d; }
.proto-bar .now { padding: 0 10px; white-space: nowrap; }
.proto-bar .now b { color: #ffc46b; font-weight: 600; margin-right: 6px; }
@media (prefers-reduced-motion: no-preference) { .view { animation: fade 160ms ease-out; } @keyframes fade { from { opacity: 0.6; } to { opacity: 1; } } }
"""

SCRIPT = r"""
(function () {
  const DATA = JSON.parse(document.getElementById('arch-data').textContent);
  const VIEWS = __VIEWS__;
  const keys = VIEWS.map(v => v[0]);
  const views = document.querySelectorAll('.view');
  const now = document.getElementById('proto-now');
  const navLinks = document.querySelectorAll('.views-nav a');
  let current = 'c4';

  function show(key) {
    if (!keys.includes(key)) return;
    current = key;
    views.forEach(v => { v.hidden = v.dataset.view !== key; });
    const i = keys.indexOf(key);
    now.innerHTML = '<b>' + String.fromCharCode(65 + i) + '</b>' + VIEWS[i][1];
    navLinks.forEach(a => a.setAttribute('aria-current', a.getAttribute('href') === '#' + key ? 'true' : 'false'));
  }
  function step(d) {
    const i = (keys.indexOf(current) + d + keys.length) % keys.length;
    location.hash = keys[i];
  }
  window.addEventListener('hashchange', () => show(location.hash.slice(1)));
  document.getElementById('proto-prev').addEventListener('click', () => step(-1));
  document.getElementById('proto-next').addEventListener('click', () => step(1));
  document.addEventListener('keydown', e => {
    const t = e.target;
    if (t && (t.closest('input, textarea, select, [contenteditable]'))) return;
    if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === 'Escape') closeDrawer();
  });

  // Details drawer
  const drawer = document.getElementById('drawer');
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const shortKey = k => k.startsWith('branch:') ? k.split(':').slice(2).join(':') :
    (k.split('#')[0] === 'opengwasdb' ? 'engine #' : 'stores #') + k.split('#')[1];
  const issueUrl = k => 'https://github.com/opengwas/' + k.split('#')[0] + '/issues/' + k.split('#')[1];
  const ghPath = (repo, p) => 'https://github.com/opengwas/' + repo + '/tree/main/' + p;
  const STATE = {building: 'Branch changes it', active: 'Open ticket cites it', planned: 'Planned epic cites it',
    scope: 'In an epic’s scope', waiting: 'Cited by a ticket waiting on a blocker', paused: 'Paused epic cites it', stale: 'Branch of a closed issue'};
  function compName(id) { return DATA.components[id] ? DATA.components[id].name : id; }
  function openComp(id) {
    const c = DATA.components[id];
    if (!c) return;
    document.querySelectorAll('.dg .node.sel').forEach(n => n.classList.remove('sel'));
    document.querySelectorAll('#c-' + CSS.escape(id)).forEach(n => n.classList.add('sel'));
    const byOwner = {};
    c.work.forEach(w => { (byOwner[w.owner] = byOwner[w.owner] || []).push(w); });
    const rank = {building: 0, active: 1, waiting: 2, planned: 3, scope: 4, paused: 5, stale: 6};
    const work = Object.keys(byOwner).sort().map(k => {
      const o = DATA.owners[k] || {title: k, status: ''};
      const ws = byOwner[k].sort((a, b) => rank[a.state] - rank[b.state]);
      const lines = ws.slice(0, 8).map(w => {
        const item = w.item.startsWith('branch:') ? 'branch ' + w.item.slice(7) : shortKey(w.item);
        return '<span class="st ' + w.state + '">' + esc(STATE[w.state] || w.state) + '</span> <span class="ev">' + esc(item) + ' · ' + esc(w.file || w.branch || '') + '</span>';
      });
      if (ws.length > 8) lines.push('<span class="ev">+' + (ws.length - 8) + ' more</span>');
      const head = o.url ? '<a href="' + esc(o.url) + '" target="_blank" rel="noopener">' + esc(shortKey(k)) + '</a>' : esc(shortKey(k));
      return '<li><span>' + head + ' ' + esc(o.title) + ' <span class="meta">(' + esc(o.status) + ')</span></span>' + lines.join('<br>') + '</li>';
    }).join('');
    const imp = list => list.length ? '<ul>' + list.map(([id, n]) => '<li><button class="comp-link" type="button" data-comp="' + esc(id) + '">' + esc(compName(id)) + '</button> <span class="meta">' + n + '</span></li>').join('') + '</ul>' : '<p class="meta">None.</p>';
    const adrs = c.adrs.length ? '<ul>' + c.adrs.map(a => '<li><a href="' + esc(a.url) + '" target="_blank" rel="noopener">' + (a.repo === 'opengwasdb' ? '' : 'stores ') + esc(a.title) + '</a> <span class="meta">' + esc(a.via === 'code' ? 'cites its code' : a.via === 'title' ? 'term in title' : 'term in text') + (a.status !== 'current' ? ', ' + esc(a.status) : '') + '</span></li>').join('') + '</ul>' : '<p class="meta">No ADR cites its code or names its terms.</p>';
    drawer.innerHTML =
      '<button class="close" type="button" id="drawer-close">Close</button>' +
      '<div><div class="label">' + esc(c.container) + ' · ' + esc(c.repo) + '</div><h2>' + esc(c.name) + '</h2></div>' +
      '<p class="meta">' + esc(c.summary) + '</p>' +
      '<section><div class="label">Code</div><p class="meta">' + c.counts.files + ' files, ' + c.counts.lines.toLocaleString() + ' lines of Python. Stages: ' + c.stages.map(s => esc(DATA.stages[s] || s)).join(', ') + '.</p><ul class="paths">' + c.paths.map(p => '<li><a href="' + esc(ghPath(c.repo, p)) + '" target="_blank" rel="noopener">' + esc(p) + '</a></li>').join('') + '</ul></section>' +
      '<section><div class="label">Work on it now</div>' + (work ? '<ul class="wk">' + work + '</ul>' : '<p class="meta">No open ticket cites its code and no live branch changes it.</p>') + '</section>' +
      '<section><div class="label">Imports (component · imports)</div>' + imp(c.out) + '</section>' +
      '<section><div class="label">Imported by</div>' + imp(c.in) + '</section>' +
      '<section><div class="label">ADRs</div>' + adrs + '</section>';
    drawer.hidden = false;
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    drawer.focus();
  }
  function closeDrawer() {
    drawer.hidden = true;
    document.querySelectorAll('.dg .node.sel').forEach(n => n.classList.remove('sel'));
  }
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-comp]');
    if (btn) { openComp(btn.dataset.comp); return; }
    const node = e.target.closest('.dg .node.clickable');
    if (node && node.id.startsWith('c-')) openComp(node.id.slice(2));
  });
  document.querySelectorAll('.dg .node.clickable').forEach(n => {
    n.setAttribute('tabindex', '0');
    n.setAttribute('role', 'button');
    n.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openComp(n.id.slice(2)); } });
  });
  show(keys.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'c4');
})();
"""


def render(data: dict, epic: str | None = None) -> str:
    epic_link = ""
    if epic:
        epic_link = f' for <a href="{esc(issue_url(epic))}" target="_blank" rel="noopener">{esc(short(epic))}</a>'
    nav = "".join(f'<a href="#{k}">{esc(name)}</a>' for k, name in VIEWS)
    blob = json.dumps(page_data(data), separators=(",", ":")).replace("</", "<\\/")
    script = SCRIPT.replace("__VIEWS__", json.dumps(VIEWS))
    return f'''<title>OpenGWASDB Architecture Map</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{STYLE}</style>
<div class="wrap">
  <header class="mast">
    <span class="eyebrow">Prototype for review{epic_link}</span>
    <h1>OpenGWASDB architecture map</h1>
    <p>One model of the engine and the registry, drawn three ways from the same data: the code at <code>main</code>, the project board and the branches in flight. Compare the views and say which helps you find your way. Switch with the bar at the bottom, the links below, or the ← → keys.</p>
    <nav class="views-nav" aria-label="Views">{nav}</nav>
  </header>
  {c4_view(data)}
  {work_view(data)}
  {pipeline_view(data)}
  {method(data)}
</div>
<aside class="drawer" id="drawer" tabindex="-1" aria-label="Component details" hidden></aside>
<div class="proto-bar" role="navigation" aria-label="Prototype views">
  <button type="button" id="proto-prev" aria-label="Previous view">←</button>
  <span class="now" id="proto-now" aria-live="polite"><b>A</b>C4 views</span>
  <button type="button" id="proto-next" aria-label="Next view">→</button>
</div>
<script type="application/json" id="arch-data">{blob}</script>
<script>{script}</script>'''
