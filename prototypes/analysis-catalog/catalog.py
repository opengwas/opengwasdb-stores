"""PROTOTYPE -- throwaway. Analysis catalog state model for a cross-store query API.

The question this answers is in README.md. This module is the part worth
keeping: pure functions over plain frozen data, no I/O, no terminal code.

Shape:

    make_fragment(store, bundle_rows, built_rows) -> Fragment     # once per Store Release change
    build_catalog(fragments, policy)              -> Catalog      # full fold, every change
    diff(old, new)                                -> Diff         # what a client would see change

A Fragment is everything one Store Release contributes. The Catalog is never
updated in place: it is re-derived from all fragments under one Policy, so the
same inputs always give the same catalog version.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass

# Registry-owned Analytical Metadata. A built Store's analyses.tsv is frozen at
# build time, but a Release Erratum corrects these in the Release Bundle without
# rebuilding (e.g. OGS-00001 #141, OGS-00006 original_sd). The bundle wins and
# the copy records which fields it overrode.
INTERPRETIVE = (
    "analysis_label", "trait_ontology_id", "trait_ontology_label", "tissue", "context",
    "assigned_ancestry", "ancestry_assignment_method", "sample_size_kind", "sample_size_scope",
    "sample_size", "n_cases", "n_controls", "stored_effect_scale", "original_effect_scale",
    "original_sd", "original_sd_method", "license", "publication_doi", "publication_pmid",
    "consortium", "first_author",
)
# Measured during or after the build; only the built Store knows them.
BUILD_ONLY = ("n_hits_5e8", "completion_median_pearson_r")
FIELDS = INTERPRETIVE + BUILD_ONLY
_IDX = {f: i for i, f in enumerate(FIELDS)}

SERVED_STATUSES = frozenset({"built", "validated"})
RETIRED_STATUSES = frozenset({"superseded", "withdrawn"})
_GCST = re.compile(r"^GCST\d+$")


@dataclass(frozen=True)
class StoreRelease:
    store_id: str
    label: str
    collection: str          # Source Collection (provider-level for the prototype)
    status: str              # registry Release Status
    derived_from: str | None
    layout: str
    completion_state: str    # observed_only | reference_completed
    trial: bool              # NOT recorded by the registry today; inferred upstream
    built: bool              # a built Store exists under the Artifact Root
    created_at: str


@dataclass(frozen=True)
class Copy:
    """One Analysis as one Store Release serves it."""

    store_id: str
    analysis_id: str
    values: tuple[str, ...]          # aligned with FIELDS
    corrected: tuple[str, ...]       # fields the Release Bundle overrode
    registry_excluded: bool          # physically built, but the bundle now excludes it

    def get(self, field: str) -> str:
        return self.values[_IDX[field]]


@dataclass(frozen=True)
class Finding:
    store_id: str
    kind: str
    n: int
    detail: str = ""


@dataclass(frozen=True)
class Fragment:
    store: StoreRelease
    copies: tuple[Copy, ...]
    findings: tuple[Finding, ...]
    digest: str


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def make_fragment(store: StoreRelease, bundle_rows: list[dict], built_rows: list[dict] | None) -> Fragment:
    """Everything one Store Release contributes to the catalog.

    Membership comes from the built Store (what is physically queryable);
    interpretive metadata from the Release Bundle where it disagrees.
    """
    findings: list[Finding] = []
    bundle = {r["analysis_id"]: r for r in bundle_rows}
    copies: list[Copy] = []

    if built_rows is None:
        if store.status in SERVED_STATUSES:
            findings.append(Finding(store.store_id, "status-without-store", 0, f"status={store.status}"))
    else:
        if store.status not in SERVED_STATUSES | RETIRED_STATUSES:
            findings.append(Finding(store.store_id, "status-lags-disk", len(built_rows), f"status={store.status}"))
        n_corrected = n_excluded = n_unbundled = n_unlabelled = 0
        for row in built_rows:
            aid = row["analysis_id"]
            b = bundle.get(aid)
            values = []
            corrected = []
            for f in FIELDS:
                v = row.get(f, "") or ""
                if b is not None and f in INTERPRETIVE:
                    bv = b.get(f, "") or ""
                    if not _same(v, bv):
                        corrected.append(f)
                        v = bv
                values.append(v)
            excluded = b is None or (b.get("exclude_from_build", "") or "").strip().lower() == "true"
            n_corrected += bool(corrected)
            n_excluded += excluded and b is not None
            n_unbundled += b is None
            n_unlabelled += not values[_IDX["analysis_label"]]
            copies.append(Copy(store.store_id, aid, tuple(values), tuple(corrected), excluded))
        built_ids = {r["analysis_id"] for r in built_rows}
        n_bundle_only = sum(
            1 for aid, r in bundle.items()
            if aid not in built_ids and (r.get("exclude_from_build", "") or "").strip().lower() != "true"
        )
        if n_corrected:
            findings.append(Finding(store.store_id, "erratum-overlay", n_corrected))
        if n_excluded:
            findings.append(Finding(store.store_id, "built-but-excluded", n_excluded))
        if n_unbundled:
            findings.append(Finding(store.store_id, "built-not-in-bundle", n_unbundled))
        if n_unlabelled:
            findings.append(Finding(store.store_id, "no-analysis-label", n_unlabelled))
        if n_bundle_only:
            findings.append(Finding(store.store_id, "bundle-not-built", n_bundle_only))

    h = hashlib.sha256(repr(store).encode())
    for c in copies:
        h.update(repr((c.analysis_id, c.values, c.registry_excluded)).encode())
    return Fragment(store, tuple(copies), tuple(findings), h.hexdigest()[:12])


@dataclass(frozen=True)
class Policy:
    """Serving policy. ADR 0008: 'default served releases' belong to the service catalogue."""

    servable_by: str = "disk"            # "status": registry status says built/validated; "disk": a Store exists
    include_trial: bool = False
    prefer: str = "observed_only"        # or "reference_completed"
    identity: str = "authority"          # "analysis_id" | "collection" | "authority"
    withdrawal_propagates: bool = True   # a Store derived from a withdrawn Store is not served either


IDENTITIES = ("analysis_id", "collection", "authority")


def identity_key(copy: Copy, store: StoreRelease, mode: str) -> str:
    """The key that decides which copies are 'the same Analysis'."""
    if mode == "analysis_id":
        return copy.analysis_id
    if mode == "collection":
        return f"{store.collection}:{copy.analysis_id}"
    # authority: whoever issued the identifier. GCST accessions are the GWAS
    # Catalog's even when another collection (MVP via dbGaP) delivers the file.
    authority = "gwas-catalog" if _GCST.match(copy.analysis_id) else store.collection
    return f"{authority}:{copy.analysis_id}"


def _withdrawn_ancestor(store: StoreRelease, stores: dict[str, StoreRelease]) -> str | None:
    seen = set()
    s = store
    while s.derived_from and s.derived_from in stores and s.derived_from not in seen:
        seen.add(s.derived_from)
        s = stores[s.derived_from]
        if s.status == "withdrawn":
            return s.store_id
    return None


def ineligible_reason(copy: Copy, store: StoreRelease, policy: Policy,
                      stores: dict[str, StoreRelease]) -> str | None:
    """Why this copy cannot be the default, or None if it can."""
    if store.status in RETIRED_STATUSES:
        return store.status
    if policy.withdrawal_propagates and _withdrawn_ancestor(store, stores):
        return "parent withdrawn"
    if policy.servable_by == "status" and store.status not in SERVED_STATUSES:
        return f"status={store.status}"
    if policy.servable_by == "disk" and not store.built:
        return "not built"
    if store.trial and not policy.include_trial:
        return "trial"
    if copy.registry_excluded:
        return "bundle excludes"
    return None


@dataclass(frozen=True)
class Entry:
    """One Analysis: every copy of it, and the one a client gets by default."""

    key: str
    copies: tuple[Copy, ...]
    default: str | None              # store_id
    why: str
    ineligible: tuple[tuple[str, str], ...]
    collections: tuple[str, ...]
    disagree: tuple[str, ...]        # interpretive fields that differ across copies

    def default_copy(self) -> Copy | None:
        return next((c for c in self.copies if c.store_id == self.default), None)


@dataclass(frozen=True)
class Catalog:
    version: str
    policy: Policy
    stores: dict[str, StoreRelease]
    entries: dict[str, Entry]
    findings: tuple[Finding, ...]
    n_copies: int

    def listed(self) -> dict[str, Entry]:
        """What `list_analyses()` returns: Analyses with a servable default."""
        return {k: e for k, e in self.entries.items() if e.default is not None}


_STATUS_RANK = {"validated": 2, "built": 1}


def _choose(copies: list[Copy], stores: dict[str, StoreRelease], policy: Policy):
    ineligible = []
    eligible = []
    for c in copies:
        r = ineligible_reason(c, stores[c.store_id], policy, stores)
        if r:
            ineligible.append((c.store_id, r))
        else:
            eligible.append(c)
    if not eligible:
        reasons = sorted({r for _, r in ineligible})
        return None, "unavailable: " + ", ".join(reasons), tuple(ineligible)

    def rank(c: Copy):
        s = stores[c.store_id]
        return (s.completion_state == policy.prefer, _STATUS_RANK.get(s.status, 0), s.created_at, s.store_id)

    eligible.sort(key=rank, reverse=True)
    best, rest = eligible[0], eligible[1:]
    if not rest:
        why = "only servable copy"
    else:
        b, r = stores[best.store_id], stores[rest[0].store_id]
        if b.completion_state != r.completion_state:
            why = f"prefer {policy.prefer}"
        elif _STATUS_RANK.get(b.status, 0) != _STATUS_RANK.get(r.status, 0):
            why = f"{b.status} over {r.status}"
        else:
            why = f"newest of {len(eligible)}"
    return best.store_id, why, tuple(ineligible)


def build_catalog(fragments: list[Fragment], policy: Policy) -> Catalog:
    stores = {f.store.store_id: f.store for f in fragments}
    groups: dict[str, list[Copy]] = defaultdict(list)
    n = 0
    for f in fragments:
        for c in f.copies:
            groups[identity_key(c, f.store, policy.identity)].append(c)
            n += 1

    entries = {}
    for key, copies in groups.items():
        default, why, inel = _choose(copies, stores, policy)
        collections = tuple(sorted({stores[c.store_id].collection for c in copies}))
        disagree = ()
        if len(copies) > 1:
            disagree = tuple(
                f for f in INTERPRETIVE
                if len({c.get(f) for c in copies}) > 1
            )
        entries[key] = Entry(key, tuple(copies), default, why, inel, collections, disagree)

    findings = [x for f in fragments for x in f.findings]
    spans = sum(1 for e in entries.values() if len(e.collections) > 1)
    if spans:
        findings.append(Finding("*", "key-spans-collections", spans, f"identity={policy.identity}"))

    h = hashlib.sha256(repr(policy).encode())
    for f in sorted(fragments, key=lambda f: f.store.store_id):
        h.update(f.digest.encode())
    return Catalog(h.hexdigest()[:10], policy, stores, entries, tuple(findings), n)


@dataclass(frozen=True)
class Diff:
    added: int
    removed: int
    moved: int
    changed: int
    examples: tuple[str, ...]
    merged: int = 0     # identity change: new Analyses formed from several old ones
    split: int = 0      # identity change: old Analyses now several

    def __str__(self) -> str:
        if self.merged or self.split:
            return (f"re-keyed: {self.merged:,} Analyses merged, {self.split:,} split; "
                    f"listed {self.added - self.removed:+,}  {self.moved:,} default moved")
        if not (self.added or self.removed or self.moved or self.changed):
            return "no change visible to clients"
        return f"+{self.added:,} listed  -{self.removed:,} listed  {self.moved:,} default moved  {self.changed:,} metadata changed"


def _rekey_diff(old: Catalog, new: Catalog) -> Diff:
    def owner(cat):
        return {(c.store_id, c.analysis_id): k for k, e in cat.entries.items() for c in e.copies}
    o, n = owner(old), owner(new)
    merged = [k for k, e in new.entries.items() if len({o.get((c.store_id, c.analysis_id)) for c in e.copies}) > 1]
    split = [k for k, e in old.entries.items() if len({n.get((c.store_id, c.analysis_id)) for c in e.copies}) > 1]
    # A client who got copy (store, analysis) by default before no longer does.
    moved = sum(1 for e in old.listed().values()
                if new.entries[n[(e.default, e.default_copy().analysis_id)]].default != e.default)
    ex = tuple(f"⋈ {k} ({'+'.join(new.entries[k].collections)})" for k in sorted(merged)[:1]) + \
        tuple(f"⋔ {k}" for k in sorted(split)[:1])
    return Diff(len(new.listed()), len(old.listed()), moved, 0, ex, len(merged), len(split))


def diff(old: Catalog, new: Catalog) -> Diff:
    """What a client listing Analyses would see change between two catalog versions."""
    if old.policy.identity != new.policy.identity:
        return _rekey_diff(old, new)
    a, b = old.listed(), new.listed()
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    moved, changed = [], []
    for k in a.keys() & b.keys():
        if a[k].default != b[k].default:
            moved.append(k)
        elif a[k].default_copy().values != b[k].default_copy().values:
            changed.append(k)
    ex = []
    for tag, ks in (("+", added), ("-", removed), ("→", moved), ("~", changed)):
        for k in sorted(ks)[:1]:
            e = b.get(k) or a.get(k)
            ex.append(f"{tag} {k} ({e.default if k in b else a[k].default} → {b[k].default if k in b else '∅'})")
    return Diff(len(added), len(removed), len(moved), len(changed), tuple(ex))
