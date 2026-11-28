"""Increment Bundles: Analyses join and leave a published Hybrid under its Store id (ADR 0045).

An increment is declared in its own directory inside the Release Bundle,
``stores/<id>/increments/<nnnn>/``. It holds ``increment.yaml`` (the
declaration, the owner's acceptance and, once published, the lineage), the
rows it adds in the bundle's columns (``analyses.tsv``), their per-Analysis
sidecars, and ``source-record.json``, the owner-attested facts the published
release did not record. It touches no other bundle file until the increment
route publishes it (#560) and folds it in.

This module owns:

- the contract (`check`), which `bundle.check` applies to every increment;
- the acceptance digest (`content_sha256`), which binds the owner's acceptance
  to the increment's bytes;
- the fold (`fold`), the pure function the route applies at publication: the
  increment's rows appended to ``analyses.tsv`` and each per-Analysis sidecar,
  and each declared drop's exclusion columns set;
- the increment's own status lifecycle (`validate_transition`).

It never opens a Store (ADR 0023).
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ogstores import bundle as bundle_module
from ogstores import manifest, resources
from ogstores.bundle import Bundle

INCREMENTS_DIRNAME: str = "increments"
INCREMENT_FILE: str = "increment.yaml"
SOURCE_RECORD_FILE: str = "source-record.json"
ANALYSES_FILE: str = "analyses.tsv"
SIDECARS_DIRNAME: str = "sidecars"

STATUSES: tuple[str, ...] = ("candidate", "accepted", "published", "withdrawn")
OPEN_STATUSES: frozenset[str] = frozenset({"candidate", "accepted"})
#: From `accepted` on, the owner's acceptance must be present and must match the bytes.
ACCEPTED_STATUSES: frozenset[str] = frozenset({"accepted", "published"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"candidate", "accepted", "withdrawn"}),
    "accepted": frozenset({"accepted", "published", "withdrawn"}),
    "published": frozenset({"published"}),
    "withdrawn": frozenset({"withdrawn"}),
}

#: `increment.yaml` keys the acceptance digest leaves out: they change after acceptance.
UNDIGESTED_KEYS: frozenset[str] = frozenset({"status", "acceptance", "accepted_warnings", "published"})
ACCEPTANCE_KEYS: tuple[str, ...] = ("accepted_by", "accepted_at", "reviewed_analysis_ids", "content_sha256")
PUBLISHED_KEYS: tuple[str, ...] = (
    "release_id", "source_release_id", "increment_release_id", "source_fingerprint", "published_at",
    "archive", "manifest_sha256", "exclusions_sha256",
)
#: The attested keys of `source-record.json`, as ADR 0109 §9 names them.
SOURCE_RECORD_KEYS: tuple[str, ...] = (
    "rsid_conflict_policy", "source_assembly", "variant_reference_sha256", "reference_panel_sha256",
    "ld_panel_digest", "indexed_subsets", "attested_by", "evidence",
)
#: Keys whose value may be an explicit null: the build took no reference panel.
NULLABLE_SOURCE_RECORD_KEYS: frozenset[str] = frozenset({"reference_panel_sha256"})

#: The per-Analysis sidecars the contract ties to an increment's rows (`bundle._check_quality_checks`).
ANCESTRY_SIDECAR: str = "sidecars/ancestry.tsv"
#: Columns of a sidecar row that name the Analysis rather than give evidence about it.
IDENTITY_COLUMNS: frozenset[str] = frozenset({"analysis_id", "source_analysis_id"})
#: Sidecars an increment keeps to itself: inventory-scoped, not per-Analysis evidence, so never folded.
INCREMENT_LOCAL_SIDECARS: frozenset[str] = frozenset({"source_readiness.tsv"})
#: The attested values of `source-record.json` whose `evidence` entry must say where each came from.
ATTESTED_VALUE_KEYS: tuple[str, ...] = tuple(k for k in SOURCE_RECORD_KEYS if k not in ("attested_by", "evidence"))

_DIRECTORY_RE = re.compile(r"\d{4}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class IncrementError(ValueError):
    """An increment that cannot be folded or read as declared."""


def directory_name(number: int) -> str:
    return f"{number:04d}"


def removal_code(number: int) -> str:
    """The exclusion code a drop records in the quality checks and exclusions sidecars."""
    return f"removed_in_increment_{directory_name(number)}"


def removed_reason(number: int, reason: str) -> str:
    """The `inclusion_reason` a dropped row takes in `analyses.tsv`."""
    return f"excluded: {removal_code(number)}: {reason}"


@dataclass(frozen=True)
class Increment:
    """One `increments/<nnnn>/` directory, read without judging it."""

    number: int
    root: Path
    document: dict[str, Any]

    @property
    def status(self) -> str | None:
        value = self.document.get("status")
        return value if isinstance(value, str) else None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def drops(self) -> list[dict[str, Any]]:
        drops = self.document.get("drops") or []
        return [d for d in drops if isinstance(d, Mapping)] if isinstance(drops, list) else []

    @property
    def drop_ids(self) -> list[str]:
        return [str(d.get("analysis_id")) for d in self.drops]

    @property
    def analyses_path(self) -> Path:
        return self.root / ANALYSES_FILE

    @property
    def adds_rows(self) -> bool:
        return self.analyses_path.exists()

    def rows(self) -> tuple[list[str], list[dict[str, str]]]:
        """The increment's rows; an increment that adds nothing has none."""
        if not self.adds_rows:
            return [], []
        header, body = _read_table(self.analyses_path)
        return header, [dict(zip(header, r)) for r in body]

    def as_bundle(self, parent: Bundle) -> Bundle:
        """The increment as a bundle of its own rows, so the bundle's row checks apply to it."""
        return Bundle(
            store_id=parent.store_id,
            root=self.root,
            release=parent.release,
            build=parent.build,
            analyses_path=self.analyses_path,
        )


def eligibility_errors(b: Bundle, *, opening: bool = True) -> list[str]:
    """Why `b` may not take an increment (ADR 0045 §1); empty when it may.

    The one predicate the contract and the emit share. Scope always applies;
    a validated Release Status applies to a bundle opening an increment.
    """
    errors: list[str] = []
    if not (b.layout == "hybrid" and b.completes_own_store):
        errors.append(
            "only a Reference-Completed Hybrid that completes its own Store takes an increment (ADR 0045 §1); "
            f"this bundle is {b.layout!r} {b.completion_state!r}"
            + (f" derived from {b.derived_from}" if b.derived_from else "")
        )
    if opening and b.status != "validated":
        errors.append(f"an increment applies to a validated bundle; its Release Status is {b.status!r}")
    return errors


def increments_root(b: Bundle) -> Path:
    return b.root / INCREMENTS_DIRNAME


def scan(b: Bundle) -> tuple[list[Increment], list[str]]:
    """Every increment of `b`, in number order, and the entries that are not one."""
    root = increments_root(b)
    if not root.exists():
        return [], []
    if root.is_symlink() or not root.is_dir():
        return [], [f"{INCREMENTS_DIRNAME}/ must be a directory, not a symbolic link or a file"]
    found: list[Increment] = []
    problems: list[str] = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or not _DIRECTORY_RE.fullmatch(entry.name):
            problems.append(f"{INCREMENTS_DIRNAME}/{entry.name} is not a four-digit increment directory")
            continue
        document = bundle_module._read_yaml(entry / INCREMENT_FILE)
        if document is None:
            problems.append(f"{INCREMENTS_DIRNAME}/{entry.name} has no {INCREMENT_FILE}")
            continue
        errors = [v for k, v in document.items() if k.startswith("__") and k.endswith("__")]
        if errors:
            problems.append(f"{INCREMENTS_DIRNAME}/{entry.name}/{INCREMENT_FILE} cannot be read: {errors[0]}")
            continue
        found.append(Increment(int(entry.name), entry, document))
    return found, problems


def load(b: Bundle) -> list[Increment]:
    """The increments of `b`, in number order; raises `IncrementError` naming any entry that is not one.

    A reader never drops what it cannot read: an unreadable declaration is not
    an absent one. `scan` returns the problems for the contract to report.
    """
    found, problems = scan(b)
    if problems:
        raise IncrementError(f"{b.store_id}: " + "; ".join(problems))
    return found


def get(b: Bundle, number: int) -> Increment:
    for inc in load(b):
        if inc.number == number:
            return inc
    raise IncrementError(f"{b.store_id} has no readable increment {directory_name(number)}")


def transition_errors(previous: Bundle, current: Bundle) -> list[str]:
    """How `current`'s increments moved from `previous`'s, against the lifecycle (ADR 0045 §2, §4)."""
    before, _ = scan(previous)
    after = {inc.number: inc for inc in scan(current)[0]}
    errors: list[str] = []
    for inc in before:
        name = directory_name(inc.number)
        now = after.get(inc.number)
        if now is None:
            errors.append(f"increment {name} was removed; an increment is never deleted, it is withdrawn")
            continue
        if inc.status in TRANSITIONS and now.status in TRANSITIONS:
            errors += [f"increment {name}: {e}" for e in validate_transition(inc.status, now.status)]
    return errors


def open_increment(b: Bundle) -> Increment | None:
    open_ = [inc for inc in load(b) if inc.is_open]
    return open_[0] if len(open_) == 1 else None


def validate_transition(before: str, after: str) -> list[str]:
    """Whether an increment may move from `before` to `after` (ADR 0045 §2)."""
    if before not in TRANSITIONS or after not in TRANSITIONS:
        return [f"increment status must be one of {', '.join(STATUSES)}; got {before!r} -> {after!r}"]
    if after not in TRANSITIONS[before]:
        return [f"increment status may not move from {before!r} to {after!r}; allowed: {sorted(TRANSITIONS[before])}"]
    return []


def declaration_sha256(document: Mapping[str, Any]) -> str:
    """The digest of `increment.yaml` without what changes after acceptance, in canonical JSON."""
    declared = {k: v for k, v in document.items() if k not in UNDIGESTED_KEYS}
    text = json.dumps(declared, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_sha256(directory: Path | str) -> str:
    """The digest the owner's acceptance names: every file of the increment, and its declaration.

    A sorted listing of ``<sha256>  <relative path>`` for every file except
    ``increment.yaml``, plus ``<sha256>  increment.yaml#declaration`` for the
    declaration's canonical digest. A symbolic link refuses.
    """
    root = Path(directory)
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise IncrementError(f"{root}: {relative} is a symbolic link; an increment holds only regular files")
        if not path.is_file() or relative == INCREMENT_FILE:
            continue
        lines.append(f"{resources.sha256_file(path)}  {relative}")
    document = bundle_module._read_yaml(root / INCREMENT_FILE) or {}
    lines.append(f"{declaration_sha256(document)}  {INCREMENT_FILE}#declaration")
    lines.sort(key=lambda line: line.split("  ", 1)[1])
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Reading tables byte-faithfully


def _read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    """`bundle.read_sidecar_rows`, its refusal naming the file."""
    try:
        return bundle_module.read_sidecar_rows(path)
    except (OSError, UnicodeDecodeError, ValueError, csv.Error) as exc:
        raise IncrementError(f"{path} {exc}") from exc


def _lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        raise IncrementError(f"{path} does not end in a newline")
    return text.splitlines()


def _excluded(row: Mapping[str, str], index: int) -> bool:
    """`manifest.classify_exclusion` of a row: a malformed flag raises, never reads as admitted."""
    return manifest.classify_exclusion(row.get("exclude_from_build"), index, row.get("analysis_id") or "")


# ---------------------------------------------------------------------------
# The contract


def _check_declaration(b: Bundle, inc: Increment) -> list[str]:
    where = f"increment {directory_name(inc.number)}"
    doc = inc.document
    errors: list[str] = []
    if doc.get("increment") != inc.number:
        errors.append(f"{where}: `increment` is {doc.get('increment')!r}, not its directory's number {inc.number}")
    if doc.get("store_id") != b.store_id:
        errors.append(f"{where}: `store_id` {doc.get('store_id')!r} is not {b.store_id!r}")
    status = doc.get("status")
    if status not in STATUSES:
        errors.append(f"{where}: status {status!r} is not one of {', '.join(STATUSES)}")
    for key in ("reason", "created_at"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            errors.append(f"{where}: `{key}` must be a non-empty string")
    drops = doc.get("drops")
    if drops is None:
        drops = []
    if not isinstance(drops, list):
        errors.append(f"{where}: `drops` must be a list")
        drops = []
    defect = False
    seen: set[str] = set()
    for item in drops:
        if not isinstance(item, Mapping):
            errors.append(f"{where}: each drop must be a mapping with analysis_id, reason and defect")
            continue
        analysis_id, reason = item.get("analysis_id"), item.get("reason")
        if not isinstance(analysis_id, str) or not analysis_id:
            errors.append(f"{where}: a drop has no analysis_id")
        elif analysis_id in seen:
            errors.append(f"{where}: drops {analysis_id!r} twice")
        else:
            seen.add(analysis_id)
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{where}: the drop of {analysis_id!r} has no reason")
        if not isinstance(item.get("defect"), bool):
            errors.append(f"{where}: the drop of {analysis_id!r} must say `defect: true` or `false`")
        defect = defect or item.get("defect") is True
    erratum = doc.get("erratum")
    if defect and not (
        isinstance(erratum, Mapping)
        and all(isinstance(erratum.get(k), str) and erratum[k].strip() for k in ("issue", "text"))
    ):
        errors.append(f"{where}: a defect removal is a Release Erratum; `erratum` needs `issue` and `text` (ADR 0005)")
    if not inc.adds_rows and not drops:
        errors.append(f"{where}: declares no change: no analyses.tsv and no drops")
    if inc.adds_rows:
        try:
            if not _read_table(inc.analyses_path)[1]:
                errors.append(
                    f"{where}: analyses.tsv has no rows; an increment that adds nothing carries no analyses.tsv"
                )
        except IncrementError as exc:
            errors.append(f"{where}: {exc}")
    if inc.adds_rows:
        generator = doc.get("generator")
        if not isinstance(generator, Mapping) or not isinstance(generator.get("commands"), list) or not generator["commands"]:
            errors.append(f"{where}: an increment that adds rows records its `generator.commands`")
        if not isinstance(doc.get("source_snapshot"), Mapping):
            errors.append(f"{where}: an increment that adds rows records its `source_snapshot`")
    require = doc.get("require_added") or []
    if not isinstance(require, list) or not all(isinstance(x, str) for x in require):
        errors.append(f"{where}: `require_added` must be a list of analysis ids")
    warnings = doc.get("accepted_warnings") or []
    if not isinstance(warnings, list) or not all(
        isinstance(w, Mapping) and all(isinstance(w.get(k), str) and w[k] for k in ("message", "accepted_by", "accepted_at"))
        for w in warnings
    ):
        errors.append(f"{where}: each accepted warning needs `message`, `accepted_by` and `accepted_at`")
    published = doc.get("published")
    if status == "published":
        if not isinstance(published, Mapping) or any(k not in published for k in PUBLISHED_KEYS):
            errors.append(f"{where}: a published increment needs a `published` block with {', '.join(PUBLISHED_KEYS)}")
        else:
            for key in ("manifest_sha256", "exclusions_sha256"):
                if not isinstance(published[key], str) or not _SHA256_RE.fullmatch(published[key]):
                    errors.append(f"{where}: `published.{key}` is not a sha256")
    elif published not in (None, {}):
        errors.append(f"{where}: only a published increment carries a `published` block")
    return errors


def _check_acceptance(inc: Increment, row_ids: list[str]) -> list[str]:
    where = f"increment {directory_name(inc.number)}"
    if inc.status not in ACCEPTED_STATUSES:
        return []
    acceptance = inc.document.get("acceptance")
    if not isinstance(acceptance, Mapping) or any(
        not acceptance.get(k) for k in ACCEPTANCE_KEYS
    ):
        return [f"{where}: an {inc.status} increment needs the owner's `acceptance`: {', '.join(ACCEPTANCE_KEYS)}"]
    errors: list[str] = []
    reviewed = acceptance.get("reviewed_analysis_ids")
    expected = sorted({*row_ids, *inc.drop_ids})
    if not isinstance(reviewed, list) or sorted(map(str, reviewed)) != expected:
        errors.append(
            f"{where}: `acceptance.reviewed_analysis_ids` must be exactly the increment's rows and drops "
            f"({len(expected)} ids); it lists {len(reviewed) if isinstance(reviewed, list) else reviewed!r}"
        )
    try:
        actual = content_sha256(inc.root)
    except IncrementError as exc:
        return [*errors, f"{where}: {exc}"]
    if acceptance.get("content_sha256") != actual:
        errors.append(
            f"{where}: `acceptance.content_sha256` {acceptance.get('content_sha256')!r} is not the increment's "
            f"{actual}: its files or declaration changed after the owner accepted it"
        )
    return errors


def _check_source_record(inc: Increment) -> list[str]:
    where = f"increment {directory_name(inc.number)}"
    if inc.status not in ACCEPTED_STATUSES:
        return []
    path = inc.root / SOURCE_RECORD_FILE
    if path.is_symlink() or not path.is_file():
        return [f"{where}: an {inc.status} increment carries the owner-attested {SOURCE_RECORD_FILE} (ADR 0045 §6)"]
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{where}: {SOURCE_RECORD_FILE} cannot be read: {exc}"]
    if not isinstance(record, Mapping):
        return [f"{where}: {SOURCE_RECORD_FILE} must be a JSON object"]
    errors = [
        f"{where}: {SOURCE_RECORD_FILE} is missing {key!r}" for key in SOURCE_RECORD_KEYS if key not in record
    ]
    for key in SOURCE_RECORD_KEYS:
        if key in record and record[key] is None and key not in NULLABLE_SOURCE_RECORD_KEYS:
            errors.append(f"{where}: {SOURCE_RECORD_FILE} {key!r} is null; only reference_panel_sha256 may be")
    if not isinstance(record.get("attested_by"), str) or not record["attested_by"].strip():
        errors.append(f"{where}: {SOURCE_RECORD_FILE} names no `attested_by`: an unattested fact is not a pin")
    for key in ("rsid_conflict_policy", "source_assembly", "ld_panel_digest"):
        if key in record and (not isinstance(record[key], str) or not record[key].strip()):
            errors.append(f"{where}: {SOURCE_RECORD_FILE} {key!r} must be a non-empty string, not {record[key]!r}")
    value = record.get("variant_reference_sha256")
    if "variant_reference_sha256" in record and not (isinstance(value, str) and _SHA256_RE.fullmatch(value)):
        errors.append(f"{where}: {SOURCE_RECORD_FILE} 'variant_reference_sha256' {value!r} is not a sha256")
    value = record.get("reference_panel_sha256")
    if value is not None and not (isinstance(value, str) and _SHA256_RE.fullmatch(value)):
        errors.append(
            f"{where}: {SOURCE_RECORD_FILE} 'reference_panel_sha256' {value!r} is neither null nor a sha256"
        )
    subsets = record.get("indexed_subsets")
    if "indexed_subsets" in record and not (
        isinstance(subsets, Mapping)
        and all(
            isinstance(v, Mapping)
            and all(isinstance(v.get(k), str) and v[k].strip() for k in ("variant_list_path", "overflow_only"))
            for v in subsets.values()
        )
    ):
        errors.append(
            f"{where}: {SOURCE_RECORD_FILE} 'indexed_subsets' must map each name to its variant_list_path "
            "and overflow_only"
        )
    evidence = record.get("evidence")
    if "evidence" in record:
        if not isinstance(evidence, Mapping) or not evidence:
            errors.append(f"{where}: {SOURCE_RECORD_FILE} 'evidence' must say where each attested value came from")
        else:
            for key in ATTESTED_VALUE_KEYS:
                said = evidence.get(key)
                if key == "indexed_subsets" and isinstance(said, Mapping) and isinstance(subsets, Mapping):
                    missing = [n for n in subsets if not (isinstance(said.get(n), str) and said[n].strip())]
                    if missing:
                        errors.append(f"{where}: {SOURCE_RECORD_FILE} 'evidence' gives none for subsets {missing}")
                elif not (isinstance(said, str) and said.strip()):
                    errors.append(f"{where}: {SOURCE_RECORD_FILE} 'evidence' gives none for {key!r}")
    return errors


def _check_sidecars(b: Bundle, inc: Increment, ids: list[str]) -> list[str]:
    where = f"increment {directory_name(inc.number)}"
    errors: list[str] = []
    sidecars = inc.root / SIDECARS_DIRNAME
    if sidecars.exists():
        for path in sorted(sidecars.iterdir()):
            relative = f"{SIDECARS_DIRNAME}/{path.name}"
            if path.suffix != ".tsv" or path.name in INCREMENT_LOCAL_SIDECARS:
                continue
            bundle_path = b.root / relative
            if not bundle_path.is_file():
                errors.append(f"{where}: {relative} has no counterpart in the bundle, so it cannot fold")
                continue
            try:
                header, _ = _read_table(path)
                bundle_header, _ = _read_table(bundle_path)
            except IncrementError as exc:
                errors.append(f"{where}: {exc}")
                continue
            if header != bundle_header:
                errors.append(f"{where}: {relative}'s header is not the bundle's, so it cannot fold")
    if (b.root / ANCESTRY_SIDECAR).is_file() and ids:
        # Ruling 6: each added or refused Analysis carries its ancestry evidence.
        path = inc.root / ANCESTRY_SIDECAR
        present: set[str] = set()
        if path.is_file():
            try:
                header, body = _read_table(path)
            except IncrementError as exc:
                return [*errors, f"{where}: {exc}"]
            if "analysis_id" in header:
                present = {r[header.index("analysis_id")] for r in body}
                for r in body:
                    evidence = [v for c, v in zip(header, r) if c not in IDENTITY_COLUMNS]
                    if not any(v.strip() for v in evidence):
                        errors.append(
                            f"{where}: {r[header.index('analysis_id')]!r} has blank ancestry evidence in "
                            f"{ANCESTRY_SIDECAR}"
                        )
        errors += [f"{where}: no ancestry evidence for {a!r} in {ANCESTRY_SIDECAR}" for a in ids if a not in present]
    return errors


def _check_tied_sidecars(b: Bundle, inc: Increment, ids: list[str], refused: list[str]) -> list[str]:
    """Rows the contract ties to `analyses.tsv` must be in the increment when the bundle carries them.

    `bundle._check_quality_checks` judges a quality-checks table that is
    present, and treats an absent one as valid; an increment whose bundle
    carries one must carry its own, or its added rows would fold without them.
    """
    where = f"increment {directory_name(inc.number)}"
    errors: list[str] = []
    if ids and (b.root / bundle_module.QUALITY_CHECKS_SIDECAR).is_file() and not (
        inc.root / bundle_module.QUALITY_CHECKS_SIDECAR
    ).is_file():
        errors.append(f"{where}: the bundle carries {bundle_module.QUALITY_CHECKS_SIDECAR}, so its rows need one too")
    if refused and (b.root / bundle_module.EXCLUSIONS_SIDECAR).is_file() and not (
        inc.root / bundle_module.EXCLUSIONS_SIDECAR
    ).is_file():
        errors.append(
            f"{where}: the bundle carries {bundle_module.EXCLUSIONS_SIDECAR}, so its refused rows "
            f"{refused[:5]} need one too"
        )
    return errors


def _check_rows(b: Bundle, inc: Increment, bundle_header: list[str], registered: set[str],
                admitted_in_bundle: set[str]) -> tuple[list[str], list[str], set[str]]:
    """Errors, the increment's row ids, and those it admits."""
    where = f"increment {directory_name(inc.number)}"
    if not inc.adds_rows:
        return [], [], set()
    try:
        header, rows = inc.rows()
    except IncrementError as exc:
        return [f"{where}: {exc}"], [], set()
    errors: list[str] = []
    if header != bundle_header:
        errors.append(f"{where}: analyses.tsv columns are not the bundle's columns in the bundle's order")
    ids = [r.get("analysis_id", "") for r in rows]
    admitted = {r.get("analysis_id", "") for i, r in enumerate(rows) if not _excluded(r, i)}
    duplicates = sorted({a for a in ids if ids.count(a) > 1})
    errors += [f"{where}: analysis_id {a!r} appears twice" for a in duplicates]
    if inc.status != "published":
        errors += [
            f"{where}: analysis_id {a!r} is already in the bundle; a replace or a re-admission is not "
            "supported yet (ADR 0045 §2)"
            for a in ids if a in registered
        ]
    for i, r in enumerate(rows):
        if _excluded(r, i) and not (r.get("inclusion_reason") or "").strip():
            errors.append(f"{where}: refused row {r.get('analysis_id')!r} has no inclusion_reason")
    sub = inc.as_bundle(b)
    row_errors, _ = bundle_module._check_analyses(sub, bundle_module._analyses_reader(sub))
    errors += [f"{where}: {e}" for e in row_errors]
    errors += [f"{where}: {e}" for e in bundle_module._check_quality_checks(sub, bundle_module._analyses_reader(sub))]
    for required in inc.document.get("require_added") or []:
        if required not in admitted:
            errors.append(f"{where}: require_added {required!r} is not an admitted row")
    return errors, ids, admitted


def _check_drops(inc: Increment, ids: list[str], admitted_in_bundle: set[str]) -> list[str]:
    """An open increment drops only Analyses the bundle admits, and none it adds itself."""
    where = f"increment {directory_name(inc.number)}"
    if inc.status == "published":
        return []
    errors = [f"{where}: drops {a!r}, which it also adds" for a in inc.drop_ids if a in ids]
    errors += [
        f"{where}: drop {a!r} is not an admitted Analysis of the bundle"
        for a in inc.drop_ids if a not in admitted_in_bundle and a not in ids
    ]
    return errors


def _check_folded(b: Bundle, inc: Increment, bundle_rows: dict[str, list[dict[str, str]]],
                  bundle_order: list[str], later_drops: dict[str, int]) -> tuple[list[str], list[int]]:
    """A published increment's rows are in the bundle, as declared, and its drops are excluded.

    Returns the errors and the positions its rows hold in `analyses.tsv`, which
    the caller holds to increment order across increments.
    """
    where = f"increment {directory_name(inc.number)}"
    errors: list[str] = []
    try:
        _, rows = inc.rows()
    except IncrementError as exc:
        return [f"{where}: {exc}"], []
    positions: list[int] = []
    for r in rows:
        analysis_id = r.get("analysis_id", "")
        found = bundle_rows.get(analysis_id) or []
        expected = {k: v for k, v in r.items() if k != "analysis_index"}
        if analysis_id in later_drops:
            expected["exclude_from_build"] = "true"
            expected.pop("inclusion_reason", None)
        matching = [
            f for f in found
            if {k: v for k, v in f.items() if k != "analysis_index" and not (
                analysis_id in later_drops and k == "inclusion_reason")} == expected
        ]
        if len(matching) != 1:
            errors.append(f"{where} row {analysis_id!r} is not folded into analyses.tsv as declared")
            continue
        positions.append(bundle_order.index(analysis_id))
    if positions != sorted(positions):
        errors.append(f"{where}: its rows are not in analyses.tsv in its order")
    for drop in inc.drops:
        analysis_id = drop.get("analysis_id")
        found = bundle_rows.get(str(analysis_id)) or []
        reason = removed_reason(inc.number, str(drop.get("reason")))
        if not any(_excluded(f, 0) and f.get("inclusion_reason") == reason for f in found):
            errors.append(f"{where}: drop {analysis_id!r} is not excluded in analyses.tsv with {reason!r}")
    return errors, positions


def check(b: Bundle, registry_root: Path | str | None = None) -> list[str]:
    """Every error in `b`'s increments; empty when it has none or all are valid (ADR 0045 §4)."""
    increments, errors = scan(b)
    if not increments and not errors:
        return []
    errors += eligibility_errors(b, opening=any(inc.is_open for inc in increments))
    numbers = [inc.number for inc in increments]
    if numbers != list(range(1, len(numbers) + 1)):
        errors.append(f"increments are numbered from 1 with no gaps; found {[directory_name(n) for n in numbers]}")
    open_ = [inc for inc in increments if inc.is_open]
    if len(open_) > 1:
        errors.append(f"a bundle has at most one open increment; {[directory_name(i.number) for i in open_]} are open")
    for earlier, later in zip(increments, increments[1:]):
        if earlier.is_open and later.status == "published":
            errors.append(
                f"increment {directory_name(later.number)} is published while "
                f"{directory_name(earlier.number)} is still open"
            )

    try:
        header, body = _read_table(b.analyses_path)
    except (IncrementError, OSError) as exc:
        return [*errors, f"increments cannot be checked against analyses.tsv: {exc}"]
    rows = [dict(zip(header, r)) for r in body]
    bundle_rows: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        bundle_rows.setdefault(r.get("analysis_id", ""), []).append(r)
    order = [r.get("analysis_id", "") for r in rows]
    admitted = {r.get("analysis_id", "") for i, r in enumerate(rows) if not _excluded(r, i)}

    registered = set(order)
    drop_owner: dict[str, int] = {}
    for inc in increments:
        if inc.status == "published":
            for analysis_id in inc.drop_ids:
                drop_owner[analysis_id] = inc.number

    last_position = -1
    for inc in increments:
        errors += _check_declaration(b, inc)
        # An open increment's ids must be new to the bundle and to every earlier increment.
        others = registered | {
            a for other in increments if other.number < inc.number and other.status != "withdrawn"
            for a in _safe_ids(other)
        }
        row_errors, ids, added = _check_rows(b, inc, header, others, admitted)
        errors += row_errors
        errors += _check_drops(inc, ids, admitted)
        errors += _check_sidecars(b, inc, ids)
        errors += _check_tied_sidecars(b, inc, ids, [a for a in ids if a not in added])
        errors += _check_acceptance(inc, ids)
        errors += _check_source_record(inc)
        if inc.status == "published":
            later = {a: n for a, n in drop_owner.items() if n > inc.number}
            fold_errors, positions = _check_folded(b, inc, bundle_rows, order, later)
            errors += fold_errors
            if positions and positions[0] <= last_position:
                errors.append(
                    f"increment {directory_name(inc.number)}: its rows are not in analyses.tsv after the rows "
                    "of every earlier increment"
                )
            last_position = max([last_position, *positions])
    return errors


def _safe_ids(inc: Increment) -> list[str]:
    try:
        return [r.get("analysis_id", "") for r in inc.rows()[1]]
    except IncrementError:
        return []


# ---------------------------------------------------------------------------
# The fold


def _replace_fields(line: str, header: Sequence[str], values: Mapping[str, str]) -> str:
    fields = line.split("\t")
    for column, value in values.items():
        fields[header.index(column)] = value
    return "\t".join(fields)


def fold(b: Bundle, number: int) -> dict[str, str]:
    """The bundle files increment `number` folds into, by path relative to the bundle (ADR 0045 §2).

    Pure: nothing is written. ``analyses.tsv`` gains the increment's rows,
    with ``analysis_index`` continuing the bundle's, and each declared drop's
    ``exclude_from_build`` and ``inclusion_reason`` are set. Each sidecar the
    increment carries gains its rows; ``quality_checks.tsv`` sets each drop's
    exclusion columns and ``exclusions.tsv`` gains each drop's row. Every other
    byte is kept. Raises `IncrementError` for what cannot fold.
    """
    inc = get(b, number)
    texts: dict[str, str] = {}

    lines = _lines(b.analyses_path)
    header = lines[0].split("\t")
    for column in ("analysis_id", "exclude_from_build", "inclusion_reason"):
        if column not in header:
            raise IncrementError(f"{b.analyses_path} has no {column!r} column, so it cannot fold")
    id_at = header.index("analysis_id")
    rows_by_id = {line.split("\t")[id_at]: i for i, line in enumerate(lines[1:], 1)}
    admitted = {
        a for a, i in rows_by_id.items()
        if not _excluded(dict(zip(header, lines[i].split("\t"))), i - 1)
    }
    for drop in inc.drops:
        analysis_id = str(drop.get("analysis_id"))
        if analysis_id not in admitted:
            raise IncrementError(f"increment {directory_name(number)} drops {analysis_id!r}, not an admitted Analysis")
        at = rows_by_id[analysis_id]
        lines[at] = _replace_fields(lines[at], header, {
            "exclude_from_build": "true",
            "inclusion_reason": removed_reason(number, str(drop.get("reason"))),
        })
    if inc.adds_rows:
        added = _lines(inc.analyses_path)
        if added[0].split("\t") != header:
            raise IncrementError(f"increment {directory_name(number)}'s columns are not the bundle's")
        next_index = len(lines) - 1
        for offset, line in enumerate(added[1:]):
            if "analysis_index" in header:
                line = _replace_fields(line, header, {"analysis_index": str(next_index + offset)})
            lines.append(line)
    texts[ANALYSES_FILE] = "\n".join(lines) + "\n"

    drop_rows = {str(d.get("analysis_id")): d for d in inc.drops}
    sidecar_names = {
        p.name for p in (inc.root / SIDECARS_DIRNAME).glob("*.tsv") if p.name not in INCREMENT_LOCAL_SIDECARS
    } if (inc.root / SIDECARS_DIRNAME).is_dir() else set()
    for relative in (bundle_module.QUALITY_CHECKS_SIDECAR, bundle_module.EXCLUSIONS_SIDECAR):
        if drop_rows and (b.root / relative).is_file():
            sidecar_names.add(Path(relative).name)
    analyses_header = header
    analyses_lines = _lines(b.analyses_path)
    for name in sorted(sidecar_names):
        relative = f"{SIDECARS_DIRNAME}/{name}"
        path = b.root / relative
        if not path.is_file():
            raise IncrementError(f"increment {directory_name(number)}'s {relative} has no counterpart in the bundle")
        side = _lines(path)
        side_header = side[0].split("\t")
        if relative == bundle_module.QUALITY_CHECKS_SIDECAR and drop_rows:
            id_col = side_header.index("analysis_id")
            for i, line in enumerate(side[1:], 1):
                analysis_id = line.split("\t")[id_col]
                if analysis_id in drop_rows:
                    side[i] = _replace_fields(line, side_header, {
                        "exclude_from_build": "true", "exclusion_reason": removal_code(number),
                    })
        if (inc.root / relative).is_file():
            extra = _lines(inc.root / relative)
            if extra[0].split("\t") != side_header:
                raise IncrementError(f"increment {directory_name(number)}'s {relative} header is not the bundle's")
            side.extend(extra[1:])
        if relative == bundle_module.EXCLUSIONS_SIDECAR:
            for analysis_id, drop in drop_rows.items():
                source = dict(zip(analyses_header, analyses_lines[rows_by_id[analysis_id]].split("\t")))
                values = {
                    "analysis_id": analysis_id,
                    "source_analysis_id": source.get("source_analysis_id", analysis_id),
                    "category": "increment_drop",
                    "reason": removal_code(number),
                    "detail": str(drop.get("reason")),
                    "exclude_from_build": "true",
                }
                side.append("\t".join(values.get(column, "") for column in side_header))
        texts[relative] = "\n".join(side) + "\n"
    return texts


__all__ = [
    "ACCEPTANCE_KEYS",
    "INCREMENTS_DIRNAME",
    "INCREMENT_FILE",
    "Increment",
    "IncrementError",
    "PUBLISHED_KEYS",
    "SOURCE_RECORD_FILE",
    "SOURCE_RECORD_KEYS",
    "STATUSES",
    "check",
    "content_sha256",
    "declaration_sha256",
    "directory_name",
    "eligibility_errors",
    "fold",
    "get",
    "load",
    "open_increment",
    "removal_code",
    "removed_reason",
    "scan",
    "transition_errors",
    "validate_transition",
]
