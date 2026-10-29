# LD Reference Panels store eigendecompositions, not LD matrices

Reference completion imputes Z via elastic net on the leading eigenvectors of each
LD block. Tracing the consumption path, the full LD matrix is never read when an
eigendecomposition is present: both completion layouts call
`load_ld_eigenvectors`, which returns from the `.ldeig.npz` cache and only falls
back to `_load_from_ld_matrix` when that cache is missing or unreadable. Inside
`impute_z_block`, eigenvalues serve solely to count components
(`n_comp = len(eigenvalues)`); the imputation itself consumes eigenvectors alone.

The existing UKB EUR panel already reflects this in practice — a sampled block
stores `values (7665,)` alongside `vectors (7665, 250)`, so only 250 components
are retained and the adjacent 219 MB `.unphased.vcor1.gz` is dead weight. Across
the panel that is ~1.3 TB of matrices supporting ~19 GB of decompositions.

## Decision

An LD Reference Panel stores the **eigendecomposition of each LD block** — all
eigenvalues plus a variance-driven number of eigenvectors — and does not retain
the LD matrix. The matrix is computed transiently during panel generation, fed to
the eigendecomposition, and discarded.

The number of eigenvectors stored is **chosen by cumulative variance, not a fixed
count**, and both the stored count and the variance it achieves are recorded per
block as panel provenance. Consumers truncate again at load time (default 0.9);
storing a variance-driven count guarantees that truncation is satisfied from
stored data rather than silently bounded by it. The historical fixed 250 was
calibrated on UKB EUR (n ≈ 50,000), whose eigenvalue spectrum is sharply
concentrated; a panel built from ~10² fewer individuals has a flatter spectrum and
needs more components to reach the same variance, so inheriting that constant
would bind hardest on the weakest panels.

This makes the eigendecomposition a **primary panel artifact rather than a cache**,
so `load_block` must treat the LD matrix as optional when the decomposition is
present, and blocks lacking a decomposition must be backfilled before the matrix
fallback is removed.

## Considered options

- **Retain full matrices plus an eigendecomposition cache (status quo).** Rejected:
  ~70× the storage for data no consumer reads, and it invites the assumption that
  a panel can be reconstructed or re-analysed from matrices that low-rank
  estimation never justified keeping.
- **Store only the eigenvectors used at load time (the 0.9-truncated set).**
  Rejected: smallest, but permanently locks the truncation threshold — the
  decomposition could never be re-truncated at a different level, and the
  eigenvalue spectrum needed to judge whether a panel is well-conditioned would be
  gone.
- **Store a fixed component count, inherited from the UKB pipeline.** Rejected: a
  constant tuned at n ≈ 50,000 silently under-resolves panels built at n ≈ 10²,
  which is precisely where imputation quality is already most at risk.

## Consequences

- Reconstruction remains available in principle: for R = VΛVᵀ, retaining V and Λ
  to numerical rank reproduces R exactly, so nothing recoverable is discarded —
  only the dimensions that carry no information.
- Panel generation's peak working storage and compute are unchanged; only the
  retained artifact shrinks. A p×p matrix is still computed per block.
- `load_ld_eigenvectors` should warn when the stored component count bounds the
  requested truncation, converting a silent loss of variance into a visible one.
- Panels become describable by their LD representation, which the store-format
  spec's LD Reference Panel requirements already anticipate as a recorded field.
- `match_variants` in the panel module does not match real panels (it compares
  panel SNP ids verbatim against store ALIDs) and is imported but never called;
  it should be fixed or deleted rather than left as an apparent entry point.
