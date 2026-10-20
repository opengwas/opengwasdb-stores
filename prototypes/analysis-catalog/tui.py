"""PROTOTYPE -- throwaway TUI over catalog.py. Run: python3 prototypes/analysis-catalog/tui.py"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import catalog as C  # noqa: E402
import scenario as S  # noqa: E402

B, D, R, Y, G, RED = "\x1b[1m", "\x1b[2m", "\x1b[0m", "\x1b[33m", "\x1b[32m", "\x1b[31m"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def fit(s: str, width: int) -> str:
    """Truncate to a visible width, keeping ANSI codes intact."""
    out, n, i = [], 0, 0
    while i < len(s):
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        if n >= width - 1 and len(_ANSI.sub("", s[i:])) > 1:
            out.append("…")
            break
        out.append(s[i])
        n += 1
        i += 1
    return "".join(out) + R

EVENTS = {
    "1": ("build MVP 12..16", S.ev_build_mvp),
    "2": ("rebuild OGS-00011", S.ev_rebuild_11),
    "3": ("status catches up", S.ev_status_catch_up),
    "4": ("erratum FinnGen BMI", S.ev_erratum),
    "5": ("supersede OGS-00006", S.ev_supersede_06),
    "6": ("withdraw OGS-00009", S.ev_withdraw_09),
    "7": ("add GTEx eQTL", S.ev_gtex),
    "8": ("add GWAS Cat. MVP", S.ev_gwascat_mvp),
}


class App:
    def __init__(self):
        t = time.perf_counter()
        self.world = S.seed()
        self.seed_s = time.perf_counter() - t
        self.watch = S.watched(self.world)
        self.policy = C.Policy()
        self.history = []
        self.cache = {}           # (store, rows ids) -> Fragment
        self.rederived = []
        self.note = ""
        self.cat = None
        self.cat, self.build_ms = self.rebuild()
        self.last_diff = None

    def fragments(self):
        out, self.rederived = [], []
        for sid, store in self.world.stores.items():
            key = (sid, id(store), id(self.world.bundles[sid]), id(self.world.built[sid]))
            f = self.cache.get(key)
            if f is None:
                t = time.perf_counter()
                f = C.make_fragment(store, list(self.world.bundles[sid]), self.world.built[sid])
                self.cache[key] = f
                self.rederived.append(f"{sid[-2:]}:{(time.perf_counter() - t) * 1000:.0f}ms")
            out.append(f)
        return out

    def rebuild(self):
        frags = self.fragments()
        t = time.perf_counter()
        cat = C.build_catalog(frags, self.policy)
        return cat, (time.perf_counter() - t) * 1000

    def apply(self, world=None, policy=None, note=""):
        self.history.append((self.world, self.policy, self.cat, self.build_ms, self.last_diff, self.note))
        if world is not None:
            self.world = world
        if policy is not None:
            self.policy = policy
        old = self.cat
        self.cat, self.build_ms = self.rebuild()
        self.last_diff = C.diff(old, self.cat)
        self.note = note or self.world.event

    def undo(self):
        if self.history:
            self.world, self.policy, self.cat, self.build_ms, self.last_diff, self.note = self.history.pop()
            self.rederived = []

    def bench(self, k=100):
        frags = self.fragments()
        t = time.perf_counter()
        big = []
        for i in range(k):
            for f in frags:
                s = replace(f.store, store_id=f"{f.store.store_id}x{i}")
                cs = tuple(replace(c, store_id=s.store_id, analysis_id=f"{c.analysis_id}~{i}") for c in f.copies)
                big.append(C.Fragment(s, cs, f.findings, f"{f.digest}{i}"))
        t_make = time.perf_counter() - t
        t = time.perf_counter()
        cat = C.build_catalog(big, self.policy)
        t_fold = time.perf_counter() - t
        self.note = (f"benchmark x{k}: {cat.n_copies:,} copies, {len(cat.entries):,} Analyses, {len(big):,} stores -> "
                     f"full fold {t_fold:.1f}s in pure Python (synthesising inputs took {t_make:.1f}s)")
        self.last_diff = None

    # -- rendering ---------------------------------------------------------

    def frame(self):
        w = shutil.get_terminal_size((140, 45)).columns
        cat, p = self.cat, self.policy
        out = []
        add = out.append
        listed = cat.listed()
        add(f"{B}PROTOTYPE · analysis catalog{R}  {D}version {cat.version} · fold {self.build_ms:.0f} ms over "
            f"{len(cat.stores)} fragments · re-derived: {' '.join(self.rederived) or 'none'}{R}")
        add(f"{B}policy{R}  servable_by={Y}{p.servable_by}{R}  trial={Y}{'included' if p.include_trial else 'excluded'}{R}"
            f"  prefer={Y}{p.prefer}{R}  identity={Y}{p.identity}{R}"
            f"  withdrawal_propagates={Y}{p.withdrawal_propagates}{R}")
        add("")

        # stores, two columns
        default_for = {}
        for e in listed.values():
            default_for[e.default] = default_for.get(e.default, 0) + 1
        n_copies = {}
        for e in cat.entries.values():
            for c in e.copies:
                n_copies[c.store_id] = n_copies.get(c.store_id, 0) + 1
        cells = []
        for sid, s in sorted(cat.stores.items()):
            flag = (f"{D}trial{R}" if s.trial else "     ")
            comp = "cmp" if s.completion_state == "reference_completed" else "obs"
            st = s.status
            col = G if st in C.SERVED_STATUSES else (RED if st in C.RETIRED_STATUSES else Y)
            built = "disk" if s.built else f"{D}----{R}"
            cells.append(f"{sid[-5:]} {s.label[:22]:<22} {col}{st:<10}{R} {built} {comp} {flag} "
                         f"{n_copies.get(sid, 0):>5} {B}{default_for.get(sid, 0):>5}{R}")
        hdr = f"{D}store label                  status     disk  lay       copy  dflt{R}"
        half = (len(cells) + 1) // 2
        add(f"{B}STORES{R}  {hdr}    {hdr}")
        for i in range(half):
            right = cells[i + half] if i + half < len(cells) else ""
            add(f"        {cells[i]}    {right}")
        add("")

        unavailable = {}
        for e in cat.entries.values():
            if e.default is None:
                unavailable[e.why] = unavailable.get(e.why, 0) + 1
        top = ", ".join(f"{v:,} {k.removeprefix('unavailable: ')}" for k, v in sorted(unavailable.items(), key=lambda x: -x[1])[:4])
        add(f"{B}CATALOG{R}  {B}{len(listed):,}{R} Analyses listed  ·  {len(cat.entries) - len(listed):,} unavailable"
            f" {D}({top}){R}  ·  {cat.n_copies:,} copies")
        kinds = {}
        for f in cat.findings:
            kinds.setdefault(f.kind, []).append(f)
        for kind, fs in kinds.items():
            parts = " ".join(f"{f.store_id[-2:] if f.store_id != '*' else ''}({f.n:,})" for f in fs)
            add(f"  {Y}{kind:<22}{R} {parts[: w - 26]}")
        add("")

        add(f"{B}LAST{R}  {self.note[: w - 6]}")
        if self.last_diff is not None:
            add(f"      {B}{self.last_diff}{R}  {D}{'  '.join(self.last_diff.examples)[: w - 70]}{R}")
        add("")

        by_aid = {}
        for e in cat.entries.values():
            for c in e.copies:
                by_aid.setdefault(c.analysis_id, []).append(e)
        add(f"{B}WATCH{R}  {D}● default  · eligible  ✗ ineligible(reason)   * bundle overrode store metadata{R}")
        for aid in self.watch:
            for e in dict((id(x), x) for x in by_aid.get(aid, [])).values():
                label = (e.default_copy() or e.copies[0]).get("analysis_label")[:20]
                inel = dict(e.ineligible)
                cs = []
                for c in sorted(e.copies, key=lambda c: c.store_id):
                    tag = c.store_id[-2:] + ("*" if c.corrected else "")
                    if c.store_id == e.default:
                        cs.append(f"{G}●{tag}{R}")
                    elif c.store_id in inel:
                        cs.append(f"{D}✗{tag}({inel[c.store_id]}){R}")
                    else:
                        cs.append(f"·{tag}")
                extra = ""
                if len(e.collections) > 1:
                    extra += f" {RED}spans {'+'.join(e.collections)}{R}"
                if e.disagree:
                    extra += f" {Y}differ: {','.join(e.disagree)}{R}"
                dc = e.default_copy()
                ont = dc.get("trait_ontology_id") if dc else ""
                head = f"  {e.key[:38]:<38} {(f"{label:<20}" if label else f"{D}{'(no label)':<20}{R}")} {D}{ont[:13]:<13}{R} "
                why = f"→ {e.default[-5:] if e.default else '∅'} {D}{e.why}{R}"
                add(fit(f"{head}{' '.join(cs)}  {why}{extra}", w))
            if aid not in by_aid:
                add(f"  {aid[:38]:<38} {D}(no copy in any built store){R}")
        add("")
        evs = [f"{B}[{k}]{R}{D}{v[0]}{R}" for k, v in EVENTS.items()]
        add(f"{D}events{R}  " + "  ".join(evs[:4]))
        add(f"        " + "  ".join(evs[4:]))
        add(f"{D}policy{R}  {B}[s]{R}{D}servable_by{R}  {B}[t]{R}{D}trial{R}  {B}[c]{R}{D}prefer completion{R}  "
            f"{B}[i]{R}{D}identity key{R}  {B}[w]{R}{D}withdrawal propagates{R}")
        add(f"{D}other{R}   {B}[b]{R}{D}benchmark x100{R}  {B}[z]{R}{D}undo{R}  {B}[q]{R}{D}quit{R}")
        return "\n".join(out)

    def handle(self, k):
        p = self.policy
        if k in EVENTS:
            self.apply(world=EVENTS[k][1](self.world))
        elif k == "s":
            v = "status" if p.servable_by == "disk" else "disk"
            self.apply(policy=replace(p, servable_by=v), note=f"policy: servable_by={v}")
        elif k == "t":
            self.apply(policy=replace(p, include_trial=not p.include_trial), note=f"policy: include_trial={not p.include_trial}")
        elif k == "c":
            v = "reference_completed" if p.prefer == "observed_only" else "observed_only"
            self.apply(policy=replace(p, prefer=v), note=f"policy: prefer={v}")
        elif k == "i":
            v = C.IDENTITIES[(C.IDENTITIES.index(p.identity) + 1) % len(C.IDENTITIES)]
            self.apply(policy=replace(p, identity=v), note=f"policy: identity={v}")
        elif k == "w":
            v = not p.withdrawal_propagates
            self.apply(policy=replace(p, withdrawal_propagates=v), note=f"policy: withdrawal_propagates={v}")
        elif k == "b":
            self.note = "benchmarking..."
            draw(self)
            self.bench()
        elif k == "z":
            self.undo()


def draw(app):
    sys.stdout.write("\x1b[2J\x1b[H" + app.frame() + "\n")
    sys.stdout.flush()


def keys():
    if not sys.stdin.isatty():
        for line in sys.stdin:
            for ch in line.strip():
                yield ch
        return
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            yield sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main():
    print("loading registry bundles and built-store analyses.tsv (read-only)...")
    app = App()
    app.note = f"seeded in {app.seed_s:.1f}s from stores/ and /data/opengwasdb/stores (read-only)"
    draw(app)
    for k in keys():
        if k == "q":
            break
        app.handle(k)
        draw(app)


if __name__ == "__main__":
    main()
