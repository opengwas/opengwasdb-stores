"""Manifest Generation: frozen Source Inventory -> candidate Release Bundle (ADR 0035).

One shared pipeline (ADR 0036) lives at the package root: the candidate
workflow, Source Inventory handling, the resolution receipt and the emitter.
Per-Source-Provider connector code (acquisition, download, metadata adapters
and provider-specific generators) lives in `manifest_generation.providers`.
Inputs (release configs, reviewed TSVs, inventories, Reference Resources) stay
under `resources/`.

Manifest Generation meets Store Release Build (`ogstores`, `workflow/`) only at
the Release Bundle: Store Release Build imports nothing from here.
"""

from pathlib import Path

#: The checkout this package is installed from (editable, pyproject.toml):
#: inputs under `resources/` and the registry under `stores/` resolve here.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# The package only works installed editable from a checkout: a copy installed
# anywhere else would resolve inputs and the registry against the wrong tree.
if not ((REPO_ROOT / "pixi.toml").is_file() and (REPO_ROOT / "stores").is_dir()):
    raise ImportError(
        f"manifest_generation must be installed editable from an opengwasdb-stores "
        f"checkout; {REPO_ROOT} has no pixi.toml and stores/ (installed from {__file__})"
    )
