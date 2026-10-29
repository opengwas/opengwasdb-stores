"""Check that every path served today still resolves in a render (#388).

Readers hold links to the pages the legacy Pages deployment serves, so a render
may not drop one. The paths come from the site's current source of truth —
`git ls-tree -r --name-only origin/main docs` — and each must be present in the
output, either as the same file or as an HTML redirect page (`aliases`). A
missing path fails the build.

    python3 -m project_site.urlcheck --paths-file <file> --output <dir>
    python3 -m project_site.urlcheck --git-rev origin/main --output <dir>
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

_REDIRECT = re.compile(
    r"""<meta\s+http-equiv=["']?refresh["']?\s+content=["']?\s*\d+\s*;\s*url=([^"'>\s]+)""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PathStatus:
    """One served path: `present`, `aliased` (a redirect to `target`) or `missing`."""

    path: str
    state: str
    target: str | None = None


def redirect_target(page: Path) -> str | None:
    """The path an HTML redirect page points at, or None when it is not one."""
    if page.suffix.lower() not in {".html", ".htm"}:
        return None
    try:
        text = page.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    match = _REDIRECT.search(text)
    if match is None:
        return None
    return match.group(1).strip("/")


def check_paths(paths: Iterable[str], output: Path) -> list[PathStatus]:
    """The state of each path relative to `output`, in the order given."""
    statuses: list[PathStatus] = []
    for path in paths:
        page = output / path
        if not page.is_file():
            statuses.append(PathStatus(path, "missing"))
            continue
        target = redirect_target(page)
        if target is None:
            statuses.append(PathStatus(path, "present"))
        elif (output / target).is_file():
            statuses.append(PathStatus(path, "aliased", target))
        else:
            statuses.append(PathStatus(path, "missing", target))
    return statuses


def summarise(statuses: Iterable[PathStatus]) -> str:
    counts = {"present": 0, "aliased": 0, "missing": 0}
    total = 0
    for status in statuses:
        counts[status.state] += 1
        total += 1
    return (
        f"{total} paths: {counts['present']} present, "
        f"{counts['aliased']} aliased, {counts['missing']} missing"
    )


def paths_from_git(repo: Path, revision: str) -> list[str]:
    """The paths `revision` serves under `docs/`, as the published site names them."""
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", revision, "--", "docs"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [path[len("docs/"):] for path in listed if path.startswith("docs/")]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--paths-file", type=Path, help="one served path per line")
    source.add_argument("--git-rev", help="a revision whose docs/ is the served set")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository for --git-rev")
    parser.add_argument("--output", type=Path, required=True, help="the rendered site")
    args = parser.parse_args(argv)

    if args.paths_file is not None:
        paths = [line.strip() for line in args.paths_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        paths = paths_from_git(args.repo, args.git_rev)

    statuses = check_paths(paths, args.output)
    for status in statuses:
        if status.state == "aliased":
            print(f"aliased  {status.path} -> {status.target}")
        else:
            print(f"{status.state:<8} {status.path}")
    print(summarise(statuses))
    return 1 if any(status.state == "missing" for status in statuses) else 0


if __name__ == "__main__":
    sys.exit(main())
