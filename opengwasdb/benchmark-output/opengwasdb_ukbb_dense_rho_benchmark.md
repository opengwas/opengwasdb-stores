# Dense Rho Matrix: genome-scale run on the real ukb-b store (issue 051)

HITL sanity-check slice of issue #41 (Dense Store Pairwise Rho Matrix). Runs
`build-dense-rho` against the real, genome-wide, Reference-Completed ukb-b
store and reviews the resulting Rho distribution for scientific plausibility.
Raw numbers: `opengwasdb_ukbb_dense_rho_benchmark.json` (same directory).

## Store

`/data/opengwasdb/wip/ukb-b-c128-completed.opengwasdb` — Dense,
Reference-Completed, 2,514 UK Biobank analyses, 11,192,757 variants
(genome-wide, hg38), ~71 GB on disk. This is the same store documented in
`ukb-b-c128-completed.PROVENANCE.md`; no other real, built, genome-wide ukb-b
store was available in this environment (`data/ukb-b/build.sh`'s target path
`/local-scratch/...` does not exist here).

## Build

```
opengwasdb build-dense-rho /data/opengwasdb/wip/ukb-b-c128-completed.opengwasdb --n-workers 16
```

Defaults used throughout (`window_bp=15000`, `z_thresh=1.0`, `min_nulls=500`).

| Metric | Value |
|---|---|
| Wall-clock | **2m 14s** |
| Peak RSS | **~47.5 GB** (49,820,732 KB) |
| CPU (user+sys) | 946.2s across 16 workers (705% avg) |
| Variants used (thinned) | 177,862 (of 11,192,757 — ~63x thinning) |
| Analysis pairs | 3,158,841 (= 2514·2513/2) |

Well within the PRD's "minutes, not hours" target (issue 051 AC). The two
costliest phases: reading the observed/null Z and mask at the 177,862 thinned
rows across all 2,514 analyses (~56s, effectively touches most row-chunks of
`data.zarr/z` since the thinned rows are spread genome-wide), and the batched
`(A, B, C, n)` sufficient-statistics matmuls (~5s, BLAS-parallel). The 16-worker
per-pair MLE phase (3.16M `minimize_scalar` calls) was not the bottleneck at
this analysis count.

## Validation

```
opengwasdb validate /data/opengwasdb/wip/ukb-b-c128-completed.opengwasdb
```

Result: **valid**. Wall-clock 15m 33s, peak RSS ~11.4 GB — this is the full
existing dense-store validator (variant axis, sqlite, analyses.tsv, completion
metadata, the streamed z/se/imputed band pass, top-hit index, *and* the new
`_validate_rho` check), not just the Rho-specific check; the added Rho check
itself is O(n_analyses²) on already-small packed arrays and negligible next to
the full-matrix band scan.

## Rho distribution

| Metric | Value |
|---|---|
| Pairs | 3,158,841 |
| NaN (below `min_nulls=500`) | **0** (0.0000%) |
| `n_null` support | min 5,485 · median 22,306 · max 104,771 |
| Rho | mean 0.0053 · median 0.0035 · std 0.0377 |
| Rho | p1 = −0.0735 · p99 = +0.0975 |
| \|Rho\| > 0.1 | 1.13% of pairs |
| \|Rho\| > 0.3 | 0.087% of pairs |
| \|Rho\| > 0.5 | 0.036% of pairs |
| Rho > 0.9 | 183 pairs (0.0058%) |

```
-1.00 to -0.90:        3
-0.90 to -0.80:        7
-0.80 to -0.70:        7
-0.70 to -0.60:       22
-0.60 to -0.50:       48
-0.50 to -0.40:       77
-0.40 to -0.30:      124
-0.30 to -0.20:      359
-0.20 to -0.10:    5,885
-0.10 to +0.00: 1,410,374  #################################################################
+0.00 to +0.10: 1,712,737  ################################################################################
+0.10 to +0.20:   23,963   #
+0.20 to +0.30:    2,762
+0.30 to +0.40:      985
+0.40 to +0.50:      426
+0.50 to +0.60:      315
+0.60 to +0.70:      180
+0.70 to +0.80:      166
+0.80 to +0.90:      218
+0.90 to +1.00:      183
```

The mass is tightly centred on zero (98.87% of all 3.16M pairs have
\|Rho\| < 0.1), which is exactly the expected shape for ukb-b: essentially
every UKB-b analysis shares most of its ~500k-participant sample with every
other one, so sample overlap is close to universal and does *not* by itself
drive Rho up — under the CML model Rho = phenotypic correlation × overlap
proportion, so with near-total overlap, Rho tracks phenotypic correlation
almost directly. A long, thin right-leaning tail of genuinely correlated pairs
sits above that mass.

## Spot checks (human review)

**Top 8 pairs by |Rho|** (highest correlation in the whole matrix, found by
sorting — not cherry-picked):

| Rho | n_null | Pair |
|---|---|---|
| +1.0000 | 104,472 | Average evening vs. daytime sound level of noise pollution |
| +1.0000 | 104,468 | Average evening vs. night-time sound level of noise pollution |
| +1.0000 | 90,394 | Heel BMD T-score (automated) vs. heel QUI (direct entry) |
| +1.0000 | 90,780 | Heel QUI, left vs. heel BMD T-score, left |
| +1.0000 | 104,693 | Average 24-hour vs. 16-hour sound level of noise pollution |
| +1.0000 | 90,615 | Heel QUI, right vs. heel BMD T-score, right |
| +1.0000 | 104,560 | Average 24-hour vs. evening sound level of noise pollution |
| +1.0000 | 104,771 | Average daytime vs. night-time sound level of noise pollution |

Both clusters are exactly right: the noise-pollution traits are the same
residential-address noise model evaluated at different times of day (near-
identical measurement, near-identical sample); heel BMD T-score is *derived
from* the heel QUI ultrasound reading, so they are two summaries of the same
scan. Notably the heel pairs resolve at **laterality** (left-with-left,
right-with-right) rather than mixing sides — the estimator is not just
picking up "any heel trait," it is correctly separating same-limb pairs from
cross-limb ones.

**Directed spot checks** (chosen before looking, from `analyses.tsv` labels):

| Rho | n_null | Pair |
|---|---|---|
| +0.9897 | 79,130 | BMI vs. BMI (two separate UKB-b array instances of the same field) |
| +0.9897 | 78,532 | Weight vs. Weight (two array instances) |
| +0.8228 | 64,486 | Weight vs. BMI |
| +0.1876 | 44,908 | Standing height vs. Weight |
| −0.0685 | 43,404 | Standing height vs. BMI |
| −0.0015 | 36,165 | Cheese consumers vs. Standing height (unrelated control) |
| −0.0275 | 38,618 | Cheese consumers vs. BMI (unrelated control) |

All directionally and magnitude-wise sensible: duplicate array instances of
the *same* field are near-1 (as expected — same trait, same participants);
BMI/Weight are strongly related (BMI is derived from weight); height/weight
show the expected mild positive relationship; the unrelated dietary-trait
control pairs sit within noise of zero.

## Findings vs. issue 051 acceptance criteria

- ✅ Completes in minutes, not hours (2m14s), bounded and recorded memory (~47.5 GB peak).
- ✅ Completed store validates (`opengwasdb validate` → valid, 15m33s).
- ✅ Rho distribution passes review: known/derived/duplicate trait pairs are strongly elevated (up to 1.0), unrelated pairs cluster near 0, support is sensible throughout.
- No surprises requiring a hypothesis in the "something looks wrong" sense. One observation worth recording for future tuning: **0 of 3,158,841 pairs were below `min_nulls=500`** — at ukb-b's sample sizes and `window_bp=15000` (177,862 thinned variants), support is never the binding constraint (minimum observed support was 5,485, ~11x the floor). A smaller store, or a much larger `window_bp` (coarser thinning, fewer variants), would be needed before `min_nulls` starts actually gating any pairs here.

## Out of scope / not re-verified here

Per ADR 0025 and the PRD, `n_workers` is a pure runtime knob with byte-identical
output to the serial path — this is unit-tested in `tests/test_dense_rho.py`
(`test_parallel_build_matches_serial_build`) on a synthetic store and was not
independently re-run at genome scale (would cost another full build for a
already-covered guarantee).
