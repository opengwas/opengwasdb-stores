#!/usr/bin/env Rscript
# Release-bundle-level smoke test for the no-cis sparse-region policy
# (issue #26). Run from the repository root:
#
#   Rscript tests/no-cis-region-policy/run_tests.R
#
# Proves that a Store Family with no `inputs.analysis_targets` (no single
# encoding gene per Analysis, e.g. small-molecule metabolomics) emits an
# analyses.tsv without fabricated gene-target columns, and that `filter`
# retains only significant/suggestive regions with zero cis rows, using a
# fixture "full" GWAS-SSF source served over a local file:// URL so the test
# needs no network access.

suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
})

fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(cond, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(cond)) fail(...)
}

fixtures_dir <- "tests/no-cis-region-policy/fixtures"
output_dir <- file.path(fixtures_dir, "output")
unlink(output_dir, recursive = TRUE)

status <- system2("Rscript", c(shQuote(file.path(fixtures_dir, "generate_fixtures.R"))))
if (status != 0) fail("Fixture generation failed")

cfg <- read_yaml(file.path(fixtures_dir, "config.yaml"))
cfg$source$ftp_base <- paste0("file://", normalizePath(file.path(fixtures_dir, "source")))
tmp_config <- tempfile(fileext = ".yaml")
writeLines(as.yaml(cfg), tmp_config)

run_mode <- function(mode, ...) {
  extra <- list(...)
  args <- c(paste0("--config=", tmp_config), paste0("--mode=", mode), unlist(extra))
  status <- system2(
    "Rscript", c("resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R", args)
  )
  if (status != 0) fail("generate.R --mode=%s exited non-zero", mode)
}

run_mode("emit")
run_mode("validate")
run_mode("filter")

analyses <- fread(file.path(output_dir, "analyses.tsv"), sep = "\t", na.strings = "")
check(all(analyses$assigned_ancestry == "EUR"),
      "emitted assigned_ancestry should be the super-population code EUR, not the source label European")
release <- read_yaml(file.path(output_dir, "release.yaml"))
regions <- fread(file.path(output_dir, "sidecars", "sparse_regions.tsv"), sep = "\t", na.strings = "")

# No fabricated gene-target columns for a family with no target gene.
for (col in c("trait_id", "gene_id", "gene_name", "trait_chr", "trait_bp", "n", "mhc",
              "target_resolution_method", "n_target_rows")) {
  check(!col %in% names(analyses), "analyses.tsv should not have a %s column for a no-target family", col)
}
check(is.null(release$sidecars$analysis_targets),
      "release.yaml should not declare a sidecars.analysis_targets pointer for a no-target family")
check(!file.exists(file.path(output_dir, "sidecars", "analysis_targets.tsv")),
      "no analysis_targets.tsv sidecar should be written for a no-target family")

# Only significant/suggestive regions retained; zero cis regions.
check(nrow(regions[region_kind == "cis"]) == 0, "no-cis policy should retain zero cis regions")
check(nrow(regions[region_kind == "significant_trans"]) == 1,
      "the 5 clustered significant hits should merge into exactly one significant_trans region")
check(nrow(regions[region_kind == "suggestive_trans"]) == 3,
      "the 3 well-separated suggestive hits should each become their own suggestive lead")

filtered <- fread(file.path(output_dir, "filtered", analyses$filtered_file[1]), sep = "\t")
check(nrow(filtered) == 8, "expected 8 retained rows (5 significant + 3 suggestive), got %d", nrow(filtered))
check(all(filtered$p_value <= 1e-5), "every retained row should be significant or suggestive, not null")

summary_dt <- fread(file.path(output_dir, "sidecars", "filter_summary.tsv"), sep = "\t", na.strings = "")
check(identical(summary_dt$status[1], "ok"), "filter_summary should record status=ok for the fixture analysis")
check(summary_dt$cis_rows[1] == 0, "cis_rows should be 0 for a no-target family")

# The Store build itself is no longer asserted here. Under ADR 0023 nothing in
# this repository but the Phase A workflow may invoke a builder, and
# build-store.py is deleted; what a trans-only release causes the workflow to
# run is covered by the golden-argv tests in tests/plan/. This suite keeps what
# it uniquely covers: that a no-target family selects and filters correctly and
# records cis_rows = 0.
#
# The behaviour the deleted assertion protected -- opengwasdb-stores#101, where
# a read-back smoke test unconditionally demanded a non-empty cis region and so
# failed every no-target family after the Store had already built -- now belongs
# to opengwasdb's own read-back validation, not to a registry-side adapter.

# The same fixture can exercise the target-resolving manifest path without
# building a Store: deterministic Ensembl mappings emitted into a temporary
# Release Bundle. Issue #141 separates the three facts #130 collapsed: the
# Trait columns carry the source-provided ontology term and trait label, the
# gene is target annotation, and an aggregate assay has no single member gene
# promoted to its identity.
target_root <- tempfile("gene-target-release-")
target_path <- tempfile("analysis-targets-", fileext = ".tsv")
somascan_path <- tempfile("somascan-targets-", fileext = ".tsv")
candidates_path <- tempfile("candidates-", fileext = ".tsv")

base_candidate <- data.table(
  STUDY.ACCESSION = "GCST900001", PUBMED.ID = 99999999L, FIRST.AUTHOR = "Fixture",
  STUDY = "single target",
  DISEASE.TRAIT = "Fixture analyte levels (FIXTURE1.1234.5.6)",
  MAPPED_TRAIT = "fixture analyte measurement", ancestry_group = "European",
  ancestry_fraction = 1, is_molecular = TRUE, molecular_subtype = "proteomics",
  store_type = "ragged", store_key = "ragged__pmid-99999999__European",
  molecular_type = "proteomics", study_design = "quantitative",
  n_cases = NA_integer_, n_controls = NA_integer_, sample_size = 5000L,
  n_variants = 8L, association_count = 5L,
  MAPPED_TRAIT_URI = "http://purl.obolibrary.org/obo/fixture_0000001"
)
label_aggregate_candidate <- copy(base_candidate)
label_aggregate_candidate[, `:=`(
  STUDY.ACCESSION = "GCST900002",
  STUDY = "aggregate target",
  DISEASE.TRAIT = "Fixture family levels (AAA.BBB.CCC.DDD.EEE.FFF.GGG.4707.50.2)",
  MAPPED_TRAIT = "fixture family measurement",
  MAPPED_TRAIT_URI = "http://www.ebi.ac.uk/efo/EFO_0020109"
)]
flag_aggregate_candidate <- copy(base_candidate)
flag_aggregate_candidate[, `:=`(
  STUDY.ACCESSION = "GCST900003",
  STUDY = "flagged aggregate target",
  DISEASE.TRAIT = "Fixture flagged analyte levels (FLAG1.9999.9.9)",
  MAPPED_TRAIT = "fixture flagged measurement",
  MAPPED_TRAIT_URI = "http://purl.obolibrary.org/obo/fixture_0000002"
)]
fwrite(
  rbindlist(list(base_candidate, label_aggregate_candidate, flag_aggregate_candidate)),
  candidates_path, sep = "\t", na = ""
)
fwrite(data.table(
  source_analysis_id = c("GCST900001", "GCST900002", "GCST900003"),
  source_seqid = c("1234-5", "4707-50", "9999-9"),
  matched_seqid = c("1234-5", "4707-50", "9999-9"),
  source_label = c(
    base_candidate$DISEASE.TRAIT,
    label_aggregate_candidate$DISEASE.TRAIT,
    flag_aggregate_candidate$DISEASE.TRAIT
  ),
  chromosome = c("1", "22", "3"),
  gene_start = c(10000000L, 31944465L, 20000000L),
  gene_end = c(10010000L, 31957603L, 20010000L),
  ensembl_gene_id = c("ENSG00000123456", "ENSG00000128245", "ENSG00000999999"),
  gene_name = c("FIXTURE1", "YWHAH", "FLAG1"),
  mapping_status = "mapped_to_ensembl",
  trait_ontology_id = c(
    "http://purl.obolibrary.org/obo/fixture_0000001",
    "http://www.ebi.ac.uk/efo/EFO_0020109",
    "http://purl.obolibrary.org/obo/fixture_0000002"
  ),
  mhc = FALSE,
  target_resolution_method = "fixture_external_authority"
), target_path, sep = "\t")
# somascan_is_multiple is 0 for 4707-50 -- the flag does not identify the one
# known aggregate -- so the source label's own member list is the tracked
# evidence for it. The flag is still honoured where present: 9999-9 is
# deliberately flagged here so that path stays covered too.
fwrite(data.table(
  seqid = c("1234-5", "4707-50", "9999-9"),
  somascan_is_multiple = c(0L, 0L, 1L)
), somascan_path, sep = "\t")

target_config <- cfg
target_config$inputs$candidates <- candidates_path
target_config$inputs$analysis_targets <- target_path
target_config$inputs$somascan_targets <- somascan_path
target_config$selection$fail_if_target_unresolved <- TRUE
target_config$selection$n_analyses <- 3
target_config$output$release_dir <- target_root
target_config$output$data_dir <- target_root
writeLines(as.yaml(target_config), tmp_config)
run_mode("emit")

target_analyses <- fread(file.path(target_root, "analyses.tsv"), sep = "\t", na.strings = "")
check(!any(c("trait_id", "gene_id", "gene_name") %in% names(target_analyses)),
      "gene-target manifest should omit retired Analysis columns")

single <- target_analyses[analysis_id == "GCST900001"]
check(single$analysis_label == "FIXTURE1",
      "single-target analysis_label should carry the resolved gene symbol")
check(single$trait_ontology_id == "FIXTURE:0000001",
      "single-target trait_ontology_id should be the source ontology term, not the gene")
check(single$trait_ontology_label == "fixture analyte measurement",
      "single-target trait_ontology_label should be the source trait label, not the authority name")
check(single$trait_ontology_mapping_method == "source_provided",
      "single-target mapping provenance should report the source-provided mapping")

label_aggregate <- target_analyses[analysis_id == "GCST900002"]
check(label_aggregate$analysis_label == "4707-50",
      "an aggregate flagged by its source label should be identified by its SeqId, not one family member gene")
check(label_aggregate$trait_ontology_id == "EFO:0020109",
      "aggregate trait_ontology_id should be the source family term, never a gene id")
check(label_aggregate$trait_ontology_label == "fixture family measurement",
      "aggregate trait_ontology_label should be the family trait label")
check(label_aggregate$trait_chr == "22" && label_aggregate$trait_bp == 31944465L,
      "aggregate gene coordinates should remain target annotation in trait_chr/trait_bp")

flag_aggregate <- target_analyses[analysis_id == "GCST900003"]
check(flag_aggregate$analysis_label == "9999-9",
      "somascan_is_multiple should identify a flagged multi-gene assay by its SeqId")
check(flag_aggregate$trait_ontology_id == "FIXTURE:0000002",
      "a flagged aggregate should still carry the source trait term")
unlink(c(target_root, target_path, somascan_path, candidates_path), recursive = TRUE)

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
