"""Reference completion: one Store id, one bundle, two phases (ADR 0037, #340).

A Store that is completed against an LD reference panel is published twice
under one id (ADR 0037). Its build phase publishes the Observed-Only Store, so
the id stays queryable while completion runs; its completion phase reads that
published Store, writes the completed one to the id's staged path, and the
replacement transaction swaps them. One Release Bundle declares both, so no
second id is minted and no accepted Observed-Only bundle has to change.

A release config says what that bundle's `complete` block is, and this module is
the one home for the vocabulary: the reference's resource, qualification and
activation, the `complete-dense` recipe, and the Reference Resource whose
declaration names the panel's status, ancestry and directory. `release_config`
refuses a config this module does not accept, and `candidate_workflow` renders
the bundle from the recipe it accepted, so the two cannot disagree.

What decides whether the bundle completes its own Store:

- a reference that is `qualified` and `active` puts both phases in the Store's
  own bundle, with `completion_state: reference_completed` and no `derived_from`;
- anything short of that -- pending, on hold, blocked -- leaves the
  Observed-Only bundle exactly as a config with no completion recipe emits it,
  and records why: the release notes and the Validation Record's acceptance
  block say the recipe is blocked. Such a reference is never opened, so its
  declaration need not exist and its panel need not be on the host.

The reference's declared status is the status the Reference Resource's own
`resource.yaml` records (`status: available`). It is read from there and
recorded, never restated in the config, and kept apart from `qualification` and
`activation`: a declaration is not an owner decision, and neither this module nor
an emitter ever promotes one to the other.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from manifest_generation.config_schema import block as _block, text as _text
from manifest_generation.source_inventory import ReferenceResource

__all__ = [
    "ACTIVATIONS",
    "COMPLETE_COMMAND",
    "COMPLETION_REFERENCE_KEYS",
    "CompletionError",
    "CompletionRecipe",
    "LD_PANEL_KIND",
    "LDReferencePanel",
    "QUALIFICATIONS",
    "RETIRED_KEYS",
    "acceptance",
    "blocked_reason",
    "complete_options",
    "completion_block",
    "completion_reference",
    "parse_completion",
    "reference_is_qualified_and_active",
]

#: The completion subcommand this pipeline emits. Store Release Build plans
#: `complete-hybrid` and `complete-ragged` too, and ADR 0037's Observed-View
#: Check covers the Dense Component of Hybrid Stores; porting those two is work
#: this ticket does not do, so a release config may declare only this one.
COMPLETE_COMMAND: str = "complete-dense"

#: The declared reference's keys, exactly. Its declared status is the Reference
#: Resource declaration's own ``status``: it is read from there and recorded, and
#: a config that restates it here is refused as an unknown key.
COMPLETION_REFERENCE_KEYS: tuple[str, ...] = ("resource_id", "qualification", "activation")

#: Every value a reference's ``qualification`` may take, and its ``activation``.
QUALIFICATIONS: tuple[str, ...] = ("qualified", "pending", "hold", "not_applicable")
ACTIVATIONS: tuple[str, ...] = ("active", "pending", "blocked")

#: Config keys ADR 0007 used to reserve a second, `derived_from`-linked
#: completed Store id. ADR 0037 completes a Store under its own id, so a config
#: that still declares one is refused rather than ignored: dropping it silently
#: would emit an Observed-Only bundle for a Store its author meant to complete
#: (#308, #312, #340).
RETIRED_KEYS: tuple[str, ...] = ("completed_store_id", "planned_completed_store_id")

#: The kind the completion's Reference Resource declares.
LD_PANEL_KIND: str = "ld_panel"

#: `complete-dense` flags whose values are the Reference Resource's: `--ancestry`
#: names both the panel directory and the Analyses it completes, so a config
#: that also sets one here would either override the declared panel or be
#: overridden by it.
PANEL_OPTIONS: tuple[str, ...] = ("ld-panel", "ancestry")


class CompletionError(ValueError):
    """A completion recipe, its reference or the panel it names is not usable."""


_block = functools.partial(_block, error=CompletionError)
#: This reader has always required a string and stripped it (the release
#: config's own gate coerces numbers and keeps the value as written).
_text = functools.partial(_text, error=CompletionError, numbers=False, strip=True)


@dataclass(frozen=True)
class LDReferencePanel:
    """The LD panel a qualified, active reference completes against, from its declaration."""

    resource_id: str
    #: The declaration's own ``status``, recorded as it is: a declared status is
    #: neither a qualification nor an activation, and is never defaulted.
    status: str
    ancestry: str
    #: The panel root, as the release config's Reference Resource entry declares
    #: it; preflight checks the panel is present there.
    location: str
    #: ``resources/reference-resources/<resource_id>/resource.yaml``.
    declaration: Path


@dataclass(frozen=True)
class CompletionRecipe:
    """A release config's completion recipe.

    ``panel`` is resolved only when the reference is qualified and active:
    a reference short of that is never opened, so its declaration need not exist
    and its panel need not be present (ADR 0037's fallback).
    """

    #: ``resource_id``, ``qualification``, ``activation``.
    reference: Mapping[str, Any]
    command: str
    #: `complete-dense` flags the config sets beside the panel and ancestry.
    options: Mapping[str, Any]
    panel: LDReferencePanel | None = None

    @property
    def resource_id(self) -> str:
        return str(self.reference["resource_id"])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def completion_reference(value: Any, where: str) -> dict:
    """The declared completion reference, checked.

    A reference that is there but malformed is refused rather than read as "not
    qualified": that would emit an Observed-Only bundle for a Store its config
    means to complete. An unknown key is refused by name, so a misspelt
    ``reference`` cannot quietly do the same.
    """
    reference = _block(value, where, COMPLETION_REFERENCE_KEYS)
    for key, allowed in (("qualification", QUALIFICATIONS), ("activation", ACTIVATIONS)):
        if reference[key] not in allowed:
            raise CompletionError(
                f"{where}.{key} {reference[key]!r} is not one of {', '.join(allowed)}"
            )
    return {
        "resource_id": _text(reference["resource_id"], f"{where}.resource_id"),
        "qualification": reference["qualification"],
        "activation": reference["activation"],
    }


def complete_options(value: Any, where: str) -> dict:
    """The `complete-dense` flags beside the declared panel, as `build.options` are beside the build.

    The panel and its ancestry are the Reference Resource's, so a config that
    names either here is refused rather than let one silently override the
    other.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CompletionError(f"{where} must be a mapping of complete-dense flags, not {value!r}")
    declared = sorted(set(PANEL_OPTIONS) & set(value))
    if declared:
        raise CompletionError(
            f"{where} sets {', '.join(declared)}, which the completion reference's Reference "
            "Resource declares; set it there"
        )
    return dict(value)


def reference_is_qualified_and_active(reference: Mapping) -> bool:
    """Whether an owner decision has qualified and activated a completion reference.

    A declared resource is not enough on its own: ``declared_status: available``
    is not a qualification, and both ``qualification`` and ``activation`` must
    say so (#308, #312).
    """
    return reference["qualification"] == "qualified" and reference["activation"] == "active"


def blocked_reason(reference: Mapping) -> str | None:
    """Why a declared reference blocks completion, or ``None`` when it does not."""
    if reference_is_qualified_and_active(reference):
        return None
    return (
        f"completion reference {reference['resource_id']} is qualification "
        f"{reference['qualification']}, activation {reference['activation']}, not qualified "
        "and active"
    )


def parse_completion(
    value: Any,
    *,
    where: str,
    superpopulation: str,
    resources: Mapping[str, ReferenceResource],
    repo_root: Path,
) -> CompletionRecipe:
    """A release config's ``completion`` block: its wording, and the panel it will use.

    The recipe's own keys are checked always. The panel is resolved only when the
    reference is qualified and active -- the one case that will complete
    anything -- because a reference short of that is never opened: its
    declaration need not exist and its panel need not be present on this host,
    which is what lets a blocked recipe emit the Observed-Only bundle (ADR 0037).
    """
    block = _block(value, where, ("reference", "command"), ("options",))
    reference = completion_reference(block["reference"], f"{where}.reference")
    command = _text(block["command"], f"{where}.command")
    if command != COMPLETE_COMMAND:
        raise CompletionError(
            f"{where}.command {command!r} is not {COMPLETE_COMMAND!r}: Dense completion is the "
            "only completion path ported so far. ADR 0037's Observed-View Check also covers the "
            "Dense Component of Hybrid Stores, so Hybrid completion is unported work rather than "
            "a refusal the contract makes"
        )
    options = complete_options(block.get("options"), f"{where}.options")

    panel = None
    if reference_is_qualified_and_active(reference):
        panel = _panel(
            reference["resource_id"],
            where=f"{where}.reference",
            superpopulation=superpopulation,
            resources=resources,
            repo_root=Path(repo_root),
        )
    return CompletionRecipe(reference=reference, command=command, options=options, panel=panel)


def _panel(
    resource_id: str,
    *,
    where: str,
    superpopulation: str,
    resources: Mapping[str, ReferenceResource],
    repo_root: Path,
) -> LDReferencePanel:
    """The declared panel a qualified, active reference completes against.

    The location is the Reference Resource entry the release config declares
    (preflight proves it present); the status, ancestry and the directory
    `complete-dense` reads come from that resource's own declaration, so the
    recipe cannot name a panel other than the one the repository declares.
    """
    resource = resources.get(resource_id)
    if resource is None:
        raise CompletionError(
            f"{where}.resource_id {resource_id!r} is not a declared Reference Resource "
            f"(reference_resources: {', '.join(resources)})"
        )
    if resource.kind != LD_PANEL_KIND:
        raise CompletionError(
            f"{where}.resource_id {resource_id!r} is a {resource.kind!r} resource, "
            f"not a {LD_PANEL_KIND!r} one"
        )
    if not resource.location:
        raise CompletionError(
            f"{where}.resource_id {resource_id!r} declares no location; the recipe's "
            "--ld-panel is the panel root the release config declares"
        )

    declaration_path = Path(repo_root) / "resources" / "reference-resources" / resource_id / "resource.yaml"
    declaration = _declaration(declaration_path, resource_id)
    status = _text(declaration.get("status"), f"{declaration_path}: status")
    ancestry = _text(declaration.get("ancestry"), f"{declaration_path}: ancestry")
    ancestry_dir = declaration.get("ancestry_dir", ancestry)
    # complete-dense joins one --ancestry into both the panel path
    # (<ld-panel>/<ancestry_dir>/<chr>/) and the Analyses' ancestry filter, so
    # the directory must be named for the ancestry (#219: `ukb` could not be).
    if ancestry_dir != ancestry:
        raise CompletionError(
            f"{declaration_path}: ancestry_dir {ancestry_dir!r} is not ancestry {ancestry!r}; "
            f"`opengwasdb {COMPLETE_COMMAND} --ancestry` names both the panel directory and the "
            "Analyses it completes, so no single value would serve"
        )
    if ancestry != superpopulation:
        raise CompletionError(
            f"{where}.resource_id {resource_id!r} completes {ancestry!r} Analyses, but "
            f"release_policy.selection_superpopulation is {superpopulation!r}: completion imputes "
            "against a panel for the Store's Assigned Ancestry"
        )
    return LDReferencePanel(
        resource_id=resource_id,
        status=status,
        ancestry=ancestry,
        location=str(resource.location),
        declaration=declaration_path,
    )


def _declaration(path: Path, resource_id: str) -> Mapping:
    if not path.is_file():
        raise CompletionError(
            f"completion reference {resource_id!r} names no Reference Resource declaration: "
            f"{path} does not exist"
        )
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CompletionError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(document, Mapping) or document.get("kind") != LD_PANEL_KIND:
        raise CompletionError(f"{path}: {resource_id!r} is not a kind: {LD_PANEL_KIND} declaration")
    return document


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def completion_block(recipe: CompletionRecipe | None) -> dict | None:
    """The `complete` block a qualified, active reference declares, else ``None``.

    The panel and ancestry are the Reference Resource's, and the remaining flags
    are the config's, so the block cannot name a panel other than the one the
    release declared and preflight proved present.
    """
    if recipe is None or recipe.panel is None:
        return None
    return {
        "command": recipe.command,
        "options": {
            "ld-panel": recipe.panel.location,
            "ancestry": recipe.panel.ancestry,
            **dict(recipe.options),
        },
    }


def acceptance(recipe: CompletionRecipe) -> dict:
    """The Validation Record's `<acceptance.completion_reference>` evidence.

    Recorded for every declared reference, so an Observed-Only bundle built from
    a blocked recipe says which reference blocked it and why, as #308 recorded
    the dbGaP MVP references and #312 recorded OGS-00009's. The declared status
    appears only when the panel was read: a blocked reference is never opened,
    and its absence is recorded as absence rather than guessed.
    """
    record: dict[str, Any] = {"resource_id": recipe.reference["resource_id"]}
    if recipe.panel is not None:
        record["declared_status"] = recipe.panel.status
    record["qualification"] = recipe.reference["qualification"]
    record["activation"] = recipe.reference["activation"]
    record["blocked"] = recipe.panel is None
    return {"completion_reference": record}
