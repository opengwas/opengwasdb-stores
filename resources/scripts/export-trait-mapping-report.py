#!/usr/bin/env python3
"""Export the data behind the Canonical Trait Mapping report (issue #161).

Reads two finished curation rounds (a validation round over a harvested
source-provided set, and a production round) plus the pinned ontology index,
and writes small per-label tables and a summary JSON under docs/data/ for
resources/scripts/trait-mapping-report.qmd. Round directories are gitignored,
so the exports are what the report (and GitHub Pages) can depend on.

    pixi run -e curation python resources/scripts/export-trait-mapping-report.py \
        --index .cache/curation/efo-v3.94.0.index.json \
        --validation .cache/curation/validation/ogs-00011.tsv \
        --validation-round .cache/curation/rounds/validation-ogs-00011 \
        --production-round .cache/curation/rounds/ukb-b-2026q3 \
        --out-dir docs/data
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from curation.gap_scan import normalize_trait_label  # noqa: E402
from curation.ontology import load_index  # noqa: E402

NONE_SUITABLE = "none_suitable"


def round_pins(round_dir: Path) -> dict:
    """The publishable pins of a round: no local paths."""
    config = yaml.safe_load((round_dir / "round.yaml").read_text(encoding="utf-8"))
    embeddings = (config["ontology"] or {}).get("embeddings") or {}
    return {
        "round_id": config["round_id"],
        "created_at": config["created_at"],
        "ontology_release": config["ontology"]["release"],
        "embedding_model": embeddings.get("model_id", ""),
        "embedding_build_id": embeddings.get("build_id", ""),
        "chooser": {k: config["chooser"][k] for k in ("id", "version", "model", "context")},
        "shortlist_size": config["shortlist_size"],
        "thresholds": config["thresholds"],
    }


def read_tsv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {path}")


def read_choices(round_dir: Path) -> dict[str, dict]:
    choices = {}
    for path in (round_dir / "choices").rglob("*.yaml"):
        if path.name.endswith(".error.yaml"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        choices[data["normalised_label"]] = data
    return choices


def read_shortlists(round_dir: Path) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for row in read_tsv(round_dir / "shortlists.tsv"):
        grouped[normalize_trait_label(row["trait_label"])].append(row)
    return grouped


def top_two(probabilities: dict[str, float]) -> tuple[float, float]:
    ranked = sorted(probabilities.values(), reverse=True)
    return ranked[0], ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)


def ancestors(ontology_id: str, by_id: dict, depth: int = 8) -> list[str]:
    chain, current = [], ontology_id
    for _ in range(depth):
        term = by_id.get(current)
        if term is None or not term.parent_id or term.parent_id in chain:
            break
        chain.append(term.parent_id)
        current = term.parent_id
    return chain


def relation(selected: str, truth: str, by_id: dict) -> str:
    """How a disagreeing pick relates to the source term in the is-a tree."""
    if truth in ancestors(selected, by_id):
        return "more specific"
    if selected in ancestors(truth, by_id):
        return "more general"
    sel, tru = by_id.get(selected), by_id.get(truth)
    if sel and tru and sel.parent_id and sel.parent_id == tru.parent_id:
        return "sibling"
    return "unrelated in is-a tree"


def export_validation(args, by_id) -> tuple[list[dict], dict]:
    truth: dict[str, set[str]] = collections.defaultdict(set)
    meta: dict[str, dict[str, str]] = {}
    for row in read_tsv(args.validation):
        if row["is_obsolete"] == "true" and not row["equivalent_ids"]:
            continue
        key = normalize_trait_label(row["trait_label"])
        truth[key] |= {row["ontology_id"], *filter(None, row["equivalent_ids"].split(","))}
        meta.setdefault(key, row)

    choices = read_choices(args.validation_round)
    shortlists = read_shortlists(args.validation_round)
    rows = []
    for key, data in sorted(choices.items()):
        if key not in truth:
            continue
        ids = truth[key]
        offered = shortlists.get(key, [])
        truth_rank = next(
            (int(c["shortlist_rank"]) for c in offered if c["ontology_id"] in ids), None
        )
        selected = data["selected_ontology_id"]
        probs = data["probabilities"]
        confidence = probs.get(selected, 0.0)
        _, margin = top_two(probs)
        correct = selected in ids
        primary = meta[key]["ontology_id"]
        sel_term = by_id.get(selected)
        rows.append(
            {
                "trait_label": data["trait_label"],
                "stratum": meta[key]["stratum"],
                "source_id": primary,
                "source_label": by_id[primary].label if primary in by_id else meta[key]["ontology_label"],
                "source_rank": truth_rank if truth_rank is not None else "",
                "selected_id": selected,
                "selected_label": sel_term.label if sel_term else "",
                "confidence": round(confidence, 4),
                "margin": round(margin, 4),
                "correct": correct,
                "abstained": selected == NONE_SUITABLE,
                "relation": (
                    ""
                    if correct or selected == NONE_SUITABLE
                    else relation(selected, primary, by_id)
                ),
                "input_tokens": data.get("input_tokens") or 0,
                "cost_usd": data.get("cost_usd") or 0.0,
            }
        )
    summary = {
        "labels": len(rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "round": round_pins(args.validation_round),
    }
    return rows, summary


def ukb_field(label: str) -> str:
    """The UK Biobank field family a ukb-b trait label belongs to."""
    lowered = label.lower()
    prefixes = [
        ("diagnoses - main icd10", "ICD-10 diagnosis (main)"),
        ("diagnoses - secondary icd10", "ICD-10 diagnosis (secondary)"),
        ("non-cancer illness code", "Self-reported illness"),
        ("cancer code", "Self-reported cancer"),
        ("type of cancer", "Cancer registry"),
        ("operation code", "Self-reported operation"),
        ("operative procedures", "OPCS procedure"),
        ("treatment/medication code", "Medication"),
        ("medication for", "Medication"),
        ("illnesses of", "Family history"),
    ]
    for prefix, name in prefixes:
        if lowered.startswith(prefix):
            return name
    administrative = (
        "pct where", "pct responsible", "treatment speciality", "main speciality",
        "methods of admission", "sources of admission", "destinations on discharge",
        "methods of discharge",
    )
    if lowered.startswith(administrative):
        return "Hospital administrative field"
    if lowered[:3] in ("3mm", "6mm") or "keratometr" in lowered:
        return "Keratometry"
    diet = ("intake", "consum", "supplement", "eaten", "never eat", "diet", "cooking",
            "butter", "bread", "cereal", "milk", "coffee", "tea ", "salt", "alcohol")
    if any(word in lowered for word in diet):
        return "Diet and supplements"
    socio = ("employment", "qualifications", "household", "transport", "heating",
             "income", "allowance", "leisure", "job", "accommodation", "pollution")
    if any(word in lowered for word in socio):
        return "Sociodemographic / environment"
    conditions = ("problems", "diagnosed by doctor", "blood clot", "fractured", "pain")
    if any(word in lowered for word in conditions):
        return "Self-reported condition or symptom"
    if lowered.startswith(("types of", "number of days/week", "time spent", "duration")) or "activity" in lowered:
        return "Physical activity / lifestyle"
    return "Other measurement / questionnaire"


def export_production(args, by_id) -> tuple[list[dict], dict]:
    round_dir = args.production_round
    config = yaml.safe_load((round_dir / "round.yaml").read_text())
    conf_t = config["thresholds"]["confidence"]
    margin_t = config["thresholds"]["margin"]
    occurrences = {
        normalize_trait_label(r["trait_label"]): int(r["occurrence_count"])
        for r in read_tsv(round_dir / "queue.tsv")
    }
    choices = read_choices(round_dir)
    rows = []
    for key, data in sorted(choices.items()):
        selected = data["selected_ontology_id"]
        probs = data["probabilities"]
        confidence = probs.get(selected, 0.0)
        _, margin = top_two(probs)
        confident = confidence >= conf_t and margin >= margin_t
        if selected == NONE_SUITABLE:
            bucket = "no suitable term" if confident else "review (uncertain abstention)"
        else:
            bucket = "auto-accepted" if confident else "review (uncertain pick)"
        term = by_id.get(selected)
        rows.append(
            {
                "trait_label": data["trait_label"],
                "field": ukb_field(data["trait_label"]),
                "analyses": occurrences.get(key, 0),
                "selected_id": selected,
                "selected_label": term.label if term else "",
                "confidence": round(confidence, 4),
                "margin": round(margin, 4),
                "bucket": bucket,
                "input_tokens": data.get("input_tokens") or 0,
                "cost_usd": data.get("cost_usd") or 0.0,
            }
        )
    summary = {
        "labels": len(rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "round": round_pins(round_dir),
    }
    return rows, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--validation-round", type=Path, required=True)
    parser.add_argument("--production-round", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "data")
    args = parser.parse_args()

    index = load_index(args.index)
    by_id = index.by_id()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    validation_rows, validation_summary = export_validation(args, by_id)
    production_rows, production_summary = export_production(args, by_id)
    write_tsv(args.out_dir / "trait-mapping-validation-ogs00011.tsv", validation_rows)
    write_tsv(args.out_dir / "trait-mapping-ukb-b-round1.tsv", production_rows)

    summary = {
        "index": {
            "release": index.ontology_release,
            "terms": len(index.terms),
            "excluded_non_curie": getattr(index, "excluded_non_curie_count", None),
            "obsolete": sum(1 for t in index.terms if t.is_obsolete),
        },
        "validation": validation_summary,
        "production": production_summary,
    }
    path = args.out_dir / "trait-mapping-summary.json"
    path.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
