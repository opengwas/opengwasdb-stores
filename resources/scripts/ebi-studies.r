## Curate GWAS Catalog studies into candidate OpenGWASDB stores.
##
## Inputs:
##   resources/data/gwas-catalog-v1.0.3.1-studies-r2026-07-10.tsv
##   resources/data/gwas-catalog-v1.0.3.1-ancestries-r2026-07-10.tsv
##
## Every row of the studies file is one GWAS Catalog "study accession", which
## in OpenGWASDB vocabulary is one Analysis (one trait tested against
## variants). This script assigns each full-sumstats Analysis to a candidate
## store:
##
##   dense  - single publication + single ancestry, >= MIN_GROUP_SIZE
##            analyses, mostly non-molecular (complex/clinical) traits
##   ragged - single publication + single ancestry, >= MIN_GROUP_SIZE
##            analyses, mostly molecular traits (proteomics, metabolomics, ...)
##   hybrid - everything left over, pooled across publications within one
##            major ancestry group
##
## Outputs (resources/data/derived/):
##   store-candidates-analyses.tsv - one row per Analysis with its store
##                                   assignment, study design, and per-analysis
##                                   sample-size / case-control counts
##   store-candidates-summary.tsv  - one row per candidate store, including a
##                                   representative variant count and sample
##                                   size for the single-publication stores
##
## This is a curation aid, not a final decision: see `needs_review` in the
## summary output for groups where the dense/ragged call or the dominant
## ancestry are ambiguous and should be checked by hand before a Store
## Family is created (see families/_candidates/).

library(data.table)

MIN_GROUP_SIZE <- 100
MOLECULAR_FRACTION_THRESHOLD <- 0.5
MOLECULAR_REVIEW_BAND <- c(0.2, 0.8)
ANCESTRY_FRACTION_REVIEW_THRESHOLD <- 0.8

## Store-size model. Two regimes, because the storage layouts differ:
##
## Dense (and hybrid) stores are a full trait x reference-variant matrix.
## Calibrated on the IEU OpenGWAS ukb-b store: 2,500 traits reference-completed
## to 12M variants occupied ~75 GB, i.e. ~2.5 bytes per (trait x variant) cell
## (the pre-imputation point, 9.5M variants at 60 GB, gives the same 2.5
## b/cell). Reference completion projects every analysis onto the *same*
## reference panel, so size is n_analyses x REF_COMPLETION_VARIANTS x 2.5 bytes
## regardless of the study's own observed density -- a store imputed to 44M
## variants still completes to the 12M reference (~30 MB per analysis).
REF_COMPLETION_VARIANTS <- 12e6
STORE_BYTES_PER_CELL <- 2.5
GENOME_MB <- 3000

## Ragged (molecular-QTL) stores are sparse: each analysis keeps only a small
## slice of the genome -- its cis window plus significant trans regions and
## suggestive hits, roughly RAGGED_KEPT_MB of the ~3,000 Mb genome. The stored
## variant count per analysis is therefore (n_variants * RAGGED_KEPT_MB /
## GENOME_MB), at ~5 bytes per stored association (a little more than the dense
## per-cell cost because of sparse indexing overhead). e.g. 3,000 analyses at
## 10M variants -> 10e6 * 20/3000 * 5 bytes * 3000 ~= 1 GB.
RAGGED_KEPT_MB <- 20             # genomic Mb retained per analysis
RAGGED_BYTES_PER_ASSOC <- 5      # bytes per stored variant (sparse indexing)

data_dir <- "resources/data"
derived_dir <- file.path(data_dir, "derived")
dir.create(derived_dir, showWarnings = FALSE)

## ---------------------------------------------------------------------
## Parsing helpers
## ---------------------------------------------------------------------

## Number of variants passing QC, taken from the "PLATFORM [SNPS PASSING QC]"
## field. The count is inside square brackets and may be prefixed with
## qualifiers such as "up to" or "at least" (e.g. "Illumina [at least
## 10916125] (imputed)"); we keep the bracketed integer and ignore the words.
parse_variant_count <- function(platform) {
  inside <- sub(".*\\[([^]]*)\\].*", "\\1", platform)
  inside[!grepl("\\[[^]]*\\]", platform)] <- NA_character_
  digits <- gsub("[^0-9]", "", inside)
  out <- suppressWarnings(as.numeric(digits))
  out[is.na(inside) | digits == ""] <- NA_real_
  out
}

## Case-control/quantitative detection and sample-size counts are resolved by
## the shared `gwas-catalog-ssf` metadata resolver (issue #48), not parsed
## inline here -- see resources/generators/lib/metadata_resolvers/gwas_catalog_ssf.R for
## the resolver contract shared with other Source Collections' resolvers.
source("resources/generators/lib/metadata_resolvers/gwas_catalog_ssf.R")

## Adapts the resolver's generic {resolution_status, stored_effect_scale,
## sample_size_kind, sample_size, n_cases, n_controls} record onto this
## script's legacy store-candidate output columns (study_design,
## n_cases, n_controls, sample_size), preserving their existing values and
## semantics exactly: n_cases/n_controls are 0 (not NA) for a resolved
## quantitative study, and every column is NA when unresolved.
resolve_study_design_columns <- function(x) {
  resolved <- resolve_gwas_catalog_ssf_metadata(x)
  if (resolved$resolution_status == "unresolved") {
    return(data.table(n_cases = NA_real_, n_controls = NA_real_,
                      sample_size = NA_real_, study_design = NA_character_))
  }
  is_case_control <- resolved$sample_size_kind == "case_control"
  data.table(
    n_cases = if (is_case_control) resolved$n_cases else 0,
    n_controls = if (is_case_control) resolved$n_controls else 0,
    sample_size = resolved$sample_size,
    study_design = if (is_case_control) "case-control" else "quantitative"
  )
}

## Assign a molecular-omics type to free text (a trait label or a study
## title). Patterns are checked most-specific first so that, e.g., a lipid
## species is called "lipidomics" rather than the more generic
## "metabolomics". Returns NA for non-molecular (complex/clinical) text.
MOLECULAR_TYPE_PATTERNS <- list(
  microbiome   = "microbiom|microbiota|relative abundance of|gut bacteri",
  glycomics    = "glycom|glycan|glycosylation",
  lipidomics   = paste0("lipidom|complex lipid|lipid species|acylcarnitine|",
                        "sphingomyel|phosphatidyl|ceramide|cholesteryl ester|",
                        "lysophosphatid|\\blipids?\\b"),
  metabolomics = "metabolom|metabolite|metabonomic|metabolic biomarker",
  proteomics   = "proteom|proteins?\\b|\\bpqtl\\b|proteogenom|immunoglobulin",
  `immune-cell` = paste0("\\b[bt] cells?\\b|nk cells?\\b|lymphocyte|monocyte|",
                        "leukocyte|absolute count|immune cell|cytokine")
)
classify_molecular <- function(text) {
  text <- tolower(text)
  type <- rep(NA_character_, length(text))
  for (label in names(MOLECULAR_TYPE_PATTERNS)) {
    hit <- is.na(type) & grepl(MOLECULAR_TYPE_PATTERNS[[label]], text, perl = TRUE)
    type[hit] <- label
  }
  type
}

## Summary statistics that return NA (rather than warning + Inf) when every
## value in a store is missing.
safe_median <- function(x) if (all(is.na(x))) NA_real_ else median(x, na.rm = TRUE)
safe_min <- function(x) if (all(is.na(x))) NA_real_ else min(x, na.rm = TRUE)
safe_max <- function(x) if (all(is.na(x))) NA_real_ else max(x, na.rm = TRUE)

studies_path <- file.path(data_dir, "gwas-catalog-v1.0.3.1-studies-r2026-07-10.tsv")
ancestries_path <- file.path(data_dir, "gwas-catalog-v1.0.3.1-ancestries-r2026-07-10.tsv")

studies <- fread(studies_path, sep = "\t", quote = "", na.strings = NULL, encoding = "UTF-8")
setnames(studies, make.names(names(studies)))

ancestries <- fread(ancestries_path, sep = "\t", quote = "", na.strings = NULL, encoding = "UTF-8")
setnames(ancestries, make.names(names(ancestries)))

## ---------------------------------------------------------------------
## 1. Keep only analyses with full summary statistics available
## ---------------------------------------------------------------------
full <- studies[FULL.SUMMARY.STATISTICS == "yes"]

## ---------------------------------------------------------------------
## 2. Derive one dominant major ancestry group per study accession
##
## A study accession can have several "initial" (discovery) stage rows in
## the ancestries table, one per contributing cohort, and each row's
## BROAD ANCESTRAL CATEGORY can itself already be a compound label (e.g.
## "European, NR"). Atomic categories are mapped to major groups, a row
## with more than one distinct major group is treated as "Multiple/Mixed",
## and then rows are pooled per accession, weighted by sample size, to
## pick the largest contributing major group.
## ---------------------------------------------------------------------
ancestry_map <- c(
  "European" = "European",
  "East Asian" = "East Asian",
  "South Asian" = "South Asian",
  "South East Asian" = "South East Asian",
  "Central Asian" = "Central Asian",
  "African American or Afro-Caribbean" = "African",
  "African unspecified" = "African",
  "Sub-Saharan African" = "African",
  "Hispanic or Latin American" = "Hispanic or Latin American",
  "Greater Middle Eastern" = "Greater Middle Eastern",
  "Native American" = "Native American",
  "Oceanian" = "Oceanian",
  "Aboriginal Australian" = "Oceanian",
  "Asian unspecified" = "Asian (unspecified)",
  "Other" = "Other",
  "Other admixed ancestry" = "Multiple/Mixed",
  "NR" = "NR/Unknown"
)

major_group_for_row <- function(raw) {
  stripped <- gsub("\\([^)]*\\)", "", raw)
  toks <- trimws(unlist(strsplit(stripped, ",")))
  toks <- toks[toks != ""]
  groups <- unique(unname(ancestry_map[toks]))
  groups <- groups[!is.na(groups)]
  if (length(groups) != 1) return("Multiple/Mixed")
  groups
}

init <- ancestries[STAGE == "initial"]
init[, n_individuals := suppressWarnings(as.numeric(NUMBER.OF.INDIVIDUALS))]
init[is.na(n_individuals), n_individuals := 0]
init[, major_group := vapply(BROAD.ANCESTRAL.CATEGORY, major_group_for_row, character(1))]

study_ancestry <- init[, .(n_individuals = sum(n_individuals)),
                        by = .(STUDY.ACCESSION, major_group)]
setorder(study_ancestry, STUDY.ACCESSION, -n_individuals)
study_ancestry[, total_individuals := sum(n_individuals), by = STUDY.ACCESSION]
dominant_ancestry <- study_ancestry[, .SD[1], by = STUDY.ACCESSION]
dominant_ancestry[, ancestry_fraction := fifelse(total_individuals > 0,
                                                  n_individuals / total_individuals,
                                                  NA_real_)]
setnames(dominant_ancestry, "major_group", "ancestry_group")
dominant_ancestry <- dominant_ancestry[, .(STUDY.ACCESSION, ancestry_group, ancestry_fraction)]

full <- merge(full, dominant_ancestry, by = "STUDY.ACCESSION", all.x = TRUE)
full[is.na(ancestry_group), ancestry_group := "NR/Unknown"]

## ---------------------------------------------------------------------
## 3. Flag molecular-trait analyses (proteomics, metabolomics, and
## related omics), the signal that separates dense from ragged stores
## ---------------------------------------------------------------------
molecular_pattern <- paste(
  "protein", "peptide", "metabolite", "metabolomic", "proteomic", "proteome",
  "metabolome", "lipidomic", "lipidome", "glycan", "glycomic", "analyte",
  "somascan", "olink", "amino acid", "apolipoprotein", "lipoprotein",
  "expression level", "transcript", "methylation", "microbiome",
  "metabonomic", "xenobiotic", "gene expression",
  sep = "|"
)
full[, is_molecular := grepl(molecular_pattern,
                              paste(DISEASE.TRAIT, MAPPED_TRAIT),
                              ignore.case = TRUE)]

## ---------------------------------------------------------------------
## 3b. Per-analysis variant count and sample-size / case-control counts
##
## Variant counts come straight from the platform field. Sample sizes are
## parsed from the free-text INITIAL SAMPLE SIZE field, which is expensive
## to parse, so we parse each distinct string once and join back (large
## molecular studies reuse one string across thousands of analyses).
## ---------------------------------------------------------------------
full[, n_variants := as.integer(parse_variant_count(PLATFORM..SNPS.PASSING.QC.))]

## GWAS Catalog ASSOCIATION COUNT: the number of genome-wide-significant
## associations for the analysis (p-value based, from the summary statistics).
## This is an objective power signal at the store level: if almost none of a
## study's analyses have any hit, the study was likely underpowered overall.
## (Sample size alone does not determine power -- case count matters more for
## binary traits, and heritability / polygenicity contribute too.)
full[, association_count := suppressWarnings(as.integer(ASSOCIATION.COUNT))]
full[is.na(association_count), association_count := 0L]
full[, has_gwas_hit := association_count > 0L]

## Per-analysis molecular subtype from the trait text (NA for non-molecular).
full[, molecular_subtype := classify_molecular(paste(DISEASE.TRAIT, MAPPED_TRAIT))]

ss_unique <- unique(full$INITIAL.SAMPLE.SIZE)
ss_lookup <- rbindlist(lapply(ss_unique, resolve_study_design_columns))
ss_lookup[, INITIAL.SAMPLE.SIZE := ss_unique]
full <- merge(full, ss_lookup, by = "INITIAL.SAMPLE.SIZE", all.x = TRUE)

## ---------------------------------------------------------------------
## 4. Group by (pubmed ID, ancestry group) and classify each group
##
## Per-row trait text is not a reliable molecular signal on its own: large
## SomaScan/Olink-style proteomics releases name each analyte by its
## specific protein (e.g. "26S proteasome non-ATPase regulatory subunit 1
## levels") rather than the generic word "protein", so the row-level
## fraction can badly understate how molecular a group is. The
## publication title reliably says "proteome"/"metabolome"/etc, so it is
## used as the primary signal, with the row-level fraction as a
## cross-check that surfaces disagreement for manual review.
## ---------------------------------------------------------------------
modal_subtype <- function(x) {
  x <- x[!is.na(x)]
  if (length(x) == 0) return(NA_character_)
  names(sort(table(x), decreasing = TRUE))[1]
}

group_stats <- full[, .(
  n_analyses = .N,
  n_molecular = sum(is_molecular),
  first_author = FIRST.AUTHOR[1],
  study_title = STUDY[1],
  earliest_date = min(DATE),
  trait_modal_type = modal_subtype(molecular_subtype),
  frac_typed = mean(!is.na(molecular_subtype))
), by = .(PUBMED.ID, ancestry_group)]

group_stats[, molecular_fraction := n_molecular / n_analyses]
group_stats[, title_is_molecular := grepl(molecular_pattern, study_title, ignore.case = TRUE)]
group_stats[, store_type := fifelse(
  n_analyses >= MIN_GROUP_SIZE,
  fifelse(title_is_molecular | molecular_fraction >= MOLECULAR_FRACTION_THRESHOLD,
          "ragged", "dense"),
  "hybrid"
)]

## Molecular type of the store's content. The study title is the strongest
## signal (a single-publication store is one omics platform); where the title
## is uninformative (e.g. a methods paper), fall back to the dominant
## per-analysis trait subtype when most analyses are typed.
group_stats[, molecular_type := {
  tt <- classify_molecular(study_title)
  fifelse(!is.na(tt), tt, fifelse(frac_typed >= 0.5, trait_modal_type, NA_character_))
}]

slugify <- function(x) gsub("(^-+|-+$)", "", gsub("[^A-Za-z0-9]+", "-", x))

group_stats[, store_key := fifelse(
  store_type == "hybrid",
  paste0("hybrid__", slugify(ancestry_group)),
  paste0(store_type, "__pmid-", PUBMED.ID, "__", slugify(ancestry_group))
)]

## ---------------------------------------------------------------------
## 5. Attach the store assignment back onto each analysis
## ---------------------------------------------------------------------
full <- merge(
  full,
  group_stats[, .(PUBMED.ID, ancestry_group, store_type, store_key,
                  title_is_molecular, molecular_type)],
  by = c("PUBMED.ID", "ancestry_group")
)

## Per-analysis reference-completed size estimate (GB). Dense/hybrid analyses
## each occupy one full reference-variant column of the matrix; ragged analyses
## keep only a genome slice (see model constants above). Summed per store below.
full[, est_analysis_gb := fifelse(
  store_type == "ragged",
  as.numeric(fcoalesce(n_variants, as.integer(REF_COMPLETION_VARIANTS))) *
    (RAGGED_KEPT_MB / GENOME_MB) * RAGGED_BYTES_PER_ASSOC / 1e9,
  REF_COMPLETION_VARIANTS * STORE_BYTES_PER_CELL / 1e9
)]

## ---------------------------------------------------------------------
## 6. Build per-store summary (hybrid rows pool across many pubmed IDs
## sharing an ancestry group, so this re-aggregates by store_key rather
## than reusing group_stats directly)
## ---------------------------------------------------------------------
store_summary <- full[, .(
  store_type = store_type[1],
  ancestry_group = ancestry_group[1],
  n_analyses = .N,
  n_pubmed_ids = uniqueN(PUBMED.ID),
  pubmed_id = if (uniqueN(PUBMED.ID) == 1) PUBMED.ID[1] else NA_integer_,
  first_author = if (uniqueN(FIRST.AUTHOR) == 1) FIRST.AUTHOR[1] else NA_character_,
  study_title = if (uniqueN(STUDY) == 1) STUDY[1] else NA_character_,
  n_molecular = sum(is_molecular),
  molecular_fraction = round(mean(is_molecular), 3),
  molecular_type = if (store_type[1] == "hybrid") NA_character_ else molecular_type[1],
  title_is_molecular = title_is_molecular[1],
  min_ancestry_fraction = round(min(ancestry_fraction, na.rm = TRUE), 3),
  n_case_control = sum(study_design == "case-control", na.rm = TRUE),
  n_quantitative = sum(study_design == "quantitative", na.rm = TRUE),
  median_variants = as.integer(safe_median(n_variants)),
  median_sample_size = as.integer(safe_median(sample_size)),
  min_sample_size = as.integer(safe_min(sample_size)),
  max_sample_size = as.integer(safe_max(sample_size)),
  prop_with_gwas_hit = round(mean(has_gwas_hit), 3),
  est_completed_size_gb = round(sum(est_analysis_gb), 1)
), by = store_key]

row_level_would_be_ragged <- store_summary$molecular_fraction >= MOLECULAR_FRACTION_THRESHOLD
ambiguous_fraction <- store_summary$store_type != "hybrid" &
  store_summary$molecular_fraction > MOLECULAR_REVIEW_BAND[1] &
  store_summary$molecular_fraction < MOLECULAR_REVIEW_BAND[2]
title_disagrees <- store_summary$store_type != "hybrid" &
  store_summary$title_is_molecular != row_level_would_be_ragged
mixed_ancestry <- store_summary$store_type != "hybrid" &
  !is.na(store_summary$min_ancestry_fraction) &
  store_summary$min_ancestry_fraction < ANCESTRY_FRACTION_REVIEW_THRESHOLD
# A dense store that nonetheless has a detectable molecular type is a likely
# mis-split (the title/traits look proteomic/metabolomic/etc.); surface it
# rather than silently moving it to ragged.
dense_looks_molecular <- store_summary$store_type == "dense" &
  !is.na(store_summary$molecular_type)
ragged_type_unresolved <- store_summary$store_type == "ragged" &
  is.na(store_summary$molecular_type)

store_summary[, review_reason := trimws(paste(
  fifelse(ambiguous_fraction, "ambiguous molecular fraction;", ""),
  fifelse(title_disagrees, "title vs trait-text disagreement;", ""),
  fifelse(mixed_ancestry, "mixed-ancestry cohort;", ""),
  fifelse(dense_looks_molecular,
          paste0("dense store looks molecular (", molecular_type, ");"), ""),
  fifelse(ragged_type_unresolved, "ragged store molecular type unresolved;", "")
))]
store_summary[review_reason == "", review_reason := NA_character_]
store_summary[, needs_review := ambiguous_fraction | title_disagrees | mixed_ancestry |
                dense_looks_molecular | ragged_type_unresolved]

setorder(store_summary, store_type, -n_analyses)

## ---------------------------------------------------------------------
## 7. Write outputs
## ---------------------------------------------------------------------
analyses_out <- full[, .(
  STUDY.ACCESSION, PUBMED.ID, FIRST.AUTHOR, STUDY, DISEASE.TRAIT, MAPPED_TRAIT,
  ancestry_group, ancestry_fraction, is_molecular, molecular_subtype,
  store_type, store_key, molecular_type,
  study_design, n_cases, n_controls, sample_size, n_variants,
  association_count, MAPPED_TRAIT_URI
)]
setorder(analyses_out, store_key, STUDY.ACCESSION)
fwrite(analyses_out, file.path(derived_dir, "store-candidates-analyses.tsv"), sep = "\t")
fwrite(store_summary, file.path(derived_dir, "store-candidates-summary.tsv"), sep = "\t")

cat(sprintf(
  "Studies: %d total, %d with full summary statistics.\n",
  nrow(studies), nrow(full)
))
cat(sprintf(
  "Candidate stores: %d dense, %d ragged, %d hybrid (%d analyses pooled).\n",
  sum(store_summary$store_type == "dense"),
  sum(store_summary$store_type == "ragged"),
  sum(store_summary$store_type == "hybrid"),
  sum(store_summary[store_type == "hybrid", n_analyses])
))
cat(sprintf(
  "Flagged for manual review: %d of %d candidate stores.\n",
  sum(store_summary$needs_review), nrow(store_summary)
))
cat("Ragged stores by molecular type:\n")
rag_types <- store_summary[store_type == "ragged", .N, by = molecular_type][order(-N)]
cat(paste0("  ", rag_types$molecular_type, ": ", rag_types$N, collapse = "\n"), "\n")
