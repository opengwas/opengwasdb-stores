"""The epic record: one YAML file per epic, written when the epic finishes (#389).

The contract is `docs/spec/project-site.md`. `validate_epic` returns the errors
of one parsed record, as `ogstores.bundle.check` does for a Release Bundle:
a list of strings, empty when the record is valid. An unknown value is `null`,
never an estimate, so a string that reads as a guess is refused.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml

STATUSES = ("active", "finished")
WORKSTREAMS = (
    "Data architecture", "Build architecture", "Interface", "Store builds",
    "Tool integration", "Analyses", "Engineering",
)
FIELDS = (
    "repo", "number", "title", "status", "workstream", "started", "finished",
    "summary", "prs", "versions", "tickets", "decisions", "test_time",
    "yardstick", "catalog", "followups",
)
TICKET_FIELDS = ("key", "title", "pr", "merge")
DECISION_FIELDS = ("date", "text", "link")
FOLLOWUP_FIELDS = ("key", "text")
TEST_RUN_FIELDS = ("commit", "wall_s", "suites")
SUMMARY_SENTENCES = (2, 4)

REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
KEY = re.compile(r"^[A-Za-z0-9._-]+#[1-9][0-9]*$")
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
CATALOG_VERSION = re.compile(r"^[0-9a-f]{16}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL = re.compile(r"^https?://\S+$")
ESTIMATE = re.compile(r"^(about|approx\.?|approximately|around|circa|est\.?|estimated)\b", re.IGNORECASE)
PLACEHOLDERS = frozenset({"", "unknown", "tbd", "tba", "todo", "n/a", "na", "none", "null", "?", "-"})
HINT = "write null when unknown"


def _is_placeholder(value: str) -> bool:
    text = value.strip()
    return text.lower() in PLACEHOLDERS or text.startswith("~") or bool(ESTIMATE.match(text))


def _is_date(value: object) -> bool:
    if isinstance(value, datetime.datetime):
        return False
    if isinstance(value, datetime.date):
        return True
    if isinstance(value, str) and ISO_DATE.match(value):
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return False


def _text(value: object, name: str, errors: list[str], *, nullable: bool = True) -> None:
    if value is None:
        if not nullable:
            errors.append(f"{name} is required")
    elif not isinstance(value, str):
        errors.append(f"{name} {value!r} is not text")
    elif _is_placeholder(value):
        errors.append(f"{name} {value!r} is a placeholder, not a value ({HINT})")


def _date(value: object, name: str, errors: list[str], *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if not _is_date(value):
        errors.append(f"{name} {value!r} is not an ISO date ({HINT})")


def _key(value: object, name: str, errors: list[str], *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not (isinstance(value, str) and KEY.match(value)):
        errors.append(f"{name} {value!r} is not a repo#n key")


def _pattern(value: object, pattern: re.Pattern[str], name: str, what: str, errors: list[str]) -> None:
    if value is None:
        return
    if not (isinstance(value, str) and pattern.match(value)):
        errors.append(f"{name} {value!r} is not {what}")


def _number(value: object, name: str, errors: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{name} {value!r} is not a number ({HINT})")


def _mapping(value: object, name: str, fields: tuple[str, ...], errors: list[str]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{name} must be a mapping with {', '.join(fields)}")
        return None
    for field in fields:
        if field not in value:
            errors.append(f"{name}.{field} is required ({HINT})")
    for field in value:
        if field not in fields:
            errors.append(f"unknown field {name + '.' + str(field)!r}")
    return value


def _list(value: object, name: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{name} must be a list (empty when there is nothing)")
        return []
    return value


def _test_run(value: object, name: str, errors: list[str]) -> None:
    if value is None:
        return
    run = _mapping(value, name, TEST_RUN_FIELDS, errors)
    if run is None:
        return
    _pattern(run.get("commit"), COMMIT, f"{name}.commit", "a 7 to 40 hex commit", errors)
    _number(run.get("wall_s"), f"{name}.wall_s", errors)
    suites = run.get("suites")
    if suites is not None:
        if not isinstance(suites, Mapping):
            errors.append(f"{name}.suites must be a mapping of suite name to seconds, or null")
        else:
            for suite, seconds in suites.items():
                _number(seconds, f"{name}.suites.{suite}", errors)


def _yardstick_ref(value: object, name: str, errors: list[str]) -> None:
    if value is None:
        return
    ok = isinstance(value, str) and value.strip() != ""
    if ok:
        path = PurePosixPath(value)
        ok = not path.is_absolute() and ".." not in path.parts
    if not ok:
        errors.append(f"{name} {value!r} is not a relative path under docs/_yardstick")


def _summary(value: object, errors: list[str]) -> None:
    _text(value, "summary", errors)
    if not isinstance(value, str) or _is_placeholder(value):
        return
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", value.strip()) if s]
    low, high = SUMMARY_SENTENCES
    if not low <= len(sentences) <= high:
        errors.append(f"summary has {len(sentences)} sentences; write {low} to {high}")


def validate_epic(document: object, filename: str | None = None) -> list[str]:
    """The errors of one parsed epic record; empty when it is valid."""
    if not isinstance(document, Mapping):
        return [f"an epic record must be a mapping, got {type(document).__name__}"]
    errors: list[str] = []
    for field in FIELDS:
        if field not in document:
            errors.append(f"{field} is required ({HINT})")
    for field in document:
        if field not in FIELDS:
            errors.append(f"unknown field {field!r}")

    repo, number = document.get("repo"), document.get("number")
    if "repo" in document and not (isinstance(repo, str) and REPO_NAME.match(repo)):
        errors.append(f"repo {repo!r} is not a repository name")
    if "number" in document and (isinstance(number, bool) or not isinstance(number, int) or number < 1):
        errors.append(f"number {number!r} is not an issue number")
    if filename is not None and isinstance(repo, str) and isinstance(number, int):
        expected = f"{repo}-{number}.yaml"
        if filename != expected:
            errors.append(f"file name {filename!r} should be {expected!r}")

    if "title" in document:
        _text(document["title"], "title", errors, nullable=False)
    status = document.get("status")
    if "status" in document and status not in STATUSES:
        errors.append(f"status {status!r} is not one of {', '.join(STATUSES)}")
    workstream = document.get("workstream")
    if workstream is not None and workstream not in WORKSTREAMS:
        errors.append(f"workstream {workstream!r} is not one of {', '.join(WORKSTREAMS)}")

    dates_ok = True
    for field in ("started", "finished"):
        if field in document:
            before = len(errors)
            _date(document[field], field, errors)
            dates_ok = dates_ok and len(errors) == before
    if dates_ok and "finished" in document:
        if status == "finished" and document["finished"] is None:
            errors.append("a finished epic needs a finished date")
        if status == "active" and document["finished"] is not None:
            errors.append("an active epic has finished: null")
    if "summary" in document:
        _summary(document["summary"], errors)

    if "prs" in document:
        for i, pr in enumerate(_list(document["prs"], "prs", errors)):
            _key(pr, f"prs[{i}]", errors)
    if "versions" in document:
        for i, tag in enumerate(_list(document["versions"], "versions", errors)):
            if not (isinstance(tag, str) and tag.strip() == tag and tag and not re.search(r"\s", tag)):
                errors.append(f"versions[{i}] {tag!r} is not a tag name")
    if "tickets" in document:
        for i, ticket in enumerate(_list(document["tickets"], "tickets", errors)):
            row = _mapping(ticket, f"tickets[{i}]", TICKET_FIELDS, errors)
            if row is None:
                continue
            _key(row.get("key"), f"tickets[{i}].key", errors, nullable=True)
            _text(row.get("title"), f"tickets[{i}].title", errors, nullable=False)
            _key(row.get("pr"), f"tickets[{i}].pr", errors, nullable=True)
            _pattern(row.get("merge"), COMMIT, f"tickets[{i}].merge", "a 7 to 40 hex commit", errors)
    if "decisions" in document:
        for i, decision in enumerate(_list(document["decisions"], "decisions", errors)):
            row = _mapping(decision, f"decisions[{i}]", DECISION_FIELDS, errors)
            if row is None:
                continue
            _date(row.get("date"), f"decisions[{i}].date", errors, nullable=False)
            _text(row.get("text"), f"decisions[{i}].text", errors, nullable=False)
            _pattern(row.get("link"), URL, f"decisions[{i}].link", "an http(s) URL", errors)
    if "test_time" in document:
        runs = _mapping(document["test_time"], "test_time", ("baseline", "final"), errors)
        if runs is not None:
            for name in ("baseline", "final"):
                _test_run(runs.get(name), f"test_time.{name}", errors)
    if "yardstick" in document:
        ys = _mapping(document["yardstick"], "yardstick", ("before", "after"), errors)
        if ys is not None:
            for name in ("before", "after"):
                _yardstick_ref(ys.get(name), f"yardstick.{name}", errors)
    if "catalog" in document:
        cat = _mapping(document["catalog"], "catalog", ("before", "after"), errors)
        if cat is not None:
            for name in ("before", "after"):
                _pattern(cat.get(name), CATALOG_VERSION, f"catalog.{name}", "a 16-hex catalog version", errors)
    if "followups" in document:
        for i, followup in enumerate(_list(document["followups"], "followups", errors)):
            row = _mapping(followup, f"followups[{i}]", FOLLOWUP_FIELDS, errors)
            if row is None:
                continue
            _key(row.get("key"), f"followups[{i}].key", errors, nullable=True)
            _text(row.get("text"), f"followups[{i}].text", errors, nullable=False)
    return errors


def load_epics(directory: Path) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Every `*.yaml` of `directory`: the valid records, and the errors by file name.

    A missing directory has no records. An unreadable or invalid file is
    reported under its name and never stops the others.
    """
    valid: list[dict[str, Any]] = []
    invalid: dict[str, list[str]] = {}
    if not directory.is_dir():
        return valid, invalid
    for path in sorted(directory.glob("*.yaml")):
        try:
            if path.is_symlink():
                raise ValueError("epic records must not be symbolic links")
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            invalid[path.name] = [f"could not be read: {exc}"]
            continue
        errors = validate_epic(document, path.name)
        if errors:
            invalid[path.name] = errors
        else:
            valid.append(document)
    return valid, invalid
