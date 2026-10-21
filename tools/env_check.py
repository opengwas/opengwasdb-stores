#!/usr/bin/env python3
"""Preflight environment check (issue #41): report and validate the runtime,
package, and native-tool versions the repository's Pixi `dev` environment is
expected to provide, and fail (non-zero exit) if anything required is
missing. Run via:

  pixi run env-check
"""
from __future__ import annotations

import importlib.metadata
import shutil
import subprocess
import sys

REQUIRED_EXECUTABLES = ["python3", "Rscript", "plink2", "bcftools", "quarto", "git", "gh", "sqlite3", "curl"]

REQUIRED_PYTHON_PACKAGES = [
    "numpy", "scipy", "pandas", "scikit-learn", "pysam", "zarr", "numcodecs",
    "typer", "pyliftover", "threadpoolctl", "opengwasdb",
]

REQUIRED_R_PACKAGES = [
    "data.table", "yaml", "R.utils", "dplyr", "ggplot2", "knitr", "scales",
    "tibble", "tidyr", "jsonlite", "curl", "DT", "httr",
]

VERSION_FLAGS = {
    "python3": "--version",
    "Rscript": "--version",
    "plink2": "--version",
    "bcftools": "--version",
    "quarto": "--version",
    "git": "--version",
    "gh": "--version",
    "sqlite3": "--version",
    "curl": "--version",
}


def check_executables() -> list[str]:
    problems = []
    print("## Executables")
    for name in REQUIRED_EXECUTABLES:
        path = shutil.which(name)
        if path is None:
            problems.append(f"missing required executable: {name}")
            print(f"  {name:12s} MISSING")
            continue
        flag = VERSION_FLAGS[name]
        result = subprocess.run([path, flag], capture_output=True, text=True)
        version_line = (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr) else "(no version output)"
        print(f"  {name:12s} {version_line}  [{path}]")
    return problems


def check_python_packages() -> list[str]:
    problems = []
    print("## Python packages")
    for name in REQUIRED_PYTHON_PACKAGES:
        try:
            version = importlib.metadata.version(name)
            print(f"  {name:16s} {version}")
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"missing required Python package: {name}")
            print(f"  {name:16s} MISSING")
    return problems


def check_r_packages() -> list[str]:
    problems = []
    print("## R packages")
    rscript = shutil.which("Rscript")
    if rscript is None:
        problems.append("Rscript not available; cannot check R packages")
        return problems
    r_expr = (
        "pkgs <- c(" + ", ".join(f'"{p}"' for p in REQUIRED_R_PACKAGES) + "); "
        "for (p in pkgs) { "
        "  v <- tryCatch(as.character(utils::packageVersion(p)), error = function(e) NA); "
        "  cat(p, ifelse(is.na(v), 'MISSING', v), '\\n') "
        "}"
    )
    result = subprocess.run([rscript, "-e", r_expr], capture_output=True, text=True)
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        name, version = parts
        print(f"  {name:16s} {version}")
        if version == "MISSING":
            problems.append(f"missing required R package: {name}")
    return problems


def main() -> int:
    problems = []
    problems += check_executables()
    problems += check_python_packages()
    problems += check_r_packages()

    print()
    if problems:
        print(f"env-check FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("env-check OK — all required tools and packages are available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
