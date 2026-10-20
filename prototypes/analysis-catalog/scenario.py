"""PROTOTYPE -- throwaway. Seeds an in-memory world from the real registry and
the real built Stores (read-only), and defines the lifecycle events the TUI fires.

Nothing here is written anywhere: every event returns a new World.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, replace
from pathlib import Path

from catalog import BUILD_ONLY, FIELDS, StoreRelease

REPO = Path(__file__).resolve().parents[2]

_COLLECTIONS = (
    ("eqtlgen", "eqtlgen-2019"),
    ("finngen-r13", "finngen-r13"),
    ("gwas-catalog", "gwas-catalog"),
    ("opengwas-gwas-vcf-ukb-b", "opengwas-ukb-b"),
    ("dbgap-phs002453", "dbgap-mvp"),
)


@dataclass(frozen=True)
class World:
    stores: dict[str, StoreRelease]
    bundles: dict[str, tuple[dict, ...]]          # registry analyses.tsv rows
    built: dict[str, tuple[dict, ...] | None]     # built Store analyses.tsv rows, None if not built
    event: str = "seeded from the registry and /data (read-only)"


def _tsv(path: Path) -> tuple[dict, ...]:
    with path.open(newline="") as fh:
        return tuple(csv.DictReader(fh, delimiter="\t"))


def _snapshot(store_id: str) -> str:
    m = re.search(r"^source_snapshot_id:\s*(\S+)", (REPO / "stores" / store_id / "release.yaml").read_text(), re.M)
    return m.group(1) if m else ""


def seed() -> World:
    stores, bundles, built = {}, {}, {}
    rows = _tsv(REPO / "stores.tsv")
    for r in rows:
        sid = r["store_id"]
        snap = _snapshot(sid)
        collection = next((c for p, c in _COLLECTIONS if snap.startswith(p)), snap or "?")
        uri = Path(r["store_uri"]) / "analyses.tsv"
        built[sid] = _tsv(uri) if uri.exists() else None
        bundles[sid] = _tsv(REPO / "stores" / sid / "analyses.tsv")
        stores[sid] = StoreRelease(
            store_id=sid,
            label=r["label"],
            collection=collection,
            status=r["status"],
            derived_from=r["derived_from"] or None,
            layout=r["layout"],
            completion_state=r["completion_state"],
            # The registry records no trial flag; the label is the only signal.
            trial="pilot" in r["label"],
            built=built[sid] is not None,
            created_at=r["created_at"],
        )
    for sid, s in stores.items():  # a completed child inherits its parent's collection
        if s.derived_from and s.collection == "?":
            stores[sid] = replace(s, collection=stores[s.derived_from].collection)
    return World(stores, bundles, built)


def _next_id(w: World) -> str:
    return f"OGS-{max(int(s[4:]) for s in w.stores) + 1:05d}"


def _included(rows) -> tuple[dict, ...]:
    return tuple(r for r in rows if (r.get("exclude_from_build", "") or "").strip().lower() != "true")


def _project(rows) -> tuple[dict, ...]:
    return tuple({"analysis_id": r["analysis_id"], **{f: r.get(f, "") or "" for f in FIELDS}} for r in rows)


# -- lifecycle events ---------------------------------------------------------

def build(w: World, sids: list[str], why: str) -> World:
    """Store Release Build publishes each bundle as it now stands."""
    stores, built = dict(w.stores), dict(w.built)
    for sid in sids:
        # Build-only facts (n_hits, completion r) are measured by the build; a
        # rebuild of the same Analyses re-measures the same values.
        before = {r["analysis_id"]: r for r in (w.built[sid] or ())}
        built[sid] = tuple({**r, **{f: before[r["analysis_id"]][f] for f in BUILD_ONLY if r["analysis_id"] in before}}
                           for r in _project(_included(w.bundles[sid])))
        stores[sid] = replace(stores[sid], built=True, status="validated")
    return replace(w, stores=stores, built=built, event=why)


def set_status(w: World, sids: list[str], status: str, why: str) -> World:
    stores = dict(w.stores)
    for sid in sids:
        stores[sid] = replace(stores[sid], status=status)
    return replace(w, stores=stores, event=why)


def erratum(w: World, sid: str, aid: str, updates: dict, why: str) -> World:
    rows = tuple({**r, **updates} if r["analysis_id"] == aid else r for r in w.bundles[sid])
    return replace(w, bundles={**w.bundles, sid: rows}, event=why)


def supersede(w: World, old: str, why: str) -> World:
    new = _next_id(w)
    s = w.stores[old]
    stores = {**w.stores, old: replace(s, status="superseded"),
              new: replace(s, store_id=new, label=s.label + "-v2", status="validated",
                           created_at="2026-10-21T00:00:00Z")}
    rows = _project(_included(w.bundles[old]))
    return replace(w, stores=stores, bundles={**w.bundles, new: rows}, built={**w.built, new: rows},
                   event=f"{why} -> {new}")


def add_store(w: World, label: str, collection: str, rows: tuple[dict, ...], why: str) -> World:
    sid = _next_id(w)
    store = StoreRelease(sid, label, collection, "validated", None, "ragged", "observed_only",
                         False, True, "2026-10-21T00:00:00Z")
    return replace(w, stores={**w.stores, sid: store}, bundles={**w.bundles, sid: rows},
                   built={**w.built, sid: rows}, event=f"{why} -> {sid}")


# -- the scripted events the TUI offers ----------------------------------------

def ev_build_mvp(w):
    sids = [s for s in ("OGS-00012", "OGS-00013", "OGS-00014", "OGS-00015", "OGS-00016") if not w.stores[s].built]
    return build(w, sids, "built + published MVP OGS-00012..16 from their accepted bundles")


def ev_rebuild_11(w):
    return build(w, ["OGS-00011"], "rebuilt OGS-00011 from its re-emitted (#203) bundle")


def ev_status_catch_up(w):
    sids = [s for s, st in w.stores.items() if st.built and st.status in ("candidate", "accepted")]
    return set_status(w, sids, "validated", f"registry status caught up to disk: {' '.join(s[-2:] for s in sids)} -> validated")


def ev_erratum(w):
    return erratum(w, "OGS-00017", "finngen-r13-BMI_IRN",
                   {"trait_ontology_id": "EFO:0004340", "trait_ontology_label": "body mass index"},
                   "Release Erratum on OGS-00017: finngen-r13-BMI_IRN -> EFO:0004340 (bundle only, no rebuild)")


def ev_supersede_06(w):
    return supersede(w, "OGS-00006", "rebuilt OGS-00006 as a new Store Release; old one superseded")


def ev_withdraw_09(w):
    return set_status(w, ["OGS-00009"], "withdrawn", "withdrew OGS-00009 (ukb-b-full-observed); OGS-00010 is derived from it")


def ev_gtex(w):
    rows = tuple({**r, "sample_size": "670", "consortium": "GTEx", "first_author": "GTEx Consortium",
                  "publication_doi": "10.1126/science.aaz1776"} for r in _project(w.bundles["OGS-00001"]))
    return add_store(w, "gtex-v8-whole-blood", "gtex-v8", rows,
                     "added a GTEx v8 whole-blood eQTL store with the same gene::tissue ID scheme")


def ev_gwascat_mvp(w):
    rows = _project(_included(w.bundles["OGS-00012"])[:40])
    return add_store(w, "gwas-catalog-ssf-mvp-reharmonised", "gwas-catalog", rows,
                     "added a GWAS Catalog SSF store re-serving 40 MVP GCST accessions")


def watched(w: World) -> list[str]:
    """Analysis IDs that each exercise one lifecycle case."""
    reg = {r["analysis_id"]: r for r in w.bundles["OGS-00011"]}
    drift = next(r["analysis_id"] for r in (w.built["OGS-00011"] or ())
                 if (reg.get(r["analysis_id"], {}).get("exclude_from_build", "") or "").lower() == "true")
    return ["ukb-b-19953", "ENSG00000175445::whole_blood", "finngen-r13-BMI_IRN", "GCST005076",
            drift, "GCST90199621", "GCST005806", "GCST90475091"]
