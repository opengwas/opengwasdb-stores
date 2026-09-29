#!/usr/bin/env python3
"""Acquire dbGaP phs002453 (MVP GIA, PMID 39024449) into a GWAS-SSF mirror.

PMID 39024449 (Verma et al. 2024, VA Million Veteran Program) deposited its
6,022 GIA Analyses on dbGaP as ``phs002453`` (open access).  EBI's *raw*
``GCST*.tsv.gz`` files for the same publication are those dbGaP files with the
columns renamed and reordered and ``NA`` written ``#NA``; EBI was serving them
at ~0.3 MB/s, dbGaP serves the same bytes at ~37 MB/s per stream.  This script
reproduces EBI's conversion so every downstream consumer (inventory freeze,
preflight, the GWAS-SSF reader) sees the same bytes it would have seen from EBI.

Subcommands, in order:

``extract``
    Stream each MD5-verified tar exactly once, writing its members to
    ``GIA/extracted/<tar directory>/<member>``.  A tar has no index, so members
    are read sequentially and their sizes are verified against the tar's
    ``.table_of_contents.txt`` listing; a member is written through a
    ``.partial`` file and renamed, so an interrupted run leaves no half file and
    a re-run skips complete members.

``map``
    Parse every extracted ``*.dbGaP.metadata.txt`` and bind each dbGaP Analysis
    to exactly one PMID 39024449 candidate-table row by
    ``(ancestry, sample_size, n_cases, n_controls)``.  META analyses are
    identified as ``ancestry_fraction < 1`` rather than by their
    ``ancestry_group`` label, which a companion change relabels.  Where several
    candidate rows share the numeric key (89 such groups: the same trait's
    maximum/mean/minimum analyses have equal N) the metadata's *Analyzed
    variable* text selects among them; where it cannot, the Analysis is reported
    ambiguous.  A full run requires a bijection over all 6,022 Analyses;
    ``--analysis``/``--member``/``--limit`` runs check only what they read.

``convert``
    For each mapped Analysis, ``gzip -dc | awk | gzip`` into EBI's mirror layout
    ``gwas-ssf/<bucket>/<GCST>/<GCST>.tsv.gz`` plus ``<GCST>.tsv.gz-meta.yaml``.
    The awk program is header-driven: columns are matched by name, the output
    column set is EBI's, and an unrecognised header aborts the file loudly
    rather than guessing.

``manifest``
    Write one acquisition manifest per candidate-table ``store_key`` with EBI's
    raw-pass manifest columns, ``status=ok`` and the data file's sha256 for
    converted files.  The manifests are consumed by
    ``resources/generators/lib/source_inventory.py``.

Usage (see ``README-acquire-dbgap-phs002453.md`` next to this script).
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_OUT = "/data/opengwasdb/raw/dbgap-phs002453"
DEFAULT_CANDIDATES = "resources/data/derived/store-candidates-analyses.tsv"
PUBMED_ID = "39024449"
DBGAP_BASE = "https://ftp.ncbi.nlm.nih.gov/dbgap/studies/phs002453/analyses/GIA"

#: dbGaP GIA ancestries, as they appear in a member name
#: ``MVP_R4.1000G_AGR.<TRAIT>.<ANC>.GIA.dbGaP.txt.gz``.
ANCESTRIES: tuple[str, ...] = ("AFR", "AMR", "EAS", "EUR", "META")

#: Ancestry label the candidate table uses for a single-ancestry Analysis.
CANDIDATE_ANCESTRY = {
    "EUR": "European",
    "AFR": "African",
    "AMR": "Hispanic or Latin American",
    "EAS": "East Asian",
}
#: Label for a cross-ancestry meta-analysis.  The candidate table carries
#: "European" for these until a companion change relabels them, so META is
#: identified by ``ancestry_fraction < 1`` and only *labelled* Multi-ancestry.
META_ANCESTRY_LABEL = "Multi-ancestry"

#: Manifest columns: exactly the ones ``download-ebi-gwas-catalog-raw.py``
#: writes, because the frozen Source Inventory reads the same schema.  The
#: first fourteen are ``source_inventory.ACQUISITION_MANIFEST_COLUMNS``'s
#: required set (``seconds`` is dropped there deliberately: per-run timing is
#: not a release fact).
MANIFEST_COLUMNS: tuple[str, ...] = (
    "analysis_id", "publication_pmid", "trait", "study_design", "sample_size",
    "harmonised_status", "status", "data_url", "yaml_url", "data_file", "yaml_file",
    "data_bytes", "yaml_bytes", "sha256", "genome_assembly", "file_type",
    "is_harmonised", "seconds", "error",
)

#: Fields of the per-Analysis dbGaP metadata file this script consumes.
METADATA_TITLE = "Title of analysis"
METADATA_DESCRIPTION = "Analysis description"
METADATA_ANALYZED_VARIABLE = "Analyzed variable"
METADATA_SAMPLE_SIZE = "Sample size"
METADATA_TRAIT_TYPE = "Phenotypic trait type"

MAPPING_COLUMNS: tuple[str, ...] = (
    "analysis_id", "member", "tar", "ancestry", "study_design", "dbgap_trait_type",
    "sample_size", "n_cases", "n_controls", "dbgap_trait_code", "analyzed_variable",
    "candidate_trait", "candidate_store_key", "match_basis", "trait_check",
)

REPORT_COLUMNS: tuple[str, ...] = (
    "kind", "analysis_id", "member", "ancestry", "study_design", "sample_size",
    "n_cases", "n_controls", "analyzed_variable", "candidates", "detail",
)

#: The awk program that performs the per-row transform.  ``$1`` is the source
#: member, ``$2`` the program, ``$3`` the output file.
#:
#: It is header-driven rather than positional: the deposit's META files exist in
#: two shapes (with and without ``r2``, and with ``q_pval``/``i2``/``direction``
#: populated), and EBI's raw file for a shape keeps exactly that shape's
#: columns.  Every value is copied through verbatim except ``NA``/empty, which
#: become ``#NA``, and ``other_allele``/``ci_*``/binary ``standard_error``,
#: which EBI derives.
CONVERTER_AWK = r"""
function die(msg) { printf "converter: %s: %s\n", FILENAME, msg > "/dev/stderr"; exit 3 }
function field(idx,   v) { if (idx == 0) return ""; return (idx <= NF ? $idx : "") }
function na(v) { return (v == "" || v == "NA") ? "#NA" : v }
# EBI renders p_value through a float, and a float does not print like the
# source text: "1" comes out "1.0" and "6e-04" comes out "0.0006".  The
# shortest decimal that round-trips, then a trailing .0 for an integer-valued
# float in fixed notation, is exactly what EBI's files contain.
function pyfloat(v,   x, p, s) {
    x = v + 0
    for (p = 1; p <= 17; p++) {
        s = sprintf("%.*g", p, x)
        if ((s + 0) == x) break
    }
    if (index(s, ".") == 0 && index(s, "e") == 0 && index(s, "E") == 0) s = s ".0"
    return s
}
# EBI int-parses base_pair_location and writes an empty cell, keeping the row,
# when the value is not an integer -- 6 such rows exist in every Analysis of
# this deposit (the source writes positions like "2.4e+07").
function pyint(v) { return (v ~ /^[+-]?[0-9]+$/) ? sprintf("%d", v + 0) : "" }
BEGIN {
    FS = "\t"; OFS = "\t"
    # EBI writes the raw files with CRLF line endings (its converter is a CSV
    # writer using the default terminator).  Matching that, not just the cells,
    # is what makes the mirror's bytes identical to EBI's.
    ORS = "\r\n"
    nout = 22
    name[1]  = "chromosome";                src[1]  = "chrom"
    name[2]  = "base_pair_location";        src[2]  = "pos"
    name[3]  = "effect_allele";             src[3]  = "ea"
    name[4]  = "other_allele";              src[4]  = ""
    name[5]  = "beta";                      src[5]  = "beta"
    name[6]  = "odds_ratio";                src[6]  = "or"
    name[7]  = "standard_error";            src[7]  = "sebeta"
    name[8]  = "effect_allele_frequency";   src[8]  = "af"
    name[9]  = "p_value";                   src[9]  = "pval"
    name[10] = "rsid";                      src[10] = "SNP_ID"
    name[11] = "ci_upper";                  src[11] = ""
    name[12] = "ci_lower";                  src[12] = ""
    name[13] = "alt";                       src[13] = "alt"
    name[14] = "n";                         src[14] = "num_samples"
    name[15] = "case_af";                   src[15] = "case_af"
    name[16] = "num_cases";                 src[16] = "num_cases"
    name[17] = "control_af";                src[17] = "control_af"
    name[18] = "num_controls";              src[18] = "num_controls"
    name[19] = "r2";                        src[19] = "r2"
    name[20] = "q_pval";                    src[20] = "q_pval"
    name[21] = "i2";                        src[21] = "i2"
    name[22] = "direction";                 src[22] = "direction"
    nknown = split("SNP_ID chrom pos ref alt ea af num_samples beta sebeta case_af " \
                   "num_cases control_af num_controls or ci pval r2 q_pval i2 direction", known, " ")
    nrequired = split("SNP_ID chrom pos ref alt ea af num_samples pval", required, " ")
}
FNR == 1 {
    for (i = 1; i <= NF; i++) {
        c = $i
        if (c in col) die("duplicate column " c)
        col[c] = i
    }
    ncols = NF
    for (c in col) {
        found = 0
        for (i = 1; i <= nknown; i++) if (known[i] == c) found = 1
        if (!found) die("unrecognised source column: " c)
    }
    for (i = 1; i <= nrequired; i++) if (!(required[i] in col)) die("missing required source column: " required[i])
    has_beta = ("beta" in col); has_se = ("sebeta" in col); has_or = ("or" in col); has_ci = ("ci" in col)
    if (!(has_beta && has_se) && !has_or) die("no recognised effect columns: expected beta+sebeta or or")
    nactive = 0
    for (i = 1; i <= nout; i++) {
        if (i == 4) active = 1
        else if (i == 7) active = (has_beta || has_or)
        else if (i == 11 || i == 12) active = has_ci
        else active = (src[i] != "" && (src[i] in col))
        if (active) { nactive++; slot[nactive] = i }
    }
    line = ""
    for (j = 1; j <= nactive; j++) line = (j > 1 ? line OFS : "") name[slot[j]]
    print line
    next
}
{
    if (NF > ncols) die("row has more fields than the header")
    line = ""
    for (j = 1; j <= nactive; j++) {
        i = slot[j]
        if (i == 4) {
            ea = field(col["ea"]); rf = field(col["ref"]); al = field(col["alt"])
            if (ea == al) v = rf
            else if (ea == rf) v = al
            else die("effect allele " ea " is neither ref (" rf ") nor alt (" al ")")
        }
        else if (i == 2) {
            pv = field(col["pos"])
            v = (pv == "" || pv == "NA") ? "#NA" : pyint(pv)
        }
        else if (i == 9) {
            pv = field(col["pval"])
            v = (pv == "" || pv == "NA") ? "#NA" : pyfloat(pv)
        }
        else if (i == 7 && !has_se) v = "#NA"
        else if (i == 11 || i == 12) {
            civ = field(col["ci"])
            if (civ == "" || civ == "NA") { lo = "#NA"; hi = "#NA" }
            else {
                k = index(civ, ",")
                if (k == 0) die("ci is not a lo,hi pair: " civ)
                lo = na(substr(civ, 1, k - 1)); hi = na(substr(civ, k + 1))
            }
            v = (i == 11 ? hi : lo)
        }
        else v = na(field(col[src[i]]))
        line = (j > 1 ? line OFS : "") v
    }
    print line
}
"""


class AcquisitionError(RuntimeError):
    """An acquisition input or output is not usable as declared."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bucket_of(accession: str) -> str:
    """EBI's 1000-accession directory bucket, e.g. ``GCST90477001-GCST90478000``."""
    digits = accession.removeprefix("GCST")
    number = int(digits)
    low = ((number - 1) // 1000) * 1000 + 1
    return f"GCST{low:0{len(digits)}d}-GCST{low + 999:0{len(digits)}d}"


def write_tsv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in columns})
    os.replace(partial, path)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def parse_tar_listing(path: Path) -> dict[str, tuple[str, int]]:
    """``member path -> (tar name, size)`` from one ``tar -tv`` table of contents.

    A TOC line is ``<mode> <owner> <size> <date> <time> <member>``; directories
    are listed too and are skipped.  The TOC is named ``<tar>.table_of_contents.txt``
    (keeping the tar's own ``phs002453.`` prefix), so the tar name is the file
    name with that suffix removed.
    """
    tar_name = path.name[: -len(".table_of_contents.txt")]
    members: dict[str, tuple[str, int]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[-1]:
            continue
        member = parts[-1]
        if member.endswith("/"):
            continue
        members[member] = (tar_name, int(parts[2]))
    return members


def load_toc(out: Path) -> dict[str, tuple[str, int]]:
    meta_dir = out / "GIA" / "meta"
    members: dict[str, tuple[str, int]] = {}
    for toc in sorted(meta_dir.glob("*.table_of_contents.txt")):
        for member, entry in parse_tar_listing(toc).items():
            if member in members:
                raise AcquisitionError(f"{member} is listed in more than one table of contents")
            members[member] = entry
    return members


def tar_md5(tars_dir: Path, tar_name: str) -> str:
    """The verified MD5 of a tar, as recorded by the fetch script."""
    for suffix in (".verified", ".md5"):
        path = tars_dir / f"{tar_name}{suffix}"
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text.split()[0]
    return ""


def parse_metadata(path: Path) -> dict[str, str]:
    """Read one ``*.dbGaP.metadata.txt`` two-column TSV into an Info->Description map."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter="\t")
        header = next(reader, None)
        if header is None or [cell.strip() for cell in header[:2]] != ["Info", "Description"]:
            raise AcquisitionError(f"{path}: not an Info/Description metadata table")
        info: dict[str, str] = {}
        for row in reader:
            if not row or not row[0].strip():
                continue
            info[row[0].strip()] = row[1].strip() if len(row) > 1 else ""
    return info


def parse_sample_size(text: str, path: Path) -> tuple[int, int, int]:
    """``(N, cases, controls)`` from ``Total Sample Size=N[; Cases=a; Controls=b]``."""
    total = re.search(r"Total Sample Size\s*=\s*(\d+)", text)
    if total is None:
        raise AcquisitionError(f"{path}: cannot parse sample size from {text!r}")
    cases = re.search(r"Cases\s*=\s*(\d+)", text)
    controls = re.search(r"Controls\s*=\s*(\d+)", text)
    return (
        int(total.group(1)),
        int(cases.group(1)) if cases else 0,
        int(controls.group(1)) if controls else 0,
    )


def normalise_trait(text: str) -> str:
    """Trait text without the GWAS Catalog's ``(PheCode N)`` suffix, case-folded."""
    value = re.sub(r"\s*\(phecode\s+[0-9.]+\)\s*$", "", text.strip().lower())
    return re.sub(r"\s+", " ", value)


def trait_check(analyzed_variable: str, candidate_trait: str) -> str:
    """How the dbGaP analyzed-variable text relates to the candidate trait text."""
    left = normalise_trait(analyzed_variable)
    right = normalise_trait(candidate_trait)
    if not left or not right:
        return "missing"
    if left == right:
        return "exact"
    if right.startswith(left):
        return "prefix"
    return "mismatch"


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One PMID 39024449 candidate-table row, reduced to what acquisition needs."""

    analysis_id: str
    trait: str
    ancestry: str
    ancestry_fraction: float
    store_key: str
    study_design: str
    n_cases: int
    n_controls: int
    sample_size: int

    @property
    def mapped_ancestry(self) -> str:
        """The dbGaP GIA ancestry this row belongs to.

        ``ancestry_fraction < 1`` is a cross-ancestry meta-analysis, whatever
        the ``ancestry_group`` column says: the companion multi-ancestry store
        split relabels those rows, and acquisition must not depend on the label.
        """
        if self.ancestry_fraction < 1:
            return "META"
        for ancestry, label in CANDIDATE_ANCESTRY.items():
            if self.ancestry == label:
                return ancestry
        raise AcquisitionError(
            f"{self.analysis_id}: unrecognised ancestry_group {self.ancestry!r} at "
            f"ancestry_fraction={self.ancestry_fraction}"
        )

    @property
    def yaml_ancestry(self) -> str:
        return META_ANCESTRY_LABEL if self.mapped_ancestry == "META" else self.ancestry

    @property
    def numeric_key(self) -> tuple[str, int, int, int]:
        return (self.mapped_ancestry, self.sample_size, self.n_cases, self.n_controls)


def read_candidates(path: Path) -> list[Candidate]:
    if not path.is_file():
        raise AcquisitionError(f"--candidates not found: {path}")
    candidates: list[Candidate] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("PUBMED.ID") != PUBMED_ID:
                continue
            candidates.append(
                Candidate(
                    analysis_id=row["STUDY.ACCESSION"],
                    trait=row["DISEASE.TRAIT"],
                    ancestry=row["ancestry_group"],
                    ancestry_fraction=float(row["ancestry_fraction"] or 0),
                    store_key=row["store_key"],
                    study_design=row["study_design"],
                    n_cases=int(row["n_cases"] or 0),
                    n_controls=int(row["n_controls"] or 0),
                    sample_size=int(row["sample_size"] or 0),
                )
            )
    return candidates


def selected_values(values: list[str] | None) -> set[str] | None:
    """Flatten repeatable, comma-separated ``--analysis``/``--member`` selections."""
    chosen: set[str] = set()
    for value in values or []:
        chosen.update(part.strip() for part in value.split(",") if part.strip())
    return chosen or None


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


@dataclass
class TarResult:
    tar: str
    extracted: int = 0
    skipped: int = 0
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)


def extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path, size: int) -> None:
    """Write one tar member atomically, verifying the byte count as it streams."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    handle = tar.extractfile(member)
    if handle is None:
        raise AcquisitionError(f"{member.name}: not a regular file in the tar")
    written = 0
    try:
        with partial.open("wb") as out:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                out.write(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        handle.close()
    if written != size:
        partial.unlink(missing_ok=True)
        raise AcquisitionError(f"{member.name}: wrote {written} bytes, table of contents says {size}")
    os.replace(partial, target)


def extract_tar(tar_path: Path, out: Path, tocs: dict[str, tuple[str, int]]) -> TarResult:
    """Stream one tar once and extract its members.

    ``tarfile``'s streaming mode cannot seek, which is the point: a member is
    only reachable by reading everything before it, so the whole tar is read in
    one pass rather than once per member.
    """
    result = TarResult(tar=tar_path.name)
    expected = {member: entry for member, entry in tocs.items() if entry[0] == tar_path.name}
    if not expected:
        return result
    pending = {
        member: size for member, (_, size) in expected.items()
        if not (out / "GIA" / "extracted" / member).is_file()
        or (out / "GIA" / "extracted" / member).stat().st_size != size
    }
    if not pending:
        result.skipped = len(expected)
        return result

    seen: set[str] = set()
    with tarfile.open(tar_path, mode="r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = member.name
            if name not in expected:
                result.unexpected.append(name)
                continue
            seen.add(name)
            size = expected[name][1]
            target = out / "GIA" / "extracted" / name
            if name not in pending and target.is_file() and target.stat().st_size == size:
                result.skipped += 1
                continue
            extract_member(tar, member, target, size)
            result.extracted += 1
    result.missing = sorted(set(expected) - seen)
    return result


def command_extract(args: argparse.Namespace) -> int:
    out = Path(args.out)
    tars_dir = out / "GIA" / "tars"
    meta_dir = out / "GIA" / "meta"
    if not tars_dir.is_dir():
        raise SystemExit(f"extract: tar directory not found: {tars_dir}")
    tars = sorted(path.name for path in tars_dir.glob("*.tar"))
    if (meta_dir / "tars.txt").is_file():
        listed = [
            line.strip() for line in (meta_dir / "tars.txt").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        tars = [name for name in listed if name in set(tars)]
    if args.tar:
        wanted = set(args.tar)
        tars = [name for name in tars if name in wanted]
        missing = wanted - set(tars)
        if missing:
            raise SystemExit(f"extract: --tar not found in {tars_dir}: {', '.join(sorted(missing))}")
    verified = [name for name in tars if (tars_dir / f"{name}.verified").is_file()]
    unverified = [name for name in tars if name not in verified]
    if unverified:
        print(f"extract: skipping {len(unverified)} tar(s) without a .verified marker")
    if not verified:
        raise SystemExit("extract: no MD5-verified tar to stream")
    if args.limit:
        verified = verified[: args.limit]

    tocs = load_toc(out)
    started = time.monotonic()
    results: list[TarResult] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(extract_tar, tars_dir / name, out, tocs): name for name in verified}
        for done, future in enumerate(as_completed(futures), start=1):
            name = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - keep a long batch alive
                failures += 1
                print(f"[{done}/{len(futures)}] {name} FAILED {exc!r}", flush=True)
                continue
            results.append(result)
            print(
                f"[{done}/{len(futures)}] {name} extracted={result.extracted} "
                f"skipped={result.skipped} missing={len(result.missing)}",
                flush=True,
            )
            if result.missing:
                failures += 1
                print(f"    missing from tar: {', '.join(result.missing[:5])}", flush=True)
            if result.unexpected:
                print(f"    not in the table of contents: {', '.join(result.unexpected[:5])}", flush=True)
    elapsed = time.monotonic() - started
    total = sum(r.extracted for r in results)
    print(f"extract: {total} member(s) written from {len(results)} tar(s) in {elapsed:.1f}s")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Analysis:
    """One extracted dbGaP Analysis, as its metadata declares it."""

    member: str
    stem: str
    tar: str
    ancestry: str
    trait_code: str
    analyzed_variable: str
    description: str
    trait_type: str
    sample_size: int
    n_cases: int
    n_controls: int

    @property
    def study_design(self) -> str:
        return "case-control" if self.trait_type == "binary" else "quantitative"

    @property
    def numeric_key(self) -> tuple[str, int, int, int]:
        return (self.ancestry, self.sample_size, self.n_cases, self.n_controls)


def metadata_members(out: Path) -> list[Path]:
    extracted = out / "GIA" / "extracted"
    return sorted(extracted.glob("*/*.dbGaP.metadata.txt"))


def load_analysis(path: Path, extracted: Path, toc_entry: tuple[str, int] | None) -> Analysis:
    """Read one Analysis and attach the data member it describes."""
    info = parse_metadata(path)
    for field_name in (METADATA_TITLE, METADATA_ANALYZED_VARIABLE, METADATA_SAMPLE_SIZE, METADATA_TRAIT_TYPE):
        if field_name not in info:
            raise AcquisitionError(f"{path}: metadata has no {field_name!r} row")
    stem = path.name[: -len(".metadata.txt")]
    parts = stem.split(".")
    if len(parts) != 6 or parts[0] != "MVP_R4" or parts[4] != "GIA" or parts[5] != "dbGaP":
        raise AcquisitionError(f"{path}: unexpected member name {path.name!r}")
    trait_code, ancestry = parts[2], parts[3]
    if ancestry not in ANCESTRIES:
        raise AcquisitionError(f"{path}: unrecognised ancestry {ancestry!r}")
    member = str(path.relative_to(extracted)).replace(".metadata.txt", ".txt.gz")
    trait_type = info[METADATA_TRAIT_TYPE].strip().lower()
    if trait_type not in ("binary trait", "quantitative trait"):
        raise AcquisitionError(f"{path}: unrecognised phenotypic trait type {info[METADATA_TRAIT_TYPE]!r}")
    sample_size, n_cases, n_controls = parse_sample_size(info[METADATA_SAMPLE_SIZE], path)
    return Analysis(
        member=member,
        stem=stem,
        tar=toc_entry[0] if toc_entry else "",
        ancestry=ancestry,
        trait_code=trait_code,
        analyzed_variable=info[METADATA_ANALYZED_VARIABLE],
        description=info.get(METADATA_DESCRIPTION, ""),
        trait_type="binary" if trait_type == "binary trait" else "quantitative",
        sample_size=sample_size,
        n_cases=n_cases,
        n_controls=n_controls,
    )


def map_analyses(
    analyses: list[Analysis], candidates: list[Candidate], *, report_unused_candidates: bool
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Bind every Analysis to one candidate row; return ``(mapping rows, problems)``."""
    by_key: dict[tuple[str, int, int, int], list[Candidate]] = {}
    for candidate in candidates:
        by_key.setdefault(candidate.numeric_key, []).append(candidate)

    rows: list[dict[str, str]] = []
    problems: list[dict[str, str]] = []
    used: dict[str, str] = {}

    def problem(kind: str, analysis: Analysis | None, candidates_here: list[Candidate], detail: str) -> None:
        problems.append({
            "kind": kind,
            "analysis_id": candidates_here[0].analysis_id if len(candidates_here) == 1 else "",
            "member": analysis.member if analysis else "",
            "ancestry": analysis.ancestry if analysis else "",
            "study_design": analysis.study_design if analysis else "",
            "sample_size": str(analysis.sample_size) if analysis else "",
            "n_cases": str(analysis.n_cases) if analysis else "",
            "n_controls": str(analysis.n_controls) if analysis else "",
            "analyzed_variable": analysis.analyzed_variable if analysis else "",
            "candidates": ", ".join(candidate.analysis_id for candidate in candidates_here),
            "detail": detail,
        })

    for analysis in analyses:
        candidates_here = by_key.get(analysis.numeric_key, [])
        chosen: Candidate | None = None
        basis = ""
        if len(candidates_here) == 1:
            chosen = candidates_here[0]
            basis = "unique-key"
        elif candidates_here:
            # Several candidate rows share (ancestry, N, cases, controls): the
            # same trait's maximum/mean/minimum analyses have equal N.  The
            # deposit's own analyzed-variable text is the disambiguator, and is
            # also the consistency check the numeric key alone cannot be.
            text = normalise_trait(analysis.analyzed_variable)
            matches = [c for c in candidates_here if normalise_trait(c.trait) == text]
            if len(matches) == 1:
                chosen = matches[0]
                basis = "trait-tiebreak"
            else:
                problem(
                    "ambiguous_analysis", analysis, candidates_here,
                    f"{len(matches)} of {len(candidates_here)} candidate rows match the analyzed variable",
                )
        else:
            problem("unmatched_analysis", analysis, [], "no candidate row has this numeric key")

        if chosen is None:
            continue
        key = chosen.analysis_id
        if key in used:
            problem("duplicate_analysis_match", analysis, [chosen], f"already matched to {used[key]}")
            continue
        used[key] = analysis.member
        check = trait_check(analysis.analyzed_variable, chosen.trait)
        if check == "mismatch":
            problem("trait_check_mismatch", analysis, [chosen], f"analyzed variable {analysis.analyzed_variable!r} vs {chosen.trait!r}")
        rows.append({
            "analysis_id": key,
            "member": analysis.member,
            "tar": analysis.tar,
            "ancestry": analysis.ancestry,
            "study_design": chosen.study_design,
            "dbgap_trait_type": analysis.trait_type,
            "sample_size": str(analysis.sample_size),
            "n_cases": str(analysis.n_cases),
            "n_controls": str(analysis.n_controls),
            "dbgap_trait_code": analysis.trait_code,
            "analyzed_variable": analysis.analyzed_variable,
            "candidate_trait": chosen.trait,
            "candidate_store_key": chosen.store_key,
            "match_basis": basis,
            "trait_check": check,
        })

    for candidate in candidates:
        if report_unused_candidates and candidate.analysis_id not in used:
            problem("unused_candidate", None, [candidate], "no extracted Analysis mapped to this candidate row")
    return rows, problems


def command_map(args: argparse.Namespace) -> int:
    out = Path(args.out)
    candidates = read_candidates(Path(args.candidates))
    if not candidates:
        raise SystemExit(f"map: no PMID {PUBMED_ID} rows in {args.candidates}")
    extracted = out / "GIA" / "extracted"
    members = metadata_members(out)
    member_needles = selected_values(args.member)
    if member_needles:
        members = [m for m in members if any(needle in str(m.relative_to(extracted)) for needle in member_needles)]
    if args.limit:
        members = members[: args.limit]
    if not members:
        raise SystemExit("map: no extracted *.dbGaP.metadata.txt found")

    tocs = load_toc(out)
    analyses: list[Analysis] = []
    for member in members:
        data_member = str(member.relative_to(extracted)).replace(".metadata.txt", ".txt.gz")
        analyses.append(load_analysis(member, extracted, tocs.get(data_member)))
    analysis_needles = selected_values(args.analysis)
    if analysis_needles:
        # Before mapping there is no accession; --analysis selects by the
        # dbGaP trait code (e.g. ``A1C_Min_INT.EAS``) or the member stem.
        analyses = [a for a in analyses if any(needle in a.stem for needle in analysis_needles)]
        if not analyses:
            raise SystemExit("map: --analysis selected no Analysis")

    # A partial run reads a subset of the mirror, so it can only be asked
    # whether everything it read is bound; only a full run can be asked whether
    # every candidate row is covered.
    covered_all = not (member_needles or analysis_needles or args.limit)
    rows, problems = map_analyses(analyses, candidates, report_unused_candidates=covered_all)
    write_tsv(out / "mapping.tsv", MAPPING_COLUMNS, rows)
    write_tsv(out / "mapping-report.tsv", REPORT_COLUMNS, problems)

    binding_ok = not any(
        p["kind"] in ("unmatched_analysis", "ambiguous_analysis", "duplicate_analysis_match")
        for p in problems
    )
    complete = binding_ok and len(candidates) == 6022 and len(rows) == len(candidates)
    print(
        f"map: {len(analyses)} Analysis(es), {len(candidates)} candidate(s), {len(rows)} matched, "
        f"{len(problems)} problem row(s); binding={'ok' if binding_ok else 'incomplete'}; "
        f"complete={'yes' if complete else 'no'}; wrote {out / 'mapping.tsv'}"
    )
    for kind, count in sorted(collections.Counter(p["kind"] for p in problems).items()):
        print(f"  {kind}={count}")
    if binding_ok and (complete or not covered_all or args.allow_partial):
        return 0
    print(
        "map: " + ("not a complete bijection over all 6,022 Analyses"
                   if binding_ok else "the Analysis/candidate binding is not a bijection"),
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------


def convert_analysis(
    row: dict[str, str], candidate: Candidate, out: Path, tars_dir: Path, force: bool
) -> tuple[str, str, float]:
    """Convert one mapped Analysis; return ``(status, error, seconds)``."""
    started = time.monotonic()
    accessions = row["analysis_id"]
    member = out / "GIA" / "extracted" / row["member"]
    directory = out / "gwas-ssf" / bucket_of(accessions) / accessions
    data_path = directory / f"{accessions}.tsv.gz"
    yaml_path = directory / f"{accessions}.tsv.gz-meta.yaml"
    if not member.is_file():
        return "error", f"extracted member not found: {member}", time.monotonic() - started
    if not force and data_path.is_file() and data_path.stat().st_size > 0 and yaml_path.is_file():
        return "skipped", "", time.monotonic() - started

    directory.mkdir(parents=True, exist_ok=True)
    partial = data_path.with_name(data_path.name + ".partial")
    partial.unlink(missing_ok=True)
    command = (
        'set -o pipefail; gzip -dc "$1" | awk -F "\\t" -v OFS="\\t" "$2" | gzip -c > "$3"'
    )
    result = subprocess.run(
        ["bash", "-c", command, "acquire-dbgap", str(member), CONVERTER_AWK, str(partial)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return "error", f"converter exited {result.returncode}: {detail[-1] if detail else 'no output'}", time.monotonic() - started

    os.replace(partial, data_path)
    document = render_metadata_yaml(row, candidate, data_path, out, tars_dir)
    yaml_partial = yaml_path.with_name(yaml_path.name + ".partial")
    yaml_partial.write_text(document, encoding="utf-8")
    os.replace(yaml_partial, yaml_path)
    return "converted", "", time.monotonic() - started


def render_metadata_yaml(
    row: dict[str, str], candidate: Candidate, data_path: Path, out: Path, tars_dir: Path
) -> str:
    """EBI's ``*-meta.yaml`` fields plus the dbGaP provenance, as one document."""
    ancestry = candidate.yaml_ancestry
    samples: dict[str, object] = {
        "sample_ancestry_category": [ancestry],
        "sample_size": int(row["sample_size"]),
    }
    if candidate.study_design == "case-control":
        samples["case_control_study"] = True
    tar = row["tar"]
    document: dict[str, object] = {
        "gwas_id": row["analysis_id"],
        "trait_description": [candidate.trait],
        "genome_assembly": "GRCh38",
        "coordinate_system": "1-based",
        "genotyping_technology": ["Genome-wide genotyping array"],
        "samples": [samples],
        "data_file_name": data_path.name,
        "file_type": "GWAS-SSF v1.0",
        "data_file_md5sum": md5_file(data_path),
        "is_harmonised": False,
        "is_sorted": False,
        "source": {
            "repository": "dbGaP",
            "study": "phs002453",
            "tar": tar,
            "member": row["member"],
            "tar_md5": tar_md5(tars_dir, tar) if tar else "",
            "url": f"{DBGAP_BASE}/{tar}" if tar else DBGAP_BASE,
        },
        "dbgap_analysis_description": metadata_description(out, row["member"]),
    }
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100)


def metadata_description(out: Path, member: str) -> str:
    metadata = out / "GIA" / "extracted" / member.replace(".txt.gz", ".metadata.txt")
    if not metadata.is_file():
        return ""
    return parse_metadata(metadata).get(METADATA_DESCRIPTION, "")


def command_convert(args: argparse.Namespace) -> int:
    out = Path(args.out)
    mapping_path = Path(args.mapping) if args.mapping else out / "mapping.tsv"
    if not mapping_path.is_file():
        raise SystemExit(f"convert: mapping not found: {mapping_path}")
    rows = read_tsv(mapping_path)
    if not rows:
        raise SystemExit(f"convert: mapping is empty: {mapping_path}")
    by_accession = {candidate.analysis_id: candidate for candidate in read_candidates(Path(args.candidates))}
    selected = selected_values(args.analysis)
    if selected:
        rows = [row for row in rows if row["analysis_id"] in selected]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit("convert: nothing selected")
    tars_dir = out / "GIA" / "tars"

    started = time.monotonic()
    counts: dict[str, int] = {}
    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(convert_analysis, row, by_accession[row["analysis_id"]], out, tars_dir, args.force): row
            for row in rows
        }
        for done, future in enumerate(as_completed(futures), start=1):
            row = futures[future]
            try:
                status, error, seconds = future.result()
            except Exception as exc:  # noqa: BLE001 - keep a long batch alive
                status, error, seconds = "error", repr(exc), 0.0
            counts[status] = counts.get(status, 0) + 1
            if status == "error":
                failures += 1
            print(
                f"[{done}/{len(futures)}] {row['analysis_id']} {status} {seconds:.1f}s {error}",
                flush=True,
            )
    elapsed = time.monotonic() - started
    print("convert: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print(f"convert: {elapsed:.1f}s wall for {len(rows)} Analysis(es)")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def manifest_row(
    candidate: Candidate, mapping: dict[str, str] | None, data_digest: str, path: Path, yaml_path: Path
) -> dict[str, str]:
    row = {name: "" for name in MANIFEST_COLUMNS}
    row.update({
        "analysis_id": candidate.analysis_id,
        "publication_pmid": PUBMED_ID,
        "trait": candidate.trait,
        "study_design": candidate.study_design,
        "sample_size": str(candidate.sample_size),
        # The dbGaP pass is not preceded by a harmonised pass on this mirror, so
        # there is no harmonised status to report; EBI's raw manifest carries the
        # other pass's fact here, and an empty value is the honest analogue.
        "harmonised_status": "",
    })
    row["data_file"] = str(path)
    row["yaml_file"] = str(yaml_path)
    row["data_url"] = f"{DBGAP_BASE}/{mapping['tar']}" if mapping and mapping.get("tar") else ""
    if path.is_file() and path.stat().st_size > 0 and yaml_path.is_file():
        row.update({
            "status": "ok",
            "data_bytes": str(path.stat().st_size),
            "yaml_bytes": str(yaml_path.stat().st_size),
            "sha256": data_digest,
            "genome_assembly": "GRCh38",
            "file_type": "GWAS-SSF v1.0",
            "is_harmonised": "false",
        })
    else:
        # Not a transfer failure: this Analysis has no converted file yet.  The
        # frozen Source Inventory classifies any non-ready status as a
        # non-member, so an incomplete conversion shows up as a missing Analysis
        # rather than as a usable one.
        row["status"] = "error"
        row["error"] = f"not converted: {path}"
    return row


def command_manifest(args: argparse.Namespace) -> int:
    out = Path(args.out)
    candidates = read_candidates(Path(args.candidates))
    if not candidates:
        raise SystemExit("manifest: no candidate rows")
    mapping_path = Path(args.mapping) if args.mapping else out / "mapping.tsv"
    mapping = {row["analysis_id"]: row for row in read_tsv(mapping_path)} if mapping_path.is_file() else {}
    selected = selected_values(args.analysis)
    if selected:
        candidates = [c for c in candidates if c.analysis_id in selected]
    if args.limit:
        candidates = candidates[: args.limit]
    if not candidates:
        raise SystemExit("manifest: nothing selected")

    # The checksum is the release fact the freeze records, so it is computed
    # here from the materialised bytes rather than carried over from conversion.
    digests: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {}
        for candidate in candidates:
            path = out / "gwas-ssf" / bucket_of(candidate.analysis_id) / candidate.analysis_id / f"{candidate.analysis_id}.tsv.gz"
            if path.is_file() and path.stat().st_size > 0:
                futures[pool.submit(sha256_file, path)] = candidate.analysis_id
        for future in as_completed(futures):
            digests[futures[future]] = future.result()

    by_store: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_store.setdefault(candidate.store_key, []).append(candidate)

    status = 0
    manifest_dir = Path(args.manifest_dir) if args.manifest_dir else out / "manifests"
    for store_key, group in sorted(by_store.items()):
        rows: list[dict[str, str]] = []
        for candidate in sorted(group, key=lambda c: c.analysis_id):
            directory = out / "gwas-ssf" / bucket_of(candidate.analysis_id) / candidate.analysis_id
            path = directory / f"{candidate.analysis_id}.tsv.gz"
            yaml_path = directory / f"{candidate.analysis_id}.tsv.gz-meta.yaml"
            rows.append(manifest_row(candidate, mapping.get(candidate.analysis_id), digests.get(candidate.analysis_id, ""), path, yaml_path))
        slug = store_key.replace("__", "-")
        manifest_path = manifest_dir / f"{slug}-download-manifest.tsv"
        write_tsv(manifest_path, MANIFEST_COLUMNS, rows)
        ready = sum(1 for row in rows if row["status"] == "ok")
        print(f"manifest: {store_key}: {ready}/{len(rows)} ok -> {manifest_path}")
        if ready != len(rows):
            status = 1
    return 0 if (args.allow_partial or selected or args.limit) else status


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=DEFAULT_OUT, help="acquisition root (default: %(default)s)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="stream verified tars into GIA/extracted")
    extract.add_argument("--jobs", type=int, default=4, help="tars extracted in parallel (default: %(default)s)")
    extract.add_argument("--tar", action="append", help="only this tar (repeatable)")
    extract.add_argument("--limit", type=int, help="only the first N tars")
    extract.set_defaults(func=command_extract)

    map_parser = subparsers.add_parser("map", help="bind dbGaP Analyses to candidate-table rows")
    map_parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    map_parser.add_argument("--member", action="append", help="only metadata files whose name contains this (repeatable)")
    map_parser.add_argument("--analysis", action="append", help="only these trait codes / member stems")
    map_parser.add_argument("--limit", type=int)
    map_parser.add_argument("--allow-partial", action="store_true", help="do not require a complete bijection")
    map_parser.set_defaults(func=command_map)

    convert = subparsers.add_parser("convert", help="convert mapped Analyses into the EBI mirror layout")
    convert.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    convert.add_argument("--mapping", help="mapping TSV (default: <out>/mapping.tsv)")
    convert.add_argument("--jobs", type=int, default=32)
    convert.add_argument("--analysis", action="append", help="only these GCST accessions (repeatable)")
    convert.add_argument("--limit", type=int)
    convert.add_argument("--force", action="store_true")
    convert.set_defaults(func=command_convert)

    manifest = subparsers.add_parser("manifest", help="write one acquisition manifest per store_key")
    manifest.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    manifest.add_argument("--mapping", help="mapping TSV (default: <out>/mapping.tsv)")
    manifest.add_argument("--manifest-dir", help="default: <out>/manifests")
    manifest.add_argument("--jobs", type=int, default=8)
    manifest.add_argument("--analysis", action="append")
    manifest.add_argument("--limit", type=int)
    manifest.add_argument("--allow-partial", action="store_true")
    manifest.set_defaults(func=command_manifest)

    args = parser.parse_args(argv)
    if getattr(args, "jobs", 1) < 1:
        parser.error("--jobs must be >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return int(args.func(args))
    except AcquisitionError as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
