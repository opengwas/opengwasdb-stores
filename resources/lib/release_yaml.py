"""Minimal indentation-based reader for this registry's release-bundle YAML.

This repository deliberately avoids a PyYAML dependency in its Python
generator scripts (see the retired build-store.py's original hand-rolled
parser, #103). That parser only handled flat keys and one level of nested
mapping, which is not
enough to read `build.yaml`'s `reference_resources` (a list of mappings, some
with their own nested mapping) or `effect_scale_validation`/`ancestry_assignment`
(nested mappings two levels deep). This module generalises it to arbitrary
nesting and block sequences, still without a YAML library, covering exactly
the block-style subset that `yaml::as.yaml()` (used on the R side to write
these files) emits: 2-space indents, quoted or bare scalars, `~`/`null` for
None, `yes`/`no` for booleans, and `[]` for an empty flow list.

It is not a general YAML parser — flow mappings, anchors, and multi-line
block scalars (`|`/`>`) are not supported, because nothing this registry
writes uses them.
"""

from __future__ import annotations

from pathlib import Path

_NULLS = {"~", "null", "Null", "NULL"}
_TRUE = {"yes", "Yes", "true", "True", "TRUE"}
_FALSE = {"no", "No", "false", "False", "FALSE"}


def _parse_scalar(raw: str) -> object:
    raw = raw.strip()
    if raw == "":
        return None
    if raw in _NULLS:
        return None
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [] if not inner else [_parse_scalar(v) for v in inner.split(",")]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1]
    return raw


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _descend_indent(lines: list[str], idx: int, current_indent: int) -> int | None:
    """Indent to recurse into for an empty (`key:`) value, or None for a bare null.

    A YAML block sequence nested under a mapping key is conventionally written
    at the *same* indent as that key (`reference_resources:` / `- resource_id:
    ...`), not indented further, so a plain `next indent > current` check would
    treat it as a sibling and stop. Only descend when the next line is either
    more indented (nested mapping) or a `-` item at the same indent (nested
    sequence); anything else at or below the current indent is a sibling key
    and this value is truly empty/null.
    """
    if idx >= len(lines):
        return None
    next_indent = _indent_of(lines[idx])
    next_is_seq_item = lines[idx][next_indent:].startswith("-")
    if next_indent > current_indent or (next_indent == current_indent and next_is_seq_item):
        return next_indent
    return None


def parse_yaml_subset(text: str) -> dict | list:
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    state = {"idx": 0}

    def parse_block(min_indent: int) -> dict | list:
        if state["idx"] >= len(lines):
            return {}
        block_indent = _indent_of(lines[state["idx"]])
        if block_indent < min_indent:
            return {}
        is_seq = lines[state["idx"]][block_indent:].startswith("-")
        container: dict | list = [] if is_seq else {}

        while state["idx"] < len(lines):
            line = lines[state["idx"]]
            indent = _indent_of(line)
            if indent != block_indent:
                break
            content = line[indent:]

            if is_seq:
                if not content.startswith("-"):
                    break
                item_body = content[1:]
                if item_body.startswith(" "):
                    item_body = item_body[1:]
                item_indent = indent + (len(content) - len(item_body))
                state["idx"] += 1
                if ":" in item_body:
                    key, _, value = item_body.partition(":")
                    key, value_str = key.strip(), value.strip()
                    if value_str == "":
                        descend_to = _descend_indent(lines, state["idx"], item_indent)
                        item: dict = {key: parse_block(descend_to) if descend_to is not None else None}
                    else:
                        item = {key: _parse_scalar(value_str)}
                    rest = parse_block(item_indent)
                    if isinstance(rest, dict):
                        item.update(rest)
                    container.append(item)  # type: ignore[union-attr]
                else:
                    container.append(_parse_scalar(item_body))  # type: ignore[union-attr]
            else:
                if ":" not in content:
                    state["idx"] += 1
                    continue
                key, _, value = content.partition(":")
                key, value_str = key.strip(), value.strip()
                state["idx"] += 1
                if value_str == "":
                    descend_to = _descend_indent(lines, state["idx"], block_indent)
                    container[key] = parse_block(descend_to) if descend_to is not None else None  # type: ignore[index]
                else:
                    container[key] = _parse_scalar(value_str)  # type: ignore[index]
        return container

    return parse_block(0)


def read_release_yaml(path: Path) -> dict:
    parsed = parse_yaml_subset(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def require_text(data: dict, *keys: str) -> str:
    current: object = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise SystemExit(f"Missing required YAML field: {'.'.join(keys)}")
        current = current[key]
    return "" if current is None else str(current)


def get(data: dict, *keys: str, default: object = None) -> object:
    current: object = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return default if current is None else current


_STATUS_RANK = {"not_run": 0, "passed": 1, "passed_with_warnings": 2, "failed": 3}
_CANONICAL_CHECK_ORDER = [
    "schema", "files", "reader_smoke_test", "ancestry", "effect_scale", "sd_estimation", "sparse_regions",
]


def _ordered(mapping: dict[str, str], canonical: list[str]) -> dict[str, str]:
    ordered = {key: mapping[key] for key in canonical if key in mapping}
    ordered.update({key: value for key, value in mapping.items() if key not in ordered})
    return ordered


def _read_flat_section(lines: list[str], header: str) -> dict[str, str]:
    """Read a `header:` section's direct `  key: value` children as strings.

    Line-based rather than routed through `parse_yaml_subset`: `warnings`
    entries are quoted strings that often contain their own colon
    (`'GCST123: some reason'`), which is not a `key: value` pair and would
    corrupt a general-purpose block-sequence parse. This only ever needs to
    read `checks:`/`reports:`, both flat one-level sections, so a dedicated
    line scan is simpler and correct for exactly that shape.
    """
    out: dict[str, str] = {}
    in_section = False
    for raw_line in lines:
        if raw_line == header:
            in_section = True
            continue
        if in_section and raw_line and not raw_line.startswith(" "):
            break
        if in_section and ":" in raw_line:
            key, value = raw_line.strip().split(":", 1)
            out[key.strip()] = value.strip().strip("'\"")
    return out


def read_previous_checks_and_warnings(path: Path) -> tuple[dict[str, str], list[str]]:
    """Read `checks.*` and `warnings` from an existing validation.yaml."""
    if not path.exists():
        return {}, []
    lines = path.read_text(encoding="utf-8").splitlines()
    checks = _read_flat_section(lines, "checks:")
    warnings: list[str] = []
    in_warnings = False
    for raw_line in lines:
        if raw_line.startswith("warnings:"):
            in_warnings = True
            continue
        if in_warnings and raw_line and not raw_line.startswith(" "):
            break
        if in_warnings and raw_line.strip().startswith("- "):
            warnings.append(raw_line.strip()[2:].strip().strip("'\""))
    return checks, warnings


def write_release_status(path: Path, status: str) -> bool:
    """Set the top-level `status:` line of a `release.yaml`, and nothing else.

    A release bundle's lifecycle state is one line in a hand-authored record;
    this updates exactly that line, preserving every other line, so a workflow
    that lands a release as `built` rather than `validated` (issue #99) does not
    rewrite the rest of the bundle's provenance. Returns True when the file was
    changed; a release with no `release.yaml` is left alone -- this workflow does
    not invent lifecycle records.
    """
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    replacement = f"status: {status}"
    for index, line in enumerate(lines):
        if line.startswith("status:"):
            if line.rstrip() == replacement:
                return False
            lines[index] = replacement
            break
    else:
        lines.insert(0, replacement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _strip_trailing_blanks(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def split_top_level_blocks(text: str) -> list[tuple[str | None, list[str]]]:
    """Split block-style YAML into `(top-level key, raw lines)` blocks.

    A block starts at an unindented `key:` line and runs to just before the next
    one, so an indented multi-line block scalar (`description: |`) stays with its
    key instead of being mistaken for the next record. Lines before the first key
    (a `---` marker, comments) come back with a `None` key. Re-joining the blocks'
    lines reproduces the input, so this is a lossless split: it is the primitive
    `merge_release_yaml` uses to refresh a few workflow-owned blocks while leaving
    a bundle's curated blocks untouched.
    """
    blocks: list[tuple[str | None, list[str]]] = []
    key: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        is_key = bool(line) and not line[0].isspace() and ":" in line
        if is_key:
            if lines:
                blocks.append((key, _strip_trailing_blanks(lines)))
            key = line.split(":", 1)[0].strip()
            lines = []
        lines.append(line)
    if lines:
        blocks.append((key, _strip_trailing_blanks(lines)))
    return blocks


def merge_release_yaml(path: Path, owned: dict[str, list[str]]) -> None:
    """Refresh the workflow-owned top-level blocks of a release-bundle record.

    `owned` maps a top-level key to the rendered lines that replace (or add) its
    block. Every other block is preserved verbatim -- a curator's multi-line
    `description`/`notes` scalars, `source_*`, `build_environment`, and any field
    added later -- so re-registering a child Store Release refreshes workflow
    provenance without discarding the bundle's curated record (issue #101).

    Owned blocks are emitted first, so a later multi-line block scalar cannot
    hide them from the line-based readers above (which do not follow indentation
    into a block scalar). A file that does not exist yet is created from `owned`
    alone.
    """
    existing = split_top_level_blocks(path.read_text(encoding="utf-8")) if path.exists() else []
    emitted: list[list[str]] = [list(lines) for lines in owned.values()]
    seen = set(owned)
    for key, lines in existing:
        if key in seen:
            continue
        if key is not None:
            seen.add(key)
        emitted.append(list(lines))
    text = "\n\n".join("\n".join(block) for block in emitted).rstrip("\n") + "\n"
    path.write_text(text, encoding="utf-8")


def top_level_scalars(text: str) -> dict[str, str]:
    """Read the scalar value of each top-level `key: value` block, and `derived_from`.

    Unlike `read_release_yaml`, this does not stop at an indented multi-line
    block scalar, so it can read the workflow-owned identity fields from a bundle
    whose curated `description`/`notes` use `|` (issue #101). A key whose value is
    a nested block (`lineage:`, `generator:`) is reported as an empty string; a
    `derived_from:` line inside such a block is surfaced under that name.
    """
    scalars: dict[str, str] = {}
    for key, lines in split_top_level_blocks(text):
        if key is None:
            continue
        head = lines[0].partition(":")[2].strip().strip("'\"")
        if head and head not in {"|", ">"}:
            scalars[key] = head
        for line in lines[1:]:
            stripped = line.strip()
            if stripped.startswith("derived_from:"):
                scalars["derived_from"] = stripped.partition(":")[2].strip().strip("'\"")
    return scalars


def read_previous_reports(path: Path) -> dict[str, str]:
    """Read the `reports.*` section from an existing validation.yaml."""
    if not path.exists():
        return {}
    return _read_flat_section(path.read_text(encoding="utf-8").splitlines(), "reports:")


def merge_validation_yaml(
    path: Path,
    *,
    validator_name: str,
    updated_checks: dict[str, str],
    new_warnings: list[str],
    default_checks: dict[str, str] | None = None,
    updated_reports: dict[str, str] | None = None,
) -> None:
    """Merge `updated_checks`/`new_warnings`/`updated_reports` into
    validation.yaml without discarding checks, reports, or warnings written
    by a different stage.

    `default_checks` supplies values (e.g. `schema: passed`) for checks this
    call is authoritative for; `updated_checks` overrides/adds specific
    checks (e.g. `{"ancestry": "passed"}`) on top of whatever was already
    recorded, so two stages (this repository's effect-scale and ancestry
    stages, the workflow's validate phase, or any adapter) can each own their
    own checks without one clobbering the other.
    """
    from datetime import UTC, datetime

    from resources.lib.build_environment import capture as capture_build_environment

    previous_checks, previous_warnings = read_previous_checks_and_warnings(path)
    checks = _ordered({**(default_checks or {}), **previous_checks, **updated_checks}, _CANONICAL_CHECK_ORDER)
    all_warnings = list(dict.fromkeys([*previous_warnings, *new_warnings]))
    reports = {**read_previous_reports(path), **(updated_reports or {})}

    ranked = [*checks.values()]
    overall_status = max(ranked, key=lambda value: _STATUS_RANK.get(value, 0)) if ranked else "not_run"

    warnings_block = (
        "warnings:\n" + "\n".join(f"  - {warning}" for warning in all_warnings)
        if all_warnings
        else "warnings: []"
    )
    check_lines = "\n".join(f"  {key}: {value}" for key, value in checks.items())
    reports_block = (
        "reports:\n" + "\n".join(f"  {key}: {value}" for key, value in reports.items())
        if reports
        else "reports: {}"
    )
    # Rooted at this module's own location (not `path.parent`, which callers
    # may point at an arbitrary directory outside the repo, e.g. a test's
    # tempdir) so provenance capture works regardless of where the
    # validation.yaml being written happens to live.
    build_environment = capture_build_environment(Path(__file__).resolve().parents[2])
    build_environment_lines = "\n".join(f"  {key}: {value}" for key, value in build_environment.items())
    path.write_text(
        "\n".join([
            f"status: {overall_status}",
            f"validated_at: {datetime.now(UTC).isoformat()}",
            "validator:",
            f"  name: {validator_name}",
            "  version: null",
            "build_environment:",
            build_environment_lines,
            "checks:",
            check_lines,
            reports_block,
            warnings_block,
            "errors: []",
            "",
        ]),
        encoding="utf-8",
    )


def read_tsv(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def repo_root(start: Path) -> Path:
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=start, text=True, stdout=subprocess.PIPE, check=True
    )
    return Path(result.stdout.strip())


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path
