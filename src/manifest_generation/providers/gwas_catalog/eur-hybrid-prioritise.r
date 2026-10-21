## Prioritise analyses within a hybrid store (default: the EUR hybrid store)
## for inclusion in a reference-completed array, and flag redundant analyses
## that can be dropped to save storage and simplify querying / interpretation.
##
## Approach (all thresholds are tunable constants below):
##   1. Cluster the store's analyses by EFO term (MAPPED_TRAIT_URI). Analyses
##      mapped to the same trait are candidate redundancies.
##   2. Score each analysis within its cluster on three axes:
##        - power       : sample size, or n_cases for case-control traits
##        - snp_density : variants passing QC
##        - hits        : curated GWAS Catalog associations (down-weighted;
##                        see the caveat in ebi-studies.r -- this reflects
##                        curation status, not true power)
##      Axes are converted to within-cluster percentile ranks and combined
##      with WEIGHTS, so a study is judged against its same-trait peers.
##   3. Rank analyses within each cluster; keep the top KEEP_PER_CLUSTER and
##      mark the rest as redundant drop candidates.
##
## Input : resources/data/derived/store-candidates-analyses.tsv
## Output: resources/data/derived/eur-hybrid-prioritisation.tsv
##
## This is a curation aid: "redundant" means a better-powered / denser analysis
## of the same trait exists in the store, not that the analysis is worthless.

library(data.table)

TARGET_STORE_KEY <- "hybrid__European"
KEEP_PER_CLUSTER <- 3
WEIGHTS <- c(power = 0.50, snp_density = 0.35, hits = 0.15)
REF_COMPLETION_VARIANTS <- 12e6   # keep in step with ebi-studies.r
STORE_BYTES_PER_CELL <- 2.5

derived_dir <- "resources/data/derived"
analyses_path <- file.path(derived_dir, "store-candidates-analyses.tsv")
if (!file.exists(analyses_path)) {
  stop("Run `Rscript resources/scripts/ebi-studies.r` first to build ",
       analyses_path)
}

analyses <- fread(analyses_path, sep = "\t", na.strings = "")
store <- analyses[store_key == TARGET_STORE_KEY]
if (nrow(store) == 0) stop("No analyses found for store_key ", TARGET_STORE_KEY)

## Power axis: cases bind power for case-control traits; sample size otherwise.
store[, power_metric := fifelse(study_design == "case-control" & n_cases > 0,
                                as.numeric(n_cases), as.numeric(sample_size))]
store[is.na(power_metric), power_metric := 0]
store[is.na(n_variants), n_variants := 0L]

## Cluster by EFO term. Blank URIs fall back to the trait label so they are
## not all pooled into one giant "unmapped" cluster.
store[, efo_cluster := fifelse(is.na(MAPPED_TRAIT_URI) | MAPPED_TRAIT_URI == "",
                               paste0("label:", MAPPED_TRAIT), MAPPED_TRAIT_URI)]
store[, cluster_size := .N, by = efo_cluster]

## Within-cluster percentile rank (0..1, ties averaged). Singletons score 1.
pct_rank <- function(x) {
  if (length(x) == 1) return(1)
  frank(x, ties.method = "average") / length(x)
}
store[, `:=`(
  pr_power = pct_rank(power_metric),
  pr_snp   = pct_rank(as.numeric(n_variants)),
  pr_hits  = pct_rank(as.numeric(association_count))
), by = efo_cluster]

store[, priority_score := round(
  WEIGHTS["power"] * pr_power +
  WEIGHTS["snp_density"] * pr_snp +
  WEIGHTS["hits"] * pr_hits, 4)]

## Rank within cluster (1 = best). Break ties deterministically on the raw
## power metric then variant count so the ordering is stable across runs.
setorder(store, efo_cluster, -priority_score, -power_metric, -n_variants,
         STUDY.ACCESSION)
store[, cluster_rank := seq_len(.N), by = efo_cluster]
store[, recommendation := fifelse(cluster_rank <= KEEP_PER_CLUSTER,
                                  "keep", "redundant")]

## Per-analysis reference-completed size estimate (one full reference-variant
## column of the dense matrix -- hybrid analyses are genome-wide GWAS), for
## tallying the storage recovered by dropping redundant analyses.
store[, est_analysis_gb := round(REF_COMPLETION_VARIANTS * STORE_BYTES_PER_CELL / 1e9, 3)]

out <- store[, .(
  STUDY.ACCESSION, PUBMED.ID, STUDY, DISEASE.TRAIT, MAPPED_TRAIT,
  efo_cluster, cluster_size, study_design, power_metric, n_variants,
  association_count, priority_score, cluster_rank, recommendation,
  est_analysis_gb
)]
setorder(out, -cluster_size, efo_cluster, cluster_rank)
fwrite(out, file.path(derived_dir, "eur-hybrid-prioritisation.tsv"), sep = "\t")

## ---------------------------------------------------------------------
## Console summary
## ---------------------------------------------------------------------
n_keep <- sum(out$recommendation == "keep")
n_drop <- sum(out$recommendation == "redundant")
cat(sprintf("Store %s: %d analyses in %d EFO clusters.\n",
            TARGET_STORE_KEY, nrow(out), uniqueN(out$efo_cluster)))
cat(sprintf("Keep %d, drop %d redundant (%.1f%%), keeping top %d per trait.\n",
            n_keep, n_drop, 100 * n_drop / nrow(out), KEEP_PER_CLUSTER))
cat(sprintf("Estimated storage recovered by dropping redundant: %.1f GB of %.1f GB.\n",
            sum(out[recommendation == "redundant", est_analysis_gb]),
            sum(out$est_analysis_gb)))
cat("\nMost redundant traits (analyses dropped):\n")
top <- out[recommendation == "redundant", .(dropped = .N), by = .(MAPPED_TRAIT, cluster_size)][order(-dropped)]
print(head(top, 10))
