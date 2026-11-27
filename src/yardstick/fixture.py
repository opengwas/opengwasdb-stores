"""The fixed small build (#390): a 2-variant GWAS-VCF through `opengwasdb build-dense-vcf`.

The same input serves the runner's timed build and the tests' tiny Store. Its
one Analysis is `fx1`; its variants are rs1 (1:10000:A:G) and rs2 (1:20000:C:T).
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from yardstick import proc

VCF = (
    "##fileformat=VCFv4.2\n"
    '##FILTER=<ID=PASS,Description="All filters passed">\n'
    '##INFO=<ID=AF,Number=A,Type=Float,Description="Allele Frequency">\n'
    '##FORMAT=<ID=EZ,Number=A,Type=Float,Description="Z-score">\n'
    '##FORMAT=<ID=ES,Number=A,Type=Float,Description="Effect size">\n'
    '##FORMAT=<ID=SE,Number=A,Type=Float,Description="Standard error">\n'
    '##FORMAT=<ID=LP,Number=A,Type=Float,Description="-log10(p-value)">\n'
    '##FORMAT=<ID=AF,Number=A,Type=Float,Description="Allele frequency">\n'
    '##FORMAT=<ID=SS,Number=A,Type=Float,Description="Sample size">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRAIT\n"
    "chr1\t10000\trs1\tA\tG\t.\tPASS\tAF=0.2\tEZ:ES:SE:LP:AF:SS\t5.0:0.1:0.02:5.0:0.2:10000\n"
    "chr1\t20000\trs2\tC\tT\t.\tPASS\tAF=0.4\tEZ:ES:SE:LP:AF:SS\t-6.666667:-0.2:0.03:8.0:0.4:10000\n"
)
ANALYSES_HEADER = (
    "analysis_id\tsource_file\ttrait_name\tsample_size\tstored_effect_scale\t"
    "original_sd_method\toriginal_sd\tassigned_ancestry\tancestry_assignment_method\n"
)


def _opengwasdb() -> list[str]:
    beside = Path(sys.executable).parent / "opengwasdb"
    return [str(beside)] if beside.is_file() else [shutil.which("opengwasdb") or "opengwasdb"]


def build_dense_store(store_path: Path, source_dir: Path, *, timeout: float | None = None) -> Path:
    """Build the fixture Dense Store at `store_path`, writing its inputs to `source_dir`."""
    source_dir.mkdir(parents=True, exist_ok=True)
    vcf = source_dir / "fixture.vcf.gz"
    with gzip.open(vcf, "wt", encoding="utf-8") as f:
        f.write(VCF)
    analyses = source_dir / "analyses.tsv"
    analyses.write_text(
        ANALYSES_HEADER + f"fx1\t{vcf}\tTest Trait\t10000\tsd\tdeclared_standardised\t\tEUR\tsource_fallback\n",
        encoding="utf-8",
    )
    # Every builder requires its Association Admission record's directory
    # (opengwasdb#357, ADR 0072); the fixture keeps it beside its Store, where
    # the builder's Python API would put it.
    admission = store_path.with_name(f"{store_path.name}.admission")
    result = proc.run_group(
        [
            *_opengwasdb(), "build-dense-vcf", str(analyses), str(store_path),
            "--store-id", "OGS-00099", "--release-id", "OGS-00099",
            "--source-reader-capability", "opengwasdb.gwas-vcf", "--source-assembly", "hg38",
            "--admission-record", str(admission),
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, "opengwasdb build-dense-vcf", result.stdout, result.stderr)
    return store_path


def timed_build(scratch: Path, *, timeout: float | None = None) -> dict:
    """Build the fixture Store once into `scratch`, and say how long it took."""
    if timeout is not None and timeout <= 0:
        return {"status": "unavailable", "layout": "dense", "error": "timeout after 0 s: the run's budget was already spent"}
    start = time.perf_counter()
    try:
        build_dense_store(scratch / "store.opengwasdb", scratch / "source", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "layout": "dense", "error": f"timeout after {timeout:g} s"}
    except subprocess.CalledProcessError as error:
        return {"status": "unavailable", "layout": "dense", "error": (error.stderr or str(error))[-500:]}
    return {"status": "measured", "layout": "dense", "seconds": round(time.perf_counter() - start, 3)}


def write_pgen_chromosomes(
    directory: Path, chroms: tuple[int, ...], sample_ids: list[str], n_variants: int, rng: Any
) -> list[Path]:
    """A synthetic phased panel, `chr<N>.pgen`, `.psam` and `.pvar` per chromosome in `directory`.

    `n_variants` variants a chromosome at random positions between 10 and 12 Mb, each
    flipping some of its neighbour's alleles, so there is LD to measure. Written with
    pgenlib, the library the readers use; no engine fixture writes a pgen. The LD
    section's tests (#504) and the registry's tiny build (#541) share it, each with its
    own `rng` (`numpy.random.Generator`).
    """
    import numpy as np
    import pgenlib

    directory.mkdir(parents=True, exist_ok=True)
    n_samples = len(sample_ids)
    pgens = []
    for chrom in chroms:
        prefix = directory / f"chr{chrom}"
        Path(f"{prefix}.psam").write_text("#IID\tSEX\n" + "".join(f"{s}\tNA\n" for s in sample_ids))
        positions = np.sort(rng.choice(np.arange(10_000_000, 12_000_000), n_variants, replace=False))
        lines = ["#CHROM\tPOS\tID\tREF\tALT\tFILTER"]
        for position in positions:
            ref, alt = "ACGT"[position % 4], "ACGT"[(position + 1) % 4]
            lines.append(f"{chrom}\t{position}\t{chrom}:{position}:{ref}:{alt}\t{ref}\t{alt}\tPASS")
        Path(f"{prefix}.pvar").write_text("\n".join(lines) + "\n")
        writer = pgenlib.PgenWriter(f"{prefix}.pgen".encode(), n_samples, n_variants, False, hardcall_phase_present=True)
        haplotypes = rng.integers(0, 2, size=2 * n_samples, dtype=np.int32)
        for _ in range(n_variants):  # LD: each variant flips some of its neighbour's alleles
            haplotypes = np.where(rng.random(2 * n_samples) < 0.15, 1 - haplotypes, haplotypes).astype(np.int32)
            writer.append_alleles(haplotypes, all_phased=True)
        writer.close()
        pgens.append(Path(f"{prefix}.pgen"))
    return pgens


def write_ld_records(ld_root: Path, *documents: dict[str, Any]) -> Path:
    """`ld_root` holding each LD Store Registry record at `<ld_id>/record.yaml`, as YAML.

    The registry's tests and the LD section's (#504) share it to lay out a registry from
    documents; it validates nothing, so a test can write a record the registry refuses.
    """
    import yaml

    from ld_registry.record import record_path

    for document in documents:
        path = record_path(ld_root, document["ld_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return ld_root
