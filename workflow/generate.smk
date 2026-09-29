"""Phase B candidate generation -- coarse dependency wiring only (issue #153).

This DAG sequences the five stages of one candidate Release Bundle:

    preflight -> resolver-manifest -> resolve -> verify -> finalise

Every rule's shell is one `--stage` invocation of the same
`resources/generators/gwas-catalog-eur-hybrid/generate_candidate.py` entry point
the operator runs through `pixi run generate-candidate`. The DAG carries no
selection, no source column name and no manifest translation; per ADR 0023 it
only wires dependencies and resource requests.

The per-Analysis work -- loading the genome-scale ancestry reference once, the
worker pool, atomic per-Analysis records and `--resume` -- is owned by
`opengwasdb resolve-analyses`, which the `resolve` rule invokes as a subprocess.
Snakemake therefore never loads a reference once per Analysis; it sequences one
resolver invocation that owns its own pool. `--resume` is passed through to that
invocation, not to Snakemake, because the resolver's fingerprint-aware checkpoint
is the authority on what can be reused.

Run it with an explicit config file (see `workflow/README.md`):

    snakemake --snakefile workflow/generate.smk --cores 64 \
      --configfile resources/generators/gwas-catalog-eur-hybrid/generate-candidate-fixture.yaml

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
RESUME = config.get("resume", False)

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

CLI = "resources/generators/gwas-catalog-eur-hybrid/generate_candidate.py"
RUN_ROOT = os.path.join(str(WORK_ROOT), str(STORE_ID))
RESUME_FLAG = " --resume" if RESUME else ""
COMMON = (
    f"python3 {CLI} {STORE_ID} --config {CONFIG} --cores {CORES} "
    f"--work-root {WORK_ROOT} --registry-root {REGISTRY_ROOT}{RESUME_FLAG}"
)


rule all:
    input:
        os.path.join(str(REGISTRY_ROOT), str(STORE_ID), "analyses.tsv")


rule preflight:
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
        os.path.join(RUN_ROOT, "resolver", "records", "index.json")
    shell:
        COMMON + " --stage resolve"


rule verify:
    input:
        rules.resolve.output
    output:
        touch(os.path.join(RUN_ROOT, "resolver", ".verified"))
    shell:
        COMMON + " --stage verify"


rule finalise:
    input:
        rules.verify.output
    output:
        os.path.join(str(REGISTRY_ROOT), str(STORE_ID), "analyses.tsv")
    shell:
        COMMON + " --stage emit"
