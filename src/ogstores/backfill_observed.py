"""Write #502's Store facts into the Validation Records registered before them.

    pixi run -e dev backfill-observed [--dry-run] [--registry-root stores] [--artifact-root /data/opengwasdb/stores]

`register` records `observed.store_bytes`, `has_hm3`, `has_rho` and
`n_top_hits` at publication (#502). A record written before that carries
`store_bytes: null` and none of the other three. This tool fills the four keys
(`register.STORE_FACT_FIELDS`) of every `built` or `validated` bundle's
`validation.yaml`, the same way `register` does:

- `store_bytes` from a stat of each file of the published Store,
  `<artifact-root>/<id>/store.opengwasdb`, and `null` when it is not on disk;
- `has_hm3` and `has_rho` from the publishing phase's plan and its step
  records under `<artifact-root>/<id>/records/`, and `null` when that phase has
  no register record, so no record says which steps built the Store;
- `n_top_hits` as `null`, until opengwas/opengwasdb#488 reports the counts.

It writes those keys and nothing else. Each record is edited as text, in
place: a key already in `observed` has its value replaced, a missing one is
inserted after the Store fact before it, and every other line keeps its bytes.
The edited document is parsed and compared with the original before anything
is written: every key but the four must be equal, or the bundle is refused.
A record with no `observed` block is skipped, since adding one would change
a key that is not a Store fact. A second run changes nothing.

It opens no Store file and writes nothing under the Artifact Root. `--dry-run`
prints what it would change and writes nothing.

Run it as `python3 -m ogstores.backfill_observed`, which the
`backfill-observed` task does. It runs once, after opengwas/opengwasdb-stores#498 rewrites every Store to
format 0.3.0, so it records the Stores as they are served. Recording
observations into an accepted bundle's Validation Record changes no
membership, so it is allowed under the Release Bundle contract
(`docs/release-metadata-schema.md`, "validation.yaml").

Exit status: 0 when every bundle was written, unchanged or skipped; 1 when a
bundle was refused.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ogstores import paths, run
from ogstores.bundle import Bundle
from ogstores.plan import BUILD_PHASE, COMPLETION_PHASE, REGISTER_STEPS, plan
from ogstores.readiness import discover_bundles
from ogstores.register import STORE_FACT_FIELDS, store_facts

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: The Release Statuses whose bundle describes a built Store.
BUILT_STATUSES: frozenset[str] = frozenset({"built", "validated"})

ABSENT = "(absent)"
_OBSERVED_LINE = re.compile(r"observed:[ \t]*(#.*)?\n?")


class Refused(Exception):
    """A record the tool will not edit, and why."""


@dataclass
class Outcome:
    store_id: str
    action: str  # "write", "unchanged", "skip" or "refuse"
    reason: str = ""
    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)


def published_phase(bundle_obj: Bundle, artifact_root: Path) -> str:
    """The phase whose Store is published: the completion phase once it has registered (ADR 0037)."""
    completion_register = paths.record_path(
        bundle_obj.store_id, REGISTER_STEPS[COMPLETION_PHASE], root=artifact_root
    )
    if bundle_obj.completes_own_store and completion_register.is_file():
        return COMPLETION_PHASE
    return BUILD_PHASE


def observed_facts(bundle_obj: Bundle, artifact_root: Path) -> dict[str, Any]:
    """The Store facts `register` would record for this bundle's published Store."""
    store_id = bundle_obj.store_id
    store_dir = paths.store_path(store_id, root=artifact_root)
    phase = published_phase(bundle_obj, artifact_root)
    registered = paths.record_path(store_id, REGISTER_STEPS[phase], root=artifact_root).is_file()
    phase_steps = (
        [step for step in plan(bundle_obj, artifact_root=artifact_root) if step.phase == phase]
        if registered
        else []
    )
    records = {}
    for step in phase_steps:
        record = run.load_record(store_id, step.name, root=artifact_root)
        if record is not None:
            records[step.name] = record
    facts = store_facts(phase_steps, records, store_dir)
    if not registered:
        # No register record says which steps built this Store, so whether it
        # holds them is not known: null, never false.
        facts["has_hm3"] = None
        facts["has_rho"] = None
    return facts


def _block(lines: list[str], start: int) -> tuple[int, str]:
    """The end (exclusive) of the indented block after `lines[start]`, and its indent."""
    end = start + 1
    indent = ""
    last_content = start
    while end < len(lines):
        line = lines[end]
        if line.strip() == "":
            end += 1
            continue
        if not line[0].isspace():
            break
        if not indent:
            indent = line[: len(line) - len(line.lstrip(" "))]
        last_content = end
        end += 1
    return last_content + 1, indent


def _key_span(lines: list[str], start: int, end: int, indent: str, key: str) -> tuple[int, int] | None:
    """The lines of `key`'s entry in the block `lines[start:end]`, its value's lines included."""
    pattern = re.compile(rf"{re.escape(indent)}{re.escape(key)}:(\s|$)")
    for index in range(start, end):
        if pattern.match(lines[index]):
            stop = index + 1
            while stop < end and (
                lines[stop].strip() == ""
                or len(lines[stop]) - len(lines[stop].lstrip(" ")) > len(indent)
                or lines[stop].startswith(indent + "- ")
            ):
                stop += 1
            while stop > index + 1 and lines[stop - 1].strip() == "":
                stop -= 1
            return index, stop
    return None


def _render(key: str, value: Any, indent: str) -> list[str]:
    dumped = yaml.safe_dump({key: value}, sort_keys=False, default_flow_style=False)
    return [indent + line + "\n" for line in dumped.splitlines()]


def edit_observed(text: str, facts: dict[str, Any]) -> str:
    """`text` with `observed`'s Store facts set to `facts`, every other line unchanged."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    heads = [index for index, line in enumerate(lines) if _OBSERVED_LINE.fullmatch(line)]
    if len(heads) != 1:
        raise Refused("validation.yaml has no block-style `observed:` mapping to edit")
    head = heads[0]
    end, indent = _block(lines, head)
    if not indent:
        raise Refused("`observed` is empty; there is no indentation to follow")

    # Each key goes where it is, or after the Store fact before it, so the keys
    # keep register's order; a first key that is missing ends the block.
    anchor: int | None = None
    for key in STORE_FACT_FIELDS:
        rendered = _render(key, facts[key], indent)
        span = _key_span(lines, head + 1, end, indent, key)
        start, stop = span if span is not None else ((end, end) if anchor is None else (anchor, anchor))
        lines[start:stop] = rendered
        end += len(rendered) - (stop - start)
        anchor = start + len(rendered)
    edited = "".join(lines)

    original_doc = yaml.safe_load(text)
    expected = copy.deepcopy(original_doc)
    expected["observed"].update(facts)
    if yaml.safe_load(edited) != expected:
        raise Refused("the edit would change a key other than the Store facts in `observed`")
    # A file without a final newline gains one only along with a real change.
    return text if edited == text + "\n" else edited


def backfill_bundle(bundle_obj: Bundle, artifact_root: Path, *, dry_run: bool) -> Outcome:
    store_id = bundle_obj.store_id
    status = bundle_obj.status or ""
    if status not in BUILT_STATUSES:
        return Outcome(store_id, "skip", f"status {status!r} describes no built Store")
    path = bundle_obj.root / "validation.yaml"
    if not path.is_file():
        return Outcome(store_id, "skip", "no validation.yaml")
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    observed = document.get("observed") if isinstance(document, dict) else None
    if not isinstance(observed, dict):
        return Outcome(
            store_id, "skip", "no `observed` block; adding one would change a key that is not a Store fact"
        )

    try:
        facts = observed_facts(bundle_obj, artifact_root)
        edited = edit_observed(text, facts)
    except (Refused, ValueError, NotImplementedError) as exc:
        return Outcome(store_id, "refuse", str(exc))

    changes = {
        key: (observed.get(key, ABSENT), facts[key])
        for key in STORE_FACT_FIELDS
        if key not in observed or observed[key] != facts[key]
    }
    if edited == text:
        return Outcome(store_id, "unchanged")
    if not dry_run:
        temp = path.with_name(f".{path.name}.backfill.{os.getpid()}")
        temp.write_text(edited, encoding="utf-8")
        os.replace(temp, path)
    return Outcome(store_id, "write", changes=changes)


def _shown(value: Any) -> str:
    return value if value == ABSENT else json.dumps(value, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--registry-root", type=Path, default=REPO_ROOT / "stores")
    parser.add_argument("--artifact-root", type=Path, default=None,
                        help="default: the deployment's Artifact Root (`paths.artifact_root()`)")
    parser.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    args = parser.parse_args(argv)
    artifact_root = args.artifact_root if args.artifact_root is not None else paths.artifact_root()

    outcomes = [
        backfill_bundle(bundle_obj, artifact_root, dry_run=args.dry_run)
        for bundle_obj in discover_bundles(args.registry_root)
    ]
    verb = "would write" if args.dry_run else "wrote"
    for outcome in outcomes:
        if outcome.action == "write":
            detail = "; ".join(f"{key}: {_shown(old)} -> {_shown(new)}" for key, (old, new) in outcome.changes.items())
            print(f"{outcome.store_id}: {verb} {detail}")
        elif outcome.action == "unchanged":
            print(f"{outcome.store_id}: unchanged")
        else:
            print(f"{outcome.store_id}: {outcome.action} ({outcome.reason})")
    counts = {action: sum(o.action == action for o in outcomes) for action in ("write", "unchanged", "skip", "refuse")}
    print(
        f"{len(outcomes)} bundles: {counts['write']} {verb}, {counts['unchanged']} unchanged, "
        f"{counts['skip']} skipped, {counts['refuse']} refused"
        + (" (dry run: nothing written)" if args.dry_run else "")
    )
    return 1 if counts["refuse"] else 0


if __name__ == "__main__":
    sys.exit(main())
