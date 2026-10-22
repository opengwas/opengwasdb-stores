# AMR LD calibration pilot: evidence (#241)

**Status:** calibration evidence for the owner's AMR decision in #229. This is
**not** a production panel, and the run claims no PASS. No production panel,
population mapping, block definition or Store was changed.

**Run:**
- Output: `/data/opengwasdb/work/epic232/229/amr-pilot-20261021T234743Z/`. The
  receipt and summary are copied to
  [`data/amr-ld-pilot-receipt.json`](data/amr-ld-pilot-receipt.json) and
  [`data/amr-ld-pilot-summary.json`](data/amr-ld-pilot-summary.json).
- Revision `0b11759`, branch `ticket/241-amr-ld-pilot`.
- Ran from 2026-10-21T23:47:44Z to 2026-10-22T14:58:12Z. All 79 steps exited 0.
- Command:

  ```sh
  taskset -c 220-223 env PYTHONNOUSERSITE=1 \
    pixi run -e dev python3 tools/ld-panel/run_amr_pilot.py --out <dir>
  ```

## Owner decisions as implemented

| Decision | Implementation |
|---|---|
| 1kGP AMR samples from the existing gnomAD cache | The native GRCh38 gnomAD v3.1.2 HGDP+1kGP VCFs. No download, no Phase 3 GRCh37 genotypes. |
| Exclude HGDP | The pilot mapping (`resources/reference-resources/hgdp1kgp-hg38-amr-ld/populations_to_superpop.tsv`, under `hgdp1kgp-hg38-ld/amr-pilot/` when the pilot ran) includes only CLM, MXL, PEL and PUR. It excludes Pima, Maya, Karitiana, Surui and Colombian by name. The production mapping is unchanged. |
| EUR block boundaries | Five frozen EUR-layout blocks (`amr-pilot/blocks.tsv`), the same five regions as the EUR calibration control's comparison. LD is estimated from AMR genotypes only. |
| Start with 345 | The 347 Phase 3 AMR IDs, filtered by gnomAD `high_quality`, give 345. Nothing is substituted for the two failures. |
| MAF = 0.01 | `construct_block --maf 0.01`. MAF is the minor-allele count over twice the called individuals. A MAF floor replaces the MAC floor (every block's provenance records `mac_threshold: null`), and the CLIs refuse both together. |
| FILTER (asked in-session) | Match the existing panels: no FILTER filter. Composition is reported below. |

## Source and cache

All five chromosome VCFs declare GRCh38 and answer an indexed region query. All
1,116 AMR and EUR-control members appear in every header. The receipt records
each VCF's size, mtime, cache marker and tabix-index SHA-256.

The cached per-ancestry PGENs cannot serve the pilot:
- they exist only for AFR, EAS, EUR and SAS;
- they were converted with `MAC >= 50`, a 7.2% MAF filter at n = 345.

The raw VCFs are unfiltered, so the MAF 0.01 floor is supported and there is no
blocker. Each block region is read by index for the 345 members and converted
with no frequency floor. The floor is then applied once, in `construct_block`.

## Membership

| | Count |
|---|---:|
| Phase 3 AMR IDs (IGSR `integrated_call_samples_v3.20130502.ALL.panel`, SHA-256 `b4023dc6…76858`) | 347 |
| Excluded: gnomAD `high_quality` false (HG01137, HG01366) | 2 |
| Members: CLM 92, MXL 64, PEL 85, PUR 104 | **345** |
| High-quality 1kGP AMR samples outside Phase 3 (the 30× expansion), not admitted | 142 |

**Relatedness** is reported, not filtered on, because no kinship threshold is
owner-approved:
- gnomAD's relatedness inference flags four members as related: HG01348,
  HG01365, HG02298 and NA19657.
- Only one related pair falls within the 345: NA19657–NA19786, kinship 0.069,
  IBD0 0.72 (second- to third-degree). Both are retained.
- No member is a subcontinental-PCA outlier.

The `high_quality` flag is not evidence of independence.

## Blocks: integrity

| Block | Input records | All-missing | Invariant | Below floor | ALID collision | Zero variance | Retained |
|---|---:|---:|---:|---:|---:|---:|---:|
| chr1:222798680-224041489 | 77,430 | 46 | 57,440 | 12,382 | 761 | 22 | 6,779 |
| chr6:30106780-30425675 | 18,034 | 2 | 11,907 | 2,909 | 291 | 0 | 2,925 |
| chr11:30827313-33937829 | 181,489 | 25 | 138,733 | 28,504 | 2,291 | 7 | 11,929 |
| chr16:60049115-62536937 | 162,892 | 113 | 125,504 | 24,286 | 1,855 | 21 | 11,113 |
| chr22:32439511-33959775 | 98,850 | 31 | 73,618 | 15,729 | 1,306 | 3 | 8,163 |

The classes add up to the input, and provenance records each block's
below-floor MAC strata. Singletons are 60–66% of the below-floor variants.

| Block | Rank | Components (0.99) | Achieved variance | Components at 0.9 (core) | λ₁/λₖ | Binding error | Verification |
|---|---:|---:|---:|---:|---:|---:|---|
| chr1:222798680-224041489 | 344 | 316 | 0.9901 | 186 (186) | 160 | 1.9e-15 | pass |
| chr6:30106780-30425675 | 344 | 250 | 0.9979 | 39 (39) | 2274 | 1.5e-15 | pass |
| chr11:30827313-33937829 | 344 | 318 | 0.9901 | 189 (189) | 131 | 1.4e-15 | pass |
| chr16:60049115-62536937 | 344 | 313 | 0.9901 | 177 (177) | 152 | 1.8e-15 | pass |
| chr22:32439511-33959775 | 344 | 313 | 0.9901 | 180 (180) | 159 | 9.6e-16 | pass |

`verify_block` re-reads each written block against freshly extracted genotypes.
It checks:
- one row per eigenvector row, with unique canonical ALIDs;
- finite, non-negative, descending eigenvalues;
- agreement with provenance, and EAF;
- exact row and allele binding (`||X v_j||^2 = λ_j`, relative error ≤ 2e-15);
- loading through core's `list_all_blocks` and `load_ld_eigenvectors`.

`find_underresolved_blocks --thresh 0.9` reports zero. Every block is
rank-deficient at n − 1 = 344, as ADR 0020 anticipates. The MHC block reaches
0.99 of the variance in under 250 components, so the 250-component floor sets
its count.

### Retained variants

| Block | MAC < 7 | MAC 7–9 | MAC 10–19 | MAC 20–49 | MAC ≥ 50 | Median MAF | > 5% missing | > 50% missing | Non-PASS FILTER |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| chr1:222798680-224041489 | 686 | 880 | 957 | 1,235 | 3,021 | 0.071 | 26.6% | 14.2% | 15.5% |
| chr6:30106780-30425675 | 60 | 341 | 420 | 656 | 1,448 | 0.074 | 9.1% | 3.1% | 4.0% |
| chr11:30827313-33937829 | 1,043 | 1,322 | 1,986 | 1,584 | 5,994 | 0.088 | 20.7% | 10.9% | 9.8% |
| chr16:60049115-62536937 | 694 | 1,465 | 1,880 | 1,373 | 5,701 | 0.087 | 18.6% | 8.4% | 7.2% |
| chr22:32439511-33959775 | 514 | 843 | 1,164 | 1,660 | 3,982 | 0.076 | 17.1% | 8.6% | 9.2% |

About half of every block's retained variants have MAC < 50, which the inherited
floor would have dropped.

A variant can show **MAC < 7 at MAF ≥ 0.01** only when calls are missing. MAF is
measured over called individuals, so sparsely called sites pass: up to 99.4% of
calls are missing at a retained site. Outside the MHC block, 8–14% of retained
variants are more than 50% missing. No missingness filter was approved, so this
is reported rather than applied.

## Held-out genotype calibration

The pilot may read reference genotypes only, so its GWAS is simulated (`tools/ld-panel/heldout_calibration.py`):
1. Split the 345 into 230 training and 115 held-out individuals, stratified by population.
2. Build the panel from the training individuals.
3. Draw each replicate's z-scores with the held-out individuals' own LD: one to
   three causal variants scaled to a peak |z| of 8, plus N(0, R_test) noise.
4. Mask 20% of variants and impute them with core's `impute_z_block`
   (truncation 0.9, `min_cor` 0.7).

A panel built from the held-out individuals themselves gives the in-sample upper
bound. Each block ran 20 replicates per panel.

| Block | Shared variants | Components at 0.9 (held-out / in-sample) | Accepted | Held-out median r (min) | Held-out median slope | Held-out median RMSE | In-sample median r |
|---|---:|---:|---:|---:|---:|---:|---:|
| chr1:222798680-224041489 | 5,583 | 134 / 74 | 20/20, 20/20 | 0.865 (0.697) | 0.77 | 0.87 | 0.983 |
| chr6:30106780-30425675 | 2,624 | 36 / 29 | 20/20, 20/20 | 0.976 (0.943) | 0.95 | 0.51 | 0.993 |
| chr11:30827313-33937829 | 10,103 | 137 / 76 | 20/20, 20/20 | 0.875 (0.774) | 0.77 | 0.87 | 0.986 |
| chr16:60049115-62536937 | 9,397 | 129 / 76 | 20/20, 20/20 | 0.852 (0.706) | 0.72 | 0.81 | 0.982 |
| chr22:32439511-33959775 | 7,204 | 133 / 76 | 20/20, 20/20 | 0.804 (0.703) | 0.64 | 0.91 | 0.974 |

Every replicate passed the quality gate. Outside the MHC, held-out masked-z
recovery has a median r of 0.80–0.88, against 0.97–0.99 in-sample. The slopes of
0.64–0.77 mean imputed z-scores are shrunk toward zero.

**These numbers are not comparable with the EUR control's 0.963**
(`docs/hgdp1kgp-ld-release-evidence.md`). That figure is a real-GWAS masking
comparison at n = 771. Here, the "GWAS" LD is itself a 115-person estimate, which
makes the metric pessimistic. It is also optimistic: training and held-out
individuals come from the same four populations, which need not match MVP's
Hispanic/Latin American ancestry mix.

## Boundary edges

Each of the ten block edges was tested for LD crossing it. The test takes a
±250 kb window at the edge and applies the same MAF floor separately in each
cohort: the 345 AMR members and the 771 HGDP+1kGP EUR-control members
(`tools/ld-panel/boundary_ld.py`). It then measures r² between every variant left
of the edge and every variant right of it.

| Edge | Left / right variants (AMR) | Pairs r² ≥ 0.2, AMR / EUR | Pairs r² ≥ 0.8, AMR / EUR | Max r², AMR / EUR | Left variants with an r² ≥ 0.5 partner, AMR / EUR |
|---|---:|---:|---:|---:|---:|
| chr1:222,798,680 | 880 / 1,253 | 9.2e-04 / 1.1e-04 | 1.8e-06 / 1.0e-06 | 0.94 / 0.82 | 0.8% / 0.9% |
| chr1:224,041,489 | 1,836 / 1,552 | 1.0e-03 / 5.8e-04 | 2.8e-06 / 4.0e-06 | 0.95 / 0.92 | 1.9% / 1.3% |
| chr6:30,106,780 | 10,014 / 2,074 | 1.3e-02 / 1.9e-02 | 1.0e-04 / 7.7e-05 | 1.00 / 0.95 | 14.3% / 13.2% |
| chr6:30,425,675 | 2,101 / 1,741 | 3.7e-02 / 6.8e-02 | 1.5e-02 / 2.0e-02 | 1.00 / 1.00 | 37.1% / 46.4% |
| chr11:30,827,313 | 1,098 / 529 | 1.5e-02 / 1.7e-02 | 3.3e-03 / 6.7e-03 | 1.00 / 1.00 | 8.9% / 9.5% |
| chr11:33,937,829 | 1,114 / 1,161 | 1.8e-04 / 3.5e-05 | 7.7e-07 / 0.0e+00 | 0.92 / 0.65 | 0.1% / 0.2% |
| chr16:60,049,115 | 1,406 / 1,400 | 1.8e-03 / 5.3e-04 | 3.0e-06 / 4.8e-06 | 0.95 / 0.89 | 2.8% / 2.2% |
| chr16:62,536,937 | 1,191 / 1,064 | 1.7e-03 / 1.1e-04 | 2.8e-05 / 0.0e+00 | 0.95 / 0.60 | 7.0% / 0.2% |
| chr22:32,439,511 | 2,298 / 1,687 | 5.1e-04 / 2.1e-04 | 7.7e-06 / 7.2e-06 | 0.98 / 0.99 | 1.7% / 1.5% |
| chr22:33,959,775 | 1,419 / 1,154 | 1.3e-03 / 2.6e-05 | 6.1e-07 / 0.0e+00 | 0.81 / 0.53 | 1.3% / 0.1% |

**Weak LD crossing an edge** (r² ≥ 0.2) is more common in AMR at 7 of 10 edges,
by 1.8–50×. Strong crossing LD (r² ≥ 0.8) is rare in both cohorts. The exceptions
are the two MHC edges and chr11:30.8 Mb, where the EUR boundaries cut LD in the
EUR cohort as much as in AMR. At chr16:62.5 Mb, 7.0% of the AMR variants left of
the edge have an r² ≥ 0.5 partner across it, against 0.2% in EUR.

These summaries are not corrected for sampling noise in r², which is larger at
n = 345 than at 771 and for low-MAF variants. Part of the AMR excess may be
noise.

**Ran:** this cross-boundary r² comparison, at the ten edges.

**Not run:**
- imputation across an edge;
- the AFR-boundary sensitivity;
- de novo AMR boundaries;
- any genome-wide boundary assessment.

## Resources

| Stage | CPU | Wall | Peak RSS |
|---|---:|---:|---:|
| Block extraction (bcftools, 8.4 Mb in total) | 11.5 min | 1.1 h | 0.31 GB |
| Conversion (plink2 `--threads 4`) | 1.3 min | 0.1 h | 1.2 GB |
| Construction (`run_panels --jobs 1`) | 0.8 min | 0.1 h | 2.8 GB |
| Verification | 0.8 min | 0.2 h | 2.8 GB |
| Calibration (20 replicates × 2 panels × 5 blocks) | 1.0 min | 0.3 h | 2.8 GB |
| Edge extraction, conversion and r² (10 edges × 2 cohorts) | 15.5 min | 13.3 h | 1.4 GB |
| **Total** | **31 min** | **15.2 h** | **2.8 GB** |

The steps ran one at a time, with one genotype reader, BLAS at one thread and a
4-CPU affinity.
- **Wall time** was almost all I/O wait: VCF extraction used 1–8% CPU. Over the
  run, `/data` was also serving the concurrent OGS-00009 forced build's GWAS-VCF
  readers and the nightly backup snapshot, which is known to throttle it. Neither
  cause was diagnosed further, so these wall times are not a budget.
- **Disk:** outputs total 5.5 GB. 5.4 GB of that is the extracted per-region VCFs,
  which can be deleted; the panel is 75 MB.

## Findings outside the AMR question

1. **The shipped HGDP+1kGP panels hold two rows under one ALID.**
   - Cause: gnomAD splits multi-allelic sites, so at a homopolymer an insertion
     `G>GA` and a deletion `GA>G` at one position both canonicalise to `pos:G:GA`.
   - Extent: every block of AFR, EAS, SAS and the EUR control has pairs. That is
     291,411, 116,794, 141,543 and 134,043 duplicated ALIDs, 1.3–2.0% of rows.
   - Effect: core maps both rows to one Store variant. #228 (AFR) and #230 (EAS)
     complete against these panels.
   - The pilot excluded every record of a shared ALID: 9–16% of above-floor
     records in these blocks, mostly homopolymer indels. After the owner's
     decision below, `construct_block` keeps the more frequent record instead.
     The shipped panels are unchanged; #348 tracks them.
   - UKB EUR (`ld/EUR`) has no duplicates on chr22.
2. **A MAF floor over called individuals admits sparsely called sites** (see
   Retained variants above). The MAC ≥ 50 floor at n ≈ 800 kept most of them out
   implicitly.

## Limitations

- n = 345 caps every block at rank 344. Calibration trains on 230.
- Calibration is simulated from reference genotypes. No GWAS association data was
  read, and #241 requires a separate read plan before any is.
- One related pair (kinship 0.069) is retained. No relatedness pruning
  sensitivity was run.
- Neither FILTER nor missingness was filtered. ALID collisions were excluded.
- This covers five blocks. Nothing here estimates genome-wide quality or cost.

## Measured recommendation for the next stage

This is evidence for the owner's decision, not acceptance.

1. **Integrity:** the tooling builds AMR blocks at MAF 0.01 that verify exactly,
   load in core and are resolved at the consumer's 0.9 truncation. Mechanically,
   a genome-wide build is ready.
2. **Decide variant QC before any genome-wide build.** The pilot shows three
   choices that change the panel:
   - a missingness ceiling: 8–14% of retained variants are more than 50% missing
     outside the MHC;
   - FILTER PASS-only: 4–16% of retained variants are non-PASS;
   - the ALID-collision rule: currently every record is excluded.
3. **The accuracy evidence cannot yet support a production call.** Held-out
   median r is 0.80–0.88 outside the MHC, but the metric has no anchor. Two ways
   to close that:
   - Run the same held-out design on a 345-person subsample of the EUR control
     cohort at these blocks. That anchors the metric to an ancestry whose
     real-GWAS performance is known (0.963 at n = 771). It reads reference
     genotypes only.
   - Run a masked-variant evaluation on OGS-00014's AMR-assigned Analyses. This
     needs an association read plan.
4. **Boundaries:** EUR boundaries serve a calibration build. Because AMR shows
   more weak crossing LD at most edges, a production gate should add the
   AFR-boundary sensitivity and an imputation test across an edge.
5. **Shipped panels:** the duplicated-ALID defect in the shipped panels should be
   resolved before #228 and #230 complete against them.

## Owner decisions after the pilot (2026-10-22)

The owner accepts this quality as the best available until a better AMR
reference exists, and is considering the genome-wide build. Variant QC for any
build from these tools:

| Rule | Implementation |
|---|---|
| Drop variants that are not PASS | `conversion_command(pass_only=True)`, which is plink2 `--var-filter`, at conversion. FILTER does not survive into the PGEN. |
| Require ≥ 80% of individuals called | `construct_block --max-missing 0.2`, inclusive at exactly 80%. Drops are counted as `n_variants_high_missing`. |
| For a shared ALID, keep one record | Of the records that pass every filter, keep the one with the higher MAF, then the higher call rate, then the first. Provenance records `alid_collision_rule: keep_highest_maf`. |

Measured on the pilot blocks' own genotypes and FILTER values:

| | Records / variants |
|---|---:|
| Pass MAF ≥ 0.01 | 47,466 |
| …and PASS and ≥ 80% called | 38,891 |
| Collision pairs among those (no ALID names more than two) | 938 |
| Retained if both records of a pair are dropped | 37,015 |
| **Retained when one record is kept** | **37,953** |

PASS and the call-rate rule resolve most collisions on their own: the pilot's
drop-all rule removed 6,504 records. In a remaining pair, the two records differ
in MAF by a median of about 0.10, so they are distinct variants. The kept record
is the one a GWAS reporting that ALID most likely means; the ALID itself cannot
say which.

`run_amr_pilot.py` now applies all three rules by default. The calibration and
boundary numbers above predate them.
