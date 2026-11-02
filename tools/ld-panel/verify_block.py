#!/usr/bin/env python3
"""Verify one written LD block against the genotypes it was built from (#241).

The public API is :func:`verify_block`. It reads the artifacts, not the
constructor's in-memory result, and checks: one row per eigenvector row,
canonical unique ALIDs, finite non-negative descending eigenvalues, provenance
agreement, that the stored components reach the consumer's truncation, EAF,
and that every eigenvector row is bound to its TSV row's variant and effect
allele. That binding check is exact: with X the standardised effect-allele
dosages, ``||X v_j||^2`` equals eigenvalue j, which a permuted or flipped row
breaks. The block is then loaded through core's public LD-panel reader.

Two checks exist because of #348. EAF is recomputed from the allele PLINK's
export actually counts -- this plink2 counts REF, and its column suffix names
it -- so the check can no longer agree with a constructor that assumed ALT. And
:func:`orientation_gate` measures a panel's EAF against an independent
reference (the UKB ancestry mixture, one group per ancestry), positively
oriented, which is what a rebuilt or repaired panel has to pass.

A block carrying a repair record is checked differently: a row drop breaks the
exact binding identity, so a repaired block is held to its pre-repair backup
instead -- its vectors must be the backup's with exactly the recorded rows
removed, and its values must be unchanged.
"""
from __future__ import annotations

import argparse, csv, gzip, json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from construct_block import _plink_extract, canonical_alid, standardised_ea_dosages
from find_underresolved_blocks import components_needed
from scratch import scratch_dir

# The UKB ancestry-mixture reference, and the group that stands in for each
# panel's ancestry (ADR 0020's panels are the HGDP+1kGP super-populations; the
# groups are the ones notes/348 used).
ANCESTRY_REFERENCE = Path("/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz")
ANCESTRY_GROUPS = {"AFR": "Africa (West)", "EAS": "Asia (East)", "SAS": "Pakistan",
                   "EUR": "United Kingdom", "AMR": "South America"}


def panel_rows(base: Path) -> list[dict[str, str]]:
    """One block's variant table, as written."""
    with base.with_suffix(".tsv").open(newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def repair_record(provenance: dict[str, object]) -> dict[str, object] | None:
    """The last repair a block records, or None for a block never repaired."""
    repairs = provenance.get("repairs") or []
    return repairs[-1] if repairs else None


def repaired_failures(backup: Path | None, rows: list[dict[str, str]], *, base: Path,
                      repair: dict[str, object]) -> list[str]:
    """Whether a repaired block is its pre-repair backup with the recorded rows gone.

    A row drop is exact for the stored LD (``values`` and the surviving rows are
    untouched) but not for the binding identity, which needs every column, and a
    shipped AFR/EAS/SAS/EUR table cannot fall back to genotypes either: the
    surviving ALID of a collision still names two source records and the table
    has no REF/ALT to tell them apart. So the identity a repaired block must
    satisfy is its backup's: the surviving table rows, in order, with every
    column but ``EAF`` byte-identical and ``EAF`` exactly one minus the old;
    ``vectors`` restricted to the same rows; and ``values`` unchanged. The table
    check is what ties the rows to the vectors -- row order is the only link
    between the files, so reversing one file must not pass (#348).
    """
    if backup is None: return ["repair_backup"]
    original_npz, original_tsv = backup.with_suffix(".ldeig.npz"), backup.with_suffix(".tsv")
    if not (original_npz.exists() and original_tsv.exists()): return ["repair_backup"]
    # No float cast: a repaired block must be the backup's bytes, so a dtype
    # change that leaves every number alone is still a mismatch (#348 review).
    with np.load(original_npz) as data:
        before_values, before_vectors = data["values"], data["vectors"]
    with np.load(base.with_suffix(".ldeig.npz")) as data:
        values, vectors = data["values"], data["vectors"]
    try:
        dropped = [int(i) for i in repair["dropped_row_indices"]]
    except (KeyError, TypeError, ValueError):
        return ["repair_record"]
    if (len(set(dropped)) != len(dropped)
            or any(i < 0 or i >= before_vectors.shape[0] for i in dropped)
            or before_vectors.shape[0] != len(rows) + len(dropped)):
        return ["repair_record"]
    keep = np.setdiff1d(np.arange(before_vectors.shape[0]), dropped)
    failed = []
    # The table: the surviving rows, in order, with every column but EAF
    # byte-identical and EAF exactly one minus the old value.
    before_lines = original_tsv.read_text().splitlines()
    before_header = before_lines[0].split("\t")
    before_rows = [line.split("\t") for line in before_lines[1:]]
    kept_rows = [[row[name] for name in before_header] for row in (dict(zip(before_header, r)) for r in before_rows)]
    kept_rows = [kept_rows[i] for i in keep]
    current = [[row.get(name) for name in before_header] for row in rows]
    if (not rows or len(before_header) != len(rows[0]) or not all(set(row) == set(before_header) for row in rows)
            or [[v for n, v in zip(before_header, row) if n != "EAF"] for row in current]
            != [[v for n, v in zip(before_header, row) if n != "EAF"] for row in kept_rows]):
        failed.append("repair_rows")
    else:
        column = before_header.index("EAF")
        if [float(row[column]) for row in current] != [1 - float(row[column]) for row in kept_rows]:
            failed.append("repair_eaf")
    if (before_values.shape != values.shape or before_values.dtype != values.dtype
            or before_values.tobytes() != values.tobytes()):
        failed.append("repair_values")
    kept_vectors = before_vectors[keep]
    if (kept_vectors.shape != vectors.shape or kept_vectors.dtype != vectors.dtype
            or kept_vectors.tobytes() != vectors.tobytes()):
        failed.append("repair_vectors")
    return failed


def verify_block(base: Path, genotypes: np.ndarray | None = None,
                 variants: list[dict[str, object]] | None = None, *,
                 thresh: float = 0.9, rtol: float = 1e-6, backup: Path | None = None) -> dict[str, object]:
    """Return a report whose ``failures`` lists every failed check by name.

    ``backup`` is the pre-repair copy of a block that carries a repair record,
    and such a block is checked **against it instead of the genotypes**: a
    shipped AFR/EAS/SAS/EUR table keeps an ALID that still names two source
    records and carries no REF/ALT to tell them apart, so it cannot be resolved
    against genotypes at all (#348). Genotypes are then neither required nor
    read, and the exact binding identity gives way to the backup identity.
    """
    from opengwasdb.completion.ld_panel import canonical_panel_alid, list_all_blocks, load_ld_eigenvectors

    rows = panel_rows(base)
    data = np.load(base.with_suffix(".ldeig.npz"))
    values, vectors = data["values"].astype(np.float64), data["vectors"].astype(np.float64)
    provenance = json.loads(base.with_suffix(".provenance.json").read_text())
    repair = repair_record(provenance)
    failures: list[str] = []
    def require(ok: bool, name: str) -> None:
        if not ok and name not in failures: failures.append(name)

    def names(row: dict[str, str], first: str, second: str) -> bool:
        try: return canonical_alid(row["CHR"], row["BP"], row[first], row[second]) == row["SNP"]
        except (ValueError, TypeError, AttributeError): return False
    alids = [r["SNP"] for r in rows]
    require(len(rows) == vectors.shape[0] == provenance["n_variants_retained"], "row_count")
    require(len(set(alids)) == len(alids) and all(
        (r["EA"] or "") < (r["OA"] or "") and names(r, "EA", "OA") and r["SNP"] == canonical_panel_alid(r["SNP"])
        for r in rows), "alids")
    # A row's source record, where recorded, must be the variant its ALID names.
    if rows and {"REF", "ALT"} <= set(rows[0]):
        require(all(names(r, "REF", "ALT") for r in rows), "alids")
    finite = bool(np.isfinite(values).all() and np.isfinite(vectors).all())
    require(finite, "finite")
    require(bool((values >= 0).all()), "non_negative")
    require(bool((np.diff(values) <= 0).all()), "descending")
    require(provenance["components_retained"] == vectors.shape[1], "provenance")
    needed = components_needed(values, thresh)
    require(needed <= vectors.shape[1], "resolved_at_thresh")

    binding_error = None
    if repair is not None:
        for name in repaired_failures(backup, rows, base=base, repair=repair):
            require(False, name)
    elif genotypes is None or variants is None:
        require(False, "binding")  # an unrepaired block can only be checked against its genotypes
    else:
        try:
            x, eaf = standardised_ea_dosages(genotypes, variants, rows)
        except (KeyError, ValueError, TypeError, AttributeError):  # a row names no source record, or more than one
            x = None
            require(False, "binding")
        # The EAF the table reports must be the effect allele's frequency,
        # computed from the genotypes through the allele PLINK's export counted.
        if finite and x is not None and "row_count" not in failures:
            require(np.allclose(eaf, [float(r["EAF"]) for r in rows], rtol=0, atol=1e-9), "eaf")
        if finite and x is not None and "binding" not in failures and "row_count" not in failures:
            projected = ((x @ vectors) ** 2).sum(axis=0)
            binding_error = float(np.max(np.abs(projected - values[:vectors.shape[1]])) / values[0])
            require(binding_error <= rtol, "binding")

    ld_dir, ancestry, chrom = base.parent.parent.parent, base.parent.parent.name, base.parent.name
    blocks = [b for b in list_all_blocks(ld_dir, ancestry, chrom) if b.tsv_path.name == f"{base.name}.tsv"]
    core_k = 0
    require(len(blocks) == 1 and blocks[0].n_ld_snps == len(rows), "core_load")
    if len(blocks) == 1:
        core_values, core_vectors = load_ld_eigenvectors(blocks[0], thresh=thresh)
        core_k = int(core_vectors.shape[1]); k = min(needed, vectors.shape[1])
        require(core_k == k and np.array_equal(core_vectors, vectors[:, :k]) and
                np.array_equal(core_values, values[:k]), "core_load")
    k = vectors.shape[1]
    positive = np.maximum(values, 0.0)
    return {
        "block": f"{chrom}/{base.name}", "failures": failures, "n_variants": len(rows),
        "rank": int((values > values[0] * 1e-10).sum()) if finite else None,
        "components_retained": k, "components_at_thresh": needed, "core_components_at_thresh": core_k,
        "thresh": thresh, "achieved_variance": float(positive[:k].sum() / (positive.sum() or 1.0)),
        "condition_number": float(values[0] / values[k - 1]) if finite and values[k - 1] > 0 else None,
        "binding_relative_error": binding_error, "repaired": repair is not None,
        "rows_dropped_by_repair": len(repair["dropped_row_indices"]) if repair else 0,
    }


def reference_ea_frequencies(reference: Path, group: str,
                             wanted: dict[str, str]) -> dict[str, float]:
    """The reference's frequency of each wanted ALID's effect allele.

    ``wanted`` maps an ALID to the effect allele a panel row names (its ``EA``).
    The reference holds one frequency per ALID, for its **own** effect allele,
    so each one is oriented to the panel's allele before it is returned (#348).
    A missing or out-of-range frequency is left out rather than counted as
    zero, and the scan stops once every wanted ALID is found.
    """
    if not reference.exists():
        raise ValueError(f"the ancestry-mixture reference {reference} is not there; it is a build input, "
                         "not something a gate may skip")
    found: dict[str, float] = {}
    with gzip.open(reference, "rt", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try: header = next(reader)
        except StopIteration: raise ValueError(f"{reference} is empty") from None
        for column, name in (("alid", "alid"), ("effect_allele", "effect_allele")):
            if name not in header: raise ValueError(f"{reference} has no {name!r} column")
        if group not in header:
            raise ValueError(f"{reference} has no {group!r} column; it has {header}")
        # Indices rather than DictReader: this is 5.8M rows, and a dict per row
        # costs gigabytes for nothing.
        alid_column, effect_column, frequency_column = header.index("alid"), header.index(
            "effect_allele"), header.index(group)
        for row in reader:
            alid = row[alid_column] if len(row) > alid_column else None
            if alid not in wanted or alid in found: continue
            try: frequency = float(row[frequency_column])
            except (IndexError, ValueError): continue
            if not 0 <= frequency <= 1: continue
            effect = row[effect_column].upper() if len(row) > effect_column else ""
            found[alid] = frequency if effect == wanted[alid].upper() else 1 - frequency
            if len(found) == len(wanted): break
    return found


def panel_eaf_vs_reference(bases: list[Path], ancestry: str, *, reference: Path = ANCESTRY_REFERENCE,
                           group: str | None = None) -> dict[str, object]:
    """Correlate a panel's stored EAF with an independent reference's, oriented to its EA.

    A correctly oriented panel correlates positively (0.9 or better on the
    panels this repository builds); the shipped panels, whose EAF is the other
    allele's frequency, correlate as strongly and negatively (#348).
    """
    group = group or ANCESTRY_GROUPS.get(ancestry)
    if group is None:
        raise ValueError(f"no reference group for ancestry {ancestry!r}; pass --reference-group")
    wanted: dict[str, str] = {}
    total = 0
    for base in bases:
        for row in panel_rows(base):
            total += 1
            wanted.setdefault(row["SNP"], row["EA"])
    found = reference_ea_frequencies(reference, group, wanted)
    panel, expected = [], []
    for base in bases:
        for row in panel_rows(base):
            if row["SNP"] in found:
                panel.append(float(row["EAF"])); expected.append(found[row["SNP"]])
    r = None
    if len(panel) >= 3 and np.ptp(panel) > 0 and np.ptp(expected) > 0:
        r = float(np.corrcoef(panel, expected)[0, 1])
    return {"ancestry": ancestry, "group": group, "reference": str(reference),
            "blocks": len(bases), "n_rows": total, "n_matched": len(panel), "r": r}


def sample_blocks(blocks: list[Path], count: int) -> list[Path]:
    """Up to ``count`` blocks, spread evenly over the list, so a sample is not one chromosome.

    This is the one sampling policy: the verifier's panel mode, the build paths
    and the repair all draw their sample here, so they gate on comparable
    evidence (#348 review).
    """
    if count <= 0 or not blocks: return []
    step = max(1, len(blocks) // count)
    return blocks[::step][:count]


def gate_panel(panel_root: Path, ancestry: str, *, reference: Path = ANCESTRY_REFERENCE,
               group: str | None = None, blocks: int = 5, min_matches: int = 1000,
               require: object = None) -> dict[str, object]:
    """Gate a panel an operator is building or finishing, and refuse the run if it fails.

    The one place the build paths share (#348): the candidates under
    ``panel_root/<ancestry>`` (filtered by ``require`` where a caller has its own
    notion of built), the sample, the reference that must exist, the refusal and
    the report all live here, so ``run_panels`` and ``build_amr_panel`` cannot
    drift apart. This is the recurrence guard: the shipped panels' EAF was the
    other allele's frequency, uniformly and exactly, and an independent
    reference is the only evidence that says so. An ancestry the run was asked
    for with nothing to gate is a refusal too -- a run may not report a panel it
    did not build.
    """
    candidates = sorted(path.with_suffix("") for path in (panel_root / ancestry).glob("*/*.tsv"))
    if require is not None: candidates = [base for base in candidates if require(base)]  # type: ignore[operator]
    if not candidates:
        raise SystemExit(f"orientation gate: no built block under {panel_root / ancestry}: the run was "
                         f"asked for {ancestry} and cannot report a panel it did not build")
    sample = sample_blocks(candidates, blocks)
    try:
        report = orientation_gate(sample, ancestry, reference=reference, group=group, min_matches=min_matches)
    except ValueError as exc:
        raise SystemExit(f"orientation gate: {exc}") from None
    report["sampled"] = [f"{b.parent.name}/{b.name}" for b in sample]
    if report["failures"]:
        raise SystemExit(
            f"orientation gate fails for {ancestry}: r={report['r']} over {report['n_matched']} matched "
            f"variants against {report['group']!r} ({report['failures']}); a block's EAF must be the "
            "frequency of its EA (#348) -- repair the panel with tools/ld-panel/repair_panel.py")
    print(f"orientation r={report['r']:.4f} matched={report['n_matched']} blocks={len(sample)} "
          f"ancestry={ancestry} group={report['group']}", flush=True)
    return report


def orientation_gate(bases: list[Path], ancestry: str, *, reference: Path = ANCESTRY_REFERENCE,
                     group: str | None = None, min_matches: int = 1000,
                     min_r: float = 0.9) -> dict[str, object]:
    """The panel-level positive-orientation gate: ``r > min_r`` on enough matches.

    It refuses, rather than passing, a sample too small to decide on: a gate
    that can pass on eleven variants decides nothing (#348).
    """
    measured = panel_eaf_vs_reference(bases, ancestry, reference=reference, group=group)
    failures: list[str] = []
    if measured["n_matched"] < min_matches:
        failures.append("orientation_matches")
    elif measured["r"] is None or not measured["r"] > min_r:
        failures.append("orientation")
    return {**measured, "min_matches": min_matches, "min_r": min_r, "failures": failures}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, help="Block path without suffix")
    ap.add_argument("--backup", type=Path, help="The block's pre-repair copy, for a repaired block (#348)")
    ap.add_argument("--pfile", type=Path); ap.add_argument("--keep", type=Path)
    ap.add_argument("--chrom"); ap.add_argument("--start", type=int); ap.add_argument("--end", type=int)
    ap.add_argument("--thresh", type=float, default=.9); ap.add_argument("--plink2", default="plink2")
    gate = ap.add_argument_group("panel orientation gate (#348)")
    gate.add_argument("--panel", type=Path, help="Panel root: run the gate on a sample of its blocks instead")
    gate.add_argument("--ancestry"); gate.add_argument("--reference-group")
    gate.add_argument("--ancestry-reference", type=Path, default=ANCESTRY_REFERENCE)
    gate.add_argument("--orientation-blocks", type=int, default=5, help="Blocks to sample (evenly spread)")
    gate.add_argument("--min-orientation-matches", type=int, default=1000)
    ap.add_argument("--out", type=Path, required=True, help="Report JSON")
    args = ap.parse_args()

    if args.panel is not None:
        if not (args.ancestry and args.reference_group):
            ap.error("--panel needs --ancestry and --reference-group")
        blocks = sorted((args.panel / args.ancestry).glob("*/*.tsv"))
        if not blocks:
            ap.error(f"no blocks under {args.panel / args.ancestry}")
        bases = sample_blocks([p.with_suffix("") for p in blocks], args.orientation_blocks)
        try:
            report = orientation_gate(bases, args.ancestry, reference=args.ancestry_reference,
                                      group=args.reference_group,
                                      min_matches=args.min_orientation_matches)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        report["sampled"] = [f"{b.parent.name}/{b.name}" for b in bases]
    else:
        # A repaired block is checked against its backup, so which alleles its
        # surviving rows count is not in question and no genotypes are needed.
        provenance = json.loads(args.base.with_suffix(".provenance.json").read_text()) if args.base else {}
        repaired = repair_record(provenance) is not None
        needed = () if repaired else ("pfile", "keep", "chrom", "start", "end")
        missing = [name for name in ("base", *needed) if getattr(args, name) is None]
        if missing:
            ap.error(f"a block check needs {', '.join('--' + m for m in missing)}")
        genotypes = variants = None
        if not repaired:
            source = SimpleNamespace(plink2=args.plink2, pfile=args.pfile, bfile=None, chrom=args.chrom,
                                     start=args.start, end=args.end, keep=args.keep)
            with scratch_dir("ld-verify-") as tmp:
                genotypes, variants, _ = _plink_extract(source, Path(tmp))
        report = verify_block(args.base, genotypes, variants, thresh=args.thresh, backup=args.backup)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: report[k] for k in ("failures", "r", "n_matched") if k in report}))
    return 1 if report["failures"] else 0


if __name__ == "__main__": raise SystemExit(main())
