"""Deterministic formatting for ancestry sidecar numeric columns (issue #143).

The ancestry solver (``scipy.optimize.nnls``, via
``opengwasdb.ancestry.assign_ancestry``) resolves the same fit to within
machine epsilon but rounds differently under different BLAS/LAPACK backends,
so a value that is mathematically zero records a few ulps of solver noise
(e.g. ``1.29166e-16``) whose low-order digits differ between machines.  When
that value is written with ``:.6g`` -- six *significant* figures -- the
sidecar faithfully records six digits of noise, so the committed fixture
cannot be byte-reproduced across hosts and the clean-tree gate (#124/#131)
goes red for a difference that is not real.

The fix is to snap sub-tolerance values to exact zero *before* formatting and
keep ``:.6g`` for everything else, so a genuinely small but meaningful value
(say a ``1e-8`` residual) still prints while zero-plus-noise becomes ``0``.
This is a data-formatting decision, so the tolerance is one named constant
shared by every ancestry sidecar writer rather than repeated inline.

Tolerance choice (issue #143).  It must sit clearly above float noise and
clearly below any value that carries meaning.  Proportions live on 0-1 and
residuals matter from roughly 1e-4 upward.  The largest solver noise observed
is 1.29166e-16 (this host) and 1.02014e-16 (CI), i.e. at or below one ulp of
float64 (eps ~= 2.22e-16); the repository already treats manifest and sidecar
proportions as "the same" at 1e-12 in ``tests/finngen-r13-pilot``.  1e-12 is
therefore ~4 orders of magnitude above the observed noise (still an order of
magnitude above a 1000x-noisier solver) and ~8 orders of magnitude below the
smallest meaningful value, so it snaps noise and nothing else.  Do not lower
it toward eps, where a different BLAS could breach it, or raise it toward
1e-4, where a real residual would be flattened.
"""

_SUB_TOLERANCE = 1e-12


def snap_sub_tolerance(value: float) -> float:
    """Return ``0.0`` when ``value`` is sub-tolerance noise, else ``value``.

    NaN passes through untouched (``abs(nan) < tol`` is False), so callers
    keep their own NaN/absence handling rather than letting noise-snapping
    turn an empty cell into a zero.
    """
    return 0.0 if abs(value) < _SUB_TOLERANCE else value


def format_sidecar_float(value: float) -> str:
    """Snap sub-tolerance noise to exact zero, then six significant figures.

    Absence (``None`` / empty cell) is the caller's responsibility: this
    function only ever turns a present numeric value into a string, so an
    empty cell stays empty and is never rendered as ``0``.
    """
    return f"{snap_sub_tolerance(value):.6g}"
