#!/usr/bin/env python3
"""Release-side benchmark report for the HM3 Indexed Variant Subset (stores #212).

Epic opengwas/opengwasdb-stores#214 makes a named ``hm3`` Indexed Variant Subset
the default derived artifact for every Dense-component Store Release. The
benchmark itself is owned by ``opengwasdb`` (opengwas/opengwasdb#272): the one
seam is ``benchmarks._query_shapes.measure_hm3_extract``. This script is the
registry-side *report* over that seam -- it discovers every registered Store,
asks the core helper for each Store's HM3 record and, where a checked-in anchor
exists, the seven generic query shapes, and renders one aggregate JSON artifact
and one generated Markdown report.

The rule this script exists to keep (CONTRIBUTING):

* HM3 is an **optional default derived artifact**, not a Store validity
  requirement. ``missing``, ``not_applicable`` and ``unsupported`` are
  non-failing operational statuses and are recorded exactly as the core helper
  returns them. They are never a bundle-check or validation failure, and the
  script never turns one into a zero-row measurement.
* A subset that is **present and structurally valid** whose extraction raises
  propagates out of the core helper; this script does not catch it. A broken
  index reported as absent would be a wrong answer that looks like a right one.
* Timing and query work are never reimplemented here. Every HM3 number comes
  from ``measure_hm3_extract``; the seven shapes come from the core generic
  harness run as a subprocess, one Store per process (the harness enforces
  result identity across stores, so different-data Stores cannot share a run).

The repository pin deliberately stays at a revision that predates Indexed
Variant Subsets (stores#197 owns the format-0.2.0 bump), so the capability is an
explicit run-time prerequisite: ``--opengwasdb-checkout PATH`` names the
``opengwasdb`` checkout the helper and harness are imported from, and the
artifact records that checkout's git revision and the imported package's
fingerprint.

Usage (run under the opengwasdb checkout's environment, which carries zarr v3):

    pixi run -e dev python <stores>/resources/scripts/release-benchmark-report.py \
        --opengwasdb-checkout /path/to/opengwasdb \
        --all --hm3-only \
        --output docs/benchmark-output/opengwasdb_hm3_release_benchmark.json \
        --report docs/benchmark-output/opengwasdb_hm3_release_benchmark.md

The JSON artifact and the Markdown report are generated, never hand-edited:
re-run the command to refresh them.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Layouts that carry a Dense Component (and therefore may hold an hm3 subset).
DENSE_COMPONENT_LAYOUTS: frozenset[str] = frozenset({"dense", "hybrid"})
RAGGED_LAYOUT = "ragged"

#: The subset name the benchmark detects, the conventional name (ADR 0061).
DEFAULT_SUBSET_NAME = "hm3"

DEFAULT_OUTPUT = Path("docs/benchmark-output/opengwasdb_hm3_release_benchmark.json")
DEFAULT_REPORT = Path("docs/benchmark-output/opengwasdb_hm3_release_benchmark.md")
DEFAULT_ANCHORS = Path("resources/scripts/release-benchmark-anchors.yaml")

#: The four core HM3 statuses. These never fail the run.
CORE_HM3_STATUSES: frozenset[str] = frozenset(
    {"measured", "missing", "not_applicable", "unsupported"}
)

#: Wrapper-level operational statuses, used only when a core measurement cannot
#: be attempted. Also non-failing: they say *why* nothing was measured.
WRAPPER_HM3_STATUSES: frozenset[str] = frozenset({"not_queryable", "no_anchor"})


class CheckoutError(RuntimeError):
    """``--opengwasdb-checkout`` is not a usable opengwasdb checkout."""


@dataclass(frozen=True)
class CoreModules:
    """The core pieces the report imports, plus what was actually imported."""

    checkout: Path
    revision: str
    opengwasdb_path: str
    opengwasdb_fingerprint: str
    opengwasdb: Any
    query_shapes: Any
    hm3: Any
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Anchor:
    """A per-Store anchor for the seven generic query shapes."""

    exposure: str | None = None
    phewas_alid: str | None = None
    region: str | None = None


@dataclass(frozen=True)
class StoreRef:
    """One registered Release Bundle and the facts the report needs about it."""

    store_id: str
    layout: str
    bundle_dir: Path
    analysis_id: str | None

    @property
    def dense_component(self) -> bool:
        return self.layout in DENSE_COMPONENT_LAYOUTS

    @property
    def ragged(self) -> bool:
        return self.layout == RAGGED_LAYOUT


# --------------------------------------------------------------------------- #
# Core checkout loading
# --------------------------------------------------------------------------- #


def _git_revision(checkout: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def load_core(checkout: Path | str) -> CoreModules:
    """Import the HM3 helper and the generic harness from an explicit checkout.

    The tracked pin deliberately predates Indexed Variant Subsets, so the
    checkout is a run-time prerequisite named on the command line. The import
    path is inserted ahead of everything else and the imported package is
    checked to actually live under the checkout -- silently measuring against
    the pinned package would be a wrong answer that looks like a right one.
    """
    checkout = Path(checkout).resolve()
    required = {
        "benchmarks/_query_shapes.py": checkout / "benchmarks" / "_query_shapes.py",
        "benchmarks/_hm3.py": checkout / "benchmarks" / "_hm3.py",
        "opengwasdb/__init__.py": checkout / "opengwasdb" / "__init__.py",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise CheckoutError(
            f"--opengwasdb-checkout {checkout} is not an opengwasdb checkout: "
            f"missing {', '.join(sorted(missing))}"
        )
    if str(checkout) in sys.path:
        sys.path.remove(str(checkout))
    sys.path.insert(0, str(checkout))

    import importlib

    opengwasdb = importlib.import_module("opengwasdb")
    query_shapes = importlib.import_module("benchmarks._query_shapes")
    hm3 = importlib.import_module("benchmarks._hm3")

    imported = Path(opengwasdb.__file__).resolve()
    if not imported.is_relative_to(checkout):
        raise CheckoutError(
            f"imported opengwasdb from {imported}, not from "
            f"--opengwasdb-checkout {checkout}; the pinned package would shadow "
            "the checkout"
        )

    provenance: dict[str, Any] = {}
    fingerprint = ""
    try:
        artifact = importlib.import_module("benchmarks._artifact")
        measured_path, fingerprint = artifact.package_fingerprint()
        provenance = {
            "opengwasdb_path": str(measured_path),
            "opengwasdb_fingerprint": fingerprint,
        }
    except Exception:  # pragma: no cover - provenance is best-effort metadata
        provenance = {}
    return CoreModules(
        checkout=checkout,
        revision=_git_revision(checkout),
        opengwasdb_path=str(imported.parent),
        opengwasdb_fingerprint=fingerprint,
        opengwasdb=opengwasdb,
        query_shapes=query_shapes,
        hm3=hm3,
        provenance=provenance,
    )


# --------------------------------------------------------------------------- #
# Registry discovery
# --------------------------------------------------------------------------- #


def first_analysis_id(analyses_tsv: Path) -> str | None:
    """The first data row's ``analysis_id``, or ``None`` when unavailable.

    The first Analysis is the HM3 extraction anchor (stores#212): it exists in
    every bundle without a per-Store measurement decision. Bundles differ in
    column order (some lead with ``analysis_index``), so the column is located
    by name rather than position.
    """
    if not analyses_tsv.is_file():
        return None
    with analyses_tsv.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            return None
        try:
            index = header.index("analysis_id")
        except ValueError:
            return None
        for row in reader:
            if len(row) > index and row[index].strip():
                return row[index].strip()
    return None


def registered_bundles(registry_root: Path | str) -> list[StoreRef]:
    """Every registered Release Bundle under ``registry_root``, sorted by id."""
    root = Path(registry_root)
    stores: list[StoreRef] = []
    if not root.is_dir():
        return stores
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or (entry / "release.yaml").is_file() is False:
            continue
        if not entry.name.startswith("OGS-"):
            continue
        build_path = entry / "build.yaml"
        if not build_path.is_file():
            continue
        raw = yaml.safe_load(build_path.read_text(encoding="utf-8")) or {}
        layout = str(raw.get("layout", "")).strip()
        stores.append(
            StoreRef(
                store_id=entry.name,
                layout=layout,
                bundle_dir=entry,
                analysis_id=first_analysis_id(entry / "analyses.tsv"),
            )
        )
    return stores


def load_anchors(path: Path | str | None) -> dict[str, Anchor]:
    """The checked-in per-Store anchor map for the seven shapes, if present.

    A Store without an entry reports ``shapes: not_configured`` (non-failing);
    its HM3 record is still measured. The anchor map is a maintained registry
    input, not a measurement: it names which Analysis and window the generic
    harness should query.
    """
    if path is None:
        return {}
    anchors_path = Path(path)
    if not anchors_path.is_file():
        return {}
    raw = yaml.safe_load(anchors_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("stores", {}) or {}
    anchors: dict[str, Anchor] = {}
    for store_id, spec in entries.items():
        spec = spec or {}
        anchors[str(store_id)] = Anchor(
            exposure=str(spec["exposure"]) if spec.get("exposure") else None,
            phewas_alid=(
                str(spec["phewas_alid"]) if spec.get("phewas_alid") else None
            ),
            region=str(spec["region"]) if spec.get("region") else None,
        )
    return anchors


def resolve_artifact_root(
    override: Path | str | None, repo_root: Path | str = REPO_ROOT
) -> Path:
    """Resolve the deployment artifact root the documented way (issue #126)."""
    if override is not None and str(override).strip():
        return Path(override)
    src = Path(repo_root) / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from ogstores import paths  # type: ignore[import-not-found]

        return Path(paths.artifact_root(repo_root=repo_root))
    except Exception:
        env = os.environ.get("OPENGWASDB_ARTIFACT_ROOT")
        if env:
            return Path(env)
        config = Path(repo_root) / "ogstores.yaml"
        if config.is_file():
            raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
            if raw.get("artifact_root"):
                return Path(str(raw["artifact_root"]))
        return Path("/data/opengwasdb/stores")


# --------------------------------------------------------------------------- #
# HM3 measurement (core seam only)
# --------------------------------------------------------------------------- #


def measure_hm3(
    core: CoreModules,
    store: StoreRef,
    store_path: Path,
    *,
    reps: int,
) -> dict[str, Any]:
    """One Store's HM3 record, computed only by the core helper.

    The core helper is ``benchmarks._query_shapes.measure_hm3_extract``. It
    needs an open query handle to time a measured subset, but classification
    does not: a Store with no variant index (a common operational state) cannot
    be opened, yet its HM3 status is still well-defined. So:

    1. try to open the Store; if it opens, call ``measure_hm3_extract`` with the
       handle -- the normal path, and the only path that produces a timing;
    2. if it cannot be opened, classify with the core's own
       ``benchmarks._hm3.classify_hm3``, the exact classifier
       ``measure_hm3_extract`` wraps. ``not_applicable``/``missing``/
       ``unsupported`` are returned via ``measure_hm3_extract(None, ...)``,
       which never touches the handle for a non-measured status;
    3. a *valid* subset on an unqueryable Store is ``not_queryable`` -- an
       operational prerequisite, not an HM3 absence, and never silently a
       timing.

    A present-and-corrupt subset raises out of ``classify_hm3`` and propagates.
    """
    if not store.analysis_id:
        return {
            "subset": DEFAULT_SUBSET_NAME,
            "status": "no_anchor",
            "detail": (
                f"{store.store_id} analyses.tsv has no first analysis_id to "
                "anchor the HM3 extraction on"
            ),
        }

    query: Any | None = None
    open_error: str | None = None
    try:
        query, _plan = core.query_shapes.open_benchmark_store(store_path)
    except Exception as exc:  # deliberately broad: any open prerequisite
        open_error = f"{type(exc).__name__}: {exc}"

    if query is not None:
        try:
            return dict(
                core.query_shapes.measure_hm3_extract(
                    query,
                    store.analysis_id,
                    store_path=store_path,
                    reps=reps,
                )
            )
        finally:
            close = getattr(query, "close", None)
            if callable(close):
                close()

    assert open_error is not None
    state = core.hm3.classify_hm3(store_path)
    if state.status == "measured":
        return {
            "subset": DEFAULT_SUBSET_NAME,
            "status": "not_queryable",
            "core_status": "measured",
            "detail": (
                "a structurally valid hm3 subset is published but the Store is "
                f"not queryable ({open_error}); build the variant index and re-run"
            ),
        }
    record = dict(
        core.query_shapes.measure_hm3_extract(
            None,
            store.analysis_id,
            store_path=store_path,
            reps=reps,
        )
    )
    record.setdefault("subset", DEFAULT_SUBSET_NAME)
    return record


# --------------------------------------------------------------------------- #
# Generic seven-shape harness (core, one Store per process)
# --------------------------------------------------------------------------- #


def run_generic_shapes(
    core: CoreModules,
    store: StoreRef,
    store_path: Path,
    anchor: Anchor,
    *,
    reps: int,
    output_path: Path,
) -> dict[str, Any]:
    """Run the core generic harness for one Store and return its shape block.

    ``benchmarks/benchmark_store_comparison.py`` enforces result identity across
    every ``--store`` it is given, so two different-data Stores cannot share a
    run: it is invoked once per Store here. Its ``hm3_extract`` field is a
    cross-check only -- the report's HM3 record always comes from
    ``measure_hm3`` above, so the two paths cannot disagree silently.

    A non-zero harness exit is a real benchmark failure, recorded loudly and
    surfaced by the caller; it is not downgraded to ``not_configured``.
    """
    command = [
        sys.executable,
        "-m",
        "benchmarks.benchmark_store_comparison",
        "--store",
        f"{store.store_id}={store_path}",
        "--reps",
        str(reps),
        "--skip-rss",
        "--output",
        str(output_path),
    ]
    if anchor.exposure:
        command += ["--exposure", anchor.exposure]
    if anchor.phewas_alid:
        command += ["--phewas-alid", anchor.phewas_alid]
    if anchor.region:
        command += ["--region", anchor.region]

    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(core.checkout) + (os.pathsep + existing if existing else "")
    )
    proc = subprocess.run(
        command,
        cwd=str(core.checkout),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "status": "error",
            "returncode": proc.returncode,
            "detail": (
                f"generic harness exited {proc.returncode}; see stderr for the "
                "first failing shape"
            ),
            "stderr_tail": proc.stderr[-4000:],
            "stdout_tail": proc.stdout[-2000:],
        }
    try:
        artifact = json.loads(Path(output_path).read_text(encoding="utf-8"))
        record = artifact["stores"][0]
    except (OSError, KeyError, ValueError) as exc:
        return {
            "status": "error",
            "returncode": 0,
            "detail": f"generic harness wrote no readable artifact: {exc}",
            "stdout_tail": proc.stdout[-2000:],
        }
    return {
        "status": "measured",
        "timings": record.get("timings", []),
        "harness_hm3_extract": record.get("hm3_extract"),
        "harness": artifact.get("harness", "benchmark_store_comparison"),
    }


# --------------------------------------------------------------------------- #
# Report assembly and rendering
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(UTC).isoformat()


def build_report(
    core: CoreModules,
    stores: list[StoreRef],
    *,
    artifact_root: Path,
    anchors: dict[str, Anchor],
    reps: int,
    hm3_only: bool,
    work_dir: Path,
) -> dict[str, Any]:
    """Measure every Store and assemble the aggregate report artifact."""
    work_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for store in stores:
        store_path = artifact_root / store.store_id / "store.opengwasdb"
        entry: dict[str, Any] = {
            "store_id": store.store_id,
            "layout": store.layout,
            "dense_component": store.dense_component,
            "artifact_path": str(store_path),
            "analysis_id": store.analysis_id,
        }
        if not store_path.is_dir():
            entry["hm3_extract"] = {
                "subset": DEFAULT_SUBSET_NAME,
                "status": "artifact_absent",
                "detail": (
                    f"{store_path} is not a built Store Release; the report "
                    "records absence rather than opening a non-Store"
                ),
            }
            entry["shapes"] = {
                "status": "not_run",
                "detail": "artifact absent",
            }
            records.append(entry)
            continue

        entry["hm3_extract"] = measure_hm3(core, store, store_path, reps=reps)

        if store.ragged:
            entry["shapes"] = {
                "status": "not_applicable",
                "detail": "Ragged-only Store: no Dense query shapes",
            }
        elif hm3_only or store.store_id not in anchors:
            entry["shapes"] = {
                "status": "not_configured",
                "detail": (
                    "--hm3-only"
                    if hm3_only
                    else f"no anchor entry for {store.store_id} in the anchor map"
                ),
            }
        else:
            entry["shapes"] = run_generic_shapes(
                core,
                store,
                store_path,
                anchors[store.store_id],
                reps=reps,
                output_path=work_dir / f"{store.store_id}.shapes.json",
            )
        records.append(entry)

    summary: dict[str, int] = {}
    for entry in records:
        status = str(entry["hm3_extract"].get("status", "unknown"))
        summary[status] = summary.get(status, 0) + 1
    shape_errors = [
        entry["store_id"]
        for entry in records
        if entry.get("shapes", {}).get("status") == "error"
    ]
    return {
        "harness": "release_benchmark_report",
        "issue": "opengwas/opengwasdb-stores#212",
        "epic": "opengwas/opengwasdb-stores#214",
        "measured_at": _now(),
        "reps": reps,
        "hm3_only": hm3_only,
        "artifact_root": str(artifact_root),
        "hm3_source": "benchmarks._query_shapes.measure_hm3_extract",
        "shapes_source": "benchmarks.benchmark_store_comparison (one Store per run)",
        "core": {
            "checkout": str(core.checkout),
            "revision": core.revision,
            "opengwasdb_path": core.opengwasdb_path,
            "opengwasdb_fingerprint": core.opengwasdb_fingerprint,
        },
        "summary": summary,
        "shape_errors": shape_errors,
        "stores": records,
    }


def _md_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _number(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    """Render the generated Markdown view of the report artifact."""
    core = report.get("core", {})
    lines: list[str] = [
        "# Release benchmark report: HM3 Indexed Variant Subset",
        "",
        "Generated by `resources/scripts/release-benchmark-report.py` "
        "(stores#212, epic#214). **Regenerate, do not hand-edit.**",
        "",
        f"- measured at: `{report.get('measured_at', '')}`",
        f"- opengwasdb checkout: `{core.get('checkout', '')}`",
        f"- checkout revision: `{core.get('revision', '')}`",
        f"- opengwasdb fingerprint: `{core.get('opengwasdb_fingerprint', '')}`",
        f"- artifact root: `{report.get('artifact_root', '')}`",
        f"- repetitions: {report.get('reps', '')}",
        f"- HM3 timing source: `{report.get('hm3_source', '')}`",
        f"- shapes source: `{report.get('shapes_source', '')}`",
        "",
        "## Stores",
        "",
        "| Store | Layout | HM3 status | median_ms | first_read_ms | p95_ms | "
        "results | subset variants | shapes | detail |",
        "|:---|:---|:---|---:|---:|---:|---:|---:|:---|:---|",
    ]
    for entry in report.get("stores", []):
        hm3 = entry.get("hm3_extract", {})
        shapes = entry.get("shapes", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(entry.get("store_id")),
                    _md_cell(entry.get("layout")),
                    _md_cell(hm3.get("status")),
                    _number(hm3.get("median_ms")),
                    _number(hm3.get("first_read_ms")),
                    _number(hm3.get("p95_ms")),
                    _md_cell(hm3.get("result_count")),
                    _md_cell(hm3.get("subset_n_variants")),
                    _md_cell(shapes.get("status")),
                    _md_cell(hm3.get("detail")),
                ]
            )
            + " |"
        )

    lines += [
        "",
        "## Summary",
        "",
    ]
    for status, count in sorted(report.get("summary", {}).items()):
        lines.append(f"- `{status}`: {count}")
    shape_errors = report.get("shape_errors", [])
    if shape_errors:
        lines.append(
            "- shape harness errors (real benchmark failures): "
            + ", ".join(shape_errors)
        )
    lines += [
        "",
        "## Interpreting HM3 status",
        "",
        "HM3 is an **optional default derived artifact**, not a Store validity",
        "requirement. A benchmark that cannot measure it reports why, and never",
        "records a zero:",
        "",
        "- `measured` — a structurally valid `hm3` subset is published and its",
        "  extraction was timed (`median_ms`, `first_read_ms`, `p95_ms`,",
        "  `result_count`; `result_count == 0` is a real result, not a missing",
        "  index).",
        "- `missing` — the Store has a Dense Component and publishes no `hm3`",
        "  subset.",
        "- `not_applicable` — the Store is Ragged-only; an Indexed Variant Subset",
        "  is Dense-only (ADR 0061).",
        "- `unsupported` — a published group declares a subset schema or encoding",
        "  this build cannot read (forward compatibility).",
        "- `not_queryable` / `no_anchor` / `artifact_absent` — wrapper operational",
        "  states: the Store could not be opened for querying, has no first",
        "  Analysis to anchor on, or is not built under the artifact root.",
        "",
        "**HM3 benchmark absence is operational evidence, not Store validation**",
        "**failure.** None of the non-`measured` statuses fails this run or any",
        "bundle check; a subset that is present and structurally valid but whose",
        "extraction raises is a real defect and propagates rather than being",
        "reported as absent.",
    ]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--opengwasdb-checkout",
        required=True,
        metavar="PATH",
        help="the opengwasdb checkout holding benchmarks/_query_shapes.py and "
        "opengwasdb/ (required: the tracked pin predates Indexed Variant Subsets)",
    )
    parser.add_argument(
        "--registry-root",
        default=None,
        help="directory of registered Release Bundles (default: <repo>/stores)",
    )
    parser.add_argument(
        "--artifact-root",
        default=None,
        help="Release Artifact root (default: resolved by ogstores.paths)",
    )
    parser.add_argument(
        "--store",
        action="append",
        default=None,
        metavar="OGS-XXXXX",
        help="report only this Store; repeatable (default: every registered Store)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="report every registered Store (the default; explicit for scripts)",
    )
    parser.add_argument(
        "--anchors",
        default=None,
        metavar="PATH",
        help=f"per-Store seven-shape anchor map (default: {DEFAULT_ANCHORS})",
    )
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument(
        "--hm3-only",
        action="store_true",
        help="measure HM3 for every Store and skip the seven-shape harness",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="scratch dir for per-Store harness artifacts (default: a temp dir)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    import tempfile

    try:
        core = load_core(args.opengwasdb_checkout)
    except CheckoutError as exc:
        raise SystemExit(f"error: {exc}") from None
    registry_root = (
        Path(args.registry_root) if args.registry_root else REPO_ROOT / "stores"
    )
    artifact_root = resolve_artifact_root(args.artifact_root, REPO_ROOT)
    anchors = load_anchors(args.anchors or (REPO_ROOT / DEFAULT_ANCHORS))

    stores = registered_bundles(registry_root)
    if args.store and args.all:
        raise SystemExit("--store and --all are mutually exclusive")
    if args.store:
        wanted = set(args.store)
        stores = [s for s in stores if s.store_id in wanted]
        missing = sorted(wanted - {s.store_id for s in stores})
        if missing:
            raise SystemExit(f"unknown Store id(s): {', '.join(missing)}")

    with tempfile.TemporaryDirectory(prefix="hm3-release-benchmark-") as tmp:
        work_dir = Path(args.work_dir) if args.work_dir else Path(tmp)
        report = build_report(
            core,
            stores,
            artifact_root=artifact_root,
            anchors=anchors,
            reps=args.reps,
            hm3_only=args.hm3_only,
            work_dir=work_dir,
        )

    _write(Path(args.output), json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write(Path(args.report), render_markdown(report))

    summary = report["summary"]
    print(
        "HM3 release benchmark report: "
        + ", ".join(f"{status}={count}" for status, count in sorted(summary.items()))
    )
    print(f"wrote {args.output}")
    print(f"wrote {args.report}")
    return 1 if report["shape_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
