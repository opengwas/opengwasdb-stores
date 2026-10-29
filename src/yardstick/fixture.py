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
    subprocess.run(
        [
            *_opengwasdb(), "build-dense-vcf", str(analyses), str(store_path),
            "--store-id", "OGS-00099", "--release-id", "OGS-00099",
            "--source-reader-capability", "opengwasdb.gwas-vcf", "--source-assembly", "hg38",
        ],
        check=True, capture_output=True, text=True, timeout=timeout,
    )
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
