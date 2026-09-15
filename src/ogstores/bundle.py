"""Release Bundle ownership: load one, and describe its status vocabulary.

`load()` reads `release.yaml`, `build.yaml` and `validation.yaml` into a frozen
`Bundle`, tolerating malformed YAML by recording the parse error in place of the
document rather than raising, so a caller can report on a broken bundle.

It never opens a Store.

See docs/spec/store-release-workflow.md and ADRs 0017, 0022, 0023, 0024.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

VALID_STATUSES: frozenset[str] = frozenset({
    "candidate",
    "accepted",
    "built",
    "validated",
    "superseded",
    "withdrawn",
})

LEGAL_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"candidate", "accepted", "withdrawn", "superseded"}),
    "accepted": frozenset({"accepted", "built", "withdrawn", "superseded"}),
    "built": frozenset({"built", "validated", "superseded", "withdrawn"}),
    "validated": frozenset({"validated", "superseded", "withdrawn"}),
    "superseded": frozenset({"superseded", "withdrawn"}),
    "withdrawn": frozenset({"withdrawn"}),
}


def is_legal_status_transition(from_status: str, to_status: str) -> bool:
    """Return True if transitioning from `from_status` to `to_status` is legal."""
    if from_status not in LEGAL_STATUS_TRANSITIONS:
        return False
    return to_status in LEGAL_STATUS_TRANSITIONS[from_status]


def validate_status_transition(from_status: str, to_status: str) -> list[str]:
    """Return a list of errors if transition from `from_status` to `to_status` is illegal."""
    errors: list[str] = []
    if from_status not in VALID_STATUSES:
        errors.append(
            f"invalid current status {from_status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    if to_status not in VALID_STATUSES:
        errors.append(
            f"invalid target status {to_status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    if not errors and not is_legal_status_transition(from_status, to_status):
        allowed = sorted(LEGAL_STATUS_TRANSITIONS.get(from_status, ()))
        errors.append(
            f"illegal status transition from {from_status!r} to {to_status!r}; "
            f"allowed transitions from {from_status!r}: {allowed}"
        )
    return errors


@dataclass(frozen=True)
class Bundle:
    store_id: str
    root: Path
    release: dict[str, Any]
    build: dict[str, Any]
    analyses_path: Path
    validation: dict[str, Any] | None = None

    @property
    def status(self) -> str | None:
        return self.release.get("status")

    @property
    def label(self) -> str | None:
        return self.release.get("label")

    @property
    def family(self) -> str | None:
        return self.release.get("family")

    @property
    def layout(self) -> str | None:
        return self.build.get("layout")

    @property
    def completion_state(self) -> str | None:
        return self.build.get("completion_state")

    @property
    def derived_from(self) -> str | None:
        return self.release.get("derived_from")

    @property
    def validation_path(self) -> Path | None:
        vp = self.root / "validation.yaml"
        return vp if vp.is_file() else None


def _read_yaml(path: Path) -> dict[str, Any] | None:
    """Parse one YAML document, recording a parse failure rather than raising."""
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as exc:
        return {"__yaml_error__": str(exc)}


def load(store_id: str, registry_root: Path | str | None = None) -> Bundle:
    """Load a Release Bundle from `stores/<store_id>` or a custom registry root."""
    if registry_root is not None:
        p = Path(registry_root)
        root = p if p.name == store_id else p / store_id
    else:
        candidates = [
            Path("stores") / store_id,
            REPO_ROOT / "stores" / store_id,
            Path.cwd() / store_id,
            Path.cwd(),
        ]
        root = next(
            (c for c in candidates if c.is_dir() and (c / "release.yaml").is_file()),
            Path("stores") / store_id,
        )

    return Bundle(
        store_id=store_id,
        root=root,
        release=_read_yaml(root / "release.yaml") or {},
        build=_read_yaml(root / "build.yaml") or {},
        analyses_path=root / "analyses.tsv",
        validation=_read_yaml(root / "validation.yaml"),
    )


__all__ = [
    "Bundle",
    "LEGAL_STATUS_TRANSITIONS",
    "VALID_STATUSES",
    "is_legal_status_transition",
    "load",
    "validate_status_transition",
]
