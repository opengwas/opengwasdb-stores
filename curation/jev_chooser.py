#!/usr/bin/env python3
"""Jev-backed chooser for the choice stage (issue #168).

The choice stage (:mod:`curation.chooser`) turns a candidate shortlist into one
proposal. :mod:`curation.stub_chooser` replays a recorded decision so the stage
is testable hermetically; this module adds a *live* chooser backed by TypeSafe's
Jev structured-decision API.

What "Jev" is here
------------------
Jev is an external, typed decision service. The chooser builds a ``choice``
question whose ``criteria`` are one text option per shortlisted ontology id --
plus a reserved ``none_suitable`` abstention option -- and the model returns one
of those option keys with a calibrated probability for every option. The
chooser maps the returned probabilities back onto the shortlist and passes them
through to :class:`~curation.chooser.ChoiceResult` unmodified (no re-scaling and
no re-calibration). The selected term is the model's explicit ``choice`` or,
when it omits one, the highest-probability option.

The ``none_suitable`` abstention lets the model decline every retrieved
candidate rather than being forced to pick a merely related term. It is carried
through to a proposals row (see :mod:`curation.choice`); a later stage decides
what to do with an abstention.

Enforced constraints
--------------------
Jev exposes at most :data:`MAX_JEV_OPTIONS` (255) options per choice, so with
the reserved abstention option at most :data:`MAX_JEV_CANDIDATES` (254) real
candidates can be represented; a larger shortlist is rejected before any
request is sent. The serialized request is also measured against a byte and a
conservative token budget, bounded by Jev's 64k-token context window. All checks
run at *configuration time* -- while the request is being built, before the
client is called -- and raise :class:`JevConfigurationError` with an actionable
message.

Clients
-------
:class:`JevClient` is the injectable seam. :class:`HttpJevClient` is the live
HTTP/JSON client (``httpx`` is imported lazily, so importing this module never
requires it) and resolves the API key from the explicit argument, then
``TYPESAFE_API_KEY``, then ``key="..."`` in ``~/.typesafe``.
:class:`FixtureJevClient` replays explicit, recorded decisions for hermetic
offline tests with no socket and no non-determinism.

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
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from curation.chooser import (
    NONE_SUITABLE,
    Candidate,
    ChoiceError,
    ChoiceResult,
    Chooser,
)

# Jev's hard option ceiling, including the reserved abstention option.
MAX_JEV_OPTIONS: int = 255

# With ``none_suitable`` occupying one option, a shortlist can expose at most
# this many real candidates.
MAX_JEV_CANDIDATES: int = MAX_JEV_OPTIONS - 1

# The default input budget. Both bounds comfortably fit a full shortlist with
# truncated definitions, while staying under Jev's 64k-token context window.
DEFAULT_MAX_INPUT_BYTES: int = 1_048_576  # 1 MiB of UTF-8 JSON
DEFAULT_MAX_INPUT_TOKENS: int = 60_000

# The hosted TypeSafe endpoint and the pinned model the live client targets.
DEFAULT_JEV_ENDPOINT: str = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL: str = "jev-1.13.0"

# The default HTTP timeout, in seconds. A 255-option Jev request can take a
# while, so this is deliberately generous.
DEFAULT_JEV_TIMEOUT: float = 120.0

# A conservative tokens-per-byte ratio for the budget estimate. It must never
# under-state the size of the payload it guards, so it rounds up.
_BYTES_PER_ESTIMATED_TOKEN: int = 4

# Definition text is truncated to keep a 255-option criteria block bounded.
DEFINITION_MAX_CHARS: int = 200

# The generic state context. It is deliberately not UK-Biobank-specific: the
# caller passes a source description when one applies, and the
# "do not pick a merely related term" rule is kept.
DEFAULT_JEV_CONTEXT: str = (
    "Each trait label is a source field description used as a GWAS phenotype. "
    "Map it to the EFO ontology term that denotes the same trait. Choose "
    "none_suitable if no option denotes the same trait; do not pick a merely "
    "related term."
)

# The question wording verified against the live API. ``trait_label`` is a
# literal placeholder: the actual label is carried in ``state.trait_label``.
DEFAULT_JEV_INSTRUCTIONS: str = "Which option is the EFO term for `trait_label`?"

# The reserved abstention option's criteria text.
NONE_SUITABLE_CRITERION: str = "None of the options denotes this trait"

# Retry policy: exponential backoff, honouring ``retry-after`` when present.
DEFAULT_JEV_MAX_ATTEMPTS: int = 6
DEFAULT_JEV_BASE_DELAY: float = 1.0

# TypeSafe's price: $0.042 per million input tokens (output is free).
DEFAULT_PRICE_PER_MTOK_INPUT: float = 0.042

# Status codes worth retrying: rate limiting, overload, and transient 5xx.
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 529})
# Status codes that must never be retried: a bad key or an invalid body.
NON_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({401, 422})

# The API-key file TypeSafe tools read when no environment key is set.
TYPESAFE_KEY_FILE: str = ".typesafe"
TYPESAFE_ENV_VAR: str = "TYPESAFE_API_KEY"
_TYPESAFE_KEY_RE = re.compile(r'key\s*=\s*"([^"]*)"')


class JevError(ChoiceError):
    """Base error for a Jev request that cannot be built or served."""


class JevConfigurationError(JevError):
    """Raised when a shortlist cannot be represented as a Jev request.

    Signals a hard configuration limit: more candidates than Jev exposes, an
    input payload over the byte/token budget, or a missing API key. It is
    raised while building the request or resolving the key, before any client
    call, so a doomed request is never sent.
    """


class JevResponseError(JevError):
    """Raised when a Jev client returns a response that violates the schema.

    A response naming an option outside the request's criteria, carrying a
    non-numeric or non-finite probability, or missing its probability
    distribution is rejected rather than coerced.
    """


class JevUnavailableError(JevError):
    """Raised when the live Jev service cannot be reached or keeps failing."""


class JevApiError(JevError):
    """Raised for a non-retryable HTTP status from the live Jev service.

    Carries the status code so callers can distinguish a bad key (401) from an
    invalid body (422). It is never retried.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def estimate_tokens(text: str) -> int:
    """A conservative token count for a serialized payload.

    Uses a fixed four-UTF-8-bytes-per-token ratio rounded up, which over-states
    the token count for ordinary English and JSON rather than under-stating it.
    The budget check may therefore reject a payload a specific tokenizer would
    have accepted; it never accepts one a tokenizer would reject.
    """
    if not text:
        return 0
    return (
        len(text.encode("utf-8")) + _BYTES_PER_ESTIMATED_TOKEN - 1
    ) // _BYTES_PER_ESTIMATED_TOKEN


def resolve_api_key(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> str:
    """Resolve the TypeSafe API key in precedence order.

    The explicit argument wins, then the ``TYPESAFE_API_KEY`` environment
    variable, then the ``key="..."`` line of ``~/.typesafe``. The key is never
    echoed in the error: a missing key raises :class:`JevConfigurationError`
    naming only the three sources.
    """
    if explicit and explicit.strip():
        return explicit.strip()

    environ = os.environ if env is None else env
    env_value = environ.get(TYPESAFE_ENV_VAR)
    if env_value and env_value.strip():
        return env_value.strip()

    home_dir = Path.home() if home is None else Path(home)
    key_path = home_dir / TYPESAFE_KEY_FILE
    try:
        text = key_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    match = _TYPESAFE_KEY_RE.search(text)
    if match and match.group(1).strip():
        return match.group(1).strip()

    raise JevConfigurationError(
        "no TypeSafe API key found: pass an explicit key, set the "
        f"{TYPESAFE_ENV_VAR} environment variable, or put "
        f'key="..." in ~/{TYPESAFE_KEY_FILE}'
    )


def _truncate_definition(definition: str) -> str:
    """Collapse whitespace and truncate a definition to ~200 characters."""
    text = " ".join(definition.split())
    if len(text) <= DEFINITION_MAX_CHARS:
        return text
    return text[: DEFINITION_MAX_CHARS - 3].rstrip() + "..."


@dataclass(frozen=True)
class JevOption:
    """One option in a Jev request: a shortlisted candidate plus the reserved
    abstention.

    ``option_id`` is the value that appears in the request's criteria map and
    in the response's probability keys. For a candidate it is the candidate's
    own ``ontology_id``, so an option maps back to a shortlist term by
    identity; for the abstention it is :data:`~curation.chooser.NONE_SUITABLE`.
    """

    option_id: str
    ontology_id: str
    ontology_label: str
    definition: str
    parent_id: str
    parent_label: str
    is_obsolete: bool

    def criteria_text(self) -> str:
        """The compact, deterministic criteria string Jev receives.

        The shape is ``<label> (parent: <parent_label>); <definition>`` with
        the definition truncated to ~200 characters, and an ``[obsolete]``
        marker appended when the term is obsolete.
        """
        label = self.ontology_label.strip() or self.ontology_id
        text = f"{label} (parent: {self.parent_label.strip()})"
        definition = _truncate_definition(self.definition)
        if definition:
            text = f"{text}; {definition}"
        if self.is_obsolete:
            text = f"{text} [obsolete]"
        return text

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
        """The real candidate option identifiers, in shortlist order."""
        return tuple(option.option_id for option in self.options)

    @property
    def response_option_ids(self) -> tuple[str, ...]:
        """The candidate ids plus the reserved abstention option."""
        return self.option_ids + (NONE_SUITABLE,)

    @property
    def request_id(self) -> str:
        """A stable digest of the trait label plus its real candidate enum."""
        digest = hashlib.sha256()
        digest.update(self.trait_label.encode("utf-8"))
        for option_id in self.option_ids:
            digest.update(b"\x00")
            digest.update(option_id.encode("utf-8"))
        return digest.hexdigest()

    @property
    def request_fingerprint(self) -> str:
        """The canonical digest of the request body, including the model id."""
        return compute_request_fingerprint(self.payload)


@dataclass(frozen=True)
class JevResponse:
    """One Jev decision: calibrated probabilities over the request's options."""

    probabilities: Mapping[str, float]
    chosen_option_id: str | None = None
    confidence: float | None = None
    model_version: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class JevCostRecord:
    """The cost of one Jev label decision, for live-run accounting."""

    trait_label: str
    cost_usd: float


class JevClient(ABC):
    """The injectable seam between the chooser and any Jev implementation."""

    #: The answering model id baked into every request body. Subclasses may
    #: override the class default with an instance attribute.
    model: str = DEFAULT_JEV_MODEL
    #: The generic state context baked into every request body.
    context: str = DEFAULT_JEV_CONTEXT

    @abstractmethod
    def decide(self, request: JevRequest) -> JevResponse:
        """Return a decision for ``request`` or raise :class:`JevError`."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


def compute_request_fingerprint(payload: Mapping[str, Any]) -> str:
    """A deterministic sha256 over the canonical request body.

    Keys are sorted and separators are compact so the digest is stable across
    runs and independent of dict insertion order. The API key is never part of
    the body, so it is excluded by construction.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_request_payload(
    trait_label: str,
    options: Sequence[JevOption],
    *,
    model: str = DEFAULT_JEV_MODEL,
    context: str = DEFAULT_JEV_CONTEXT,
) -> dict[str, Any]:
    """Build the TypeSafe ``choice`` request for a shortlist.

    The ``criteria`` map holds one option per candidate (keyed by its ontology
    id) plus the reserved ``none_suitable`` abstention, so a conforming model
    can only return a key from the shortlist or the abstention.
    """
    criteria: dict[str, str] = {
        option.option_id: option.criteria_text() for option in options
    }
    criteria[NONE_SUITABLE] = NONE_SUITABLE_CRITERION
    return {
        "model": model,
        "state": {
            "context": context,
            "trait_label": trait_label,
        },
        "questions": {
            "term": {
                "type": "choice",
                "instructions": DEFAULT_JEV_INSTRUCTIONS,
                "criteria": criteria,
            }
        },
    }


# ---------------------------------------------------------------------------
# Response coercion and fixture client
# ---------------------------------------------------------------------------


def _coerce_probabilities(value: Any) -> dict[str, float]:
    """Coerce a probability field into ``{option_id: probability}``.

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


def _coerce_optional_int(value: Any, field_name: str) -> int | None:
    """Coerce an optional non-negative integer usage field."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise JevResponseError(f"{field_name} must be an integer, got {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise JevResponseError(
            f"{field_name} is not an integer: {value!r}"
        ) from exc
    if number < 0:
        raise JevResponseError(
            f"{field_name} must be non-negative, got {number!r}"
        )
    return number


def _coerce_optional_float(value: Any, field_name: str) -> float | None:
    """Coerce an optional finite, non-negative float field."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise JevResponseError(
            f"{field_name} is not a number: {value!r}"
        ) from exc
    if not math.isfinite(number) or number < 0.0:
        raise JevResponseError(
            f"{field_name} must be finite and non-negative, got {value!r}"
        )
    return number


def coerce_response(raw: Any) -> JevResponse:
    """Coerce a fixture or decoded JSON value into a :class:`JevResponse`.

    The canonical shape is the live TypeSafe response::

        {"model": "jev-1.13.0",
         "answers": {"term": {"type": "choice", "choice": "EFO:...",
                              "confidence": 0.88,
                              "probabilities": {...}}},
         "usage": {"input_tokens": 468, "output_tokens": 81}}

    The shorthand ``{"probabilities": ..., "chosen_option_id": ...,
    "cost_usd": ...}`` and a bare ``{option_id: probability}`` mapping are also
    accepted so recorded fixtures stay terse.
    """
    if isinstance(raw, JevResponse):
        return raw
    if not isinstance(raw, Mapping):
        raise JevResponseError(
            f"Jev response must be an object, got {type(raw).__name__}"
        )

    model_version = str(raw.get("model") or "")
    confidence: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None

    if "answers" in raw:
        answers = raw.get("answers")
        if not isinstance(answers, Mapping):
            raise JevResponseError("Jev response 'answers' must be an object")
        term = answers.get("term")
        if not isinstance(term, Mapping):
            raise JevResponseError(
                "Jev response is missing its 'term' answer object"
            )
        probabilities = _coerce_probabilities(term.get("probabilities", {}))
        chosen = term.get("choice")
        confidence = _coerce_optional_float(term.get("confidence"), "confidence")
        usage = raw.get("usage") or {}
        if not isinstance(usage, Mapping):
            raise JevResponseError("Jev response 'usage' must be an object")
        input_tokens = _coerce_optional_int(
            usage.get("input_tokens"), "input_tokens"
        )
        output_tokens = _coerce_optional_int(
            usage.get("output_tokens"), "output_tokens"
        )
    elif "probabilities" in raw or "chosen_option_id" in raw or "cost_usd" in raw:
        probabilities = _coerce_probabilities(raw.get("probabilities", {}))
        chosen = raw.get("chosen_option_id", raw.get("selected_option_id"))
        confidence = _coerce_optional_float(raw.get("confidence"), "confidence")
        model_version = str(raw.get("model_version") or model_version)
        input_tokens = _coerce_optional_int(
            raw.get("input_tokens"), "input_tokens"
        )
        cost = _coerce_optional_float(raw.get("cost_usd"), "cost_usd")
    else:
        # A bare ``{option_id: probability}`` mapping is accepted for
        # convenience, but the object form is canonical.
        probabilities = _coerce_probabilities(raw)
        chosen = None

    if chosen is not None and not isinstance(chosen, str):
        raise JevResponseError(
            f"chosen option must be a string, got {type(chosen).__name__}"
        )

    return JevResponse(
        probabilities=probabilities,
        chosen_option_id=(
            chosen.strip() if isinstance(chosen, str) and chosen.strip() else None
        ),
        confidence=confidence,
        model_version=model_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
            or "answers" in data
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
        *,
        model: str = DEFAULT_JEV_MODEL,
        context: str = DEFAULT_JEV_CONTEXT,
    ) -> None:
        if isinstance(responses, (str, Path)):
            self._responses = load_fixture(responses)
        else:
            self._responses = parse_fixture(responses)
        self._strict = strict
        self.model = model
        self.context = context
        self.calls: list[JevRequest] = []

    @classmethod
    def from_path(
        cls,
        path: Path | str,
        strict: bool = True,
        *,
        model: str = DEFAULT_JEV_MODEL,
        context: str = DEFAULT_JEV_CONTEXT,
    ) -> "FixtureJevClient":
        """Build a fixture client from a JSON or TSV fixture path."""
        return cls(path, strict=strict, model=model, context=context)

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


def _parse_retry_after(headers: Mapping[str, Any] | None) -> float | None:
    """Parse a ``retry-after`` header's integer/float seconds, if present."""
    if not headers:
        return None
    value: Any = None
    for key, candidate in headers.items():
        if str(key).lower() == "retry-after":
            value = candidate
            break
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        # An HTTP-date retry-after cannot be honoured as a delay without a
        # clock; fall back to the exponential schedule.
        return None
    if not math.isfinite(seconds) or seconds < 0.0:
        return None
    return seconds


def _redact_secret(text: str, headers: Mapping[str, str]) -> str:
    """Remove a bearer token from diagnostic text before it is raised.

    The API key is never part of a request body, but a misbehaving server (or a
    proxy in front of it) could echo an Authorization header. Redacting the
    resolved token here keeps a key-free error diagnosable from a
    ``.error.yaml`` file.
    """
    authorization = headers.get("authorization") or headers.get("Authorization") or ""
    prefix = "Bearer "
    if authorization.startswith(prefix):
        token = authorization[len(prefix):].strip()
        if token:
            text = text.replace(token, "***")
    return text


def _response_body_text(response: Any) -> str:
    """Best-effort response body text for a failed-request diagnostic.

    Handles an ``httpx``-style ``.text`` attribute, then a JSON body, then raw
    bytes; any failure yields an empty string so the diagnostic never masks the
    original error. The body is truncated so a large server error page cannot
    bloat an ``.error.yaml`` file.
    """
    text = ""
    body = getattr(response, "text", None)
    if isinstance(body, str):
        text = body
    if not text:
        try:
            decoded = response.json()
        except Exception:  # noqa: BLE001 - a body may not be JSON at all
            decoded = None
        if decoded is not None:
            try:
                text = json.dumps(decoded, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                text = str(decoded)
    if not text:
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            text = bytes(content).decode("utf-8", errors="replace")
    text = " ".join(text.split())
    if len(text) > 1000:
        text = text[:1000] + "..."
    return text


class HttpJevClient(JevClient):
    """A :class:`JevClient` that calls the hosted TypeSafe Jev endpoint.

    ``httpx`` is imported lazily so importing :mod:`curation.jev_chooser` (and
    running the hermetic test suite) never requires it. ``client_factory`` is
    injectable so the request/response handling can be tested without a socket.
    ``sleep`` is injectable so retry backoff never actually waits in tests.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_JEV_ENDPOINT,
        model: str = DEFAULT_JEV_MODEL,
        api_key: str | None = None,
        timeout: float = DEFAULT_JEV_TIMEOUT,
        context: str = DEFAULT_JEV_CONTEXT,
        *,
        max_attempts: int = DEFAULT_JEV_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_JEV_BASE_DELAY,
        price_per_mtok_input: float = DEFAULT_PRICE_PER_MTOK_INPUT,
        sleep: Callable[[float], None] = time.sleep,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not endpoint:
            raise JevConfigurationError("a Jev endpoint is required")
        if not model:
            raise JevConfigurationError("a Jev model id is required")
        if timeout <= 0:
            raise JevConfigurationError(f"timeout must be positive, got {timeout}")
        if max_attempts < 1:
            raise JevConfigurationError(
                f"max_attempts must be at least 1, got {max_attempts}"
            )
        if base_delay < 0:
            raise JevConfigurationError(
                f"base_delay must be non-negative, got {base_delay}"
            )
        if price_per_mtok_input < 0:
            raise JevConfigurationError(
                f"price_per_mtok_input must be non-negative, got "
                f"{price_per_mtok_input}"
            )
        self._endpoint = endpoint
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._context = context
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._price_per_mtok_input = price_per_mtok_input
        self._sleep = sleep
        self._client_factory = client_factory
        self._resolved_key: str | None = None

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def model(self) -> str:
        return self._model

    @property
    def context(self) -> str:
        return self._context

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def price_per_mtok_input(self) -> float:
        return self._price_per_mtok_input

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

    def _resolve_key(self) -> str:
        if self._resolved_key is None:
            self._resolved_key = resolve_api_key(self._api_key)
        return self._resolved_key

    def _backoff_seconds(self, attempt: int) -> float:
        return self._base_delay * (2 ** attempt)

    def _retry_delay(self, response: Any, attempt: int) -> float:
        retry_after = _parse_retry_after(getattr(response, "headers", None))
        if retry_after is not None:
            return retry_after
        return self._backoff_seconds(attempt)

    def _post_with_retries(
        self,
        client: Any,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> Any:
        endpoint = self._endpoint
        for attempt in range(self._max_attempts):
            last_attempt = attempt == self._max_attempts - 1
            try:
                response = client.post(endpoint, json=body, headers=headers)
            except JevError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalize transport failures
                if last_attempt:
                    raise JevUnavailableError(
                        f"hosted Jev request to {endpoint!r} failed after "
                        f"{self._max_attempts} attempt(s)"
                    ) from exc
                self._sleep(self._backoff_seconds(attempt))
                continue

            status = getattr(response, "status_code", None)
            if status in NON_RETRYABLE_STATUS_CODES:
                body_text = _redact_secret(
                    _response_body_text(response), headers
                )
                detail = f": {body_text}" if body_text else ""
                raise JevApiError(
                    f"hosted Jev request to {endpoint!r} returned "
                    f"non-retryable status {status}{detail}",
                    status_code=status,
                )
            retryable = status in RETRYABLE_STATUS_CODES or (
                isinstance(status, int) and 500 <= status < 600
            )
            if retryable:
                if last_attempt:
                    raise JevUnavailableError(
                        f"hosted Jev request to {endpoint!r} failed after "
                        f"{self._max_attempts} attempt(s) with status {status}"
                    )
                self._sleep(self._retry_delay(response, attempt))
                continue

            try:
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - other non-2xx statuses
                raise JevUnavailableError(
                    f"hosted Jev request to {endpoint!r} failed: {exc}"
                ) from exc
            return response

        # Unreachable: the loop either returns or raises on the last attempt.
        raise JevUnavailableError(
            f"hosted Jev request to {endpoint!r} failed after "
            f"{self._max_attempts} attempt(s)"
        )

    def decide(self, request: JevRequest) -> JevResponse:
        api_key = self._resolve_key()
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        }
        body = request.payload

        client = self._make_client()
        try:
            response = self._post_with_retries(client, body, headers)
            data = response.json()
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        parsed = coerce_response(data)
        if parsed.cost_usd is None and parsed.input_tokens is not None:
            parsed = replace(
                parsed,
                cost_usd=parsed.input_tokens / 1e6 * self._price_per_mtok_input,
            )
        return parsed


# ---------------------------------------------------------------------------
# The chooser
# ---------------------------------------------------------------------------


@dataclass
class JevChooser(Chooser):
    """A :class:`~curation.chooser.Chooser` backed by a Jev decision service.

    ``configure`` builds and validates the typed request (candidate cap, byte
    budget, token budget) before anything is sent; ``select`` calls the client,
    maps the returned option probabilities back to the shortlist (plus the
    reserved abstention), and passes them through to
    :class:`~curation.chooser.ChoiceResult` unmodified.
    """

    client: JevClient
    chooser_id: str = "jev"
    chooser_version: str = "1"
    max_options: int = MAX_JEV_CANDIDATES
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    #: Per-label costs recorded from responses that report one.
    cost_records: list[JevCostRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.max_options < 1:
            raise JevConfigurationError(
                f"max_options must be at least 1, got {self.max_options}"
            )
        if self.max_options > MAX_JEV_CANDIDATES:
            raise JevConfigurationError(
                f"Jev exposes at most {MAX_JEV_OPTIONS} options including the "
                f"{NONE_SUITABLE!r} abstention, so at most "
                f"{MAX_JEV_CANDIDATES} real candidates; "
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

    def estimate_cost_usd(
        self,
        trait_label: str,
        candidates: Sequence[Candidate],
    ) -> float | None:
        """Reserve the request's estimated input-token cost before sending."""
        price = getattr(self.client, "price_per_mtok_input", None)
        if price is None:
            return None
        try:
            request = self.configure(trait_label, list(candidates))
        except JevError:
            # A request that cannot be configured is never sent, so it costs
            # nothing; do not let its reserve block later work.
            return 0.0
        return request.estimated_input_tokens / 1e6 * float(price)

    def configure(
        self,
        trait_label: str,
        candidates: Sequence[Candidate],
    ) -> JevRequest:
        """Build and validate the Jev request for one shortlist.

        Raises :class:`JevConfigurationError` before any client call when the
        shortlist exceeds :data:`MAX_JEV_CANDIDATES`, or when the serialized
        candidate payload exceeds the byte or token budget.
        """
        if len(candidates) > self.max_options:
            raise JevConfigurationError(
                f"Jev can represent at most {self.max_options} real candidates "
                f"plus the {NONE_SUITABLE!r} abstention, but the shortlist for "
                f"{trait_label!r} has {len(candidates)}. Reduce the shortlist "
                f"size (choice: --shortlist-size, candidates: --shortlist-size) "
                f"or split the label before choosing."
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
            if option_id == NONE_SUITABLE:
                raise JevConfigurationError(
                    f"shortlist for {trait_label!r} uses the reserved abstention "
                    f"id {NONE_SUITABLE!r} as a candidate"
                )
            if option_id in seen_ids:
                raise JevConfigurationError(
                    f"shortlist for {trait_label!r} repeats ontology_id "
                    f"{option_id!r}; Jev criteria must be unique"
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

        payload = build_request_payload(
            trait_label,
            options,
            model=self.client.model,
            context=self.client.context,
        )
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

        try:
            probabilities = dict(response.probabilities)
            option_ids = set(request.option_ids)
            allowed_ids = option_ids | {NONE_SUITABLE}
            invented = sorted(set(probabilities) - allowed_ids)
            if invented:
                raise JevResponseError(
                    f"Jev returned probabilities for option(s) outside the "
                    f"shortlist and abstention: {', '.join(invented)}"
                )
            if not probabilities:
                raise JevResponseError(
                    f"Jev returned no probability distribution for {trait_label!r}"
                )

            # The reserved abstention is always an option in the request, so make
            # sure it is present even if a terse fixture omitted it.
            probabilities.setdefault(NONE_SUITABLE, 0.0)

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

            response_option_ids = request.response_option_ids
            if response.chosen_option_id is not None:
                selected = response.chosen_option_id
                if selected not in allowed_ids:
                    raise JevResponseError(
                        f"Jev selected {selected!r}, which is not in the shortlist "
                        f"or the {NONE_SUITABLE!r} abstention"
                    )
            else:
                # Deterministic tie-break: the highest probability, then the
                # shortlist's own order so a flat distribution still resolves.
                selected = max(
                    response_option_ids,
                    key=lambda option_id: (
                        probabilities.get(option_id, 0.0),
                        -response_option_ids.index(option_id),
                    ),
                )

            if response.cost_usd is not None:
                self.cost_records.append(
                    JevCostRecord(trait_label=trait_label, cost_usd=response.cost_usd)
                )
        except JevResponseError as exc:
            # The response was paid for before it was rejected; carry its usage
            # and cost so the round can persist a diagnosable, key-free error
            # file and still account for what was spent.
            exc.raw_response = response.raw
            exc.input_tokens = response.input_tokens
            exc.cost_usd = response.cost_usd
            if response.cost_usd is not None:
                self.cost_records.append(
                    JevCostRecord(trait_label=trait_label, cost_usd=response.cost_usd)
                )
            raise

        # The calibrated distribution is passed through unmodified; the
        # Chooser base class validates its coverage and normalisation.
        return ChoiceResult(
            selected_ontology_id=selected,
            probabilities=probabilities,
            chooser_id=self.chooser_id,
            chooser_version=self.chooser_version,
            model_version=response.model_version,
            model_confidence=response.confidence,
            input_tokens=response.input_tokens,
            cost_usd=response.cost_usd,
            raw_response=response.raw,
            request_fingerprint=request.request_fingerprint,
        )
