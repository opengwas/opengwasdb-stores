"""Tiny LD fixtures for the yardstick's LD section (#504), built with the engine's own builders.

The panel is `yardstick.fixture.write_pgen_chromosomes`'s, the writer the registry's
tiny build shares: two chromosomes of 300 variants each. `build_stores` builds it twice
through `opengwasdb.ld`, once as an LD Store (`build`) and once as a Genotype Store
(`build_genotypes`), in about a second. Both carry Partner Rows: no engine builder makes
a Store without them. `record` makes LD Store Registry records for the Stores (written with
`yardstick.fixture.write_ld_records`), and `analyses` a three-Analysis stand-in for #403's 37, in its layout. No host data is read.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from yardstick.fixture import write_pgen_chromosomes

REPO_ROOT = Path(__file__).resolve().parents[2]
CHROMS = (21, 22)
N_SAMPLES = 60
N_VARIANTS = 300
LD_ID, GENOTYPE_ID = "OGL-00006", "OGL-00007"


def build_stores(root: Path) -> dict[str, Path]:
    """The panel as an LD Store and as a Genotype Store: `{ld_store: path, genotype_store: path, panel: dir}`."""
    import opengwasdb.ld as ld
    from opengwasdb.ld.build import build_genotypes

    ids = [f"S{i:03d}" for i in range(N_SAMPLES)]
    panel = root / "panel"
    pgens = write_pgen_chromosomes(panel, CHROMS, ids, N_VARIANTS, np.random.default_rng(504))
    keep = panel / "T.keep"
    keep.write_text("#IID\n" + "".join(f"{s}\n" for s in ids))
    inputs = ld.BuildInputs(
        pgen=tuple(pgens), keep=keep, pcs=None, pc_source=None, panel_id="T", ancestry="EUR",
        genotype_source="synthetic (tests/yardstick/ld_fixture.py)", r_scale=511, t_partner=0.01,
        k=0, k_min=0, mac_min=1, info_min=None,
    )
    stores = {"ld_store": root / "T.store", "genotype_store": root / "T-genotypes.store", "panel": panel}
    ld.build(inputs, stores["ld_store"])
    build_genotypes(inputs, stores["genotype_store"])
    return stores


def record(ld_id: str, kind: str, location: Path, **provenance: Any) -> dict[str, Any]:
    """A valid record of `kind` at `location`: the committed record of the same kind, relocated.

    `provenance` overrides its provenance's fields; a value of None removes the field.
    """
    document = copy.deepcopy(yaml.safe_load((REPO_ROOT / "stores" / "ld" / {"ld_store": LD_ID, "genotype_store": GENOTYPE_ID}[kind] / "record.yaml").read_text()))
    document["ld_id"] = ld_id
    document["location"] = str(location)
    # OGL-00006 is an overlay whose links point into OGL-00001; a fixture Store has no links.
    document["measured"].pop("links", None)
    document.pop("unrecorded_targets", None)
    for key, value in provenance.items():
        if value is None:
            document["provenance"].pop(key, None)
        else:
            document["provenance"][key] = value
    return document


def analyses(root: Path, alids: list[str], count: int = 3) -> Path:
    """`count` Analyses in #403's layout: `<store>__<analysis>/hits.tsv` (alid, z), and the `manifest.json` naming them."""
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(403)
    entries = [{"store": "OGS-00099", "analysis_id": f"fx{i}"} for i in range(count)]
    for i, entry in enumerate(entries):
        picked = alids[i::count]
        z = rng.normal(0, 4, len(picked)) + 6
        directory = root / f"{entry['store']}__{entry['analysis_id']}"
        directory.mkdir()
        (directory / "hits.tsv").write_text(
            "alid\tz\n" + "".join(f"{a}\t{v:.4f}\n" for a, v in zip(picked, z)), encoding="utf-8"
        )
    (root / "manifest.json").write_text(json.dumps({"kb": 3000, "r2": [0.01], "analyses": entries}), encoding="utf-8")
    return root
