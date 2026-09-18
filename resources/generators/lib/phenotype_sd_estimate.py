"""Thin CLI bridge to OpenGWASDB's phenotype-SD estimator."""
from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version

import numpy as np

from opengwasdb.build.phenotype_sd import estimate_phenotype_sd
from opengwasdb.model.enums import OriginalSdMethod


def _array(value: str | None) -> np.ndarray | None:
    if value is None:
        return None
    return np.asarray(json.loads(value), dtype=float)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", required=True)
    parser.add_argument("--sample-size", required=True, type=float)
    parser.add_argument("--se")
    parser.add_argument("--af")
    parser.add_argument("--beta")
    args = parser.parse_args()
    result = estimate_phenotype_sd(
        OriginalSdMethod(args.method), args.sample_size,
        se=_array(args.se), af=_array(args.af), beta=_array(args.beta),
    )
    try:
        package_version = version("opengwasdb")
    except PackageNotFoundError:
        package_version = "unknown"
    print(json.dumps({
        "sd": result.sd if np.isfinite(result.sd) else None,
        "method": result.method.value,
        "dispersion": result.dispersion if np.isfinite(result.dispersion) else None,
        "notes": result.notes,
        "estimator_version": f"opengwasdb:{package_version}",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
