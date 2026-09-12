#!/usr/bin/env python3
"""One-off migration of the seven accepted Store Releases onto the executable
`build.yaml` schema (issue #97, ADR 0022).

#95 added the executable schema *beside* the pre-#95 shape, so nothing broke
while the migration was outstanding: a `build.yaml` could still declare the
inert `builder.entrypoint`, and `resources/lib/release_plan.py` loaded it as a
"legacy" plan. This is the contract half of that expand-contract migration. It
rewrites the seven Trial Store Releases checked into `families/` so there is one
schema under one filename, then drops `builder.entrypoint` so an operator can no
longer tell two shapes apart.

The seven-path inventory below is authoritative: it is the seven Trial Store
Releases enumerated by issue #93 and `docs/release-seam-audit.md` (the
opengwasdb#117 rebuild matrix). It is intentionally a closed list, not a glob:
the other checked-in `build.yaml` files (ukb-b, metabolome's non-European
slices, candidate pqtl-release bundles) stay on the legacy schema and remain
loadable, exactly as #95 promised.

What the migration changes, and nothing else:

1. the top-level `builder:` block becomes a top-level `build:` block whose
   `command` is the `opengwasdb` CLI subcommand the recorded `builder.entrypoint`
   implies, and whose `arguments` carry the required identity flags for that
   subcommand (`store-id`/`release-id` for a build-* command, `release-id` for a
   complete-* command);
2. the existing top-level `source:` block gains the fixed-input boundary,
   `root` (derived from the release's own `artifacts` block -- `source_dir`,
   else `filtered_dir`, else `download_dir`) and `analyses: analyses.tsv`.

Everything else -- layout, completion state, normalisation, ancestry
assignment, effect-scale validation, reference resources, artifacts, notes -- is
left byte-identical and in place, so the diff is only the intended key changes.

Idempotent: a release whose `build.yaml` already has no `builder:` block is left
untouched (byte-identical), so running this script twice changes nothing the
second time. Run from the repository root:

    python3 scripts/migrate-build-yaml-executable.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The authoritative seven Trial Store Releases (issue #93, release-seam audit).
RELEASES = [
    "families/eqtlgen-cis-pilot/releases/pilot-10",
    "families/eqtlgen-cis-pilot/releases/pilot-10-completed",
    "families/finngen-r13/releases/r13-pilot-20",
    "families/gwas-catalog-eur-hybrid/releases/eur-hybrid-pilot-10",
    "families/gwas-catalog-eur-hybrid/releases/eur-hybrid-quant-pilot-10",
    "families/metabolome-plasma-2023/releases/2023-chen-full-european",
    "families/pqtl-interval-2018/releases/2018-sun-pilot-10",
]

# The recorded `builder.entrypoint` -> the `opengwasdb` CLI subcommand that now
# names the operation (ADR 0022). Both build and completion commands are here:
# a Reference-Completed child release records its own completion entrypoint.
ENTRYPOINT_COMMANDS = {
    "opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest": "build-dense-vcf",
    "opengwasdb.layouts.hybrid.build:build_hybrid_from_vcf_manifest": "build-hybrid",
    "opengwasdb.layouts.ragged.build_ssf:build_ragged_from_ssf": "build-ragged-ssf",
    "opengwasdb.layouts.ragged.build_besd:build_ragged_from_besd": "build-ragged-besd",
    "opengwasdb.layouts.dense.complete:complete_dense_store": "complete-dense",
    "opengwasdb.layouts.hybrid.complete:complete_hybrid_store": "complete-hybrid",
    "opengwasdb.layouts.ragged.complete:complete_ragged_store": "complete-ragged",
}

# The `artifacts` key that names the directory holding the fixed-input source
# files, in derivation order. A Dense/BESD release records `source_dir`; a
# Ragged release records `filtered_dir`; a Hybrid release records `download_dir`.
_SOURCE_DIR_KEYS = ("source_dir", "filtered_dir", "download_dir")

_ENTRYPOINT_LINE = re.compile(r"^\s+entrypoint:\s*(?P<value>.*?)\s*$")
_SCALAR_LINE = re.compile(r"^(?P<indent> +)(?P<key>[^:]+):\s*(?P<value>.*?)\s*$")


def _block_bounds(lines: list[str], key: str) -> tuple[int, int] | None:
    """Half-open `[start, end)` line range of a top-level `key:` block."""
    marker = f"{key}:"
    start = next((i for i, line in enumerate(lines) if line == marker), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and lines[end].startswith(" "):
        end += 1
    return start, end


def _block_scalar(lines: list[str], start: int, end: int, key: str) -> str | None:
    """The value of an indented `  key: value` line inside `lines[start:end]`."""
    for line in lines[start:end]:
        match = _SCALAR_LINE.match(line)
        if match and match.group("key") == key:
            return match.group("value").strip().strip("'\"") or None
    return None


def _top_level_scalar(lines: list[str], key: str) -> str | None:
    """The value of a column-0 `key: value` line."""
    prefix = f"{key}:"
    for line in lines:
        if line.startswith(prefix) and not line.startswith(" "):
            return line[len(prefix):].strip().strip("'\"") or None
    return None


def _entrypoint(lines: list[str]) -> str:
    bounds = _block_bounds(lines, "builder")
    assert bounds is not None  # caller checked before calling
    start, end = bounds
    value = _block_scalar(lines, start, end, "entrypoint")
    if value is None:
        raise SystemExit("builder: block has no entrypoint")
    return value


def _source_root(lines: list[str]) -> str:
    bounds = _block_bounds(lines, "artifacts")
    if bounds is None:
        raise SystemExit("build.yaml has no artifacts: block to derive source.root from")
    start, end = bounds
    for key in _SOURCE_DIR_KEYS:
        value = _block_scalar(lines, start, end, key)
        if value:
            return value
    raise SystemExit(f"artifacts: block records none of {', '.join(_SOURCE_DIR_KEYS)} to derive source.root from")


def _command_and_arguments(entrypoint: str, store_family_id: str, family_release_id: str) -> tuple[str, list[str]]:
    command = ENTRYPOINT_COMMANDS.get(entrypoint)
    if command is None:
        raise SystemExit(f"no opengwasdb CLI subcommand known for entrypoint {entrypoint!r}")
    if command.startswith("complete-"):
        # complete-* takes no --store-id; --release-id names the child release.
        arguments = [f"    release-id: {family_release_id}"]
    else:
        arguments = [
            f"    store-id: {store_family_id}",
            f"    release-id: {family_release_id}",
        ]
    return command, arguments


def migrate_text(text: str) -> str:
    """Return `text` rewritten to the executable schema, or unchanged if it is already there."""
    lines = text.split("\n")
    if _block_bounds(lines, "builder") is None:
        return text  # already executable: idempotent no-op

    store_family_id = _top_level_scalar(lines, "store_family_id")
    family_release_id = _top_level_scalar(lines, "family_release_id")
    if not store_family_id or not family_release_id:
        raise SystemExit("build.yaml is missing store_family_id/family_release_id")
    entrypoint = _entrypoint(lines)
    command, arguments = _command_and_arguments(entrypoint, store_family_id, family_release_id)

    builder_start, builder_end = _block_bounds(lines, "builder")  # type: ignore[misc]
    build_lines = ["build:", f"  command: {command}", "  arguments:", *arguments]
    lines[builder_start:builder_end] = build_lines

    source = _block_bounds(lines, "source")
    if source is None:
        raise SystemExit("build.yaml has no source: block to add the fixed-input boundary to")
    source_start, _ = source
    fixed_input = [
        f"  root: {_source_root(lines)}",
        "  analyses: analyses.tsv",
    ]
    lines[source_start + 1 : source_start + 1] = fixed_input

    return "\n".join(lines)


def migrate_release(release_dir: Path) -> bool:
    """Rewrite `<release_dir>/build.yaml`; return whether anything changed."""
    build_yaml = release_dir / "build.yaml"
    original = build_yaml.read_text(encoding="utf-8")
    migrated = migrate_text(original)
    if migrated == original:
        return False
    build_yaml.write_text(migrated, encoding="utf-8")
    return True


def main() -> int:
    for relative in RELEASES:
        release_dir = REPO_ROOT / relative
        changed = migrate_release(release_dir)
        print(f"{'migrated' if changed else 'unchanged'} {relative}/build.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
