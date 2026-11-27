"""The LD section's measures (#504): one LD Store or Genotype Store, inside a fresh worker process.

Every query goes through `ogquery.ld.open`, which reads an LD Store with `StoreReader` and a
Genotype Store with `GenotypeReader` (the `genotype` extra); nothing here imports the engine.
The timing method is the GWAS section's (`yardstick.measure.time_calls`: one first read, kept
apart, then the warm reads), and so is peak memory (`yardstick.measure.sample_peak`, one
`region_3mb` read in a fresh interpreter). The sizes are opengwas/opengwasdb-stores#504's
table, the shapes of opengwasdb's `scripts/ld/bench/latency.py` (ogld#8): `matrix` of 10 and
500 variants, `region` 1 and 3 Mb wide, `proxies` of 1, 10 and 100 variants at r² ≥ 0.8
within 10 Mb, and `clump` of opengwasdb#403's 37 Analyses' hits at r² 0.01 within 3 Mb.

`python -m yardstick.ld pick LOCATION` draws the workload's variants, read-only, as
`latency.py` drew them (below), and prints them as the workload's YAML.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import ogquery.ld
from ogquery.ld.format import claims_partner_rows

from yardstick.measure import attempt, sample_peak, time_calls
from yardstick.ld_queries import LD_MATRIX, LD_PROXIES, LD_REGIONS
from yardstick.workload import HITS_COLUMNS, LD_MATRIX_N, LD_MEASURES, hits_header, read_clump

#: The queries that read a Store's Partner Rows, which a Store claiming none cannot answer.
PARTNER_QUERIES = (*LD_PROXIES, "clump")
#: The query whose peak memory is sampled in a fresh process: the largest read every kind answers.
PEAK_QUERY = "region_3mb"


def matrix_inputs(variants: list[str], n: int) -> list[str]:
    """`matrix_N`'s variants from the workload's 500: all, or every (500 / N)th, so they stay scattered."""
    return list(variants) if n == len(variants) else list(variants[:: LD_MATRIX_N // n])


def variants_sha256(variants: list[str]) -> str:
    """The digest of a variant list, as the result records it: one ALID a line, in order."""
    return hashlib.sha256("".join(f"{v}\n" for v in variants).encode()).hexdigest()


def inapplicable(ld_id: str, claims: bool) -> dict[str, dict[str, Any]]:
    """The measures a Store cannot answer, each `not_applicable` with its reason, from whether it claims Partner Rows.

    `claims` is ogquery's own rule, `ogquery.ld.format.claims_partner_rows`, asked of the Store
    the worker opened: a recorded `t_partner` and, for an LD Store, format 0.2.0 or later, or a
    Genotype Store's sidecar (ld-store §7, §13).
    """
    if claims:
        return {}
    reason = (f"{ld_id} claims no Partner Rows (ogquery.ld.format.claims_partner_rows: a recorded "
              "t_partner and, for an LD Store, format 0.2.0 or later), which proxies and clump read (ld-store §13)")
    return {name: {"status": "not_applicable", "reason": reason} for name in PARTNER_QUERIES}


def parse_hits(where: str, data: bytes) -> tuple[list[str], Any]:
    """One Analysis's hits file: its ALIDs and |z|, the clump's score, as latency.py's `hits` reads them.

    The header must be the columns `HITS_COLUMNS` names, in order (`workload.hits_header`, the
    preflight's parser too), and every row exactly those two fields: a file of other columns is
    refused, never read by position.
    """
    import numpy as np

    header = hits_header(data)
    if header != HITS_COLUMNS:
        raise ValueError(f"{where}: header {header}, not {HITS_COLUMNS}")
    alids: list[str] = []
    scores: list[float] = []
    for number, line in enumerate(data.decode("utf-8").splitlines()[1:], 2):
        fields = line.split("\t")
        if len(fields) != len(HITS_COLUMNS):
            raise ValueError(f"{where}:{number}: {len(fields)} fields, not {len(HITS_COLUMNS)}")
        alids.append(fields[0])
        scores.append(abs(float(fields[1])))
    return alids, np.asarray(scores, dtype=np.float64)


def bytes_read(location: Path, also: Sequence[Path] = ()) -> int:
    """The bytes of every file a query of the Store can read: every file under `location`, links followed.

    An overlay's `near/`, `far/` and `partners/` are links into other Stores, and its queries read
    them, so they count. `also` are files outside the walk its queries read too: a Genotype Store's
    pinned pgen, psam and pvar files. Each real file counts once, by its device and inode, however
    many links, pins or filesets reach it, and a directory is walked once, so a link cycle ends. A
    dangling link or an unreadable directory raises rather than being skipped.
    """
    files: set[tuple[int, int]] = set()
    directories: set[tuple[int, int]] = set()
    total = 0
    for path in also:
        status = os.stat(path)
        if (status.st_dev, status.st_ino) not in files:
            files.add((status.st_dev, status.st_ino))
            total += status.st_size
    pending = [location]
    while pending:
        directory = pending.pop()
        status = directory.stat()
        if (status.st_dev, status.st_ino) in directories:
            continue
        directories.add((status.st_dev, status.st_ino))
        for entry in os.scandir(directory):
            status = os.stat(entry.path)  # follows the link; a dangling one raises
            key = (status.st_dev, status.st_ino)
            if entry.is_dir(follow_symlinks=True):
                pending.append(Path(entry.path))
            elif key not in files:
                files.add(key)
                total += status.st_size
    return total


def store_bytes(location: Path, kind: str, provenance: dict[str, Any]) -> dict[str, Any]:
    """The Store's size on disk: `bytes`, every file its queries read, so two kinds compare.

    `bytes` follows links (`bytes_read`): an overlay's linked pairs count. A Genotype Store adds
    `genotype_bytes`, the pgen, psam and pvar files its provenance pins, which its queries read
    (`ld_registry.writer.genotype_bytes`, which refuses a pin that does not resolve or whose size
    is not its own). `store_bytes` is the LD Store Registry's own figure, its records'
    `measured.store_bytes`: a Store's own files, no link followed (`store_size_on_disk`), so it
    differs from `bytes` exactly where a Store links elsewhere or reads genotypes beside it.
    """
    from ld_registry.writer import genotype_bytes
    from ogstores.store_size import store_size_on_disk

    pinned: list[Path] = []
    record: dict[str, Any] = {"status": "measured", "source": "walk", "store_bytes": store_size_on_disk(location)}
    if kind == "genotype_store":
        # The registry's figure, and its check of every pin (present, its recorded size); the
        # pins it has checked then join the walk's set, so a file reached twice counts once.
        record["genotype_bytes"] = genotype_bytes(location, provenance)
        pinned = [location / fileset[part]["path"] for fileset in provenance["filesets"] for part in ("pgen", "psam", "pvar")]
    record["bytes"] = bytes_read(location, pinned)
    return record


def measure_ld_store(entry: dict[str, Any], repetitions: dict[str, int]) -> dict[str, Any]:
    """Every LD measure but `peak_rss` on one Store; `entry` is the runner's spec (`runner._ld_entry`)."""
    reps_of = lambda name: repetitions.get(name, repetitions["default"])  # noqa: E731
    location = Path(entry["location"])
    wanted = [name for name in LD_MEASURES if name != "peak_rss"]
    out: dict[str, Any] = {}
    known: dict[str, dict[str, Any]] = {}

    def take(name: str, fn: Callable[[], dict[str, Any]]) -> None:
        if name in known:
            out[name] = known[name]
        else:
            attempt(out, wanted, name, fn)

    # The timed open is this process's first: nothing has opened the Store before it.
    take("open", lambda: time_calls(lambda: ogquery.ld.open(location).close(), reps_of("open")))
    store = ogquery.ld.open(location)
    try:
        # What the Store cannot answer, from the Store the queries run on, by ogquery's own rule.
        known.update(inapplicable(entry["ld_id"], claims_partner_rows(dict(store.provenance))))
        matrix = entry["matrix"]
        for name, n in LD_MATRIX.items():
            alids = matrix_inputs(matrix["variants"], n)
            take(name, lambda name=name, alids=alids: _with_absent(
                lambda: store.matrix(alids), reps_of(name), lambda m: len(m.variants),
            ))
        region = entry["region"]
        for name, width in LD_REGIONS.items():
            take(name, lambda name=name, width=width: time_calls(
                lambda: store.region(region["chromosome"], region["centre"], width // 2),
                reps_of(name), count=lambda m: len(m.variants),
            ))
        proxies = entry["proxies"]
        for name in LD_PROXIES:
            alids = proxies["variants"][name]
            take(name, lambda name=name, alids=alids: _with_absent(
                lambda: store.proxies(alids, r2=proxies["r2"], kb=proxies["kb"]), reps_of(name), len,
            ))

        def clump() -> dict[str, Any]:
            spec = entry["clump"]
            # One read: the bytes hashed are the bytes parsed, and they must be the preflight's.
            digest, _, files = read_clump(Path(spec["path"]), spec["analyses"], spec["file"])
            if digest != spec["sha256"]:
                raise RuntimeError(f"the clump input changed after it was checked: its sha256 is now {digest}, "
                                   f"the run checked {spec['sha256']}")
            names = spec["analyses"]
            hits = [parse_hits(f"{name}/{spec['file']}", files[name]) for name in names]
            last: list[Any] = []

            def every_analysis() -> list[Any]:
                last[:] = [store.clump(alids, score, r2=spec["r2"], kb=spec["kb"]) for alids, score in hits]
                return last

            calls = [0]

            def rows_with_a_lead_each(found: list[Any]) -> int:
                # Every Analysis must return a lead on every call: a non-empty total can hide one that returned none.
                calls[0] += 1
                for name, clumps in zip(names, found, strict=True):
                    if not leads_of(clumps):
                        raise RuntimeError(f"{name} returned no lead (call {calls[0]} of {reps_of('clump') + 1})")
                return sum(len(c) for c in found)

            record = time_calls(every_analysis, reps_of("clump"), count=rows_with_a_lead_each)
            by_analysis = {name: leads_of(clumps) for name, clumps in zip(names, last, strict=True)}
            return {**record, "analyses": len(hits), "hits": sum(len(a) for a, _ in hits),
                    "leads": sum(by_analysis.values()), "min_leads": min(by_analysis.values()),
                    "leads_by_analysis": by_analysis}

        take("clump", clump)
        take("bytes_on_disk", lambda: store_bytes(location, entry["kind"], dict(store.provenance)))
    finally:
        store.close()
    return out


def leads_of(clumps: Any) -> int:
    """How many clumps one Analysis made: its rows that lead their own clump (`lead` is the row's own ALID)."""
    return int((clumps.lead == clumps.alid).sum())


def _with_absent(call: Callable[[], Any], reps: int, count: Callable[[Any], int]) -> dict[str, Any]:
    """`time_calls` of `call`, with `absent`: how many requested ALIDs the Variant Set does not hold."""
    last: list[Any] = []

    def keeping() -> Any:
        last[:] = [call()]
        return last[0]

    record = time_calls(keeping, reps, count=count)
    return {**record, "absent": len(last[0].absent)}


def measure_ld_peak(entry: dict[str, Any]) -> dict[str, Any]:
    """Open the Store and read one `region_3mb`, sampling RSS. Run in a fresh interpreter."""
    store = ogquery.ld.open(Path(entry["location"]))
    try:
        region = entry["region"]
        record = sample_peak(
            lambda: store.region(region["chromosome"], region["centre"], LD_REGIONS[PEAK_QUERY] // 2),
            count=lambda m: len(m.variants),
        )
        return {**record, "query": PEAK_QUERY}
    finally:
        store.close()


# ---------------------------------------------------------------- picking the workload's variants


def draw(store: Any, count: int, seed: int, chromosome: str | None) -> list[str]:
    """`count` variants at seeded rows of one chromosome, or of the genome, as latency.py's `Sample.rows` drew them.

    latency.py takes `default_rng(seed).choice(rows, count, replace=False)` over the rows of the
    chromosome (or of the whole Variant Set, chromosomes in order), sorted. The same draw over the
    same Variant Set picks the same variants, so `matrix_500` on chr22 at seed 8 is ogld#8's.
    """
    import numpy as np

    chromosomes = [chromosome] if chromosome else [str(c) for c in range(1, 23)]
    sizes = [store.count_in(c, 0, 2**40) for c in chromosomes]
    rows = np.sort(np.random.default_rng(seed).choice(np.arange(sum(sizes)), count, replace=False))
    starts = np.cumsum([0, *sizes])
    picked: list[str] = []
    for i, c in enumerate(chromosomes):
        local = rows[(rows >= starts[i]) & (rows < starts[i + 1])] - starts[i]
        if len(local):
            alids = store.variants_in(c, 0, 2**40).alid
            picked += [str(alids[j]) for j in local]
    return picked


def pick(location: Path, *, matrix_chromosome: str = "22", seed: int = 8, r2: float = 0.8, kb: float = 10_000) -> dict[str, Any]:
    """The workload's `matrix` and `proxies` lists from the Store at `location`.

    A proxies list must have a proxy, since an empty answer measures nothing: each takes the first
    seed from `seed` whose draw does, as latency.py drew ten seeds from 8 for its one-variant lists.
    """
    store = ogquery.ld.open(location)
    try:
        seeds: dict[str, int] = {}
        lists: dict[str, list[str]] = {}
        for name, n in LD_PROXIES.items():
            for tried in range(seed, seed + 10):
                drawn = draw(store, n, tried, None)
                if len(store.proxies(drawn, r2=r2, kb=kb)):
                    seeds[name], lists[name] = tried, drawn
                    break
            else:
                raise RuntimeError(f"no draw of {n} variants from seed {seed} has a proxy at r2 >= {r2} within {kb} kb")
        return {
            "matrix": {"chromosome": matrix_chromosome, "seed": seed,
                       "variants": draw(store, LD_MATRIX_N, seed, matrix_chromosome)},
            "proxies": {"r2": r2, "kb": kb, "seeds": seeds, "variants": lists},
        }
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    import yaml

    parser = argparse.ArgumentParser(description="Draw the yardstick's LD variants from one Store, read-only")
    parser.add_argument("command", choices=["pick"])
    parser.add_argument("location", type=Path)
    parser.add_argument("--seed", type=int, default=8)
    args = parser.parse_args(argv)
    yaml.safe_dump(pick(args.location, seed=args.seed), sys.stdout, sort_keys=False, width=200)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
