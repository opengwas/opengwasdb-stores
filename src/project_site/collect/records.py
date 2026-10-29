"""The records collector: the epic records and the yardstick results, validated (#389).

An invalid file is reported by name and never stops the others. The yardstick
results are #390's: they are read through the validator that spec defines,
`yardstick.result.validate`, unless another is passed in.
An absent or empty directory is not an error.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Callable

from ogstores import paths
from yardstick import result as yardstick_result

from project_site import records

Validator = Callable[[dict[str, Any]], list[str]]


def yardstick_errors(document: dict[str, Any]) -> list[str]:
    """`yardstick.result.validate`, as a list of errors: any failure, by the file that caused it.

    A refusal is the validator's own message; anything else it raises is that
    file's error too, named by its exception type. One unreadable result is a
    refused file, never an unavailable Records source (#389 review, item 1).
    """
    try:
        yardstick_result.validate(document)
    except yardstick_result.ResultError as exc:
        return [str(exc)]
    except Exception as exc:  # noqa: BLE001 - one file never makes the source unavailable
        return [f"{type(exc).__name__}: {exc}"]
    return []


def default_epics_dir() -> Path:
    return paths.REPO_ROOT / "docs" / "_epics"


def default_yardstick_dir() -> Path:
    return paths.REPO_ROOT / "docs" / "_yardstick"


def _json_ready(value: Any) -> Any:
    """YAML reads an unquoted `2026-10-21` as a date; the file carries it as the ISO text."""
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    return value


def _validated(validator: Validator, document: dict[str, Any]) -> list[str]:
    """One file's errors: a validator that raises is that file's error, never the source's."""
    try:
        return list(validator(document))
    except Exception as exc:  # noqa: BLE001 - one file never makes the source unavailable
        return [f"{type(exc).__name__}: {exc}"]


def _yardstick(directory: Path, validator: Validator | None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    invalid: dict[str, list[str]] = {}
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            invalid[path.name] = [f"could not be read: {exc}"]
            continue
        if not isinstance(document, dict):
            errors = ["a result must be a JSON object"]
        else:
            errors = _validated(validator or yardstick_errors, document)
        if errors:
            invalid[path.name] = errors
        else:
            results.append({"name": path.name, "result": document})
    return {"results": results, "invalid": invalid}


def collect(
    epics_dir: Path | None = None, yardstick_dir: Path | None = None, yardstick_validator: Validator | None = None
) -> dict[str, Any]:
    valid, invalid = records.load_epics(default_epics_dir() if epics_dir is None else Path(epics_dir))
    return {
        "epics": _json_ready(valid),
        "invalid": invalid,
        "yardstick": _yardstick(default_yardstick_dir() if yardstick_dir is None else Path(yardstick_dir), yardstick_validator),
    }
