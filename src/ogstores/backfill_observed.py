"""Write #502's Store facts and #512's format version into the Validation Records registered before them.

    pixi run -e dev backfill-observed [--dry-run] [--registry-root stores] [--artifact-root /data/opengwasdb/stores]

`register` records `observed.format_version` (#512, from `opengwasdb info
--format json` when no step reported it) and `observed.store_bytes`, `has_hm3`, `has_rho` and
`n_top_hits` at publication (#502). A record written before that carries
`store_bytes: null` and none of the other three. This tool fills those keys
(`BACKFILL_FIELDS`) of every `built` or `validated` bundle's
`validation.yaml`, the same way `register` does:

- `store_bytes` from a stat of each file of the published Store,
  `<artifact-root>/<id>/store.opengwasdb`;
- `has_hm3` and `has_rho` from the publishing phase's plan and its step
  records under `<artifact-root>/<id>/records/` (`register.store_facts`), and
  `null` when no register record says which steps built the Store;
- `format_version` from `opengwasdb info --format json` on the Store, through
  `ogstores.store_info.store_info`, which `readiness` re-exports (the one reader, ADR 0023): the command's report
  fills a `null`. A recorded value is never overwritten: a Store whose `info`
  disagrees with it, or whose `info` fails, is refused;
- `completion_state` from the same `info` report, when it names one: a record
  written before #307 has none, and a rewrite's `publish-rewrite` carried the
  gap (#525). A recorded state that disagrees with `info` is refused;
- `n_top_hits` as `null`, until opengwas/opengwasdb#488 reports the counts.

A bundle whose Store is not under the Artifact Root is reported by name as
`missing` and left alone: there is nothing to measure. A record with no
`observed` block is skipped, since adding one would change a key that is not a
Store fact.

It writes those keys and nothing else. Each record is edited as text, in
place: a key already in `observed` has its value replaced, a missing one is
inserted after the Store fact before it, and every other line keeps its bytes.
The bundle is refused, and its file left as it was, when a mapping in the
record holds a key twice, when a line the edit replaces carries a comment
(which the edit would drop), or when the edited document, parsed, differs from
the original in any key but those five. A second run changes nothing.

It opens no Store file itself (`info` does) and writes nothing under the Artifact Root. `--dry-run`
prints what it would change and writes nothing.

Run it as `python3 -m ogstores.backfill_observed`, which the
`backfill-observed` task does. It runs once, after
opengwas/opengwasdb-stores#498 rewrites every Store to format 0.3.0, so it
records the Stores as they are served. Recording observations into an accepted
bundle's Validation Record changes no membership, so the Release Bundle
contract allows it (`docs/release-metadata-schema.md`, "validation.yaml").

Exit status: 1 when a bundle was refused, 0 otherwise.
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
from ogstores.paths import REPO_ROOT
from ogstores.plan import BUILD_PHASE, REGISTER_STEPS, plan, release_register
from ogstores.readiness import discover_bundles
from ogstores.register import STORE_FACT_FIELDS, read_completion_state, read_format_version, store_facts

#: The Release Statuses whose bundle describes a built Store.
BUILT_STATUSES: frozenset[str] = frozenset({"built", "validated"})

#: The `observed` keys the backfill may write: `format_version` (#512), then
#: the Store facts `register` records at publication (#502).
BACKFILL_FIELDS: tuple[str, ...] = ("format_version", *STORE_FACT_FIELDS, "completion_state")

#: The outcomes, in the order the summary counts them. `missing` is a bundle
#: whose Store is not under the Artifact Root, so there is nothing to measure.
ACTIONS: tuple[str, ...] = ("write", "unchanged", "missing", "skip", "refuse")

ABSENT = "(absent)"
_OBSERVED_LINE = re.compile(r"observed:[ \t]*(#.*)?\n?")
# A YAML comment: `#` at the start of a line or after whitespace.
_COMMENT = re.compile(r"(^|\s)#")


class Refused(Exception):
    """A record the tool will not edit, and why."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a mapping holding a key twice.

    PyYAML keeps the last of two equal keys, so a record that already held a
    Store fact twice, or an edit that added a second, would compare equal to
    the intended document and pass unnoticed.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise Refused(f"validation.yaml holds the key {key!r} twice in one mapping")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load(text: str) -> Any:
    return yaml.load(text, Loader=_UniqueKeyLoader)


@dataclass
class Outcome:
    store_id: str
    action: str  # one of ACTIONS
    reason: str = ""
    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)


def published_phase(bundle_obj: Bundle, artifact_root: Path) -> str | None:
    """The phase whose register published the Store now served, or None when none has registered.

    `plan.release_register` names the register that finishes the release.
    Until its record exists, a release that completes its own Store serves the
    Store its build phase registered (ADR 0037). `register` decides this while
    it runs; the backfill reads it from the register records left behind.
    """
    phases = {register_step: phase for phase, register_step in REGISTER_STEPS.items()}
    for register_step in dict.fromkeys((release_register(bundle_obj), REGISTER_STEPS[BUILD_PHASE])):
        if paths.record_path(bundle_obj.store_id, register_step, root=artifact_root).is_file():
            return phases[register_step]
    return None


def observed_facts(bundle_obj: Bundle, artifact_root: Path) -> dict[str, Any]:
    """The Store facts `register` would record for this bundle's published Store."""
    store_id = bundle_obj.store_id
    store_dir = paths.store_path(store_id, root=artifact_root)
    phase = published_phase(bundle_obj, artifact_root)
    registered = phase is not None
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
    facts = {"format_version": read_format_version(store_dir), **store_facts(phase_steps, records, store_dir)}
    # Only when `info` reports one: a record written before #307 (OGS-00016's
    # rewrite, #525) has none, and the engine's report is the measurement.
    completion_state = read_completion_state(store_dir)
    if completion_state is not None:
        facts["completion_state"] = completion_state
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
    for key in (k for k in BACKFILL_FIELDS if k in facts):
        rendered = _render(key, facts[key], indent)
        span = _key_span(lines, head + 1, end, indent, key)
        if span is not None and any(_COMMENT.search(line) for line in lines[span[0]:span[1]]):
            raise Refused(f"`observed.{key}` carries a comment, which replacing it would drop")
        start, stop = span if span is not None else ((end, end) if anchor is None else (anchor, anchor))
        lines[start:stop] = rendered
        end += len(rendered) - (stop - start)
        anchor = start + len(rendered)
    edited = "".join(lines)

    original_doc = _load(text)
    expected = copy.deepcopy(original_doc)
    expected["observed"].update(facts)
    if _load(edited) != expected:
        raise Refused("the edit would change a key other than the Store facts in `observed`")
    # A file without a final newline gains one only along with a real change.
    return text if edited == text + "\n" else edited


def backfill_bundle(bundle_obj: Bundle, artifact_root: Path, *, dry_run: bool) -> Outcome:
    store_id = bundle_obj.store_id
    status = bundle_obj.status or ""
    if status not in BUILT_STATUSES:
        return Outcome(store_id, "skip", f"status {status!r} describes no built Store")
    store_dir = paths.store_path(store_id, root=artifact_root)
    if not store_dir.is_dir():
        return Outcome(store_id, "missing", f"Store missing from the Artifact Root: {store_dir}")
    path = bundle_obj.root / "validation.yaml"
    if not path.is_file():
        return Outcome(store_id, "skip", "no validation.yaml")
    text = path.read_text(encoding="utf-8")
    try:
        document = _load(text)
    except Refused as exc:
        return Outcome(store_id, "refuse", str(exc))
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

    recorded = observed.get("format_version")
    if recorded is not None and recorded != facts["format_version"]:
        return Outcome(
            store_id, "refuse",
            f"`observed.format_version` records {recorded!r} but `opengwasdb info` reports "
            f"{facts['format_version']!r}; the backfill fills a null and overwrites nothing",
        )
    recorded_state = observed.get("completion_state")
    if recorded_state is not None and "completion_state" in facts and recorded_state != facts["completion_state"]:
        return Outcome(
            store_id, "refuse",
            f"`observed.completion_state` records {recorded_state!r} but `opengwasdb info` reports "
            f"{facts['completion_state']!r}; the backfill fills a null and overwrites nothing",
        )
    changes = {
        key: (observed.get(key, ABSENT), facts[key])
        for key in BACKFILL_FIELDS
        if key in facts
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
    counts = {action: sum(o.action == action for o in outcomes) for action in ACTIONS}
    print(
        f"{len(outcomes)} bundles: {counts['write']} {verb}, {counts['unchanged']} unchanged, "
        f"{counts['missing']} with the Store missing, {counts['skip']} skipped, {counts['refuse']} refused"
        + (" (dry run: nothing written)" if args.dry_run else "")
    )
    return 1 if counts["refuse"] else 0


if __name__ == "__main__":
    sys.exit(main())
