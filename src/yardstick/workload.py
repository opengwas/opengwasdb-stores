"""Load and check the yardstick workload (resources/yardstick/workload.yaml)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from ld_registry.ids import is_valid_ld_id
from ld_registry.record import LD_ROOT
from yardstick.ld_queries import LD_MATRIX, LD_PROXIES, LD_REGIONS
from yardstick.result import LD_KINDS, LD_MEASURES, LD_TIMED

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "resources" / "yardstick" / "workload.yaml"
#: The LD Store Registry's records (docs/spec/ld-store-registry.md): where an LD Store is found.
DEFAULT_LD_ROOT = REPO_ROOT / LD_ROOT

SCHEMA = 1
#: Lookup shapes: variants x Analyses, each drawn from the entry's fixed 100 (`lookup_inputs`: the first variant, every tenth, or all).
LOOKUP_N = 100
LOOKUP_SHAPES = {
    "lookup_1x100": (1, 100), "lookup_100x1": (100, 1), "lookup_10x100": (10, 100),
    "lookup_100x10": (100, 10), "lookup_100x100": (100, 100),
}
MEASURES = (
    "open", "analyses_table", "variants_table", "resolve",
    "phewas_rsid", "phewas_alid", "phewas_position",
    "region_100kb", "region", *LOOKUP_SHAPES,
    "analysis_full", "analysis_full_observed_only", "analysis_hm3",
    "top_hits", "range_by_analysis", "rho", "rho_row", "rho_matrix",
    "bytes_on_disk", "peak_rss",
)
LAYOUTS = ("dense", "ragged", "hybrid")
# What each measure reads from a Store's entry in the workload.
NEEDS = {
    "resolve": ("rsid",),
    "region_100kb": ("region_100kb",),
    "analysis_full_observed_only": ("analysis_id",),
    "top_hits": ("top_hits",),
    "range_by_analysis": ("cis",),
    "rho": ("rho",), "rho_row": ("rho",), "rho_matrix": ("rho",),
    **{name: ("lookup",) for name in LOOKUP_SHAPES},
    "phewas_rsid": ("rsid",),
    "phewas_alid": ("alid",),
    "phewas_position": ("position",),
    "analysis_full": ("analysis_id",),
    "analysis_hm3": ("analysis_id",),
    "peak_rss": ("analysis_id",),
    "region": ("region",),
}


class WorkloadError(ValueError):
    pass


TOP_HITS_PATHS = ("index", "scan")


def applies(name: str, store: dict[str, Any]) -> bool:
    """Whether a measure can run on this Store, from its declared `layout` and `rho` alone.

    The one owner of the layout rule: a measure that does not apply is recorded `not_applicable` and needs
    no input or count. `analysis_hm3` is a Dense or Hybrid query, `range_by_analysis` a Ragged one, and
    the rho reads need a non-Ragged Store whose entry declares a Rho Matrix.
    """
    if name == "range_by_analysis":
        return store["layout"] == "ragged"
    if name == "analysis_hm3":
        return store["layout"] != "ragged"
    if name in ("rho", "rho_row", "rho_matrix"):
        return store["layout"] != "ragged" and bool(store.get("rho"))
    return True


#: Measures that read an input and so need `input_rows[<measure>]`: the rows one call returned when the
#: workload was written. `range_by_analysis` only on a Ragged entry, rho only where `rho` declares a Rho Matrix.
ROW_COUNTED = (
    "phewas_rsid", "phewas_alid", "phewas_position", "resolve", "region_100kb", "region",
    *LOOKUP_SHAPES, "analysis_full", "analysis_full_observed_only", "top_hits", "range_by_analysis",
    "rho", "rho_row", "rho_matrix",
)


def lookup_inputs(lookup: dict[str, Any], n_variants: int, n_analyses: int) -> tuple[list[str], list[str]]:
    """A lookup shape's variants and Analyses from the entry's fixed lists of 100.

    One variant is the first; ten are every tenth, so they stay scattered; 100 are all. Analyses are the first N.
    """
    variants = lookup["variants"]
    picked = variants[:1] if n_variants == 1 else variants[:: LOOKUP_N // n_variants]
    return picked, lookup["analyses"][:n_analyses]


def _check_inputs(store_id: str, store: dict[str, Any]) -> None:
    lookup = store.get("lookup")
    if lookup is not None:
        for key in ("variants", "analyses"):
            values = lookup.get(key)
            if not isinstance(values, list) or len(values) != LOOKUP_N or len(set(values)) != LOOKUP_N:
                raise WorkloadError(f"{store_id}: lookup.{key} must be {LOOKUP_N} distinct identifiers")
    hits = store.get("top_hits")
    if hits is not None and hits.get("path") not in TOP_HITS_PATHS:
        raise WorkloadError(f"{store_id}: top_hits.path must be one of {TOP_HITS_PATHS}, got {hits.get('path')!r}")
    counts = store.get("input_rows") or {}
    for name in store["measures"]:
        if name not in ROW_COUNTED or not applies(name, store):
            continue
        rows = counts.get(name)
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise WorkloadError(f"{store_id}: input_rows lacks a row count for {name}")
        # `top_hits` may be empty by design: the Store has none, and the measure is `not_applicable`.
        if rows == 0 and name != "top_hits":
            raise WorkloadError(f"{store_id}: input for {name} returned no rows when it was checked")


def check(workload: dict[str, Any]) -> None:
    if workload.get("schema") != SCHEMA:
        raise WorkloadError(f"workload schema must be {SCHEMA}, got {workload.get('schema')!r}")
    stores = workload.get("stores")
    if not isinstance(stores, list) or not stores:
        raise WorkloadError("workload lists no stores")
    seen: set[str] = set()
    for store in stores:
        store_id = store.get("store_id")
        if not store_id or store_id in seen:
            raise WorkloadError(f"missing or duplicate store_id {store_id!r}")
        seen.add(store_id)
        if store.get("layout") not in LAYOUTS:
            raise WorkloadError(f"{store_id}: layout must be one of {LAYOUTS}")
        status = store.get("status")
        if status == "pending":
            if store.get("measures"):
                raise WorkloadError(f"{store_id}: a pending Store lists no measures")
            if not store.get("reason"):
                raise WorkloadError(f"{store_id}: a pending Store says why")
        elif status == "measured":
            measures = store.get("measures") or []
            for name in measures:
                if name not in MEASURES:
                    raise WorkloadError(f"{store_id}: unknown measure {name!r}")
                if name in ("rho", "rho_row", "rho_matrix") and "rho" not in store:
                    raise WorkloadError(f"{store_id}: measure {name} needs 'rho': false, or the Rho Matrix's ids")
                if not applies(name, store):
                    continue
                for key in NEEDS.get(name, ()):
                    if key not in store:
                        raise WorkloadError(f"{store_id}: measure {name} needs {key!r}")
            _check_inputs(store_id, store)
        else:
            raise WorkloadError(f"{store_id}: status must be measured or pending")
    if "ld" in workload:
        _check_ld(workload["ld"])


# ---------------------------------------------------------------- the LD section (#504)

# The queries' names and shapes are `yardstick.ld_queries`'s: `LD_MATRIX`, `LD_REGIONS`, `LD_PROXIES`.
#: How many variants the workload's matrix list holds: the largest `matrix_N`.
LD_MATRIX_N = max(LD_MATRIX.values())
LD_CLUMP_KEYS = ("env", "default", "analyses", "file", "sha256", "r2", "kb")
#: The columns of each Analysis's hits file, in order: its ALID, and the z the clump scores by |z|.
HITS_COLUMNS = ["alid", "z"]


def _mapping(where: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkloadError(f"{where} must be a mapping, got {value!r}")
    return value


def _distinct(where: str, values: Any, n: int) -> None:
    if (not isinstance(values, list) or len(values) != n
            or not all(isinstance(v, str) and v for v in values) or len(set(values)) != n):
        raise WorkloadError(f"{where} must be {n} distinct ALIDs")


def _check_ld(section: Any) -> None:
    """The LD section's shape: what it names, never what the registry or the disk holds (`resolve_ld`)."""
    section = _mapping("ld", section)
    reps = _mapping("ld.repetitions", section.get("repetitions"))
    if not all(isinstance(key, str) for key in reps):
        raise WorkloadError(f"ld.repetitions keys must be measure names, got {sorted(map(repr, reps))}")
    if "default" not in reps or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in reps.values()):
        raise WorkloadError("ld.repetitions must give a default, and every count a whole number of at least 1")
    unknown = sorted(set(reps) - {"default", *LD_TIMED})
    if unknown:
        raise WorkloadError(f"ld.repetitions names unknown measures {unknown}")
    stores = section.get("stores")
    if not isinstance(stores, list) or not stores:
        raise WorkloadError("ld.stores must list the LD Stores measured")
    seen: set[str] = set()
    for index, store in enumerate(stores):
        store = _mapping(f"ld.stores[{index}]", store)
        ld_id = store.get("ld_id")
        if not is_valid_ld_id(ld_id) or ld_id in seen:
            raise WorkloadError(f"ld: missing, malformed or duplicate ld_id {ld_id!r}")
        seen.add(ld_id)
        if store.get("kind") not in LD_KINDS:
            raise WorkloadError(f"{ld_id}: kind must be one of {LD_KINDS}, got {store.get('kind')!r}")
    matrix = _mapping("ld.matrix", section.get("matrix"))
    for key in ("chromosome", "seed"):
        if key not in matrix:
            raise WorkloadError(f"ld.matrix lacks {key}")
    _distinct("ld.matrix.variants", matrix.get("variants"), LD_MATRIX_N)
    region = _mapping("ld.region", section.get("region"))
    if not isinstance(region.get("chromosome"), str) or not isinstance(region.get("centre"), int) \
            or region["centre"] <= max(LD_REGIONS.values()) // 2:
        raise WorkloadError("ld.region needs a chromosome and a centre more than half the widest region from its start")
    proxies = _mapping("ld.proxies", section.get("proxies"))
    for key in ("r2", "kb"):
        if key not in proxies:
            raise WorkloadError(f"ld.proxies lacks {key}")
        _number(f"ld.proxies.{key}", proxies[key])
    seeds = _mapping("ld.proxies.seeds", proxies.get("seeds"))
    lists = _mapping("ld.proxies.variants", proxies.get("variants"))
    for name, n in LD_PROXIES.items():
        _distinct(f"ld.proxies.variants.{name}", lists.get(name), n)
        if not isinstance(seeds.get(name), int):
            raise WorkloadError(f"ld.proxies.seeds lacks {name}")
    clump = _mapping("ld.clump", section.get("clump"))
    for key in LD_CLUMP_KEYS:
        if key not in clump:
            raise WorkloadError(f"ld.clump lacks {key}")
    if not isinstance(clump["analyses"], int) or isinstance(clump["analyses"], bool) or clump["analyses"] < 1:
        raise WorkloadError("ld.clump.analyses must be how many Analyses the input holds")
    for key, what in (("env", "an environment variable's name"), ("default", "a path"), ("file", "a file name")):
        if not isinstance(clump[key], str) or not clump[key].strip():
            raise WorkloadError(f"ld.clump.{key} must be {what}, got {clump[key]!r}")
    if not _is_sha256(clump["sha256"]):
        raise WorkloadError(f"ld.clump.sha256 must be 64 lower-case hex characters, got {clump['sha256']!r}")
    for key in ("r2", "kb"):
        _number(f"ld.clump.{key}", clump[key])


def _number(where: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkloadError(f"{where} must be a number, got {value!r}")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def resolve_ld(workload: dict[str, Any], ld_root: Path | str = DEFAULT_LD_ROOT) -> list[dict[str, Any]]:
    """Each LD entry's Store from its LD Store Registry record: location, kind and far_bp.

    The record is the one place an LD Store's path is written (ADR 0042), so the workload names
    `ld_id` alone. A record that is missing, of another kind, or whose far_bp is narrower than a
    region the workload reads is refused here, before anything is opened. Whether the Store has
    Partner Rows is its own provenance's to say, read when the runner opens it (`yardstick.ld`).
    """
    from ld_registry.record import InvalidRecords, valid_records

    try:
        records = {r["ld_id"]: r for r in valid_records(ld_root)}
    except InvalidRecords as error:
        raise WorkloadError(f"the LD Store Registry at {ld_root} is invalid: {error}") from None
    targets = []
    for entry in workload["ld"]["stores"]:
        ld_id = entry["ld_id"]
        record = records.get(ld_id)
        if record is None:
            raise WorkloadError(f"{ld_id}: no record in the LD Store Registry at {ld_root}")
        if record["kind"] != entry["kind"]:
            raise WorkloadError(f"{ld_id}: the workload says {entry['kind']}, its record {record['kind']}")
        far_bp = (record.get("provenance") or {}).get("far_bp")
        if not isinstance(far_bp, int):
            raise WorkloadError(f"{ld_id}: its record's provenance has no far_bp")
        for name, width in LD_REGIONS.items():
            if width > far_bp:
                raise WorkloadError(f"{ld_id}: {name} is {width} bp wide, wider than its far_bp of {far_bp}")
        targets.append({"ld_id": ld_id, "kind": record["kind"], "label": record.get("label"),
                        "location": record["location"], "far_bp": far_bp})
    return targets


def _analysis_directories(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def hits_header(data: bytes) -> list[str]:
    """A hits file's columns, from its first line: the one parser the preflight and the worker share."""
    return data.split(b"\n", 1)[0].decode("utf-8").rstrip("\r").split("\t")


class ClumpRead(NamedTuple):
    """One read of the clump input: the aggregate `digest`, the manifest's own, and every hits file's bytes."""

    digest: str
    manifest_sha256: str
    files: dict[str, bytes]


def read_clump(root: Path, names: list[str], file: str, *, manifest: bytes | None = None) -> ClumpRead:
    """One read of the clump input: its digest, and the bytes of every Analysis's hits file it was taken over.

    The digest is sha256 over `manifest.json <sha256>` and then `<name>/<file> <sha256>`, one line
    each, in the order of `names`. `manifest` is the manifest's bytes when the caller has read
    them already (the preflight, which takes `names` from them), so the digest is over the very
    bytes it parsed; otherwise they are read here, once. The preflight and the worker each read
    the input once through here, so the worker measures exactly the bytes whose digest it
    compares with the preflight's.
    """
    manifest_bytes = (root / "manifest.json").read_bytes() if manifest is None else manifest
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    digest = hashlib.sha256(f"manifest.json {manifest_sha256}\n".encode())
    files: dict[str, bytes] = {}
    for name in names:
        data = (root / name / file).read_bytes()
        digest.update(f"{name}/{file} {hashlib.sha256(data).hexdigest()}\n".encode())
        files[name] = data
    return ClumpRead(digest.hexdigest(), manifest_sha256, files)


def clump_sha256(root: Path, file: str) -> str:
    """The digest the workload pins (`clump.sha256`): `read_clump`'s, over every Analysis directory in name order."""
    return read_clump(root, _analysis_directories(root), file).digest


def clump_input(workload: dict[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The clump's Analyses (opengwasdb#403's 37), from `$<env>` else the workload's default, checked.

    The directory holds `manifest.json` and one `<store>__<analysis_id>` directory per Analysis
    it names, each with its hits file (`alid`, `z`). It is outside every repository, so it must
    be exactly the one the workload pins: the manifest's Analyses, as many as `clump.analyses`,
    each directory present and no other, each hits file headed by its columns, and the whole
    input's digest `clump.sha256`. A substitute is refused, never measured. The result records
    the path, where it came from, the Analyses, the hit count and the digests.
    """
    clump = workload["ld"]["clump"]
    environ = os.environ if environ is None else environ
    configured = (environ.get(clump["env"]) or "").strip()
    root = Path(configured or clump["default"])
    if not root.is_dir():
        raise WorkloadError(f"clump input {root} is not a directory (set ${clump['env']})")
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise WorkloadError(f"clump input {root} has no manifest.json")
    # Read once: the names, the manifest's digest and the aggregate digest all come from these bytes.
    manifest_bytes = manifest.read_bytes()
    try:
        named = sorted(f"{a['store']}__{a['analysis_id']}" for a in json.loads(manifest_bytes.decode("utf-8"))["analyses"])
    except (ValueError, KeyError, TypeError) as error:
        raise WorkloadError(f"clump input {root}: manifest.json does not name its Analyses ({error})") from None
    if len(named) != clump["analyses"]:
        raise WorkloadError(f"clump input {root}: its manifest names {len(named)} Analyses, the workload {clump['analyses']}")
    found = _analysis_directories(root)
    if found != named:
        missing, extra = sorted(set(named) - set(found)), sorted(set(found) - set(named))
        raise WorkloadError(f"clump input {root}: its directories are not the manifest's: missing {missing}, extra {extra}")
    for name in named:
        if not (root / name / clump["file"]).is_file():
            raise WorkloadError(f"clump input: {name} has no {clump['file']}")
    read = read_clump(root, named, clump["file"], manifest=manifest_bytes)
    digest = read.digest
    hits = 0
    for name, data in read.files.items():
        header = hits_header(data)
        if header != HITS_COLUMNS:
            raise WorkloadError(f"clump input: {name}/{clump['file']}'s header is {header}, not {HITS_COLUMNS}")
        hits += len(data.splitlines()) - 1
    if digest != clump["sha256"]:
        raise WorkloadError(f"clump input {root} is not the workload's: its sha256 is {digest}, the workload pins {clump['sha256']}")
    return {
        "path": str(root), "source": "env" if configured else "default", "env": clump["env"],
        "file": clump["file"], "analyses": named, "hits": hits,
        "manifest_sha256": read.manifest_sha256, "sha256": digest,
    }


def load(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    workload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    check(workload)
    return workload


def sha256(path: Path = DEFAULT_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
