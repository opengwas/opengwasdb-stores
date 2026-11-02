"""Fixture repositories for the architecture collector's tests (#418).

Small git repositories built in a temporary directory, so the collector reads
them the way it reads the real ones: through `git ls-tree` and `git cat-file`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(root: Path, files: dict[str, str], *, branch: str = "main") -> str:
    """A repository at `root` with `files` committed on `branch`; returns the commit."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", branch)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


REGISTRY_MODEL = """\
repo: opengwas/opengwasdb-stores
system: {name: OpenGWASDB, summary: The system.}
people:
  - {id: curator, name: Curator, summary: Runs builds.}
external:
  - {id: sources, name: Sources, summary: Summary statistics.}
containers:
  - {id: engine, name: Engine, repo: opengwasdb, tech: Python, summary: Builds stores.}
  - {id: generation, name: Generation, repo: opengwasdb-stores, tech: Python, summary: Makes manifests.}
container_relations:
  - [curator, generation, "runs"]
  - [generation, engine, "builds with"]
stages:
  - {id: build, name: Build, summary: Build a store}
  - {id: query, name: Query, summary: Look values up}
components:
  - id: gen
    name: Manifest Generation
    container: generation
    stages: [build]
    summary: Makes manifests.
    paths: [src/gen/]
  - id: ogs
    name: Release orchestration
    container: generation
    stages: [build]
    summary: Runs releases.
    paths: [src/ogs/]
"""

ENGINE_MODEL = """\
repo: opengwas/opengwasdb
layers:
  - {layer: 3, name: Entry}
  - {layer: 2, name: Use}
  - {layer: 1, name: Storage}
  - {layer: 0, name: Foundations}
components:
  - id: cli
    name: Engine CLI
    container: engine
    layer: 3
    stages: [build]
    summary: The command line.
    paths: [opengwasdb/cli/, opengwasdb/__init__.py]
  - id: query
    name: Query
    container: engine
    layer: 2
    stages: [query]
    summary: Looks values up.
    paths: [opengwasdb/query/]
  - id: check
    name: Checks
    container: engine
    layer: 2
    stages: [build]
    summary: Checks stores.
    paths: [opengwasdb/check/]
  - id: store
    name: Store envelope
    container: engine
    layer: 1
    stages: [build]
    summary: Paths and manifest.
    paths: [opengwasdb/store/]
  - id: model
    name: Store contract model
    container: engine
    layer: 0
    stages: [build]
    summary: Contracts.
    paths: [opengwasdb/model/]
"""

REGISTRY_FILES = {
    "docs/architecture/components.yaml": REGISTRY_MODEL,
    "src/gen/__init__.py": "",
    "src/gen/make.py": "from opengwasdb.store import paths\nfrom opengwasdb.model import enums\nimport opengwasdb.store.util\nimport os\n",
    "src/ogs/run.py": "from gen import make\nfrom opengwasdb.cli import main\n",
    "README.md": "readme\n",
}

ENGINE_FILES = {
    "docs/architecture/components.yaml": ENGINE_MODEL,
    "opengwasdb/__init__.py": "",
    "opengwasdb/cli/__init__.py": "",
    "opengwasdb/cli/main.py": "from ..query import facade\nfrom opengwasdb.store import paths\n",
    "opengwasdb/check/run.py": "from opengwasdb.query import facade\n",
    "opengwasdb/query/__init__.py": "",
    "opengwasdb/query/facade.py": "from opengwasdb.store import paths\nfrom opengwasdb.cli import main\n",
    "opengwasdb/store/__init__.py": "",
    "opengwasdb/store/paths.py": "from opengwasdb.model import enums\nfrom opengwasdb.store import util\nfrom opengwasdb.query import facade\n",
    "opengwasdb/store/util.py": "",
    "opengwasdb/model/store/util.py": "",
    "opengwasdb/model/__init__.py": "",
    "opengwasdb/model/enums.py": "from opengwasdb.store import util\n",
}

# ADRs: what each one cites or names decides where it is placed.
REGISTRY_ADRS = {
    # a cited path; the title also names a component, but the code is the better evidence
    "docs/adr/0001-one-pipeline.md": "# 0001. Manifest Generation is one pipeline\n\nThe code is src/gen/make.py.\n",
    # a dotted engine module and a dotted registry module
    "docs/adr/0002-dotted.md": "# 0002. Dotted names\n\nSee `opengwasdb.store.paths` and `gen.make`.\n",
    # no code cited; a component's name is in the title
    "docs/adr/0003-title.md": "# 0003. Release orchestration runs in Snakemake\n\nNo code is cited.\n",
    # a component's name only in the body, however often
    "docs/adr/0004-body.md": "# 0004. Something else\n\n" + "Manifest Generation. " * 12 + "\n",
    # a bare suffix places only when exactly one tracked file ends with it
    "docs/adr/0005-suffix.md": "# 0005. Suffixes\n\nSee store/paths.py but not store/util.py.\n",
}
ENGINE_ADRS = {
    "docs/adr/0001-query.md": "# 0001. Query is layer free\n\nBody.\n",
    "docs/adr/0002-cites.md": "# 0002. Layers\n\nopengwasdb/cli/main.py and opengwasdb/check/run.py\n\nSuperseded by 0009.\n",
    "docs/adr/0003-none.md": "# 0003. Nothing\n\nNo cites.\n",
}
