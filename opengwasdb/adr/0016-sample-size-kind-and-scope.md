# Factor sample-size kind and scope

Analysis metadata represents sample size with two independent fields rather than one expanding mode enum. Kind distinguishes participant count, case/control counts, effective N, and unknown values; scope distinguishes Analysis-wide values, per-variant values, and absence.

When source data provides per-variant case and control counts, the builder selects the smallest lossless representation automatically. It may use scalar counts, total N plus a constant case fraction, sparse residuals from that fraction, or full per-variant counts; the selected encoding does not alter the source semantics exposed by queries.

