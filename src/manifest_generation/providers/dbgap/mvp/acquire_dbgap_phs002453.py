#!/usr/bin/env python3
"""Acquire dbGaP phs002453 (MVP GIA, PMID 39024449) into a GWAS-SSF mirror.

PMID 39024449 (Verma et al. 2024, VA Million Veteran Program) deposited its
6,022 GIA Analyses on dbGaP as ``phs002453`` (open access).  EBI's *raw*
``GCST*.tsv.gz`` files for the same publication are those dbGaP files with the
columns renamed and reordered and ``NA`` written ``#NA``; EBI was serving them
at ~0.3 MB/s, dbGaP serves the same bytes at ~37 MB/s per stream.  This script
reproduces EBI's conversion so every downstream consumer (inventory freeze,
preflight, the GWAS-SSF reader) sees the same bytes it would have seen from EBI,
except for round positions EBI blanked (see ``position`` in the converter).

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
    Write the Source Collection's one shared Acquisition Manifest (#290): one
    row per mapped Analysis with its location, size, sha256 and optional
    metadata sidecar, or a non-ready status and the reason there is no file.
    The acquisition's own per-store download manifests may supply the recorded
    sha256 (``--recorded-manifests``), so a metadata-only reproduction does not
    re-read the corpus.

Usage (see ``README.md`` next to this module)::

    python3 -m manifest_generation.providers.dbgap.mvp.acquire_dbgap_phs002453 --help
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TextIO

import yaml

from manifest_generation import REPO_ROOT
from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    load_acquisition_manifest,
)
from manifest_generation.connector_tsv import write_connector
from manifest_generation.source_inventory import READY_STATUSES
from manifest_generation.providers.dbgap.metadata import (
    DbgapError as AcquisitionError,
    TarResult,
    extract_member,
    extract_tar,
    load_toc,
    md5_file,
    metadata_path_for,
    parse_metadata,
    parse_sample_size,
    parse_tar_listing,
    sha256_file,
    tar_md5,
)

DEFAULT_OUT = "/data/opengwasdb/raw/dbgap-phs002453"
DEFAULT_CANDIDATES = "resources/data/derived/store-candidates-analyses.tsv"
DEFAULT_DISCREPANCIES = str(REPO_ROOT / "resources/scripts/dbgap-phs002453-key-discrepancies.yaml")
PUBMED_ID = "39024449"

#: Dispositions a key-discrepancy ledger record may carry.  Anything else is an
#: error, so an unrecognised value can never be read as "resolved".
DISCREPANCY_DISPOSITIONS: tuple[str, ...] = ("resolved", "owner_decision_required")
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

#: The per-store download-manifest columns the ``refresh`` command still merges.
#: The shared Acquisition Manifest is ``acquisition_manifest.COLUMNS``; these
#: are the acquisition's own record of what it converted, not the connector.
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

#: The per-record trait-label review queue.  A mismatch does not change the
#: numeric-key binding; it is surfaced with its original labels so a human can
#: disposition it rather than have a spelling heuristic accept it silently.
REVIEW_COLUMNS: tuple[str, ...] = (
    "analysis_id", "member", "trait_category", "disposition", "trait_check",
    "analyzed_variable", "candidate_trait",
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
# awk reads a subnormal ("1e-321"; the deposit has p-values down to ~1e-321)
# as 0, which would turn a strong association into p = 0: a value that reads
# as 0 but is not written as zero is copied through verbatim instead.
function pyfloat(v,   x, p, s) {
    x = v + 0
    if (x == 0 && v !~ /^[+-]?(0+\.?0*|\.0+)([eE][+-]?[0-9]+)?$/) return v
    for (p = 1; p <= 17; p++) {
        s = sprintf("%." p "g", x)
        if ((s + 0) == x) break
    }
    if (index(s, ".") == 0 && index(s, "e") == 0 && index(s, "E") == 0) s = s ".0"
    return s
}
# The deposit writes 6 positions per Analysis in scientific notation
# ("2.4e+07" for 24000000: R prints a round integer that way).  EBI's
# converter int-parses the cell and leaves it empty, so the reader drops those
# variants; here an exact integer is written as one, which is the only place the
# mirror deliberately differs from EBI.  Anything else that is not an integer
# still becomes an empty cell, as in EBI's file.
function position(v,   x) {
    if (v ~ /^[+-]?[0-9]+$/) return sprintf("%d", v + 0)
    if (v ~ /^[0-9]+(\.[0-9]+)?[eE]\+?[0-9]+$/) {
        x = v + 0
        if (x == int(x)) return sprintf("%.0f", x)
    }
    return ""
}
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
            v = (pv == "" || pv == "NA") ? "#NA" : position(pv)
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


# ---------------------------------------------------------------------------
# Source Collection helpers
# ---------------------------------------------------------------------------


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


def open_refresh_partial(path: Path, out_root: Path) -> tuple[Path, TextIO]:
    """Create a fresh metadata temp, never truncate an existing inode or alias.

    Containment alone cannot detect hardlinks or symlinks to another output row.
    Exclusive creation also refuses stale regular temps and aliases introduced
    after validation. Completed reruns remain valid because replace consumes the
    temp. Existing temps are preserved for inspection, not blindly deleted.
    """
    partial = path.with_name(path.name + ".partial")
    for target in (path, partial):
        ensure_write_within(target, out_root)
        if target.is_symlink():
            raise AcquisitionError(f"refusing metadata symlink: {target}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = partial.open("x", newline="", encoding="utf-8")
    except FileExistsError as exc:
        raise AcquisitionError(f"refusing existing metadata temporary file: {partial}") from exc
    return partial, handle


def write_tsv_guarded(
    path: Path, columns: tuple[str, ...], rows: list[dict[str, str]], out_root: Path
) -> None:
    """Atomically write refresh metadata using an isolated, exclusive temp."""
    partial, handle = open_refresh_partial(path, out_root)
    with handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(partial, path)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


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


def canonical_trait(text: str) -> str:
    """Formatting-only canonical form of a trait label.

    Unicode-normalises, strips the GWAS Catalog's ``(PheCode N)`` suffix,
    case-folds, removes apostrophes/quotes, and replaces every non-alphanumeric
    run with a space.  It deliberately does NOT map spelling variants, plurals,
    abbreviations or synonyms: ``lipoid``/``lipid``, ``prostate``/``prostrate``
    and ``H. pylori``/``Helicobacter pylori`` must stay distinct so they land in
    the human review queue rather than being accepted by a typo heuristic.
    """
    value = unicodedata.normalize("NFKC", text.strip()).casefold()
    value = re.sub(r"\s*\(phecode\s+[0-9.]+\s*\)\s*$", "", value)
    for straight, curly in (("'", "\u2019"), ("'", "\u2018"), ('"', "\u201c"), ('"', "\u201d")):
        value = value.replace(curly, straight)
    value = value.replace("'", "").replace('"', "")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def trait_disposition(
    analyzed_variable: str, candidate_trait: str, *, trait_policy: "OwnerPolicy | None" = None
) -> tuple[str, str]:
    """``(trait_category, disposition)`` for one trait-text mismatch.

    ``trait_category`` is the raw classification: ``punctuation_only`` when the
    two labels agree after :func:`canonical_trait`, otherwise ``lexical_variant``;
    it never asserts the labels are lexically equivalent.

    ``disposition`` keeps the original *pending* default for the category
    (``formatting_only_pending_policy`` or ``owner_scientific_review``) unless a
    validated, cited ``trait_policy`` (a top-level ``owner_decisions`` record in
    the ledger) is supplied, in which case it is
    ``resolved_<authority>_authoritative`` -- for the ticket #220 decision,
    ``resolved_dbgap_authoritative``.  A missing or incomplete policy therefore
    stays pending rather than being assumed resolved.
    """
    if canonical_trait(analyzed_variable) == canonical_trait(candidate_trait):
        category, pending = "punctuation_only", "formatting_only_pending_policy"
    else:
        category, pending = "lexical_variant", "owner_scientific_review"
    if trait_policy is None:
        return category, pending
    token = re.sub(r"[^a-z0-9]+", "-", trait_policy.authority.casefold()).strip("-")
    return category, f"resolved_{token}_authoritative"


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
                    n_cases=int(float(row["n_cases"] or 0)),
                    n_controls=int(float(row["n_controls"] or 0)),
                    sample_size=int(float(row["sample_size"] or 0)),
                )
            )
    return candidates


def selected_values(values: list[str] | None) -> set[str] | None:
    """Flatten repeatable, comma-separated ``--analysis``/``--member`` selections."""
    chosen: set[str] = set()
    for value in values or []:
        chosen.update(part.strip() for part in value.split(",") if part.strip())
    return chosen or None


@dataclass(frozen=True)
class KeyDiscrepancy:
    """One committed, evidence-backed numeric-key disagreement in the ledger."""

    member: str
    analysis_id: str
    disposition: str
    ancestry: str
    sample_size: int
    n_cases: int
    n_controls: int
    candidate_sample_size: int
    candidate_n_cases: int
    candidate_n_controls: int
    owner_question: str
    owner_decision: str = ""

    @property
    def dbgap_key(self) -> tuple[str, int, int, int]:
        return (self.ancestry, self.sample_size, self.n_cases, self.n_controls)

    @property
    def candidate_numeric_key(self) -> tuple[int, int, int]:
        return (self.candidate_sample_size, self.candidate_n_cases, self.candidate_n_controls)


def required_text(value: object) -> str:
    """A required provenance field; ``None``, blank or non-text is missing.

    Absence is never coerced into evidence: a YAML ``null`` must not become the
    string ``"None"`` and pass a completeness check.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()


@dataclass(frozen=True)
class OwnerPolicy:
    """A validated, cited owner policy, e.g. the trait-label decision."""

    id: str
    authority: str
    quote: str
    recorded: str
    citation: str
    effect: str = ""


def read_trait_policy(path: Path) -> OwnerPolicy | None:
    """The validated ``owner_decisions`` trait policy, or ``None`` if absent/incomplete.

    A missing or incomplete policy is *pending*, never assumed resolved: only a
    record with a non-empty authority, quote, recorded timestamp and citation is
    returned.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for decision in document.get("owner_decisions") or []:
        if not isinstance(decision, dict) or decision.get("id") != "traits":
            continue
        fields = {
            name: required_text(decision.get(name))
            for name in ("authority", "quote", "recorded", "citation")
        }
        if not all(fields.values()):
            return None
        return OwnerPolicy(id="traits", effect=required_text(decision.get("effect")), **fields)
    return None


def read_discrepancies(path: Path) -> list[KeyDiscrepancy]:
    """Read the key-discrepancy ledger, failing loudly on a malformed record."""
    if not path.is_file():
        raise AcquisitionError(f"--discrepancies not found: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records = document.get("records") or []
    discrepancies: list[KeyDiscrepancy] = []
    for index, record in enumerate(records):
        where = f"{path}:records[{index}]"
        try:
            dbgap = record["dbgap"]
            candidate = record["candidate"]
            disposition = record["disposition"]
            member = str(record["member"]).strip()
            analysis_id = str(record["analysis_id"]).strip()
            if disposition not in DISCREPANCY_DISPOSITIONS:
                raise AcquisitionError(f"{where}: unknown disposition {disposition!r}")
            raw_decision = record.get("owner_decision")
            owner_decision = ""
            decision_fields: dict[str, str] = {}
            if isinstance(raw_decision, dict):
                owner_decision = " ".join(
                    (
                        required_text(raw_decision.get("quote"))
                        or required_text(raw_decision.get("statement"))
                    ).split()
                )
                decision_fields = {
                    name: required_text(raw_decision.get(name))
                    for name in ("authority", "recorded", "citation")
                }
            elif raw_decision is not None:
                owner_decision = " ".join(required_text(raw_decision).split())
            if disposition == "resolved":
                missing = sorted(
                    name for name in ("authority", "recorded", "citation")
                    if not decision_fields.get(name)
                )
                if not owner_decision or missing:
                    raise AcquisitionError(
                        f"{where}: a resolved record needs an owner_decision with "
                        "authority, quote, recorded and citation"
                    )
            discrepancy = KeyDiscrepancy(
                member=member,
                analysis_id=analysis_id,
                disposition=disposition,
                ancestry=str(dbgap["ancestry"]).strip(),
                sample_size=int(dbgap["sample_size"]),
                n_cases=int(dbgap.get("n_cases", 0)),
                n_controls=int(dbgap.get("n_controls", 0)),
                candidate_sample_size=int(candidate["sample_size"]),
                candidate_n_cases=int(candidate.get("n_cases", 0)),
                candidate_n_controls=int(candidate.get("n_controls", 0)),
                owner_question=" ".join(str(record.get("owner_question", "")).split()),
                owner_decision=owner_decision,
            )
        except (TypeError, KeyError, ValueError) as exc:
            raise AcquisitionError(f"{where}: malformed record ({exc})") from exc
        discrepancies.append(discrepancy)
    ids = [record.analysis_id for record in discrepancies]
    if len(ids) != len(set(ids)):
        raise AcquisitionError(f"{path}: duplicate analysis_id in the discrepancy ledger")
    return discrepancies


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


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


def load_analysis(
    path: Path, extracted: Path, toc_entry: tuple[str, int] | None, data_member: str | None = None
) -> Analysis:
    """Read one Analysis and attach the data member it describes.

    ``data_member`` overrides the sibling ``.txt.gz`` for the one deposit
    Analysis whose data file sits in a different tar from its metadata file.
    """
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
    member = data_member or str(path.relative_to(extracted)).replace(".metadata.txt", ".txt.gz")
    trait_type = info[METADATA_TRAIT_TYPE].strip().lower()
    if trait_type not in ("binary trait", "quantitative trait"):
        raise AcquisitionError(f"{path}: unrecognised phenotypic trait type {info[METADATA_TRAIT_TYPE]!r}")
    sample_size, n_cases, n_controls = parse_sample_size(info[METADATA_SAMPLE_SIZE], path)
    # The mapping writes the candidate table's convention: 0 stands for "the
    # study has no such count" (a quantitative study). The adapter and this
    # manifest generator read a 0 there as absence, never as a count (#290).
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
        n_cases=n_cases or 0,
        n_controls=n_controls or 0,
    )


def map_analyses(
    analyses: list[Analysis],
    candidates: list[Candidate],
    *,
    report_unused_candidates: bool,
    discrepancies: Sequence[KeyDiscrepancy] = (),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Bind every Analysis to one candidate row; return ``(mapping rows, problems)``.

    A numeric-key disagreement is never resolved by guessing.  ``discrepancies``
    is the committed ledger of known disagreements: a ``resolved`` entry binds
    the named pair with ``match_basis=discrepancy-ledger`` after a recorded human
    decision, and an ``owner_decision_required`` entry is reported as
    ``numeric_key_discrepancy`` and keeps the bijection gate red.  Every entry is
    re-validated against the extracted metadata and the candidate table, so a
    stale or contradictory ledger fails loudly rather than passing silently.
    """
    by_key: dict[tuple[str, int, int, int], list[Candidate]] = {}
    for candidate in candidates:
        by_key.setdefault(candidate.numeric_key, []).append(candidate)

    rows: list[dict[str, str]] = []
    problems: list[dict[str, str]] = []
    used: dict[str, str] = {}
    matched_members: set[str] = set()

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
        matched_members.add(analysis.member)
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

    # Reconcile the committed ledger.  This runs after the exact-key pass so a
    # record that is no longer needed (or now collides with a real binding) is
    # detected as stale instead of silently replacing a valid binding.
    by_member = {analysis.member: analysis for analysis in analyses}
    by_accession = {candidate.analysis_id: candidate for candidate in candidates}
    for discrepancy in discrepancies:
        analysis = by_member.get(discrepancy.member)
        if analysis is None:
            # A partial run selected other members; the full run must account
            # for every ledger record.
            if report_unused_candidates:
                raise AcquisitionError(
                    f"discrepancy ledger names an Analysis that was not extracted: {discrepancy.member}"
                )
            continue
        candidate = by_accession.get(discrepancy.analysis_id)
        if candidate is None:
            raise AcquisitionError(
                f"discrepancy ledger names a candidate row that is absent: {discrepancy.analysis_id}"
            )
        if analysis.numeric_key != discrepancy.dbgap_key:
            raise AcquisitionError(
                f"discrepancy ledger key drift for {discrepancy.member}: "
                f"ledger={discrepancy.dbgap_key} metadata={analysis.numeric_key}"
            )
        if analysis.numeric_key == candidate.numeric_key:
            raise AcquisitionError(
                f"discrepancy ledger entry for {discrepancy.analysis_id} is stale: "
                "the metadata and candidate numeric keys now agree"
            )
        candidate_key = (candidate.sample_size, candidate.n_cases, candidate.n_controls)
        if candidate_key != discrepancy.candidate_numeric_key:
            raise AcquisitionError(
                f"discrepancy ledger candidate key drift for {discrepancy.analysis_id}: "
                f"ledger={discrepancy.candidate_numeric_key} candidate={candidate_key}"
            )
        if candidate.mapped_ancestry != analysis.ancestry:
            raise AcquisitionError(
                f"discrepancy ledger ancestry drift for {discrepancy.analysis_id}: "
                f"metadata={analysis.ancestry} candidate={candidate.mapped_ancestry}"
            )
        if discrepancy.member in matched_members or discrepancy.analysis_id in used:
            raise AcquisitionError(
                f"discrepancy ledger entry for {discrepancy.analysis_id} conflicts with an existing binding"
            )
        problems[:] = [
            row for row in problems
            if not (
                (row["kind"] == "unmatched_analysis" and row["member"] == discrepancy.member)
                or (row["kind"] == "unused_candidate" and row["analysis_id"] == discrepancy.analysis_id)
            )
        ]
        if discrepancy.disposition == "resolved":
            used[discrepancy.analysis_id] = discrepancy.member
            matched_members.add(discrepancy.member)
            check = trait_check(analysis.analyzed_variable, candidate.trait)
            rows.append({
                "analysis_id": discrepancy.analysis_id,
                "member": analysis.member,
                "tar": analysis.tar,
                "ancestry": analysis.ancestry,
                "study_design": candidate.study_design,
                "dbgap_trait_type": analysis.trait_type,
                "sample_size": str(analysis.sample_size),
                "n_cases": str(analysis.n_cases),
                "n_controls": str(analysis.n_controls),
                "dbgap_trait_code": analysis.trait_code,
                "analyzed_variable": analysis.analyzed_variable,
                "candidate_trait": candidate.trait,
                "candidate_store_key": candidate.store_key,
                "match_basis": "discrepancy-ledger",
                "trait_check": check,
            })
            if check == "mismatch":
                problem(
                    "trait_check_mismatch", analysis, [candidate],
                    f"analyzed variable {analysis.analyzed_variable!r} vs {candidate.trait!r}",
                )
        else:
            problem(
                "numeric_key_discrepancy", analysis, [candidate],
                f"{discrepancy.disposition}: metadata key {analysis.numeric_key} vs "
                f"candidate key {candidate.numeric_key}; {discrepancy.owner_question}",
            )
    return rows, problems


def build_review_rows(
    rows: list[dict[str, str]], *, trait_policy: OwnerPolicy | None = None
) -> list[dict[str, str]]:
    """Per-record trait-label queue for every ``trait_check`` mismatch.

    Keeps the original dbGaP and Catalog labels, the raw ``trait_category`` and
    the ``disposition`` derived from the validated ``trait_policy`` (pending if
    none is supplied).
    """
    review: list[dict[str, str]] = []
    for row in rows:
        if row.get("trait_check") != "mismatch":
            continue
        category, disposition = trait_disposition(
            row["analyzed_variable"], row["candidate_trait"], trait_policy=trait_policy
        )
        review.append({
            "analysis_id": row["analysis_id"],
            "member": row["member"],
            "trait_category": category,
            "disposition": disposition,
            "trait_check": row["trait_check"],
            "analyzed_variable": row["analyzed_variable"],
            "candidate_trait": row["candidate_trait"],
        })
    return review


def command_map(args: argparse.Namespace) -> int:
    out = Path(args.out)
    candidates = read_candidates(Path(args.candidates))
    if not candidates:
        raise SystemExit(f"map: no PMID {PUBMED_ID} rows in {args.candidates}")
    discrepancies = read_discrepancies(Path(args.discrepancies))
    trait_policy = read_trait_policy(Path(args.discrepancies))
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
    data_by_name: dict[str, list[str]] = collections.defaultdict(list)
    for listed in tocs:
        data_by_name[Path(listed).name].append(listed)
    analyses: list[Analysis] = []
    for member in members:
        data_member = str(member.relative_to(extracted)).replace(".metadata.txt", ".txt.gz")
        if data_member not in tocs:
            # dbGaP filed a data file in a different tar from its metadata.
            elsewhere = data_by_name.get(Path(data_member).name, [])
            if len(elsewhere) == 1:
                data_member = elsewhere[0]
        analyses.append(load_analysis(member, extracted, tocs.get(data_member), data_member))
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
    rows, problems = map_analyses(
        analyses, candidates, report_unused_candidates=covered_all, discrepancies=discrepancies
    )
    write_tsv(out / "mapping.tsv", MAPPING_COLUMNS, rows)
    write_tsv(out / "mapping-report.tsv", REPORT_COLUMNS, problems)
    review = build_review_rows(rows, trait_policy=trait_policy)
    write_tsv(out / "mapping-review.tsv", REVIEW_COLUMNS, review)

    binding_ok = not any(
        p["kind"] in (
            "unmatched_analysis", "ambiguous_analysis", "duplicate_analysis_match",
            "numeric_key_discrepancy",
        )
        for p in problems
    )
    complete = binding_ok and len(candidates) == 6022 and len(rows) == len(candidates)
    print(
        f"map: {len(analyses)} Analysis(es), {len(candidates)} candidate(s), {len(rows)} matched, "
        f"{len(problems)} problem row(s), {len(review)} trait review row(s); "
        f"binding={'ok' if binding_ok else 'incomplete'}; "
        f"complete={'yes' if complete else 'no'}; wrote {out / 'mapping.tsv'}"
    )
    for kind, count in sorted(collections.Counter(p["kind"] for p in problems).items()):
        print(f"  {kind}={count}")
    if binding_ok and (complete or not covered_all or args.allow_partial):
        return 0
    pending = sum(1 for p in problems if p["kind"] == "numeric_key_discrepancy")
    if binding_ok:
        message = "not a complete bijection over all 6,022 Analyses"
    elif pending:
        message = (
            f"the Analysis/candidate binding has {pending} numeric-key discrepancy(ies) "
            "awaiting a recorded owner decision"
        )
    else:
        message = "the Analysis/candidate binding is not a bijection"
    print("map: " + message, file=sys.stderr)
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


def catalog_provenance(row: dict[str, str], candidate: Candidate) -> dict[str, object] | None:
    """The Catalog values, kept only when they differ from the canonical dbGaP values.

    A row whose Catalog metadata already agrees with the deposit adds no block,
    so an ordinary converted sidecar keeps EBI's field set.  A disagreement
    (the 2 sample-size discrepancies and the 57 label mismatches) preserves the
    Catalog counterpart as provenance rather than discarding it.
    """
    canonical_sample_size = int(row["sample_size"])
    if (
        candidate.trait == row.get("analyzed_variable", "")
        and candidate.sample_size == canonical_sample_size
        and candidate.n_cases == int(row.get("n_cases") or 0)
        and candidate.n_controls == int(row.get("n_controls") or 0)
    ):
        return None
    return {
        "trait_description": [candidate.trait],
        "sample_size": candidate.sample_size,
        "n_cases": candidate.n_cases,
        "n_controls": candidate.n_controls,
        "ancestry_group": candidate.ancestry,
        "ancestry_fraction": candidate.ancestry_fraction,
    }


def render_metadata_yaml(
    row: dict[str, str], candidate: Candidate, data_path: Path, out: Path, tars_dir: Path
) -> str:
    """EBI's ``*-meta.yaml`` fields plus dbGaP-canonical metadata and provenance.

    Canonical metadata follows the owner decision for ticket #220 (2026-10-11):
    the dbGaP ``Analyzed variable`` and deposit sample size are authoritative.
    A Catalog counterpart that disagrees is retained under ``catalog_metadata``.
    """
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
        "trait_description": [row.get("analyzed_variable") or candidate.trait],
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
    catalog = catalog_provenance(row, candidate)
    if catalog is not None:
        document["catalog_metadata"] = catalog
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100)


def metadata_description(out: Path, member: str) -> str:
    extracted = out / "GIA" / "extracted"
    metadata = metadata_path_for(extracted, member)
    if metadata is None:
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
    statuses: dict[str, str] = {}
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
            statuses[row["analysis_id"]] = status
            if status == "error":
                failures += 1
            print(
                f"[{done}/{len(futures)}] {row['analysis_id']} {status} {seconds:.1f}s {error}",
                flush=True,
            )
    elapsed = time.monotonic() - started
    # The files this run's code wrote, recorded at conversion time (#290 review).
    converted = [
        analysis_id for analysis_id, status in statuses.items() if status == "converted"
    ]
    receipt = write_convert_receipt(out, converted)
    if receipt is not None:
        print(f"convert: version receipt {receipt}")
    print("convert: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print(f"convert: {elapsed:.1f}s wall for {len(rows)} Analysis(es)")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


#: The module a converted dbGaP file is rewritten by, as lineage records it.
REFORMAT_TOOL_MODULE = "manifest_generation.providers.dbgap.mvp.acquire_dbgap_phs002453"


#: Where ``convert`` keeps the receipts that say which code wrote each file.
CONVERT_RECEIPTS_DIRNAME = Path("GIA") / "convert-receipts"

#: The version a file's converter carries when no run recorded one.
UNRECORDED_VERSION = (
    "sha256:unrecorded (no conversion-time code identity recorded; searched the conversion "
    "runs and the metadata-refresh run records; see #290)"
)


def module_sha256() -> str:
    """This module's own digest: the converter's version when nothing better is recorded."""
    return sha256_file(Path(__file__).resolve())


def code_sha() -> str:
    """The repository commit executing this code, or ``""`` when it cannot be read."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
    except OSError:  # pragma: no cover - a host without git
        return ""
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", commit) else ""


def reformat_tool() -> str:
    """The rewrite tool and its version: this module's name and its own sha256 (#290 review)."""
    return f"{REFORMAT_TOOL_MODULE} sha256:{module_sha256()}"


def write_convert_receipt(out: Path, analysis_ids: Sequence[str]) -> Path | None:
    """Record which code converted which files in this run (#290 review).

    The version is the module's digest and the commit executing it, written at
    conversion time: a later ``manifest`` run reads it instead of stamping its
    own version onto files it did not write.
    """
    if not analysis_ids:
        return None
    directory = out / CONVERT_RECEIPTS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = directory / f"{recorded_at.replace(':', '')}-{os.getpid()}.json"
    document = {
        "tool": REFORMAT_TOOL_MODULE,
        "module_sha256": module_sha256(),
        "code_sha": code_sha(),
        "recorded_at": recorded_at,
        "analyses": sorted(analysis_ids),
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def recorded_versions(out: Path) -> dict[str, str]:
    """``analysis_id -> reformat_tool`` from the conversion receipts convert wrote."""
    versions: dict[str, str] = {}
    for path in sorted((out / CONVERT_RECEIPTS_DIRNAME).glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcquisitionError(f"{path} is not a conversion receipt: {exc}") from exc
        digest = document.get("module_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AcquisitionError(f"{path} records no module_sha256")
        tool = f"{REFORMAT_TOOL_MODULE} sha256:{digest}"
        commit = document.get("code_sha")
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
            tool += f" code:{commit}"
        for analysis_id in document.get("analyses") or []:
            versions[str(analysis_id)] = tool
    return versions


def historical_versions(directory: Path) -> dict[str, str]:
    """``analysis_id -> reformat_tool`` from a conversion run's own record (#290 review).

    A metadata-refresh run records its code commit and, per Analysis, what it
    did: a file it converted carries that commit as its version, while a file it
    only canonicalised was converted by an earlier run whose code identity the
    record does not name. Nothing is inferred beyond what the record says.
    """
    summary_path = directory / "parent-run-summary.json"
    receipt_path = directory / "refresh-receipt.tsv"
    if not summary_path.is_file() or not receipt_path.is_file():
        raise AcquisitionError(
            f"--conversion-history {directory} records no parent-run-summary.json and "
            "refresh-receipt.tsv pair"
        )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    commit = summary.get("code_sha")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise AcquisitionError(f"{summary_path} records no code_sha")
    versions: dict[str, str] = {}
    for row in read_tsv(receipt_path):
        analysis_id = row.get("analysis_id", "")
        if not analysis_id:
            raise AcquisitionError(f"{receipt_path}: a row has no analysis_id")
        if row.get("action") == "new-conversion":
            versions[analysis_id] = f"{REFORMAT_TOOL_MODULE} code:{commit}"
    return versions


def reformat_lineage(
    mapping: dict[str, str],
    *,
    tars_dir: Path,
    extracted: Path,
    original_sha256: str,
    original_size: int,
    tool: str = "",
) -> dict[str, str]:
    """The tar member a converted file was rewritten from, and what changed it.

    A dbGaP member is one file inside a published tar, so its location is
    ``<tar path>#<member>``: the tar the deposit published and the member
    inside it. The checksum is the extracted member's own sha256 -- the tar
    itself is deleted after extraction, so its recorded md5 identifies the
    container rather than these bytes (ADR 0036's reformatting rule, #290).
    """
    tar = mapping.get("tar", "")
    member = mapping.get("member", "")
    if not tar or not member:
        raise AcquisitionError(
            f"{mapping.get('analysis_id', '?')}: the mapping names no tar/member, so the "
            "rewritten file has no original to record"
        )
    original = extracted / member
    if not original.is_file():
        raise AcquisitionError(
            f"{mapping['analysis_id']}: the extracted original {original} is gone, so the "
            "rewritten file has no durable original identity to record"
        )
    actual = original.stat().st_size
    if actual != original_size:
        raise AcquisitionError(
            f"{mapping['analysis_id']}: the deposited member is {original_size} bytes, but "
            f"{original} is {actual}; it changed after extraction"
        )
    return {
        "original_location": f"{tars_dir / tar}#{member}",
        "original_sha256": original_sha256,
        "reformat_tool": tool or reformat_tool(),
        "original_bytes": str(original_size),
    }


def member_sizes(out: Path) -> dict[str, int]:
    """``member -> size`` from the deposit's tables of contents.

    An empty deposit (no tables of contents) is no sizes, not an error; a bad
    one -- a duplicate member, an unsafe path -- is the deposit's own refusal
    (``AcquisitionError``), which names its rule and must not be swallowed
    (#290 review).
    """
    return {member: size for member, (_tar, size) in load_toc(out).items()}


def manifest_row(
    mapping: dict[str, str],
    data_root: Path,
    recorded: dict[str, dict[str, str]] | None,
    digest: str | None,
    lineage: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """One shared Acquisition Manifest row for one mapped Analysis (#290).

    ``recorded`` is the acquisition's own per-store download manifest row when
    there is one: it carries the sha256 the acquisition computed while it
    converted the file, so a metadata-only reproduction need not re-read 4 TB.
    The recorded size is re-checked against disk, and a recorded non-ready
    status is carried rather than turned into a usable file.
    """
    analysis_id = mapping["analysis_id"]
    directory = data_root / "gwas-ssf" / bucket_of(analysis_id) / analysis_id
    path = directory / f"{analysis_id}.tsv.gz"
    yaml_path = directory / f"{analysis_id}.tsv.gz-meta.yaml"
    row = {name: "" for name in ACQUISITION_MANIFEST.columns}
    row["source_analysis_id"] = analysis_id
    row["source_url"] = f"{DBGAP_BASE}/{mapping['tar']}" if mapping.get("tar") else ""

    if recorded and recorded.get("status") and recorded["status"] not in READY_STATUSES:
        row["status"] = recorded["status"]
        row["error"] = recorded.get("error", "")
        return row
    if not path.is_file() or path.stat().st_size == 0:
        # An explained absence: this Analysis has no converted file yet, so the
        # Source Inventory keeps a non-ready row for it and it never becomes a
        # release member. The status names the reason rather than "error".
        row["status"] = "data_absent_upstream"
        row["error"] = f"not converted: {path}"
        return row
    if digest is None:
        raise AcquisitionError(f"no sha256 for {analysis_id}; cannot write a ready manifest row")
    recorded_bytes = (recorded or {}).get("data_bytes")
    if recorded_bytes and int(recorded_bytes) != path.stat().st_size:
        raise AcquisitionError(
            f"{analysis_id}: recorded data_bytes {recorded_bytes} is not the file's size "
            f"{path.stat().st_size}; re-convert before recording it"
        )
    row.update({
        "status": "ok",
        "location": str(path),
        "bytes": str(path.stat().st_size),
        "sha256": digest,
    })
    if yaml_path.is_file() and yaml_path.stat().st_size > 0:
        row["sidecar_location"] = str(yaml_path)
        row["sidecar_bytes"] = str(yaml_path.stat().st_size)
    if lineage is not None:
        row.update(lineage)
    return row


def read_mapping_rows(path: Path) -> list[dict[str, str]]:
    """The acquisition mapping's rows, requiring the columns a manifest needs."""
    if not path.is_file():
        raise AcquisitionError(f"mapping not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        missing = [column for column in ("analysis_id", "member", "tar") if column not in (reader.fieldnames or [])]
        if missing:
            raise AcquisitionError(f"{path} is missing column(s): {', '.join(missing)}")
        rows = [dict(row) for row in reader]
    if not rows:
        raise AcquisitionError(f"mapping is empty: {path}")
    return rows


def read_recorded_manifests(directory: Path) -> dict[str, dict[str, str]]:
    """``analysis_id -> row`` from the acquisition's per-store download manifests."""
    recorded: dict[str, dict[str, str]] = {}
    for path in sorted(directory.glob("*-download-manifest.tsv")):
        for row in read_tsv(path):
            analysis_id = row.get("analysis_id", "")
            if not analysis_id:
                raise AcquisitionError(f"{path}: a recorded manifest row has no analysis_id")
            if analysis_id in recorded:
                raise AcquisitionError(f"{analysis_id} is recorded in more than one download manifest")
            recorded[analysis_id] = row
    return recorded


def command_manifest(args: argparse.Namespace) -> int:
    """Write the Source Collection's one shared Acquisition Manifest (#290)."""
    out = Path(args.out)
    mapping_path = Path(args.mapping) if args.mapping else out / "mapping.tsv"
    data_root = Path(args.data_root) if args.data_root else out
    mapping = read_mapping_rows(mapping_path)
    if args.analysis:
        wanted = set(args.analysis)
        mapping = [row for row in mapping if row["analysis_id"] in wanted]
    if args.limit:
        mapping = mapping[: args.limit]
    if not mapping:
        raise AcquisitionError("manifest: nothing selected")
    recorded: dict[str, dict[str, str]] = {}
    if args.recorded_manifests:
        recorded = read_recorded_manifests(Path(args.recorded_manifests))
        unknown = sorted(set(recorded) - {row["analysis_id"] for row in mapping})
        if unknown and not (args.analysis or args.limit):
            raise AcquisitionError(
                f"recorded manifests name Analysis/Analyses the mapping does not: {', '.join(unknown[:10])}"
            )

    # The checksum is the release fact freeze records. It is the acquisition's
    # own recorded digest when there is one; otherwise it is computed here from
    # the materialised bytes.
    digests: dict[str, str] = {}
    missing_recorded = [
        row["analysis_id"] for row in mapping
        if row["analysis_id"] not in recorded
        and (data_root / "gwas-ssf" / bucket_of(row["analysis_id"]) / row["analysis_id"] / f"{row['analysis_id']}.tsv.gz").is_file()
    ]
    if missing_recorded:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = {
                pool.submit(
                    sha256_file,
                    data_root / "gwas-ssf" / bucket_of(analysis_id) / analysis_id / f"{analysis_id}.tsv.gz",
                ): analysis_id
                for analysis_id in missing_recorded
            }
            for future in as_completed(futures):
                digests[futures[future]] = future.result()
    for analysis_id, row in recorded.items():
        if row.get("sha256"):
            digests[analysis_id] = row["sha256"]

    # The original identity of every file acquisition rewrote (#290 review): the
    # tar member it came from, and that member's own sha256. The tar itself is
    # deleted after extraction, so the extracted member is the durable original.
    tars_dir = out / "GIA" / "tars"
    extracted = out / "GIA" / "extracted"
    sizes = member_sizes(out)
    ready_rows = [
        row for row in mapping
        if row["analysis_id"] in digests
    ]
    original_digests: dict[str, str] = {}
    originals = {row["analysis_id"]: extracted / row["member"] for row in ready_rows}
    missing = sorted(
        analysis_id for analysis_id, path in originals.items() if not path.is_file()
    )
    if missing:
        raise AcquisitionError(
            f"{len(missing)} rewritten file(s) have no durable original to record: "
            + ", ".join(missing[:10])
            + (" ..." if len(missing) > 10 else "")
        )
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(sha256_file, path): analysis_id
            for analysis_id, path in originals.items()
        }
        for future in as_completed(futures):
            original_digests[futures[future]] = future.result()
    versions = recorded_versions(out)
    for directory in args.conversion_history or []:
        versions.update(historical_versions(Path(directory)))
    lineage_by_id: dict[str, dict[str, str]] = {}
    for row in ready_rows:
        analysis_id = row["analysis_id"]
        size = originals[analysis_id].stat().st_size
        recorded_size = sizes.get(row["member"])
        if recorded_size is not None and recorded_size != size:
            raise AcquisitionError(
                f"{analysis_id}: the table of contents records {recorded_size} bytes for "
                f"{row['member']}, but the extracted member is {size}"
            )
        lineage_by_id[analysis_id] = reformat_lineage(
            row, tars_dir=tars_dir, extracted=extracted,
            original_sha256=original_digests[analysis_id], original_size=size,
            tool=versions.get(analysis_id, f"{REFORMAT_TOOL_MODULE} {UNRECORDED_VERSION}"),
        )
    rows = [
        manifest_row(
            row, data_root, recorded.get(row["analysis_id"]), digests.get(row["analysis_id"]),
            lineage_by_id.get(row["analysis_id"]),
        )
        for row in mapping
    ]
    manifest_path = Path(args.manifest) if args.manifest else out / "acquisition-manifest.tsv"
    write_connector(manifest_path, ACQUISITION_MANIFEST, ACQUISITION_MANIFEST.columns, rows)
    loaded = load_acquisition_manifest(manifest_path)
    print(
        f"manifest: {len(loaded.entries)} rows ({loaded.readiness_counts}) -> {manifest_path}"
    )
    return 0


# ---------------------------------------------------------------------------
# refresh (metadata-only canonicalisation + manifest merge)
# ---------------------------------------------------------------------------

#: Per-accession receipt for a metadata refresh.  ``data_file_md5sum`` is the
#: identity carried forward from the existing sidecar, never re-validated here.
REFRESH_RECEIPT_COLUMNS: tuple[str, ...] = (
    "analysis_id", "member", "store_key", "action", "changed_fields",
    "data_file_md5sum", "data_file_name", "sha256_provenance", "source_sidecar", "note",
)

#: Strict digest shapes; a malformed value is an error, never silently accepted.
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_MD5_HEX = re.compile(r"^[0-9a-f]{32}$")


def validate_digest(value: str, kind: str, where: str) -> str:
    pattern = _SHA256_HEX if kind == "sha256" else _MD5_HEX
    value = (value or "").strip().lower()
    if not pattern.match(value):
        raise AcquisitionError(f"{where}: invalid {kind} digest {value!r}")
    return value


def ensure_isolated(out: Path, source: Path, label: str) -> None:
    """Refuse an output root that overlaps a read-only source root."""
    out_r, src_r = out.resolve(), source.resolve()
    if out_r == src_r or out_r in src_r.parents or src_r in out_r.parents:
        raise AcquisitionError(f"--out {out_r} must not overlap {label} {src_r}")


def resolved_within(child: Path, root: Path) -> bool:
    """True when ``child`` resolves to a path inside ``root`` (symlink-safe)."""
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_write_within(target: Path, out_root: Path, *, follow_final: bool = True) -> None:
    """Every write target/temp must resolve inside the isolated root.

    Checks the deepest existing ancestor so a symlinked parent cannot redirect a
    write into a read-only source tree.  ``follow_final=False`` is for a data
    symlink we create on purpose: its *parent* must be inside the root, but its
    target is the read-only source and may be outside.
    """
    out_r = out_root.resolve()
    if follow_final:
        resolved = target.resolve()
    else:
        resolved = target.parent.resolve() / target.name
    if not (resolved == out_r or out_r in resolved.parents):
        raise AcquisitionError(f"write target {target} escapes the isolated root {out_r}")


def validate_sidecar_binding(
    document: dict[str, object], row: dict[str, str], accession: str, where: str
) -> None:
    """The sidecar's source/data bindings must agree with the mapping row."""
    source = document.get("source")
    if not isinstance(source, dict):
        raise AcquisitionError(f"{where}: sidecar has no source provenance")
    if source.get("member") != row["member"]:
        raise AcquisitionError(
            f"{where}: source.member {source.get('member')!r} != mapping member {row['member']!r}"
        )
    if source.get("tar") != row["tar"]:
        raise AcquisitionError(f"{where}: source.tar {source.get('tar')!r} != mapping tar {row['tar']!r}")
    validate_digest(str(source.get("tar_md5", "")), "md5", f"{where} source.tar_md5")
    if document.get("data_file_name") != f"{accession}.tsv.gz":
        raise AcquisitionError(
            f"{where}: data_file_name {document.get('data_file_name')!r} != {accession}.tsv.gz"
        )


def canonical_manifest_row(
    inherited: dict[str, str], row: dict[str, str], candidate: Candidate,
    *, data_file: str, yaml_file: str,
) -> dict[str, str]:
    """A canonical manifest row: regenerated metadata fields + inherited data identity.

    Canonical metadata (``trait``, ``sample_size``, ``study_design``, ``data_url``)
    is regenerated from the mapping/candidate so no stale Catalog field survives.
    Data identity (``status``, sizes, ``sha256``, assembly, ...) is inherited from
    ``inherited``; it is provenance carried forward, not a current validation.
    """
    result = {name: "" for name in MANIFEST_COLUMNS}
    result.update({
        "analysis_id": candidate.analysis_id,
        "publication_pmid": PUBMED_ID,
        "trait": row.get("analyzed_variable") or candidate.trait,
        "study_design": candidate.study_design,
        "sample_size": row.get("sample_size") or str(candidate.sample_size),
        "data_url": f"{DBGAP_BASE}/{row['tar']}" if row.get("tar") else "",
        "harmonised_status": inherited.get("harmonised_status", ""),
    })
    for name in ("status", "data_bytes", "yaml_bytes", "sha256", "genome_assembly",
                 "file_type", "is_harmonised", "seconds", "error"):
        result[name] = inherited.get(name, "")
    result["data_file"] = data_file
    result["yaml_file"] = yaml_file
    digest = str(result.get("sha256", "")).strip()
    if result["status"] == "ok":
        # A ready row must carry a valid sha256; an empty/malformed digest is a
        # non-ready row, never a silent success.
        if not digest or not _SHA256_HEX.match(digest.lower()):
            result["status"] = "error"
            result["error"] = "ready row has no valid sha256"
        else:
            result["sha256"] = digest.lower()
    elif not str(result.get("error", "")).strip():
        result["error"] = "non-ready row without an error message"
    return result


def read_sidecar_document(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AcquisitionError(f"{path}: sidecar is not a YAML mapping")
    return document


def load_sidecar_index(root: Path) -> dict[str, Path]:
    """``accession -> sidecar path`` under ``<root>/<bucket>/<GCST>/``."""
    index: dict[str, Path] = {}
    for path in sorted(root.glob("*/GCST*/GCST*.tsv.gz-meta.yaml")):
        accession = path.name[: -len(".tsv.gz-meta.yaml")]
        if accession in index:
            raise AcquisitionError(
                f"duplicate sidecar for {accession}: {index[accession]} and {path}"
            )
        index[accession] = path
    return index


def canonical_sidecar(
    existing: dict[str, object], row: dict[str, str], candidate: Candidate, *, source_sidecar: str
) -> tuple[dict[str, object], list[str]]:
    """Return ``(document, changed_fields)`` for a metadata-only canonical refresh.

    The data identity (``data_file_md5sum``, ``data_file_name``, ``source``) is
    carried forward from the existing sidecar and never recomputed; a
    ``metadata_refresh`` block records that provenance.  The canonical values
    follow the owner decision (#220): dbGaP is authoritative and the Catalog
    counterpart is retained under ``catalog_metadata`` only where it differs.
    """
    document = dict(existing)
    changed: list[str] = []
    canonical_trait = row.get("analyzed_variable") or candidate.trait
    samples: dict[str, object] = {
        "sample_ancestry_category": [candidate.yaml_ancestry],
        "sample_size": int(row["sample_size"]),
    }
    if candidate.study_design == "case-control":
        samples["case_control_study"] = True
    if document.get("trait_description") != [canonical_trait]:
        changed.append("trait_description")
    document["trait_description"] = [canonical_trait]
    if document.get("samples") != [samples]:
        changed.append("samples")
    document["samples"] = [samples]
    catalog = catalog_provenance(row, candidate)
    if catalog:
        if document.get("catalog_metadata") != catalog:
            changed.append("catalog_metadata")
        document["catalog_metadata"] = catalog
    elif "catalog_metadata" in document:
        changed.append("catalog_metadata")
        del document["catalog_metadata"]
    carried = document.get("data_file_md5sum")
    if not isinstance(carried, str) or not carried.strip():
        raise AcquisitionError(f"{source_sidecar}: existing sidecar has no data_file_md5sum")
    document["metadata_refresh"] = {"reused_data_md5sum": True, "source_sidecar": source_sidecar}
    return document, changed


def merge_manifest_rows(
    existing_rows: list[dict[str, str]],
    canonical: dict[str, dict[str, str]],
    refreshed: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    """Replace refreshed accessions and canonicalise the trait/size of the rest.

    Preserved rows keep every other column byte-for-byte (including their
    recorded ``sha256``/``data_bytes``, treated as carried-forward provenance);
    refreshed rows come from the isolated new conversion manifest.  An accession
    absent from the existing manifest, or a manifest row outside the canonical
    set, fails loudly rather than fabricating an ``ok`` row.
    """
    existing_ids = [row["analysis_id"] for row in existing_rows]
    if len(existing_ids) != len(set(existing_ids)):
        raise AcquisitionError("existing manifest has duplicate analysis_id rows")
    unknown = sorted(set(refreshed) - set(existing_ids))
    if unknown:
        raise AcquisitionError(
            f"refreshed accession(s) absent from the existing manifest: {', '.join(unknown)}"
        )
    outside = sorted(set(existing_ids) - set(canonical))
    if outside:
        raise AcquisitionError(
            f"existing manifest row(s) outside the candidate store set: {', '.join(outside[:10])}"
        )
    missing = sorted(set(canonical) - set(existing_ids))
    if missing:
        raise AcquisitionError(
            f"candidate store accession(s) absent from the existing manifest: {', '.join(missing[:10])}"
        )
    merged: list[dict[str, str]] = []
    for row in existing_rows:
        accession = row["analysis_id"]
        canonical_row = canonical.get(accession, {})
        new = dict(row)
        if canonical_row:
            for field in ("trait", "sample_size", "study_design", "data_url"):
                if field in canonical_row:
                    new[field] = canonical_row[field]
        if accession in refreshed:
            new = dict(refreshed[accession])
        merged.append(new)
    return merged


def command_refresh(args: argparse.Namespace) -> int:
    out = Path(args.out)
    mapping_path = Path(args.mapping) if args.mapping else out / "mapping.tsv"
    rows = read_tsv(mapping_path)
    if not rows:
        raise SystemExit(f"refresh: mapping is empty: {mapping_path}")
    source_sidecar_root = Path(args.source_sidecars)
    source_manifest_dir = Path(args.source_manifest_dir)
    new_sidecar_root = Path(args.new_sidecars) if args.new_sidecars else None
    raw_new_manifest_dir = Path(args.new_manifest_dir) if args.new_manifest_dir else None
    ensure_isolated(out, source_sidecar_root, "--source-sidecars")
    ensure_isolated(out, source_manifest_dir, "--source-manifest-dir")
    if new_sidecar_root is not None:
        ensure_isolated(out, new_sidecar_root, "--new-sidecars")
    if raw_new_manifest_dir is not None:
        ensure_isolated(out, raw_new_manifest_dir, "--new-manifest-dir")
    manifest_dir = Path(args.manifest_dir) if args.manifest_dir else out / "manifests"
    if not resolved_within(manifest_dir, out):
        raise AcquisitionError(f"--manifest-dir {manifest_dir} must resolve inside --out {out}")
    candidates = read_candidates(Path(args.candidates))
    by_accession: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.analysis_id in by_accession:
            raise AcquisitionError(f"duplicate candidate accession {candidate.analysis_id}")
        by_accession[candidate.analysis_id] = candidate
    mapping_by_id: dict[str, dict[str, str]] = {}
    seen_members: set[str] = set()
    for row in rows:
        accession = row["analysis_id"]
        if accession in mapping_by_id:
            raise AcquisitionError(f"duplicate mapping analysis_id {accession}")
        member = row["member"]
        if not member or not member.endswith(".txt.gz") or not member.startswith("MVP_R4.1000G_AGR."):
            raise AcquisitionError(f"{accession}: unexpected member binding {member!r}")
        if member in seen_members:
            raise AcquisitionError(f"duplicate mapping member {member}")
        seen_members.add(member)
        if not row.get("tar"):
            raise AcquisitionError(f"{accession}: mapping has no source tar")
        if accession not in by_accession:
            raise AcquisitionError(f"{accession}: not present in the candidate table")
        candidate = by_accession[accession]
        if row.get("candidate_store_key") and row["candidate_store_key"] != candidate.store_key:
            raise AcquisitionError(
                f"{accession}: store_key binding mismatch "
                f"({row['candidate_store_key']} vs {candidate.store_key})"
            )
        mapping_by_id[accession] = row

    if set(mapping_by_id) != set(by_accession):
        missing = sorted(set(by_accession) - set(mapping_by_id))
        extra = sorted(set(mapping_by_id) - set(by_accession))
        raise AcquisitionError(
            f"mapping/candidate membership mismatch: missing={missing[:10]} extra={extra[:10]}"
        )

    source_sidecars = load_sidecar_index(source_sidecar_root) if source_sidecar_root.is_dir() else {}
    new_sidecars = load_sidecar_index(new_sidecar_root) if new_sidecar_root and new_sidecar_root.is_dir() else {}
    new_manifests: dict[str, dict[str, str]] = {}
    if raw_new_manifest_dir and raw_new_manifest_dir.is_dir():
        for path in sorted(raw_new_manifest_dir.glob("*-download-manifest.tsv")):
            for manifest_row in read_tsv(path):
                if manifest_row["analysis_id"] in new_manifests:
                    raise AcquisitionError(
                        f"duplicate new manifest analysis_id {manifest_row['analysis_id']}"
                    )
                new_manifests[manifest_row["analysis_id"]] = manifest_row
    unknown = sorted(set(new_manifests) - set(by_accession))
    if unknown:
        raise AcquisitionError(f"new manifest row(s) not in the candidate set: {', '.join(unknown[:10])}")
    if set(new_manifests) != set(new_sidecars):
        raise AcquisitionError(
            "new sidecar/manifest sets differ: "
            f"sidecars_only={sorted(set(new_sidecars) - set(new_manifests))[:10]} "
            f"manifests_only={sorted(set(new_manifests) - set(new_sidecars))[:10]}"
        )
    for accession, new_row in new_manifests.items():
        if new_row["status"] != "ok":
            raise AcquisitionError(f"{accession}: new manifest row is not ok (status={new_row['status']!r})")
        validate_digest(new_row["sha256"], "sha256", f"{accession} new manifest sha256")
        expected_data = new_sidecar_root / bucket_of(accession) / accession / f"{accession}.tsv.gz"
        expected_yaml = new_sidecar_root / bucket_of(accession) / accession / f"{accession}.tsv.gz-meta.yaml"
        if Path(new_row["data_file"]).resolve() != expected_data.resolve():
            raise AcquisitionError(f"{accession}: new manifest data_file does not bind the new source data path")
        if Path(new_row["yaml_file"]).resolve() != expected_yaml.resolve():
            raise AcquisitionError(f"{accession}: new manifest yaml_file does not bind the new sidecar path")

    out_sidecars = out / "gwas-ssf"
    receipt: list[dict[str, str]] = []
    for accession in sorted(mapping_by_id):
        row = mapping_by_id[accession]
        candidate = by_accession[accession]
        if accession in new_sidecars:
            document = read_sidecar_document(new_sidecars[accession])
            if document.get("gwas_id") != accession:
                raise AcquisitionError(f"{new_sidecars[accession]}: gwas_id != {accession}")
            validate_sidecar_binding(document, row, accession, str(new_sidecars[accession]))
            action, changed, source, provenance = "new-conversion", ["all"], str(new_sidecars[accession]), "derived"
            source_data = new_sidecar_root / bucket_of(accession) / accession / f"{accession}.tsv.gz"
        else:
            path = source_sidecars.get(accession)
            if path is None:
                raise AcquisitionError(
                    f"{accession}: no existing sidecar under {args.source_sidecars} (refusing to fabricate one)"
                )
            existing = read_sidecar_document(path)
            if existing.get("gwas_id") != accession:
                raise AcquisitionError(f"{path}: gwas_id != {accession}")
            validate_sidecar_binding(existing, row, accession, str(path))
            document, changed = canonical_sidecar(existing, row, candidate, source_sidecar=str(path))
            action, source, provenance = ("canonicalised" if changed else "unchanged"), str(path), "inherited"
            source_data = source_sidecar_root / bucket_of(accession) / accession / f"{accession}.tsv.gz"
        validate_digest(str(document.get("data_file_md5sum", "")), "md5", f"{accession} sidecar")
        directory = out_sidecars / bucket_of(accession) / accession
        target = directory / f"{accession}.tsv.gz-meta.yaml"
        link = directory / f"{accession}.tsv.gz"
        ensure_write_within(directory, out)
        ensure_write_within(link, out, follow_final=False)
        directory.mkdir(parents=True, exist_ok=True)
        partial, handle = open_refresh_partial(target, out)
        with handle:
            handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100))
        os.replace(partial, target)
        if link.is_symlink():
            if link.resolve() != source_data.resolve():
                raise AcquisitionError(f"{link}: existing symlink points elsewhere")
        elif link.exists():
            raise AcquisitionError(f"{link}: refusing to replace a non-symlink file")
        elif source_data.exists():
            os.symlink(source_data.resolve(), link)
        receipt.append({
            "analysis_id": accession,
            "member": row["member"],
            "store_key": candidate.store_key,
            "action": action,
            "changed_fields": ",".join(changed),
            "data_file_md5sum": str(document.get("data_file_md5sum", "")),
            "data_file_name": str(document.get("data_file_name", "")),
            "sha256_provenance": provenance,
            "source_sidecar": source,
            "note": "data identity carried forward" if provenance == "inherited" else "new conversion",
        })
    write_tsv_guarded(out / "refresh-receipt.tsv", REFRESH_RECEIPT_COLUMNS, receipt, out)

    manifest_dir = Path(args.manifest_dir) if args.manifest_dir else out / "manifests"
    by_store: dict[str, list[str]] = {}
    for accession, candidate in by_accession.items():
        if accession in mapping_by_id:
            by_store.setdefault(candidate.store_key, []).append(accession)
    failures = 0
    merged_total = 0
    for store_key, accessions in sorted(by_store.items()):
        slug = store_key.replace("__", "-")
        existing_path = source_manifest_dir / f"{slug}-download-manifest.tsv"
        if not existing_path.is_file():
            raise AcquisitionError(f"missing existing manifest: {existing_path}")
        existing_rows = read_tsv(existing_path)
        existing_by_id: dict[str, dict[str, str]] = {}
        for existing_row in existing_rows:
            if existing_row["analysis_id"] in existing_by_id:
                raise AcquisitionError(f"{existing_path}: duplicate analysis_id {existing_row['analysis_id']}")
            existing_by_id[existing_row["analysis_id"]] = existing_row
        if set(existing_by_id) != set(accessions):
            raise AcquisitionError(f"{slug}: existing manifest rows do not match the candidate store set")
        merged: list[dict[str, str]] = []
        for existing_row in existing_rows:
            accession = existing_row["analysis_id"]
            candidate = by_accession[accession]
            mapping_row = mapping_by_id[accession]
            inherited = dict(new_manifests.get(accession, existing_row))
            directory = out_sidecars / bucket_of(accession) / accession
            data_path = directory / f"{accession}.tsv.gz"
            yaml_path = directory / f"{accession}.tsv.gz-meta.yaml"
            if accession not in new_sidecars:
                expected_source = source_sidecar_root / bucket_of(accession) / accession / f"{accession}.tsv.gz"
                if Path(existing_row["data_file"]).resolve() != expected_source.resolve():
                    raise AcquisitionError(
                        f"{accession}: existing manifest data_file does not bind the source data path"
                    )
            if not data_path.exists():
                # Metadata preview may report a non-ready row; the full manifest
                # must not claim readiness for absent data.
                inherited["status"] = "error"
                inherited["error"] = f"data file missing: {data_path}"
            else:
                actual_bytes = data_path.stat().st_size
                recorded = inherited.get("data_bytes", "")
                if recorded and recorded != str(actual_bytes):
                    inherited["status"] = "error"
                    inherited["error"] = f"data size mismatch: manifest {recorded} != actual {actual_bytes}"
                else:
                    inherited["data_bytes"] = str(actual_bytes)
            canonical_row = canonical_manifest_row(
                inherited, mapping_row, candidate, data_file=str(data_path), yaml_file=str(yaml_path)
            )
            canonical_row["yaml_bytes"] = str(yaml_path.stat().st_size)
            if canonical_row["status"] != "ok" and not args.preview:
                failures += 1
            merged.append(canonical_row)
        manifest_path = manifest_dir / f"{slug}-download-manifest.tsv"
        write_tsv_guarded(manifest_path, MANIFEST_COLUMNS, merged, out)
        merged_total += len(merged)
        print(f"refresh: {store_key}: {len(merged)} rows -> {manifest_path}")
    print(f"refresh: {len(receipt)} sidecar(s), {merged_total} manifest row(s), preview={bool(args.preview)}")
    return 1 if failures else 0


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
    map_parser.add_argument(
        "--discrepancies", default=DEFAULT_DISCREPANCIES,
        help="key-discrepancy ledger (default: %(default)s)",
    )
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

    manifest = subparsers.add_parser(
        "manifest", help="write the Source Collection's one shared Acquisition Manifest"
    )
    manifest.add_argument("--mapping", help="mapping TSV (default: <out>/mapping.tsv)")
    manifest.add_argument("--data-root", help="root of the converted gwas-ssf tree (default: <out>)")
    manifest.add_argument(
        "--recorded-manifests",
        help="the acquisition's per-store download manifests, whose sha256 values are reused",
    )
    manifest.add_argument("--manifest", help="output TSV (default: <out>/acquisition-manifest.tsv)")
    manifest.add_argument(
        "--conversion-history",
        action="append",
        help=(
            "a conversion run's own record (parent-run-summary.json and refresh-receipt.tsv) "
            "whose code_sha and per-Analysis actions recover a historical converter version"
        ),
    )
    manifest.add_argument("--jobs", type=int, default=8)
    manifest.add_argument("--analysis", action="append")
    manifest.add_argument("--limit", type=int)
    manifest.set_defaults(func=command_manifest)

    refresh = subparsers.add_parser(
        "refresh",
        help="metadata-only canonical sidecar refresh + manifest merge (no association reads)",
    )
    refresh.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    refresh.add_argument("--mapping", help="mapping TSV (default: <out>/mapping.tsv)")
    refresh.add_argument("--source-sidecars", required=True, help="existing gwas-ssf root (read-only, metadata only)")
    refresh.add_argument("--source-manifest-dir", required=True, help="existing manifest directory (read-only)")
    refresh.add_argument("--new-sidecars", default="", help="newly converted gwas-ssf root (the retry outputs)")
    refresh.add_argument("--new-manifest-dir", default="", help="newly converted manifest directory")
    refresh.add_argument("--manifest-dir", help="default: <out>/manifests")
    refresh.add_argument(
        "--preview", action="store_true",
        help="metadata preview: absent data is reported non-ready instead of failing the run",
    )
    refresh.set_defaults(func=command_refresh)

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
