"""A helper the registry's and the index's tests share (#540): the LD panels' declarations, alone.

`copy_declarations(repo)` copies every `resources/reference-resources/*/resource.yaml`
of this repository under `repo`, and no other file: the resources' 42 MB tree
is not needed to list the panels, and `git add`ing it costs a second.
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOURCES = Path("resources", "reference-resources")


def copy_declarations(repo: Path) -> Path:
    """Copy the repository's `resource.yaml` files under `repo`; return `repo`'s reference-resources root."""
    for declaration in sorted((REPO_ROOT / RESOURCES).glob("*/resource.yaml")):
        target = repo / RESOURCES / declaration.parent.name / declaration.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(declaration, target)
    return repo / RESOURCES
