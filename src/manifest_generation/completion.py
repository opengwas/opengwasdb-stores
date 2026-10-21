"""Reference completion: one Store id, one bundle, two phases (ADR 0037, #340).

A Store that is completed against an LD reference panel is published twice
under one id (ADR 0037). Its build phase publishes the Observed-Only Store, so
the id stays queryable while completion runs; its completion phase reads that
published Store, writes the completed one to the id's staged path, and the
replacement transaction swaps them. One Release Bundle declares both, so no
second id is minted and no accepted Observed-Only bundle has to change.

A release config says what that bundle's `complete` block is, and this module is
the one home for the vocabulary: `completion.state`, the reference's declared
status, qualification and activation, the `complete-dense` recipe, and the
Reference Resource whose declaration names the panel and the ancestry it
completes. `manifest_generation.release_config` refuses a config this module
does not accept, and `manifest_generation.candidate_workflow` renders the
bundle from the recipe it accepted, so the two cannot disagree.

What decides whether the bundle completes its own Store:

- a reference that is `qualified` and `active` puts both phases in the Store's
  own bundle, with `completion_state: reference_completed` and no `derived_from`;
- anything short of that -- pending, on hold, blocked -- leaves the
  Observed-Only bundle exactly as a config with no completion recipe emits it,
  and records why: the release notes and the Validation Record's acceptance
  block say the recipe is blocked.

The reference's `declared_status` is the status the Reference Resource's own
`resource.yaml` records (`status: available`), and is kept apart from
`qualification` and `activation`: a declaration is not an owner decision, and
neither this module nor an emitter ever promotes one to the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from manifest_generation.source_inventory import ReferenceResource

__all__ = [
    "ACTIVATIONS",
    "COMPLETE_COMMAND",
    "COMPLETION_REFERENCE_KEYS",
    "CompletionError",
    "CompletionRecipe",
    "LD_PANEL_KIND",
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

#: The completion subcommand a Dense Store is completed with. Store Release
#: Build plans `complete-hybrid` and `complete-ragged` too, but not for a single
#: id: the Observed-View Check that gates the replacement compares no other
#: layout yet (ADR 0037), so a release config may declare only this one.
COMPLETE_COMMAND: str = "complete-dense"

#: The declared reference's keys, exactly. ``declared_status`` is the status the
#: Reference Resource's declaration records; ``qualification`` and ``activation``
#: are the owner's, and are never inferred from it.
COMPLETION_REFERENCE_KEYS: tuple[str, ...] = (
    "resource_id",
    "declared_status",
    "qualification",
    "activation",
)

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


@dataclass(frozen=True)
class CompletionRecipe:
    """A release config's completion recipe, checked against its Reference Resource."""

    #: ``resource_id``, ``declared_status``, ``qualification``, ``activation``.
    reference: Mapping[str, Any]
    command: str
    #: `complete-dense` flags the config sets beside the panel and ancestry.
    options: Mapping[str, Any]
    #: The panel root, as the release config's Reference Resource entry declares
    #: it; preflight checks the panel is present there.
    panel_location: str
    #: The panel's ancestry, from its declaration's ``ancestry``.
    ancestry: str
    #: ``resources/reference-resources/<resource_id>/resource.yaml``.
    declaration: Path

    @property
    def resource_id(self) -> str:
        return str(self.reference["resource_id"])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _block(value: Any, where: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> Mapping:
    """``value`` as a mapping with every required key and no unknown one."""
    if not isinstance(value, Mapping):
        raise CompletionError(
            f"{where} must be a mapping with {', '.join(required)}, not {value!r}"
        )
    unknown = sorted(str(key) for key in value if key not in required and key not in optional)
    if unknown:
        raise CompletionError(
            f"{where} has unknown key(s) {', '.join(unknown)}; it reads only "
            + ", ".join(list(required) + list(optional))
        )
    missing = [key for key in required if value.get(key) is None or value.get(key) == ""]
    if missing:
        raise CompletionError(f"{where} is missing required key(s) {', '.join(missing)}")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompletionError(f"{where} must be text, not {value!r}")
    return value.strip()


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
        "declared_status": _text(reference["declared_status"], f"{where}.declared_status"),
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
    """A release config's ``completion`` block, checked against its Reference Resource.

    Everything the recipe needs beyond the wording is a registry fact: the
    panel's location is the Reference Resource entry the release config declares
    (preflight proves it present), and the panel's ancestry and the directory
    `complete-dense` reads come from that resource's own declaration, so the
    bundle names the panel the repository declares rather than a second copy of
    its facts.
    """
    block = _block(value, where, ("reference", "command"), ("options",))
    reference = completion_reference(block["reference"], f"{where}.reference")
    command = _text(block["command"], f"{where}.command")
    if command != COMPLETE_COMMAND:
        raise CompletionError(
            f"{where}.command {command!r} is not {COMPLETE_COMMAND!r}: a Store that completes its "
            "own id is completed only where the Observed-View Check compares it, which is the "
            "Dense layout (ADR 0037)"
        )
    options = complete_options(block.get("options"), f"{where}.options")

    resource_id = reference["resource_id"]
    resource = resources.get(resource_id)
    if resource is None:
        raise CompletionError(
            f"{where}.reference.resource_id {resource_id!r} is not a declared Reference Resource "
            f"(reference_resources: {', '.join(resources)})"
        )
    if resource.kind != LD_PANEL_KIND:
        raise CompletionError(
            f"{where}.reference.resource_id {resource_id!r} is a {resource.kind!r} resource, "
            f"not a {LD_PANEL_KIND!r} one"
        )

    declaration_path = (
        Path(repo_root) / "resources" / "reference-resources" / resource_id / "resource.yaml"
    )
    declaration = _declaration(declaration_path, where, resource_id)
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
            f"{where}.reference.resource_id {resource_id!r} completes {ancestry!r} Analyses, but "
            f"release_policy.selection_superpopulation is {superpopulation!r}: completion imputes "
            "against a panel of the ancestors the Store holds"
        )
    declared = declaration.get("status")
    if declared is not None and str(declared) != reference["declared_status"]:
        raise CompletionError(
            f"{where}.reference.declared_status is {reference['declared_status']!r}, but "
            f"{declaration_path} declares status {declared!r}: the declared status is the "
            "resource's own, and is recorded rather than assumed"
        )
    if not resource.location:
        raise CompletionError(
            f"{where}.reference.resource_id {resource_id!r} declares no location; the recipe's "
            f"--ld-panel is the panel root the release config declares"
        )
    return CompletionRecipe(
        reference=reference,
        command=command,
        options=options,
        panel_location=str(resource.location),
        ancestry=ancestry,
        declaration=declaration_path,
    )


def _declaration(path: Path, where: str, resource_id: str) -> Mapping:
    if not path.is_file():
        raise CompletionError(
            f"{where}.reference.resource_id {resource_id!r} names no Reference Resource "
            f"declaration: {path} does not exist"
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
    if recipe is None or not reference_is_qualified_and_active(recipe.reference):
        return None
    return {
        "command": recipe.command,
        "options": {
            "ld-panel": recipe.panel_location,
            "ancestry": recipe.ancestry,
            **dict(recipe.options),
        },
    }


def acceptance(recipe: CompletionRecipe) -> dict:
    """The Validation Record's `<acceptance.completion_reference>` evidence.

    Recorded for every declared reference, so an Observed-Only bundle built from
    a blocked recipe says which reference blocked it and why, as #308 recorded
    the dbGaP MVP references and #312 recorded OGS-00009's.
    """
    return {
        "completion_reference": {
            **dict(recipe.reference),
            "blocked": not reference_is_qualified_and_active(recipe.reference),
        }
    }
