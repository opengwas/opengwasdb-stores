#!/usr/bin/env python3
"""Verify the maintained HapMap3 GRCh38 variant list (stores#208, epic #214).

The list is a Reference Resource: a large build input that lives outside Git at
``/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt`` and is consumed
by ``opengwasdb build-indexed-subset --variant-list``. This tool answers the one
question an operator has before a build: *is the local copy the expected one?*

    pixi run python resources/reference-resources/hapmap3-hg38/verify_hm3_resource.py

It refuses loudly -- naming the path and, for a bad line, its number -- when the
list is missing, malformed, duplicated, non-canonical, the wrong length, or does
not match the declared SHA-256. It also checks that the declared source
checksum and the rsID reconciliation balance, so a list that no longer matches
its recorded provenance cannot pass.

The canonical-ALID rule is the pinned opengwasdb ``parse_canonical_alid``
round-trip, not a private copy: a candidate is accepted only if it is already
canonical, so ``chr1:...``, ``23:...``, lower-case alleles and unsorted allele
pairs are all refused. Alt contigs such as ``8_KI270821v1_alt`` are canonical
and allowed; the list is in coordinate order, not lexicographic, and order is
not constrained here because the subset builder sorts Store indices itself.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import sys
from pathlib import Path

import yaml

# The pinned opengwasdb owns the canonical-ALID convention. Import it from the
# repository root so the verifier reads the same rule the consumer applies; the
# repository pin predates `build-indexed-subset`, so the round-trip is built
# from `parse_canonical_alid` rather than importing the newer
# `read_variant_list` (opengwas/opengwasdb#264). stores#209 advances the pin.

from opengwasdb.variants.axis import parse_canonical_alid

DEFAULT_RESOURCE = Path(__file__).resolve().parent / "resource.yaml"


class VerificationError(Exception):
    """The resource is not the declared one; the caller must not proceed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_alid_or_none(candidate: str) -> str | None:
    """``candidate`` in canonical form, or ``None`` when it is not canonical.

    Mirrors ``opengwasdb``'s variant-list reader: parse, reorder the allele pair
    lexically, and require the rebuilt string to equal the candidate exactly. A
    parse that only succeeds through normalisation (a ``chr`` prefix, a numeric
    chromosome alias, lower-case alleles) therefore fails, which is the point.
    """

    parsed = parse_canonical_alid(candidate)
    if parsed is None:
        return None
    a1, a2 = sorted((parsed.effect_allele, parsed.other_allele))
    canonical = f"{parsed.chromosome}:{parsed.position}:{a1}:{a2}"
    return canonical if canonical == candidate else None


def read_expected(resource_path: Path) -> dict:
    if not resource_path.is_file():
        raise VerificationError(f"resource declaration not found: {resource_path}")
    try:
        document = yaml.safe_load(resource_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise VerificationError(f"{resource_path}: not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise VerificationError(f"{resource_path}: declaration is not a mapping")
    for field in ("resource_id", "kind", "genome_build", "location", "row_count", "checksums"):
        if document.get(field) in (None, ""):
            raise VerificationError(f"{resource_path}: required field {field!r} is missing")
    if document["kind"] != "variant_list":
        raise VerificationError(f"{resource_path}: expected kind 'variant_list', found {document['kind']!r}")
    if document["genome_build"] != "GRCh38":
        raise VerificationError(f"{resource_path}: expected genome_build 'GRCh38', found {document['genome_build']!r}")
    if not isinstance(document["row_count"], int) or document["row_count"] <= 0:
        raise VerificationError(f"{resource_path}: row_count must be a positive integer")
    return document


def verify_alid_file(
    path: Path,
    *,
    expected_sha256: str | None,
    expected_bytes: int | None,
    expected_rows: int | None,
) -> int:
    """Return the row count, or raise :class:`VerificationError`.

    Blank lines are skipped exactly as the variant-list reader skips them; the
    first malformed or repeated line refuses the whole list.
    """

    if not path.is_file():
        raise VerificationError(f"ALID list not found: {path}")
    raw = path.read_bytes()

    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise VerificationError(
            f"{path}: SHA-256 mismatch: declared {expected_sha256}, actual {actual_sha256}"
        )
    if expected_bytes is not None and len(raw) != expected_bytes:
        raise VerificationError(
            f"{path}: byte size mismatch: declared {expected_bytes}, actual {len(raw)}"
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"{path}: not valid UTF-8: {exc}") from exc

    seen: set[str] = set()
    rows = 0
    for number, line in enumerate(text.splitlines(), start=1):
        candidate = line.strip()
        if not candidate:
            continue
        canonical = canonical_alid_or_none(candidate)
        if canonical is None:
            raise VerificationError(f"{path}:{number}: {candidate!r} is not a canonical ALID")
        if canonical in seen:
            raise VerificationError(f"{path}:{number}: duplicate ALID {canonical!r}")
        seen.add(canonical)
        rows += 1

    if expected_rows is not None and rows != expected_rows:
        raise VerificationError(
            f"{path}: row count mismatch: declared {expected_rows}, actual {rows}"
        )
    return rows


def verify_source(
    path: Path,
    *,
    expected_md5: str | None,
    expected_sha256: str | None,
    expected_bytes: int | None,
    expected_rsid_rows: int | None,
) -> int:
    """Verify the LDSC source snapshot and return its rsID row count."""

    if not path.is_file():
        raise VerificationError(f"HapMap3 source not found: {path}")
    if expected_md5:
        actual_md5 = md5_file(path)
        if actual_md5 != expected_md5:
            raise VerificationError(f"{path}: MD5 mismatch: declared {expected_md5}, actual {actual_md5}")
    if expected_sha256 and sha256_file(path) != expected_sha256:
        raise VerificationError(f"{path}: SHA-256 mismatch")
    if expected_bytes is not None and path.stat().st_size != expected_bytes:
        raise VerificationError(
            f"{path}: byte size mismatch: declared {expected_bytes}, actual {path.stat().st_size}"
        )

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
        lines = [stripped for stripped in (line.strip() for line in handle) if stripped]
    if not lines:
        raise VerificationError(f"{path}: source is empty")
    if not lines[0].upper().startswith("SNP"):
        raise VerificationError(f"{path}: source header is not 'SNP A1 A2': {lines[0]!r}")
    rsid_rows = len(lines) - 1
    if expected_rsid_rows is not None and rsid_rows != expected_rsid_rows:
        raise VerificationError(
            f"{path}: rsID row count mismatch: declared {expected_rsid_rows}, actual {rsid_rows}"
        )
    return rsid_rows


def verify_reconciliation(document: dict, *, row_count: int) -> dict:
    derivation = document.get("derivation")
    if not isinstance(derivation, dict):
        raise VerificationError("resource declaration has no derivation block")
    requested = derivation.get("rsids_requested")
    resolved = derivation.get("resolved")
    absent = derivation.get("absent_from_store_axis")
    incompatible = derivation.get("allele_incompatible")
    if not all(isinstance(value, int) for value in (requested, resolved, absent, incompatible)):
        raise VerificationError("derivation counts must be integers")
    if absent < 0 or incompatible < 0 or requested <= 0:
        raise VerificationError("derivation counts must be non-negative")
    if requested - absent - incompatible != resolved:
        raise VerificationError(
            "derivation does not balance: "
            f"{requested} requested - {absent} absent - {incompatible} incompatible != {resolved} resolved"
        )
    if resolved != row_count:
        raise VerificationError(
            f"derivation resolved {resolved} but the list has {row_count} rows"
        )
    if derivation.get("duplicate_alids") != 0:
        raise VerificationError("derivation records duplicate ALIDs; the maintained list must have none")
    return derivation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--resource", type=Path, default=DEFAULT_RESOURCE,
                        help=f"resource.yaml to read (default: {DEFAULT_RESOURCE})")
    parser.add_argument("--alid-file", type=Path, default=None,
                        help="override the declared ALID list location")
    parser.add_argument("--source", type=Path, default=None,
                        help="LDSC source snapshot to verify (default: the declared snapshot when present)")
    parser.add_argument("--require-source", action="store_true",
                        help="fail when the declared source snapshot is absent")
    parser.add_argument("--quiet", action="store_true", help="print only failures")
    args = parser.parse_args(argv)

    try:
        document = read_expected(args.resource)
        checksums = document["checksums"]
        alid_path = args.alid_file or Path(document["location"])
        rows = verify_alid_file(
            alid_path,
            expected_sha256=checksums.get("alid_sha256"),
            expected_bytes=checksums.get("alid_bytes"),
            expected_rows=document["row_count"],
        )
        verify_reconciliation(document, row_count=rows)

        source = document.get("source") or {}
        source_path = args.source or (Path(source["snapshot"]) if source.get("snapshot") else None)
        source_rows = None
        if source_path is not None and source_path.is_file():
            source_rows = verify_source(
                source_path,
                expected_md5=checksums.get("source_md5"),
                expected_sha256=checksums.get("source_sha256"),
                expected_bytes=source.get("bytes"),
                expected_rsid_rows=source.get("rsid_rows"),
            )
        elif args.require_source:
            raise VerificationError(f"HapMap3 source snapshot not found: {source_path}")
        elif not args.quiet and source_path is not None:
            print(f"NOTE: source snapshot not present, skipped: {source_path}", file=sys.stderr)
    except VerificationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(
            f"OK: {document['resource_id']} ({document['genome_build']}) "
            f"{rows:,} canonical ALIDs, 0 duplicates, sha256={checksums.get('alid_sha256')}"
        )
        if source_rows is not None:
            print(f"    source verified: {source_rows:,} rsIDs, md5={checksums.get('source_md5')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
