"""The component panel on the architecture pages (#435): click a component, see what the map knows.

On Pipeline, Structure and health and the Work map, a component's box in the
engine and registry diagrams and its name in the page's lists and tables are
links to `#component-<id>`. Following one opens a panel beside the page with the
component's code (its size, stages and the paths it claims, linked to GitHub at
the commit read), the work on it now, its imports each way and its ADRs. The
link is an address, so a component's details can be shared, and Back closes
the panel. The architecture map prototype (#417) had this panel; this is that
panel on the site.

Each page embeds the panel's data as JSON, which a short inline script reads,
with no library and nothing fetched. The data is built here from
`architecture.json` and `work.json` alone, and only from the fields the panel
shows: the collected files hold host paths, which are never published (#391
review, item 12). Every fact goes through `json.dumps` and then has `<`, `>`,
`&` and the backtick written as `\\u` escapes, so none can become markup, end
the `<script>` it sits in, or close the page's raw block.

What a collector could not measure is unknown, never "none": without
`work.json`, or for the engine's components when `work.json` says the engine
was not measured, a component's `work` is `null` with a `work_note` saying why.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Callable, Mapping, Sequence

from project_site import pages, paths

Document = Mapping[str, Any]

# The link target that opens a component's panel: `#component-<id>`.
ANCHOR = "component-"

# A component id as the models write them. Only such an id becomes a link, so an
# id is never markup and never needs escaping in an address.
_ID = re.compile(r"[a-z][a-z0-9_]*\Z")

REPOSITORY_URL = {"registry": paths.REGISTRY_URL, "engine": paths.ENGINE_URL}

# How the architecture collector placed an ADR on a component.
PLACED_BY = {"code": "cites its code", "title": "names it in its title"}

# What JSON cannot hold safely inside a page: a tag, an entity, a backtick that
# would close the raw block, and the two line separators JavaScript once refused.
_SAFE_JSON = str.maketrans({
    "<": "\\u003c", ">": "\\u003e", "&": "\\u0026", "`": "\\u0060", " ": "\\u2028", " ": "\\u2029",
})


def linkable(cid: Any) -> bool:
    """Whether `cid` has the form of a component id, and so can be a panel's address."""
    return isinstance(cid, str) and bool(_ID.match(cid))


class Panel:
    """The panel for one page: the links that open it, and the block that carries it.

    `states` is the Work map's states with what each means, in its order, and
    `size` says a component's size the way the pages do.
    """

    def __init__(
        self,
        documents: Mapping[str, Document | None],
        *,
        states: Sequence[tuple[str, str]],
        size: Callable[[Mapping[str, Any]], str],
    ) -> None:
        self.data = _data(documents, states, size)

    def link(self, name: Any, cid: Any) -> str:
        """`name` as a link that opens `cid`'s panel, or as literal text when there is no panel for it."""
        if self.data is not None and linkable(cid) and cid in self.data["components"]:
            return pages._link(name, f"#{ANCHOR}{cid}")
        return pages.escape(" ".join(str(name).split()))

    def block(self) -> list[str]:
        """The page's raw HTML block: the panel, its data, its style and its script; nothing without data."""
        if self.data is None:
            return []
        blob = json.dumps(self.data, ensure_ascii=False, separators=(",", ":")).translate(_SAFE_JSON)
        return [
            "```{=html}",
            '<aside id="arch-panel" class="arch-panel" tabindex="-1" aria-label="Component details" hidden></aside>',
            f'<script type="application/json" id="arch-panel-data">{blob}</script>',
            f"<style>{STYLE}</style>",
            f"<script>{SCRIPT}</script>",
            "```",
            "",
        ]


def _data(
    documents: Mapping[str, Document | None],
    states: Sequence[tuple[str, str]],
    size: Callable[[Mapping[str, Any]], str],
) -> dict[str, Any] | None:
    arch = pages.available(documents, "architecture")
    if arch is None:
        return None
    model = arch.get("model") or {}
    containers = {c.get("id"): c.get("name") for c in model.get("containers") or [] if isinstance(c, Mapping)}
    stages = {s.get("id"): s.get("name") for s in model.get("stages") or [] if isinstance(s, Mapping)}
    components = [c for c in arch.get("components") or [] if isinstance(c, Mapping) and linkable(c.get("id"))]
    ids = {c["id"] for c in components}

    out: dict[str, list[list[Any]]] = defaultdict(list)
    into: dict[str, list[list[Any]]] = defaultdict(list)
    for e in arch.get("edges") or []:
        if isinstance(e, Mapping) and e.get("from") in ids and e.get("to") in ids:
            out[e["from"]].append([e["to"], e["imports"], bool(e.get("upward"))])
            into[e["to"]].append([e["from"], e["imports"], bool(e.get("upward"))])

    adrs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for adr in arch.get("adrs") or []:
        if not isinstance(adr, Mapping):
            continue
        for cid, via in (adr.get("components") or {}).items():
            adrs[cid].append({
                "title": adr.get("title"), "url": adr.get("url"), "repo": adr.get("repo"),
                "status": adr.get("status"), "via": PLACED_BY.get(via, via),
            })

    engine_model = bool((arch.get("engine") or {}).get("model"))
    work, owners = _work(pages.available(documents, "work"), components, [state for state, _ in states])
    found: dict[str, Any] = {}
    for c in components:
        repo = c.get("repo")
        commit = (arch.get(repo) or {}).get("commit") or (arch.get(repo) or {}).get("ref") or "main"
        base = REPOSITORY_URL.get(repo)
        found[c["id"]] = {
            "name": c.get("name"),
            "summary": c.get("summary"),
            "repo": repo,
            "container": containers.get(c.get("container"), c.get("container")),
            "layer": c.get("layer"),
            "size": size(c),
            "stages": [stages.get(s, s) for s in c.get("stages") or []],
            "paths": [[p, f"{base}/tree/{commit}/{p}" if base else None] for p in c.get("paths") or []],
            "out": sorted(out[c["id"]], key=lambda row: -row[1]),
            "in": sorted(into[c["id"]], key=lambda row: -row[1]),
            "imports_note": None if engine_model or repo != "registry" else (
                "The engine has no architecture model at the ref read, so its imports into the engine "
                "cannot be placed on the engine's components."
            ),
            "adrs": adrs[c["id"]],
            **work[c["id"]],
        }
    return {"components": found, "owners": owners, "states": dict(states)}


def _work(
    work: Document | None, components: list[Mapping[str, Any]], order: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Each component's work by owner, or unknown with the reason; and the owners named."""
    if work is None:
        note = "The work collection could not be read, so the work on it is unknown."
        return {c["id"]: {"work": None, "work_note": note} for c in components}, {}
    unmeasured = work.get("unmeasured") or {}
    by_component: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for s in work.get("signals") or []:
        if isinstance(s, Mapping):
            by_component[s.get("component")][s.get("owner")].append(
                {"state": s.get("state"), "item": s.get("item"), "what": list(s.get("what") or [])}
            )
    rank = {state: index for index, state in enumerate(order)}
    found: dict[str, dict[str, Any]] = {}
    named: set[str] = set()
    for c in components:
        reason = unmeasured.get(c.get("repo"))
        if reason:
            found[c["id"]] = {"work": None, "work_note": f"Work on the {c.get('repo')}'s side was not measured: {reason}."}
            continue
        groups = []
        for owner, items in sorted(by_component[c["id"]].items(), key=lambda pair: str(pair[0])):
            named.add(owner)
            items.sort(key=lambda i: (rank.get(i["state"], len(rank)), str(i["item"])))
            groups.append({"owner": owner, "items": items})
        found[c["id"]] = {"work": groups, "work_note": None}
    owners = {
        o["key"]: {"title": o.get("title"), "url": o.get("url"), "status": o.get("status")}
        for o in work.get("owners") or [] if isinstance(o, Mapping) and o.get("key") in named
    }
    return found, owners


STYLE = """
.arch-panel { position: fixed; z-index: 1060; top: 0; right: 0; bottom: 0; width: min(440px, 100vw);
  overflow-y: auto; padding: 1.25rem 1.25rem 3rem; background: var(--bs-body-bg, #fff); color: var(--bs-body-color, #212529);
  border-left: 1px solid var(--bs-border-color, #dee2e6); box-shadow: -8px 0 24px rgba(0, 0, 0, 0.14); font-size: 0.92rem; }
.arch-panel[hidden] { display: none; }
.arch-panel h2 { margin: 0.1rem 0 0.5rem; font-size: 1.35rem; border: 0; padding: 0; }
.arch-panel h2:focus { outline: none; }
.arch-panel h3 { margin: 1.1rem 0 0.35rem; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--bs-secondary-color, #6c757d); border: 0; padding: 0; }
.arch-panel ul { margin: 0; padding-left: 1.1rem; }
.arch-panel li { margin: 0.15rem 0; }
.arch-panel-close { float: right; font: inherit; font-size: 0.85rem; background: none; cursor: pointer;
  border: 1px solid var(--bs-border-color, #dee2e6); border-radius: 999px; padding: 0.1rem 0.8rem; color: inherit; }
.arch-panel-note { color: var(--bs-secondary-color, #6c757d); }
.arch-panel-path, .arch-panel-what { font-family: var(--bs-font-monospace, monospace); font-size: 0.8rem; overflow-wrap: anywhere; }
.arch-panel-work > li { list-style: none; margin-left: -1.1rem; padding-left: 0.6rem; border-left: 2px solid var(--bs-border-color, #dee2e6); }
.arch-panel-state { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; }
.arch-panel-state.building, .arch-panel-state.active { font-weight: 600; color: var(--bs-primary, #2780e3); }
.arch-panel-up { color: #c0392b; }
svg .arch-node a:hover path, svg .arch-node a:hover polygon,
svg .arch-node a:focus path, svg .arch-node a:focus polygon { stroke: var(--bs-primary, #2780e3); stroke-width: 2.5px; }
svg .arch-node.arch-selected path, svg .arch-node.arch-selected polygon { stroke: var(--bs-primary, #2780e3); stroke-width: 3px; }
@media (max-width: 640px) {
  .arch-panel { top: auto; left: 0; width: 100%; max-height: 72vh; border-left: 0;
    border-top: 1px solid var(--bs-border-color, #dee2e6); border-radius: 12px 12px 0 0; }
}
"""

# Draws the panel from the page's data. Facts are set as text, never as HTML.
SCRIPT = r"""
(function () {
  var source = document.getElementById('arch-panel-data');
  var panel = document.getElementById('arch-panel');
  if (!source || !panel) return;
  var DATA = JSON.parse(source.textContent);
  var PREFIX = '#component-';
  var opener = null;

  function el(tag, attrs) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (attrs[key] !== null && attrs[key] !== undefined) node.setAttribute(key, attrs[key]);
    });
    Array.prototype.slice.call(arguments, 2).forEach(function add(child) {
      if (child === null || child === undefined || child === '') return;
      if (Array.isArray(child)) { child.forEach(add); return; }
      node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return node;
  }
  function note(text) { return el('p', {'class': 'arch-panel-note'}, text); }
  function list(items, attrs) { return el('ul', attrs || {}, items.map(function (item) { return el('li', {}, item); })); }
  function component(id) {
    var known = DATA.components[id];
    return known ? el('a', {href: PREFIX + id}, known.name) : el('span', {}, id);
  }
  function imports(rows, none) {
    if (!rows.length) return note(none);
    return list(rows.map(function (row) {
      return [component(row[0]), ' ', el('span', {'class': row[2] ? 'arch-panel-up' : 'arch-panel-note'},
        row[1] + (row[1] === 1 ? ' import' : ' imports') + (row[2] ? ', against the layering' : ''))];
    }));
  }
  function work(c) {
    if (c.work === null) return note(c.work_note);
    if (!c.work.length) return note('No open ticket or epic cites its code, and no live branch changes it.');
    return list(c.work.map(function (group) {
      var owner = DATA.owners[group.owner] || {};
      return [
        owner.url ? el('a', {href: owner.url}, group.owner) : el('span', {}, group.owner),
        owner.title && owner.title !== group.owner ? ' ' + owner.title : '',
        owner.status ? el('span', {'class': 'arch-panel-note'}, ' (' + owner.status + ')') : '',
        list(group.items.map(function (item) {
          return [el('span', {'class': 'arch-panel-state ' + item.state, title: DATA.states[item.state] || null}, item.state),
            ' ', item.item, item.what.length ? el('span', {'class': 'arch-panel-what'}, ' · ' + item.what.join(', ')) : ''];
        }))
      ];
    }), {'class': 'arch-panel-work'});
  }
  function draw(id) {
    var c = DATA.components[id];
    var stages = c.stages.length ? ' Stages: ' + c.stages.join(', ') + '.' : '';
    while (panel.firstChild) panel.removeChild(panel.firstChild);
    panel.appendChild(el('div', {},
      el('button', {type: 'button', 'class': 'arch-panel-close'}, 'Close'),
      note(c.container + ' · ' + c.repo + (c.layer !== null ? ' · layer ' + c.layer : '')),
      el('h2', {id: 'arch-panel-title', tabindex: '-1'}, c.name),
      el('p', {}, c.summary),
      el('h3', {}, 'Code'),
      el('p', {}, c.size + '.' + stages),
      c.paths.length ? list(c.paths.map(function (p) {
        return p[1] ? el('a', {href: p[1], 'class': 'arch-panel-path'}, p[0]) : el('span', {'class': 'arch-panel-path'}, p[0]);
      })) : '',
      el('h3', {}, 'Work on it now'), work(c),
      el('h3', {}, 'Imports'), c.imports_note ? note(c.imports_note) : '', imports(c.out, 'It imports no other component.'),
      el('h3', {}, 'Imported by'), imports(c['in'], 'No other component imports it.'),
      el('h3', {}, 'ADRs'),
      c.adrs.length ? list(c.adrs.map(function (a) {
        return [el('a', {href: a.url}, a.title), el('span', {'class': 'arch-panel-note'},
          ' (' + a.repo + '; ' + a.via + (a.status && a.status !== 'current' ? '; ' + a.status : '') + ')')];
      })) : note('No ADR cites its code or names it in its title.')
    ));
    panel.setAttribute('aria-labelledby', 'arch-panel-title');
  }
  function select(id) {
    Array.prototype.forEach.call(document.querySelectorAll('.arch-node.arch-selected'), function (n) { n.classList.remove('arch-selected'); });
    if (id) Array.prototype.forEach.call(document.querySelectorAll('.arch-c-' + id), function (n) { n.classList.add('arch-selected'); });
  }
  function open(id, focus) {
    if (!Object.prototype.hasOwnProperty.call(DATA.components, id)) return false;
    draw(id);
    panel.hidden = false;
    select(id);
    if (focus) document.getElementById('arch-panel-title').focus();
    return true;
  }
  function close() {
    if (panel.hidden) return;
    panel.hidden = true;
    select(null);
    if (location.hash.indexOf(PREFIX) === 0) history.replaceState(null, '', location.pathname + location.search);
    if (opener && document.contains(opener)) opener.focus();
    opener = null;
  }
  function fromAddress() {
    if (location.hash.indexOf(PREFIX) === 0 && open(decodeURIComponent(location.hash.slice(PREFIX.length)), false)) return;
    panel.hidden = true;
    select(null);
  }
  document.addEventListener('click', function (event) {
    if (event.target.closest('.arch-panel-close')) { close(); return; }
    var link = event.target.closest('a');
    if (!link) return;
    var href = link.getAttribute('href') || link.getAttribute('xlink:href') || '';
    if (href.indexOf(PREFIX) !== 0 || !Object.prototype.hasOwnProperty.call(DATA.components, href.slice(PREFIX.length))) return;
    event.preventDefault();
    if (!panel.contains(link)) opener = link;
    history.pushState(null, '', href);
    open(href.slice(PREFIX.length), true);
  });
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape') close(); });
  window.addEventListener('popstate', fromAddress);
  window.addEventListener('hashchange', fromAddress);
  fromAddress();
})();
"""
