"""Where the site build reads and writes, and what it renders (#388).

Nothing the site build writes goes under `/home` or `/tmp`: the work root is
`/data/opengwasdb/work/site`, and the output sits under it, one directory per
checkout. Each path is overridable by an environment variable so a publish run
can keep its scratch tree apart from a developer's.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# The two repositories the site shows: this registry, and the engine whose docs
# it stages at the pinned revision.
REGISTRY_URL = "https://github.com/opengwas/opengwasdb-stores"
ENGINE_URL = "https://github.com/opengwas/opengwasdb"

# Defaults, all under /data. `git clone` keeps the engine's history here; the
# checkout being rendered names its own output directory.
WORK_ROOT = Path("/data/opengwasdb/work/site")
WORK_ROOT_ENV = "OPENGWASDB_SITE_ROOT"
OUTPUT_ENV = "OPENGWASDB_SITE_OUT"
CLONE_ENV = "OPENGWASDB_SITE_ENGINE_CLONE"

# The ref `site-publish` renders until the cutover moves it to `origin/main`.
DEFAULT_PUBLISH_REF = "origin/dev"

_PIN = re.compile(r"git\+https://github\.com/opengwas/opengwasdb\?rev=([0-9a-f]{40})")


def repo_root() -> Path:
    """The checkout being documented: the one this package is installed from."""
    return Path(__file__).resolve().parents[2]


def work_root() -> Path:
    return Path(os.environ.get(WORK_ROOT_ENV) or WORK_ROOT)


def checkout_name(repo: Path) -> str:
    return repo.resolve().name


def output_dir(repo: Path) -> Path:
    """Where a render puts the site: `$OPENGWASDB_SITE_OUT`, else under the work root."""
    configured = os.environ.get(OUTPUT_ENV)
    if configured:
        return Path(configured)
    return work_root() / checkout_name(repo) / "_site"


def staging_dir(repo: Path) -> Path:
    """The mirror of `docs/` the render reads, beside the output.

    Rendering a mirror, rather than `docs/` itself, is what lets the build
    rewrite links and spell Mermaid fences without editing a source file.
    """
    return work_root() / checkout_name(repo) / "_stage"


def preview_dir(repo: Path) -> Path:
    """The tree the live preview serves: the mirror, synced file by file.

    `quarto preview` renders into `_site/` inside the tree it serves, so the
    preview serves a synced copy. Re-staging then never removes a file the
    preview is writing, which is what a rebuild of the mirror itself would do.
    """
    return work_root() / checkout_name(repo) / "_preview"


def engine_clone() -> Path:
    return Path(os.environ.get(CLONE_ENV) or (WORK_ROOT / "opengwasdb.git"))


def engine_staging(repo: Path) -> Path:
    """`docs/opengwasdb/`: the engine's docs, archived there and git-ignored."""
    return repo / "docs" / "opengwasdb"


def glossary_page(repo: Path) -> Path:
    """`docs/glossary.md`: CONTEXT.md, staged as the site's glossary page."""
    return repo / "docs" / "glossary.md"


def pinned_opengwasdb_revision(lock: Path) -> str:
    """The opengwasdb commit `pixi.lock` pins; the site documents that runtime."""
    revisions = set(_PIN.findall(lock.read_text(encoding="utf-8")))
    if len(revisions) != 1:
        raise ValueError(f"{lock} pins {len(revisions)} opengwasdb revisions, expected 1: {sorted(revisions)}")
    return revisions.pop()


def commit_of(repo: Path) -> str:
    """The commit `repo` has checked out; the one its docs are rendered from."""
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
