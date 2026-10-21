#!/usr/bin/env python3
"""Reusable dbGaP plumbing: tar tables of contents and ``*.dbGaP.metadata.txt`` (#290).

dbGaP is a host whose datasets each look different (ADR 0036), so each is its
own Source Collection with its own adapter. What they share is the deposit's
shape: MD5-verified tars with a ``.table_of_contents.txt`` listing, members
named ``MVP_R4.1000G_AGR.<TRAIT>.<ANCESTRY>.GIA.dbGaP.txt.gz`` beside a
``*.dbGaP.metadata.txt`` Info/Description table, and a sample size written
``Total Sample Size=N[; Cases=a; Controls=b]``. This module holds that plumbing
so the MVP acquisition and adapter read it one way and a later dbGaP dataset
does not copy it again.

Nothing here knows a PMID, a candidate table or a Store: those belong to the
Source Collection's adapter (``providers/dbgap/mvp/``). ``parse_metadata``
refuses anything that is not an Info/Description table rather than guessing,
and ``parse_tar_listing`` refuses an unsafe member path, so a malformed deposit
fails loudly before it can write outside the extraction root.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path


class DbgapError(RuntimeError):
    """A dbGaP deposit file cannot be read as the shared plumbing expects."""


#: The rows of a ``*.dbGaP.metadata.txt`` Info/Description table this repository reads.
METADATA_TITLE = "Title of analysis"
METADATA_DESCRIPTION = "Analysis description"
METADATA_ANALYZED_VARIABLE = "Analyzed variable"
METADATA_SAMPLE_SIZE = "Sample size"
METADATA_TRAIT_TYPE = "Phenotypic trait type"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - dbGaP's own checksum
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_tar_listing(path: Path) -> dict[str, tuple[str, int]]:
    """``member path -> (tar name, size)`` from one ``tar -tv`` table of contents.

    A TOC line is ``<mode> <owner> <size> <date> <time> <member>``; directories
    are listed too and are skipped. The TOC is named
    ``<tar>.table_of_contents.txt`` (keeping the tar's own ``phs002453.``
    prefix), so the tar name is the file name with that suffix removed.
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
        if member.startswith("/") or ".." in Path(member).parts:
            raise DbgapError(f"{path.name}: unsafe member path {member!r}")
        members[member] = (tar_name, int(parts[2]))
    return members


def load_toc(out: Path) -> dict[str, tuple[str, int]]:
    """Every member listed by ``GIA/meta/*.table_of_contents.txt``, refusing a duplicate."""
    meta_dir = out / "GIA" / "meta"
    members: dict[str, tuple[str, int]] = {}
    for toc in sorted(meta_dir.glob("*.table_of_contents.txt")):
        for member, entry in parse_tar_listing(toc).items():
            if member in members:
                raise DbgapError(f"{member} is listed in more than one table of contents")
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
            raise DbgapError(f"{path}: not an Info/Description metadata table")
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
        raise DbgapError(f"{path}: cannot parse sample size from {text!r}")
    cases = re.search(r"Cases\s*=\s*(\d+)", text)
    controls = re.search(r"Controls\s*=\s*(\d+)", text)
    return (
        int(total.group(1)),
        int(cases.group(1)) if cases else 0,
        int(controls.group(1)) if controls else 0,
    )


def metadata_path_for(extracted: Path, member: str) -> Path | None:
    """The metadata file describing ``member``, allowing for a cross-tar deposit.

    dbGaP filed one Analysis's data file in a different tar from its metadata
    file, so the sibling path is tried first and then the whole extracted tree
    by file name. ``None`` when neither is unique.
    """
    sibling = extracted / member.replace(".txt.gz", ".metadata.txt")
    if sibling.is_file():
        return sibling
    found = sorted(extracted.glob("*/" + sibling.name))
    return found[0] if len(found) == 1 else None


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
        raise DbgapError(f"{member.name}: not a regular file in the tar")
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
        raise DbgapError(f"{member.name}: wrote {written} bytes, table of contents says {size}")
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


__all__ = [
    "DbgapError",
    "METADATA_ANALYZED_VARIABLE",
    "METADATA_DESCRIPTION",
    "METADATA_SAMPLE_SIZE",
    "METADATA_TITLE",
    "METADATA_TRAIT_TYPE",
    "TarResult",
    "extract_member",
    "extract_tar",
    "load_toc",
    "md5_file",
    "metadata_path_for",
    "parse_metadata",
    "parse_sample_size",
    "parse_tar_listing",
    "sha256_file",
    "tar_md5",
]
