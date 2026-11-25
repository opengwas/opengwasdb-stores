"""The acceptance of a rebuilt Store, through `opengwasdb ld info` alone (#541; ADR 0023).

Every field of the two provenances is compared, leaf by leaf. A difference in
the build's own fields, which no two builds share, is expected. They are a
closed list: `build.created_at`, `build.wall_seconds`, `build.peak_memory_bytes`
(it depends on the machine), and each `software.<key>` version, each only when
both sides hold it. A field one side lacks fails, whatever it is. Every other
difference fails the acceptance. Every difference, expected or not, is
reported, never ignored. The PC Check's five scan statistics
(`TOLERATED_SCAN_STATISTICS`) are held to a relative tolerance of 1e-9 instead
of exactly, and a difference within it is reported as such.

The rebuilt Store's provenance must also hold what an HGDP+1kGP Store records
(`provenance_problems`), the checks of opengwasdb's `test_hgdp1kgp_stores.py`
that the command line shows. The Store is validated by its own build's
commands, never again here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from ld_registry import record
from ld_registry.provenance import ld_info, provenance_sha256
from ld_registry.recipe import K, K_MIN, R_SCALE, RecipeRefusal, panel_id
from ogstores.store_info import Runner

#: The record's check of an ISO 8601 UTC time, which `build.created_at` must be.
_utc_time = next(field.check for field in record.FIELDS if field.name == "provenance.created_at")

#: The fields a build measures of itself: the closed list.
BUILD_OWN = frozenset({"build.created_at", "build.wall_seconds", "build.peak_memory_bytes"})
#: A version of one package the build ran with, such as `software.zarr`.
SOFTWARE_VERSION = re.compile(r"software\.[^.\[\]]+")
#: The PC Check's diagnostic statistics, held to `RELATIVE_TOLERANCE`, not exactly: the closed
#: list (owner, 25 November 2026, after the acceptance run of #541). Their last digits depend on
#: the numerical library (OGL-00009 at ogld 0.5.0 and its rebuild at opengwasdb 0.20.0 differ at
#: about 1e-15 relative in 77 of them). The PCs kept, the scan's `pc` and `window`,
#: `pc_check.dropped`, and every value the Store holds are compared exactly.
TOLERATED_SCAN_STATISTICS = frozenset({"ratio", "rest_null_q99", "rest_share", "window_null_q99", "window_share"})
RELATIVE_TOLERANCE = 1e-9
_SCAN_STATISTIC = re.compile(r"adjustment\.pc_check\.scan\[\d+\]\.(?P<statistic>[^.\[\]]+)")


class _Absent:
    def __repr__(self) -> str:
        return "(absent)"


#: The value of a field one side does not have.
ABSENT: Any = _Absent()


class Difference(NamedTuple):
    field: str
    original: Any
    rebuilt: Any

    @property
    def build_own(self) -> bool:
        """One of the build's own fields, held on both sides; a software version must be text on both."""
        if self.original is ABSENT or self.rebuilt is ABSENT:
            return False
        if self.field in BUILD_OWN:
            return True
        return bool(SOFTWARE_VERSION.fullmatch(self.field)) and isinstance(self.original, str) and isinstance(
            self.rebuilt, str
        )

    @property
    def within_tolerance(self) -> bool:
        """A PC Check scan statistic, a number on both sides, within `RELATIVE_TOLERANCE` of the original."""
        match = _SCAN_STATISTIC.fullmatch(self.field)
        if not match or match["statistic"] not in TOLERATED_SCAN_STATISTICS:
            return False
        numbers = [v for v in (self.original, self.rebuilt) if isinstance(v, float | int) and not isinstance(v, bool)]
        return len(numbers) == 2 and math.isclose(self.original, self.rebuilt, rel_tol=RELATIVE_TOLERANCE, abs_tol=0.0)


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _walk(prefix: str, original: Any, rebuilt: Any) -> Iterator[Difference]:
    if isinstance(original, dict) and isinstance(rebuilt, dict):
        for key in sorted(set(original) | set(rebuilt)):
            if key not in original:
                yield Difference(_join(prefix, key), ABSENT, rebuilt[key])
            elif key not in rebuilt:
                yield Difference(_join(prefix, key), original[key], ABSENT)
            else:
                yield from _walk(_join(prefix, key), original[key], rebuilt[key])
    elif isinstance(original, list) and isinstance(rebuilt, list) and len(original) == len(rebuilt):
        for number, (left, right) in enumerate(zip(original, rebuilt)):
            yield from _walk(f"{prefix}[{number}]", left, right)
    elif type(original) is not type(rebuilt) or original != rebuilt:
        yield Difference(prefix, original, rebuilt)


def differences(original: dict[str, Any], rebuilt: dict[str, Any]) -> list[Difference]:
    """Every field in which `rebuilt`'s provenance differs from `original`'s, sorted by field."""
    return sorted(_walk("", original, rebuilt), key=lambda difference: difference.field)


def failures(found: list[Difference]) -> list[Difference]:
    """The differences that fail the acceptance: all but the build's own and those within tolerance."""
    return [difference for difference in found if not (difference.build_own or difference.within_tolerance)]


def render(found: list[Difference]) -> str:
    """One line per difference, the build's own and those within tolerance marked as such."""
    def mark(d: Difference) -> str:
        return " (the build's own)" if d.build_own else " (within tolerance)" if d.within_tolerance else ""

    return "\n".join(f"{d.field}: {d.original!r} -> {d.rebuilt!r}{mark(d)}" for d in found)


def accept(
    original: Path,
    rebuilt: Path,
    *,
    recorded_sha256: str | None = None,
    keep: Path | None = None,
    info_runner: Runner | None = None,
) -> tuple[list[Difference], list[str]]:
    """The acceptance of `rebuilt` against `original`, through `opengwasdb ld info` alone (ADR 0023).

    It returns every difference, and the problems that fail the run besides
    them: the original's provenance no longer being the one its record's digest
    names, and what the rebuilt Store's provenance must record
    (`provenance_problems`, n against `keep` when given). The rebuilt Store's
    own commands validated it; this does not validate it again.
    """
    before = ld_info(original, runner=info_runner)
    problems: list[str] = []
    if recorded_sha256 is not None and provenance_sha256(before) != recorded_sha256:
        problems.append(
            f"{original}: its provenance's sha256 is {provenance_sha256(before)}, not the record's {recorded_sha256}"
        )
    after = ld_info(rebuilt, runner=info_runner)
    problems += [f"{rebuilt}: {problem}" for problem in provenance_problems(after, keep_count(keep) if keep else None)]
    return differences(before, after), problems


def keep_count(keep: Path) -> int:
    """A keep file's samples: one IID a line under its `#IID` header."""
    lines = [line for line in Path(keep).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines or lines[0] != "#IID":
        raise RecipeRefusal(f"{keep}: not a keep file (its first line must be '#IID')")
    return len(lines) - 1


def provenance_problems(info: dict[str, Any], keep_n: int | None = None) -> list[str]:
    """What an HGDP+1kGP Store's `opengwasdb ld info` must record, each problem naming its field.

    These are the checks of opengwasdb's `tests/ld/acceptance/test_hgdp1kgp_stores.py` that the
    command line shows (ADR 0023), moved here with the build (#541): the panel and its
    unrelated samples, the scale, the PC Check's k, and the build's own record. An LD Store also
    records its Near Band and its PC Diagnostic; a Genotype Store has neither. Its
    `counts.index_entries`, a 0.1.0 Store's Proxy Index, is not required: a Store the locked
    engine builds holds Partner Rows instead.
    """
    def at(dotted: str) -> Any:
        value: Any = info
        for key in dotted.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        return value

    def number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    problems: list[str] = []

    def need(dotted: str, ok: bool, wanted: str) -> None:
        if not ok:
            problems.append(f"{dotted}: {at(dotted)!r}; {wanted}")

    ancestry = at("panel.ancestry")
    need("panel.id", at("panel.id") == panel_id(str(ancestry)), f"must be {panel_id(str(ancestry))!r}")
    if keep_n is not None:
        need("panel.n", at("panel.n") == keep_n, f"must be the keep file's {keep_n} samples")
    need("encoding.r_scale", at("encoding.r_scale") == R_SCALE, f"must be {R_SCALE} (ADR 0090)")
    need("n_variants", number(at("n_variants")) and at("n_variants") > 0, "must be above 0")
    need("t_far", number(at("t_far")) and 0 < at("t_far") < 1, "must be between 0 and 1")
    need("build.created_at", isinstance(at("build.created_at"), str) and _utc_time(at("build.created_at")) is None,
         "an ISO 8601 UTC time")
    need("build.wall_seconds", number(at("build.wall_seconds")) and at("build.wall_seconds") > 0, "must be above 0")
    need("build.peak_memory_bytes", number(at("build.peak_memory_bytes")) and at("build.peak_memory_bytes") > 0,
         "must be above 0")
    need("software.ogld", isinstance(at("software.ogld"), str) and bool(at("software.ogld")), "the builder's version")
    need("adjustment.k_target", at("adjustment.k_target") == K, f"must be {K}")
    need("adjustment.k_min", at("adjustment.k_min") == K_MIN, f"must be {K_MIN}")
    k = at("adjustment.k")
    need("adjustment.k", number(k) and K_MIN <= k <= K, f"must be {K_MIN} to {K}")
    covariates = at("adjustment.covariates")
    need("adjustment.covariates", isinstance(covariates, list) and len(covariates) == k, "one a kept PC")
    need("adjustment.pc_check.dropped", isinstance(at("adjustment.pc_check.dropped"), list),
         "the PC Check's dropped PCs are recorded, even when none")
    if "filesets" in info:  # a Genotype Store: no Near Band, no PC Diagnostic
        return problems
    need("counts.near_pairs", number(at("counts.near_pairs")) and at("counts.near_pairs") > 0, "must be above 0")
    if not isinstance(at("pc_diagnostic"), dict):
        need("pc_diagnostic", False, "an LD Store records its PC Diagnostic")
        return problems
    need("pc_diagnostic.pairs.n_pairs", number(at("pc_diagnostic.pairs.n_pairs")) and at("pc_diagnostic.pairs.n_pairs") > 0,
         "must be above 0")
    for rate in ("rate_r2_ge_0_01_unadjusted", "rate_r2_ge_0_01_adjusted", "rate_r2_ge_0_01_sampling"):
        need(f"pc_diagnostic.{rate}", number(at(f"pc_diagnostic.{rate}")), "must be a finite number")
    change = at("pc_diagnostic.near_100kb_change")
    need("pc_diagnostic.near_100kb_change",
         isinstance(change, dict) and set(change) == {"0", "0.5", "0.9", "0.99", "1"}
         and all(number(v) for v in change.values()),
         "the quantiles 0, 0.5, 0.9, 0.99 and 1, each finite")
    return problems
