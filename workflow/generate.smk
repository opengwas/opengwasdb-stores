"""Manifest Generation -- coarse dependency wiring only (issues #153, #287).

This DAG sequences the stages of one candidate Release Bundle:

    freeze -> preflight -> resolver-manifest -> resolve -> verify -> render -> emit

Every rule's shell is one `--stage` invocation of the one Manifest Generation
entry point, `python3 -m manifest_generation.generate_candidate`, that the
operator runs through `pixi run generate-candidate`, for any release config
(`docs/spec/release-config.md`). The DAG carries no selection, no source column
name and no manifest translation; per ADR 0023 it only wires dependencies and
resource requests.

The per-Analysis work -- loading the genome-scale ancestry reference once, the
worker pool, atomic per-Analysis records and `--resume` -- is owned by
`opengwasdb resolve-analyses`, which the `resolve` rule invokes as a subprocess.
Snakemake therefore never loads a reference once per Analysis; it sequences one
resolver invocation that owns its own pool. The resolver always resumes from the
Source Collection's Resolution Area (#284), because its fingerprint-aware
checkpoint is the authority on what can be reused; Snakemake reuses nothing. The
`resolve` rule's output is the run's resolution receipt in the Work Root, never
a file in the shared area, which other Selections' runs also write. The
`freeze` rule's output is the freeze stage's report in the Work Root: the
frozen Source Inventory itself is never overwritten.

Run it with explicit `--config` values (see `workflow/README.md`); `config` is
any release config, and `snapshot_id` its `source_collection.inventory.snapshot_id`:

    snakemake --snakefile workflow/generate.smk --cores 64 \
      --config store_id=OGS-00011 \
               config=resources/generators/gwas-catalog-eur-hybrid/release-eur-hybrid-full.yaml \
               snapshot_id=gwas-catalog-ssf-eur-hybrid-2026-10-20 \
               work_root=/data/opengwasdb/work/gwas-catalog-eur-hybrid \
               registry_root=stores

`resolution_root=<dir>` overrides where the Source Collection's Resolution Area
lives (default: OPENGWASDB_RESOLUTION_ROOT, else `ogstores.yaml`'s
`resolution_root`). Every run reuses each record whose fingerprint still
matches; there is no resume switch.

Production operators should prefer `pixi run generate-candidate`, which needs no
Snakemake environment; this DAG exists so a future multi-release batch can reuse
the same stages.
"""

import os

STORE_ID = config.get("store_id")
CONFIG = config.get("config")
CORES = str(config.get("cores", 64))
WORK_ROOT = config.get("work_root")
REGISTRY_ROOT = config.get("registry_root", "stores")
SNAPSHOT_ID = config.get("snapshot_id")
RESOLUTION_ROOT = config.get("resolution_root")

_missing = [
    name
    for name, value in (
        ("store_id", STORE_ID),
        ("config", CONFIG),
        ("work_root", WORK_ROOT),
        ("snapshot_id", SNAPSHOT_ID),
    )
    if not value
]
if _missing:
    raise ValueError(
        "workflow/generate.smk requires --config "
        + " ".join(f"{name}=<value>" for name in _missing)
    )

CLI = "-m manifest_generation.generate_candidate"
RUN_ROOT = os.path.join(str(WORK_ROOT), str(STORE_ID))
RESOLUTION_ROOT_FLAG = f" --resolution-root {RESOLUTION_ROOT}" if RESOLUTION_ROOT else ""
COMMON = (
    f"python3 {CLI} {STORE_ID} --config {CONFIG} --cores {CORES} "
    f"--work-root {WORK_ROOT} --registry-root {REGISTRY_ROOT}{RESOLUTION_ROOT_FLAG}"
)


rule all:
    input:
        os.path.join(str(REGISTRY_ROOT), str(STORE_ID), "analyses.tsv")


rule freeze:
    output:
        os.path.join(RUN_ROOT, "freeze", f"{SNAPSHOT_ID}.json")
    shell:
        COMMON + " --stage freeze"


rule preflight:
    input:
        rules.freeze.output
    output:
        os.path.join(RUN_ROOT, "preflight", f"{SNAPSHOT_ID}.json")
    shell:
        COMMON + " --stage preflight"


rule resolver_manifest:
    input:
        rules.preflight.output
    output:
        os.path.join(RUN_ROOT, "resolver", "analyses.tsv")
    shell:
        COMMON + " --stage prepare"


rule resolve:
    input:
        rules.resolver_manifest.output
    output:
        os.path.join(RUN_ROOT, "resolver", "resolution_receipt.json")
    shell:
        COMMON + " --stage resolve"


rule verify:
    input:
        rules.resolve.output
    output:
        touch(os.path.join(RUN_ROOT, "resolver", ".verified"))
    shell:
        COMMON + " --stage verify"


rule render:
    input:
        rules.verify.output
    output:
        os.path.join(RUN_ROOT, "bundle", "analyses.tsv")
    shell:
        COMMON + " --stage render"


rule emit:
    input:
        rules.render.output
    output:
        os.path.join(str(REGISTRY_ROOT), str(STORE_ID), "analyses.tsv")
    shell:
        COMMON + " --stage emit"
