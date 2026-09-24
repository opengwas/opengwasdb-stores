#!/usr/bin/env python3
"""Fixture-backed stub chooser for the choice stage (issue #167).

The choice stage's job is to turn a candidate shortlist into a proposal. The
model, tool, or process that actually weighs candidates is deliberately outside
this ticket: it is an interface (:mod:`curation.chooser`) plus a stub that
replays pre-recorded decisions. The stub is what makes the whole stage testable
hermetically -- no model, no network, no non-determinism -- so the proposal
table and its invariants can be exercised before any real chooser exists.

A fixture is a mapping from ``trait_label`` to a recorded choice::

    {
      "body mass index": {
        "selected_ontology_id": "EFO:0004340",
        "probabilities": {"EFO:0004340": 0.8, "EFO:0004338": 0.2}
      }
    }

It may be given as an in-memory mapping, a JSON document (the mapping above, or
a list of records each carrying ``trait_label``), or a TSV with columns
``trait_label``, ``selected_ontology_id``, and ``probabilities``. The
``probabilities`` cell is either a JSON object or the compact
``EFO:0004340=0.8,EFO:0004338=0.2`` form used elsewhere in the pipeline.

Replay is deterministic and strict. A candidate the fixture does not mention
gets probability 0.0, so a recorded distribution need only name the terms it
scores. A trait label the fixture does not mention raises
:class:`StubChooserError`: an unrecorded label must fail loudly rather than be
answered with an invented default. Shortlist membership is still enforced by
:meth:`curation.chooser.Chooser.choose`, so a fixture that names a term outside
the shortlist raises :class:`~curation.chooser.SelectionNotInShortlistError`
exactly as a live chooser would.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from curation.chooser import (
    Candidate,
    ChoiceError,
    ChoiceResult,
    Chooser,
)


class StubChooserError(ChoiceError):
    """Raised when a stub fixture is missing, malformed, or incomplete."""


@dataclass(frozen=True)
class RecordedChoice:
    """One pre-recorded decision: the selected term and its distribution."""

    selected_ontology_id: str
    probabilities: dict[str, float]


def _coerce_probabilities(value: Any) -> dict[str, float]:
    """Coerce a fixture probability field into ``{ontology_id: probability}``.

    Accepts a mapping directly, a JSON object string, or the compact
    ``id=probability,id=probability`` form. Anything else is a fixture error.
    """
    if isinstance(value, Mapping):
        raw = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        if text.startswith("{"):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise StubChooserError(
                    f"probabilities is not valid JSON: {exc}"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise StubChooserError(
                    "probabilities JSON must be an object of ontology_id -> number"
                )
            raw = decoded
        else:
            raw = _parse_compact_probabilities(text)
    else:
        raise StubChooserError(
            "probabilities must be an object, a JSON object string, or "
            f"'id=probability,...', got {type(value).__name__}"
        )

    probabilities: dict[str, float] = {}
    for ontology_id, probability in raw.items():
        try:
            probabilities[str(ontology_id)] = float(probability)
        except (TypeError, ValueError) as exc:
            raise StubChooserError(
                f"probability for {ontology_id!r} is not a number: {probability!r}"
            ) from exc
    return probabilities


def _parse_compact_probabilities(text: str) -> dict[str, float]:
    """Parse ``id=probability,id=probability`` into a mapping."""
    probabilities: dict[str, float] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        ontology_id, separator, raw_probability = part.partition("=")
        if not separator:
            raise StubChooserError(
                f"malformed probability entry {part!r}; expected id=probability"
            )
        try:
            probabilities[ontology_id.strip()] = float(raw_probability)
        except ValueError as exc:
            raise StubChooserError(
                f"probability for {ontology_id!r} is not a number: "
                f"{raw_probability!r}"
            ) from exc
    return probabilities


def _coerce_record(trait_label: str, raw: Any) -> RecordedChoice:
    """Coerce one fixture entry into a :class:`RecordedChoice`."""
    if isinstance(raw, RecordedChoice):
        return raw
    if not isinstance(raw, Mapping):
        raise StubChooserError(
            f"fixture entry for {trait_label!r} must be an object, got "
            f"{type(raw).__name__}"
        )
    selected = raw.get("selected_ontology_id", raw.get("ontology_id"))
    if not isinstance(selected, str) or not selected.strip():
        raise StubChooserError(
            f"fixture entry for {trait_label!r} has no selected_ontology_id"
        )
    probabilities = _coerce_probabilities(raw.get("probabilities", {}))
    return RecordedChoice(
        selected_ontology_id=selected.strip(),
        probabilities=probabilities,
    )


def parse_fixture(data: Any) -> dict[str, RecordedChoice]:
    """Normalise a fixture mapping or list of records into recorded choices."""
    choices: dict[str, RecordedChoice] = {}

    if isinstance(data, Mapping):
        # A single record is accepted for convenience, but a mapping keyed by
        # trait label is the canonical form.
        if "trait_label" in data and (
            "selected_ontology_id" in data or "ontology_id" in data
        ):
            label = str(data["trait_label"]).strip()
            if not label:
                raise StubChooserError("fixture record has an empty trait_label")
            choices[label] = _coerce_record(label, data)
            return choices
        items = data.items()
    elif isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        records: list[tuple[str, Any]] = []
        for index, record in enumerate(data):
            if not isinstance(record, Mapping):
                raise StubChooserError(
                    f"fixture record {index} must be an object, got "
                    f"{type(record).__name__}"
                )
            label = str(record.get("trait_label", "")).strip()
            if not label:
                raise StubChooserError(
                    f"fixture record {index} has no trait_label"
                )
            records.append((label, record))
        items = records
    else:
        raise StubChooserError(
            f"fixture must be a mapping or a list of records, got "
            f"{type(data).__name__}"
        )

    for trait_label, raw in items:
        label = str(trait_label).strip()
        if not label:
            raise StubChooserError("fixture has an entry with an empty trait_label")
        choices[label] = _coerce_record(label, raw)
    return choices


def parse_fixture_tsv(text: str) -> dict[str, RecordedChoice]:
    """Parse the TSV fixture form into recorded choices."""
    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    columns = list(reader.fieldnames or [])
    for required in ("trait_label", "selected_ontology_id"):
        if required not in columns:
            raise StubChooserError(
                f"fixture TSV has no {required} column; columns: "
                + (", ".join(columns) or "(none)")
            )
    records: list[dict[str, Any]] = []
    for row_index, row in enumerate(reader):
        label = (row.get("trait_label") or "").strip()
        if not label:
            raise StubChooserError(
                f"fixture TSV row {row_index} has no trait_label"
            )
        records.append(
            {
                "trait_label": label,
                "selected_ontology_id": row.get("selected_ontology_id") or "",
                "probabilities": row.get("probabilities") or "",
            }
        )
    return parse_fixture(records)


def load_fixture(path: Path | str) -> dict[str, RecordedChoice]:
    """Load a JSON or TSV fixture from disk, failing loudly on a bad shape."""
    fixture_path = Path(path)
    if not fixture_path.is_file():
        raise StubChooserError(f"stub fixture does not exist: {fixture_path}")
    text = fixture_path.read_text(encoding="utf-8")

    if fixture_path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StubChooserError(
                f"{fixture_path} is not valid JSON: {exc}"
            ) from exc
        return parse_fixture(data)

    if fixture_path.suffix.lower() in {".tsv", ".txt", ".tab"}:
        return parse_fixture_tsv(text)

    # Unknown extension: try JSON, then TSV, so a fixture without a telling
    # suffix still loads.
    try:
        return parse_fixture(json.loads(text))
    except json.JSONDecodeError:
        return parse_fixture_tsv(text)


class StubChooser(Chooser):
    """A :class:`Chooser` that replays pre-recorded fixture decisions."""

    def __init__(
        self,
        fixture: Mapping[str, Any] | Sequence[Any] | Path | str,
        chooser_id: str = "stub",
        chooser_version: str = "1",
    ) -> None:
        if isinstance(fixture, (str, Path)):
            self._choices = load_fixture(fixture)
        else:
            self._choices = parse_fixture(fixture)
        self.chooser_id = chooser_id
        self.chooser_version = chooser_version

    @classmethod
    def from_path(
        cls,
        path: Path | str,
        chooser_id: str = "stub",
        chooser_version: str = "1",
    ) -> "StubChooser":
        """Build a stub chooser from a JSON or TSV fixture path."""
        return cls(path, chooser_id=chooser_id, chooser_version=chooser_version)

    def recorded_labels(self) -> tuple[str, ...]:
        """The trait labels the fixture has a recorded decision for."""
        return tuple(self._choices)

    def select(
        self,
        trait_label: str,
        candidates: list[Candidate],
    ) -> ChoiceResult:
        recorded = self._choices.get(trait_label)
        if recorded is None:
            raise StubChooserError(
                f"stub fixture has no recorded choice for trait label "
                f"{trait_label!r}"
            )

        # Start from the recorded distribution (so an invented id reaches
        # Chooser.choose's membership check rather than being dropped), then
        # give every unmentioned candidate probability 0.0 so the result
        # covers exactly the shortlist.
        probabilities: dict[str, float] = dict(recorded.probabilities)
        for candidate in candidates:
            probabilities.setdefault(candidate.ontology_id, 0.0)

        return ChoiceResult(
            selected_ontology_id=recorded.selected_ontology_id,
            probabilities=probabilities,
            chooser_id=self.chooser_id,
            chooser_version=self.chooser_version,
        )
