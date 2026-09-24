#!/usr/bin/env python3
"""Jev-backed chooser for the choice stage (issue #168).

The choice stage (:mod:`curation.chooser`) turns a candidate shortlist into one
proposal. :mod:`curation.stub_chooser` replays a recorded decision so the stage
is testable hermetically; this module adds a *live* chooser backed by a
Jev-style TypeSafe structured-decision model.

What "Jev" is here
------------------
Jev is treated as an external, typed decision service. The chooser builds a
structured request whose response schema constrains the answer to a JSON enum
of the shortlist's own ontology identifiers, so the model can only ever return
one of the candidates it was handed -- it cannot invent a term candidate
generation never retrieved. The service returns calibrated probabilities over
that enum; the chooser passes those probabilities through to
:class:`~curation.chooser.ChoiceResult` unmodified (no re-scaling and no
re-calibration), and the selected term is the model's explicit choice or, when
it omits one, the highest-probability option.

Enforced constraints
--------------------
Jev caps an enum at ``MAX_JEV_OPTIONS`` (255) options, so a shortlist larger
than that cannot be represented and is rejected before any request is sent.
Jev also bounds the *input* it accepts, so the serialized candidate payload is
measured in both bytes and a conservative token estimate and rejected when it
exceeds the configured budget. All three checks run at *configuration time* --
while the request is being built, before the client is called -- and raise
:class:`JevConfigurationError` with an actionable message.

Clients
-------
:class:`JevClient` is the injectable seam. :class:`HttpJevClient` is the live
HTTP/JSON client (``httpx`` is imported lazily, so importing this module never
requires it); :class:`FixtureJevClient` replays explicit, recorded decisions
for hermetic offline tests with no socket and no non-determinism.

CLI
---
The chooser is registered with :mod:`curation.choice`
(``--chooser jev``); see that module's ``--jev-*`` flags.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from curation.chooser import (
    Candidate,
    ChoiceError,
    ChoiceResult,
    Chooser,
)

# Jev's hard enum-size ceiling. A structured response cannot expose more than
# 255 options, so a shortlist larger than this is impossible to represent.
MAX_JEV_OPTIONS: int = 255

# The default input budget. Both bounds are deliberately generous enough for a
# full 255-option shortlist with definitions and evidence, while still refusing
# an unbounded payload.
DEFAULT_MAX_INPUT_BYTES: int = 1_048_576  # 1 MiB of UTF-8 JSON
DEFAULT_MAX_INPUT_TOKENS: int = 262_144  # 256 Ki tokens (estimate)

# The hosted model the live client targets unless overridden.
DEFAULT_JEV_MODEL: str = "jev-typesafe-v1"

# The default HTTP timeout, in seconds.
DEFAULT_JEV_TIMEOUT: float = 60.0

# A conservative tokens-per-byte ratio for the budget estimate. It must never
# under-state the size of the payload it guards, so it rounds up.
_BYTES_PER_ESTIMATED_TOKEN: int = 4


class JevError(ChoiceError):
    """Base error for a Jev request that cannot be built or served."""


class JevConfigurationError(JevError):
    """Raised when a shortlist cannot be represented as a Jev request.

    Signals a hard configuration limit: more options than Jev exposes, or an
    input payload over the byte/token budget. It is raised while building the
    request, before any client call, so a doomed request is never sent.
    """


class JevResponseError(JevError):
    """Raised when a Jev client returns a response that violates the schema.

    A response naming an option outside the request's enum, carrying a
    non-numeric or non-finite probability, or missing its probability
    distribution is rejected rather than coerced.
    """


class JevUnavailableError(JevError):
    """Raised when the live Jev service cannot be reached or fails."""


def estimate_tokens(text: str) -> int:
    """A conservative token count for a serialized payload.

    Uses a fixed four-UTF-8-bytes-per-token ratio rounded up, which over-states
    the token count for ordinary English and JSON rather than under-stating it.
    The budget check may therefore reject a payload a specific tokenizer would
    have accepted; it never accepts one a tokenizer would reject.
    """
    if not text:
        return 0
    return (len(text.encode("utf-8")) + _BYTES_PER_ESTIMATED_TOKEN - 1) // _BYTES_PER_ESTIMATED_TOKEN


@dataclass(frozen=True)
class JevOption:
    """One enum option in a Jev request: a shortlisted candidate.

    ``option_id`` is the value that appears in the request's JSON enum and in
    the response's probability keys. It is the candidate's own
    ``ontology_id``, so an option maps back to a shortlist term by identity.
    """

    option_id: str
    ontology_id: str
    ontology_label: str
    definition: str
    parent_id: str
    parent_label: str
    is_obsolete: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.option_id,
            "label": self.ontology_label,
            "definition": self.definition,
            "parent_id": self.parent_id,
            "parent_label": self.parent_label,
            "is_obsolete": self.is_obsolete,
        }


@dataclass(frozen=True)
class JevRequest:
    """A validated, ready-to-send Jev decision request."""

    trait_label: str
    options: tuple[JevOption, ...]
    payload: Mapping[str, Any]
    payload_bytes: int
    estimated_input_tokens: int

    @property
    def option_ids(self) -> tuple[str, ...]:
        """The enum's option identifiers, in shortlist order."""
        return tuple(option.option_id for option in self.options)

    @property
    def request_id(self) -> str:
        """A stable digest of the trait label plus its enum, for fixture lookup."""
        digest = hashlib.sha256()
        digest.update(self.trait_label.encode("utf-8"))
        for option_id in self.option_ids:
            digest.update(b"\x00")
            digest.update(option_id.encode("utf-8"))
        return digest.hexdigest()


@dataclass(frozen=True)
class JevResponse:
    """One Jev decision: calibrated probabilities over the request's enum."""

    probabilities: Mapping[str, float]
    chosen_option_id: str | None = None
    cost_usd: float | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class JevCostRecord:
    """The cost of one Jev label decision, for live-run accounting."""

    trait_label: str
    cost_usd: float


class JevClient(ABC):
    """The injectable seam between the chooser and any Jev implementation."""

    @abstractmethod
    def decide(self, request: JevRequest) -> JevResponse:
        """Return a decision for ``request`` or raise :class:`JevError`."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Response coercion and fixture client
# ---------------------------------------------------------------------------


def _coerce_probabilities(value: Any) -> dict[str, float]:
    """Coerce a fixture probability field into ``{option_id: probability}``.

    Accepts a mapping directly, a JSON object string, or the compact
    ``id=probability,id=probability`` form. Anything else is a fixture error.
    """
    if isinstance(value, Mapping):
        raw: Mapping[Any, Any] = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        if text.startswith("{"):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise JevResponseError(
                    f"probabilities is not valid JSON: {exc}"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise JevResponseError(
                    "probabilities JSON must be an object of option_id -> number"
                )
            raw = decoded
        else:
            return _parse_compact_probabilities(text)
    else:
        raise JevResponseError(
            "probabilities must be an object, a JSON object string, or "
            f"'id=probability,...', got {type(value).__name__}"
        )

    probabilities: dict[str, float] = {}
    for option_id, probability in raw.items():
        try:
            probabilities[str(option_id)] = float(probability)
        except (TypeError, ValueError) as exc:
            raise JevResponseError(
                f"probability for {option_id!r} is not a number: {probability!r}"
            ) from exc
    return probabilities


def _parse_compact_probabilities(text: str) -> dict[str, float]:
    """Parse ``id=probability,id=probability`` into a mapping."""
    probabilities: dict[str, float] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        option_id, separator, raw_probability = part.partition("=")
        if not separator:
            raise JevResponseError(
                f"malformed probability entry {part!r}; expected id=probability"
            )
        try:
            probabilities[option_id.strip()] = float(raw_probability)
        except ValueError as exc:
            raise JevResponseError(
                f"probability for {option_id!r} is not a number: "
                f"{raw_probability!r}"
            ) from exc
    return probabilities


def coerce_response(raw: Any) -> JevResponse:
    """Coerce a fixture or decoded JSON value into a :class:`JevResponse`."""
    if isinstance(raw, JevResponse):
        return raw
    if not isinstance(raw, Mapping):
        raise JevResponseError(
            f"Jev response must be an object, got {type(raw).__name__}"
        )

    if "probabilities" in raw or "chosen_option_id" in raw or "cost_usd" in raw:
        probabilities = _coerce_probabilities(raw.get("probabilities", {}))
        chosen = raw.get("chosen_option_id", raw.get("selected_option_id"))
        cost = raw.get("cost_usd")
    else:
        # A bare ``{option_id: probability}`` mapping is accepted for
        # convenience, but the object form is canonical.
        probabilities = _coerce_probabilities(raw)
        chosen = None
        cost = None

    if chosen is not None and not isinstance(chosen, str):
        raise JevResponseError(
            f"chosen_option_id must be a string, got {type(chosen).__name__}"
        )
    if cost is not None:
        try:
            cost = float(cost)
        except (TypeError, ValueError) as exc:
            raise JevResponseError(f"cost_usd is not a number: {cost!r}") from exc
        if not math.isfinite(cost) or cost < 0.0:
            raise JevResponseError(
                f"cost_usd must be finite and non-negative, got {cost!r}"
            )

    return JevResponse(
        probabilities=probabilities,
        chosen_option_id=chosen.strip() if isinstance(chosen, str) and chosen.strip() else None,
        cost_usd=cost,
        raw=dict(raw),
    )


def parse_fixture(data: Any) -> dict[str, JevResponse]:
    """Normalise a JSON fixture mapping or list of records into responses.

    The canonical form is keyed by ``trait_label``; a list of records each
    carrying ``trait_label`` is also accepted. A record may be a full response
    object or a bare ``{option_id: probability}`` mapping.
    """
    responses: dict[str, JevResponse] = {}

    if isinstance(data, Mapping):
        if "trait_label" in data and (
            "probabilities" in data
            or "chosen_option_id" in data
            or "cost_usd" in data
        ):
            label = str(data["trait_label"]).strip()
            if not label:
                raise JevResponseError("fixture record has an empty trait_label")
            responses[label] = coerce_response(data)
            return responses
        items: Sequence[tuple[str, Any]] = list(data.items())
    elif isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        records: list[tuple[str, Any]] = []
        for index, record in enumerate(data):
            if not isinstance(record, Mapping):
                raise JevResponseError(
                    f"fixture record {index} must be an object, got "
                    f"{type(record).__name__}"
                )
            label = str(record.get("trait_label", "")).strip()
            if not label:
                raise JevResponseError(f"fixture record {index} has no trait_label")
            body = {key: value for key, value in record.items() if key != "trait_label"}
            records.append((label, body))
        items = records
    else:
        raise JevResponseError(
            f"fixture must be a mapping or a list of records, got "
            f"{type(data).__name__}"
        )

    for trait_label, raw in items:
        label = str(trait_label).strip()
        if not label:
            raise JevResponseError("fixture has an entry with an empty trait_label")
        responses[label] = coerce_response(raw)
    return responses


def parse_fixture_tsv(text: str) -> dict[str, JevResponse]:
    """Parse the TSV fixture form into responses."""
    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    columns = list(reader.fieldnames or [])
    if "trait_label" not in columns:
        raise JevResponseError(
            "fixture TSV has no trait_label column; columns: "
            + (", ".join(columns) or "(none)")
        )
    if "probabilities" not in columns:
        raise JevResponseError(
            "fixture TSV has no probabilities column; columns: "
            + (", ".join(columns) or "(none)")
        )
    records: list[dict[str, Any]] = []
    for row_index, row in enumerate(reader):
        label = (row.get("trait_label") or "").strip()
        if not label:
            raise JevResponseError(f"fixture TSV row {row_index} has no trait_label")
        record: dict[str, Any] = {
            "trait_label": label,
            "probabilities": row.get("probabilities") or "",
        }
        if row.get("chosen_option_id"):
            record["chosen_option_id"] = row["chosen_option_id"]
        if row.get("cost_usd"):
            record["cost_usd"] = row["cost_usd"]
        records.append(record)
    return parse_fixture(records)


def load_fixture(path: Path | str) -> dict[str, JevResponse]:
    """Load a JSON or TSV Jev fixture from disk, failing loudly on a bad shape."""
    fixture_path = Path(path)
    if not fixture_path.is_file():
        raise JevResponseError(f"Jev fixture does not exist: {fixture_path}")
    text = fixture_path.read_text(encoding="utf-8")

    if fixture_path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JevResponseError(
                f"{fixture_path} is not valid JSON: {exc}"
            ) from exc
        return parse_fixture(data)

    if fixture_path.suffix.lower() in {".tsv", ".txt", ".tab"}:
        return parse_fixture_tsv(text)

    try:
        return parse_fixture(json.loads(text))
    except json.JSONDecodeError:
        return parse_fixture_tsv(text)


class FixtureJevClient(JevClient):
    """A :class:`JevClient` that replays recorded decisions.

    Fixtures are keyed by ``trait_label`` (the canonical form) or by the
    request's stable ``request_id`` digest. A label the fixture does not name
    fails loudly by default rather than returning an invented default; an
    unrecorded call is always visible via :attr:`calls`.
    """

    def __init__(
        self,
        responses: Mapping[str, Any] | Sequence[Any] | Path | str,
        strict: bool = True,
    ) -> None:
        if isinstance(responses, (str, Path)):
            self._responses = load_fixture(responses)
        else:
            self._responses = parse_fixture(responses)
        self._strict = strict
        self.calls: list[JevRequest] = []

    @classmethod
    def from_path(cls, path: Path | str, strict: bool = True) -> "FixtureJevClient":
        """Build a fixture client from a JSON or TSV fixture path."""
        return cls(path, strict=strict)

    def recorded_labels(self) -> tuple[str, ...]:
        """The trait labels the fixture has a recorded decision for."""
        return tuple(self._responses)

    def decide(self, request: JevRequest) -> JevResponse:
        self.calls.append(request)
        recorded = self._responses.get(request.trait_label)
        if recorded is None:
            recorded = self._responses.get(request.request_id)
        if recorded is None:
            if self._strict:
                raise JevResponseError(
                    f"Jev fixture has no recorded decision for trait label "
                    f"{request.trait_label!r}"
                )
            # Non-strict mode still refuses to invent a selection: it returns
            # an empty distribution, which the chooser rejects as malformed.
            return JevResponse(probabilities={})
        return recorded


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class HttpJevClient(JevClient):
    """A :class:`JevClient` that calls a hosted Jev HTTP/JSON endpoint.

    ``httpx`` is imported lazily so importing :mod:`curation.jev_chooser` (and
    running the hermetic test suite) never requires it. ``client_factory`` is
    injectable so the request/response handling can be tested without a socket.
    """

    def __init__(
        self,
        endpoint: str,
        model: str = DEFAULT_JEV_MODEL,
        api_key: str | None = None,
        timeout: float = DEFAULT_JEV_TIMEOUT,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not endpoint:
            raise JevConfigurationError("a Jev endpoint is required")
        if not model:
            raise JevConfigurationError("a Jev model id is required")
        self._endpoint = endpoint
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._client_factory = client_factory

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def model(self) -> str:
        return self._model

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            import httpx  # noqa: PLC0415 - lazy so the stdlib path needs no httpx
        except ImportError as exc:  # pragma: no cover - httpx ships in curation env
            raise JevUnavailableError(
                "httpx is required for the hosted Jev client"
            ) from exc
        return httpx.Client(timeout=self._timeout)

    def decide(self, request: JevRequest) -> JevResponse:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        body = dict(request.payload)
        body["model"] = self._model

        client = self._make_client()
        try:
            response = client.post(self._endpoint, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
        except JevError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize any client failure
            raise JevUnavailableError(
                f"hosted Jev request to {self._endpoint!r} failed: {exc}"
            ) from exc
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        return coerce_response(data)


# ---------------------------------------------------------------------------
# The chooser
# ---------------------------------------------------------------------------


@dataclass
class JevChooser(Chooser):
    """A :class:`~curation.chooser.Chooser` backed by a Jev decision service.

    ``configure`` builds and validates the typed request (enum size, byte
    budget, token budget) before anything is sent; ``select`` calls the client,
    maps the returned enum probabilities back to the shortlist, and passes them
    through to :class:`~curation.chooser.ChoiceResult` unmodified.
    """

    client: JevClient
    chooser_id: str = "jev"
    chooser_version: str = "1"
    max_options: int = MAX_JEV_OPTIONS
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    #: Per-label costs recorded from responses that report one.
    cost_records: list[JevCostRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.max_options < 1:
            raise JevConfigurationError(
                f"max_options must be at least 1, got {self.max_options}"
            )
        if self.max_options > MAX_JEV_OPTIONS:
            raise JevConfigurationError(
                f"Jev caps options at {MAX_JEV_OPTIONS} candidates; "
                f"max_options={self.max_options} cannot be represented"
            )
        if self.max_input_bytes < 1:
            raise JevConfigurationError(
                f"max_input_bytes must be positive, got {self.max_input_bytes}"
            )
        if self.max_input_tokens < 1:
            raise JevConfigurationError(
                f"max_input_tokens must be positive, got {self.max_input_tokens}"
            )

    @property
    def total_cost_usd(self) -> float:
        """Total recorded spend across every label this chooser has decided."""
        return math.fsum(record.cost_usd for record in self.cost_records)

    def configure(
        self,
        trait_label: str,
        candidates: Sequence[Candidate],
    ) -> JevRequest:
        """Build and validate the typed Jev request for one shortlist.

        Raises :class:`JevConfigurationError` before any client call when the
        shortlist exceeds :data:`MAX_JEV_OPTIONS`, or when the serialized
        candidate payload exceeds the byte or token budget.
        """
        if len(candidates) > self.max_options:
            raise JevConfigurationError(
                f"Jev caps options at {self.max_options} candidates, but the "
                f"shortlist for {trait_label!r} has {len(candidates)}. Reduce "
                f"the shortlist size (choice: --shortlist-size, candidates: "
                f"--shortlist-size) or split the label before choosing."
            )

        options: list[JevOption] = []
        seen_ids: set[str] = set()
        for candidate in candidates:
            option_id = (candidate.ontology_id or "").strip()
            if not option_id:
                raise JevConfigurationError(
                    f"shortlist for {trait_label!r} contains a candidate with "
                    "no ontology_id; Jev needs an identifier for every option"
                )
            if option_id in seen_ids:
                raise JevConfigurationError(
                    f"shortlist for {trait_label!r} repeats ontology_id "
                    f"{option_id!r}; Jev enum options must be unique"
                )
            seen_ids.add(option_id)
            options.append(
                JevOption(
                    option_id=option_id,
                    ontology_id=option_id,
                    ontology_label=candidate.ontology_label,
                    definition=candidate.definition,
                    parent_id=candidate.parent_id,
                    parent_label=candidate.parent_label,
                    is_obsolete=candidate.is_obsolete,
                )
            )

        option_ids = [option.option_id for option in options]
        payload = build_request_payload(trait_label, options, option_ids)
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        payload_bytes = len(serialized.encode("utf-8"))
        estimated_tokens = estimate_tokens(serialized)

        if payload_bytes > self.max_input_bytes:
            raise JevConfigurationError(
                f"Jev input budget exceeded for {trait_label!r}: payload is "
                f"{payload_bytes} bytes but the budget is {self.max_input_bytes} "
                f"bytes. Reduce the shortlist size or shorten candidate evidence."
            )
        if estimated_tokens > self.max_input_tokens:
            raise JevConfigurationError(
                f"Jev input token budget exceeded for {trait_label!r}: payload "
                f"is an estimated {estimated_tokens} tokens but the budget is "
                f"{self.max_input_tokens}. Reduce the shortlist size or shorten "
                f"candidate evidence."
            )

        return JevRequest(
            trait_label=trait_label,
            options=tuple(options),
            payload=payload,
            payload_bytes=payload_bytes,
            estimated_input_tokens=estimated_tokens,
        )

    def select(
        self,
        trait_label: str,
        candidates: list[Candidate],
    ) -> ChoiceResult:
        """Ask Jev to decide among ``candidates`` and return the proposal."""
        request = self.configure(trait_label, candidates)
        response = self.client.decide(request)

        probabilities = dict(response.probabilities)
        option_ids = set(request.option_ids)
        invented = sorted(set(probabilities) - option_ids)
        if invented:
            raise JevResponseError(
                f"Jev returned probabilities for option(s) outside the shortlist "
                f"enum: {', '.join(invented)}"
            )
        if not probabilities:
            raise JevResponseError(
                f"Jev returned no probability distribution for {trait_label!r}"
            )

        for option_id, probability in probabilities.items():
            if isinstance(probability, bool) or not isinstance(probability, (int, float)):
                raise JevResponseError(
                    f"Jev probability for {option_id!r} is not a number: "
                    f"{probability!r}"
                )
            if not math.isfinite(probability) or probability < 0.0:
                raise JevResponseError(
                    f"Jev probability for {option_id!r} must be finite and "
                    f"non-negative, got {probability!r}"
                )

        if response.chosen_option_id is not None:
            selected = response.chosen_option_id
            if selected not in option_ids:
                raise JevResponseError(
                    f"Jev selected {selected!r}, which is not in the shortlist enum"
                )
        else:
            # Deterministic tie-break: the highest probability, then the
            # shortlist's own order so a flat distribution still resolves.
            selected = max(
                request.option_ids,
                key=lambda option_id: (
                    probabilities.get(option_id, 0.0),
                    -request.option_ids.index(option_id),
                ),
            )

        if response.cost_usd is not None:
            self.cost_records.append(
                JevCostRecord(trait_label=trait_label, cost_usd=response.cost_usd)
            )

        # The calibrated distribution is passed through unmodified; the
        # Chooser base class validates its coverage and normalisation.
        return ChoiceResult(
            selected_ontology_id=selected,
            probabilities=probabilities,
            chooser_id=self.chooser_id,
            chooser_version=self.chooser_version,
        )


def build_request_payload(
    trait_label: str,
    options: Sequence[JevOption],
    option_ids: Sequence[str],
) -> dict[str, Any]:
    """Build the TypeSafe structured-decision payload for a shortlist.

    The response schema constrains ``probabilities`` to exactly the enum's
    options and ``chosen_option_id`` to the same enum, so a conforming model
    can only return an identifier from the shortlist.
    """
    probability_properties = {
        option_id: {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        }
        for option_id in option_ids
    }
    return {
        "task": "trait_ontology_mapping_choice",
        "trait_label": trait_label,
        "instruction": (
            "Choose the single ontology term that best matches the trait label "
            "and return a calibrated probability for every option."
        ),
        "options": [option.to_payload() for option in options],
        "response_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["probabilities"],
            "properties": {
                "probabilities": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(option_ids),
                    "properties": probability_properties,
                },
                "chosen_option_id": {
                    "type": "string",
                    "enum": list(option_ids),
                },
            },
        },
    }
