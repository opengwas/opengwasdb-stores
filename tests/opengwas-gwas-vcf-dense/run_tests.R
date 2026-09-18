#!/usr/bin/env Rscript
# Unit-level smoke test for resources/generators/lib/opengwas_gwas_vcf_dense.R (issue
# #50) -- the pure candidate-selection and resolution-application logic
# behind the ukb-b Manifest Generator. Run from the repository root:
#
#   Rscript tests/opengwas-gwas-vcf-dense/run_tests.R
#
# The live OpenGWAS API call (resolve_batch() -> fetch_opengwas_gwasinfo())
# is deliberately out of scope here, same reasoning as
# tests/metadata-resolvers/opengwas-api/run_tests.R: this repository's test
# suites are documented as data-only and network-free, and a live call
# additionally needs a per-user token. Fixture `resolution` tables below are
# constructed directly in the resolve_opengwas_api_metadata()-shaped
# contract, standing in for what a real API round-trip would produce.
#
# This suite exists because the logic it covers had two real bugs caught
# only by running the real generator against the real ukb-b batch (issue
# #50's implementation): (1) fread() silently mistyping an
# almost-all-blank `exclude_from_build` column as logical rather than
# character, so a downstream `!= "true"` comparison treated every row as
# buildable; (2) fwrite() writing a literal quoted `""` for an
# intentionally-blank derivations field instead of leaving it blank,
# because the code used `""` rather than `NA_character_` for "not
# applicable". Bug (2) is exercised directly against in-memory data.tables
# below; bug (1) only reproduces through an actual fwrite()/fread() round
# trip (in-memory data.tables are already correctly typed), so the
# dedicated round-trip check near the end of this file writes a real
# temporary file and mirrors generate.R's validate_emit() buildable-row
# filter against it.

suppressPackageStartupMessages(library(data.table))

source("resources/generators/lib/metadata_resolvers/ontology_contract.R")
source("resources/generators/lib/metadata_resolvers/canonical_trait_table.R")
source("resources/generators/lib/opengwas_gwas_vcf_dense.R")

fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(cond, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(cond)) fail(...)
}

base_candidate_row <- function(analysis_id) {
  data.table(
    analysis_id = analysis_id, source_analysis_id = analysis_id,
    source_label = paste("Fixture", analysis_id), source_file = paste0("/fixture/", analysis_id, ".vcf.gz"),
    source_genome_build = "GRCh37", license = NA_character_,
    trait_ontology_id = NA_character_, trait_ontology_label = NA_character_,
    stored_effect_scale = NA_character_, original_effect_scale = NA_character_, original_sd_method = NA_character_,
    sample_size_kind = "total", sample_size_scope = "analysis_level", sample_size = 999999L,
    n_cases = NA_real_, n_controls = NA_real_,
    inclusion_reason = "fixture batch", exclude_from_build = NA_character_
  )
}

base_resolution_row <- function(analysis_id, status, scale = NA_character_, kind = NA_character_,
                                 n = NA_real_, ncase = NA_real_, ncontrol = NA_real_, notes = NA_character_) {
  data.table(
    analysis_id = analysis_id, resolution_status = status, resolution_notes = notes,
    stored_effect_scale = scale, sample_size_kind = kind, sample_size = n,
    n_cases = ncase, n_controls = ncontrol
  )
}

# --- select_analyses(): --only-analysis-id filters to the requested subset
# of an already-loaded candidate table (unlike gwas-ssf-ragged, there is no
# separate download stage to make "partial" ambiguous here).
candidates3 <- rbindlist(lapply(c("A", "B", "C"), base_candidate_row))
sel <- select_analyses(candidates3, only_analysis_id = "A,C")
check(identical(sort(sel$analysis_id), c("A", "C")), "select_analyses: --only-analysis-id filter mismatch, got %s",
      paste(sel$analysis_id, collapse = ","))

sel2 <- select_analyses(candidates3, max_analyses = 2L)
check(nrow(sel2) == 2, "select_analyses: --max-analyses should cap at 2, got %d", nrow(sel2))

# --- apply_resolution(): the ukb-b-10001-shaped case -- a case-control
# Analysis resolved to stored_effect_scale=sd (the issue #50 finding),
# overwriting the candidate's stale sample_size_kind="total" default.
candidates <- base_candidate_row("ukb-b-10001")
resolution <- base_resolution_row("ukb-b-10001", "resolved", scale = "sd", kind = "case_control",
                                   n = 463010, ncase = 6661, ncontrol = 456349)
applied <- apply_resolution(candidates, resolution)
row <- applied$analyses[analysis_id == "ukb-b-10001"]
check(row$stored_effect_scale == "sd", "resolved cc: expected stored_effect_scale=sd, got %s", row$stored_effect_scale)
check(row$original_effect_scale == "sd", "resolved cc: original_effect_scale should mirror stored_effect_scale")
check(row$sample_size_kind == "case_control", "resolved cc: expected sample_size_kind=case_control, got %s", row$sample_size_kind)
check(row$n_cases == 6661 && row$n_controls == 456349, "resolved cc: n_cases/n_controls mismatch")
check(row$original_sd_method == "declared_standardised", "resolved cc: expected original_sd_method=declared_standardised, got %s", row$original_sd_method)
check(is.na(row$exclude_from_build), "resolved cc: exclude_from_build should stay NA (not excluded), got %s", row$exclude_from_build)
drows <- applied$derivations[analysis_id == "ukb-b-10001"]
check(nrow(drows) == 2, "resolved cc: expected one derivations row per explained field, got %d", nrow(drows))
scale_drow <- drows[field == "stored_effect_scale"]
size_drow <- drows[field == "sample_size_kind"]
check(scale_drow$value == "sd", "resolved cc: stored_effect_scale derivations value mismatch")
check(size_drow$value == "case_control", "resolved cc: sample_size_kind derivations value mismatch")
check(!is.na(size_drow$evidence) && grepl("n_cases=6661", size_drow$evidence), "resolved cc: derivations evidence should cite n_cases")
check(is.na(scale_drow$notes) && is.na(size_drow$notes), "resolved cc: derivations notes should be NA (blank) for a resolved row, not empty string")

# --- apply_resolution(): a log_or Analysis derives original_sd_method =
# binary_trait (not declared_standardised).
candidates <- base_candidate_row("ieu-a-7")
resolution <- base_resolution_row("ieu-a-7", "resolved", scale = "log_or", kind = "case_control",
                                   n = 184305, ncase = 60801, ncontrol = 123504)
applied <- apply_resolution(candidates, resolution)
row <- applied$analyses[analysis_id == "ieu-a-7"]
check(row$original_sd_method == "binary_trait", "log_or: expected original_sd_method=binary_trait, got %s", row$original_sd_method)

# --- apply_resolution(): an Analysis explicitly resolved as "unresolved"
# (a real gwasinfo record came back but had no usable counts) is excluded
# from build with an explanatory inclusion_reason, not left with blank
# fields and no signal.
candidates <- base_candidate_row("fixt-unresolved")
resolution <- base_resolution_row("fixt-unresolved", "unresolved", notes = "gwasinfo record had neither a usable ncase/ncontrol pair nor a usable sample_size")
applied <- apply_resolution(candidates, resolution)
row <- applied$analyses[analysis_id == "fixt-unresolved"]
check(is.na(row$stored_effect_scale), "unresolved: stored_effect_scale should be NA")
check(identical(row$exclude_from_build, "true"), "unresolved: expected exclude_from_build=true, got %s", row$exclude_from_build)
check(grepl("excluded_reason:", row$inclusion_reason), "unresolved: inclusion_reason should explain the exclusion, got %s", row$inclusion_reason)
drows <- applied$derivations[analysis_id == "fixt-unresolved"]
check(nrow(drows) == 2, "unresolved: expected one derivations row per explained field, got %d", nrow(drows))
check(all(is.na(drows$value)), "unresolved: derivations value should be NA, never a defaulted value")
check(all(!is.na(drows$notes) & nzchar(drows$notes)), "unresolved: derivations notes should carry the resolution_notes")

# --- apply_resolution(): an analysis_id present in candidates but entirely
# absent from `resolution` (resolve_batch() got no record back at all --
# the real "3 excluded" cases hit during the actual ukb-b run) is treated
# the same as an explicit unresolved row, not silently dropped or kept
# with stale blank fields.
candidates <- base_candidate_row("fixt-missing-from-api")
resolution <- base_resolution_row("some-other-id", "resolved", scale = "sd", kind = "total", n = 1000)
applied <- apply_resolution(candidates, resolution)
row <- applied$analyses[analysis_id == "fixt-missing-from-api"]
check(identical(row$exclude_from_build, "true"), "missing-from-api: expected exclude_from_build=true, got %s", row$exclude_from_build)
check(grepl("no gwasinfo record returned", row$inclusion_reason), "missing-from-api: inclusion_reason should explain no record was returned")

# --- Round trip through a real file (bug 1 above): a mostly-resolved batch
# with one excluded_from_build row, written with fwrite() and read back
# exactly as generate.R's validate_emit() reads analyses.tsv, including its
# `colClasses = list(character = "exclude_from_build")` fix. Without that
# fix, fread() infers `exclude_from_build` as logical (its only non-blank
# value being the string "true"), and `exclude_from_build != "true"` then
# coerces the logical TRUE to the string "TRUE" for comparison -- silently
# treating every row, including the excluded one, as buildable.
candidates <- rbindlist(list(base_candidate_row("fixt-rt-ok"), base_candidate_row("fixt-rt-excluded")))
resolution <- rbindlist(list(
  base_resolution_row("fixt-rt-ok", "resolved", scale = "sd", kind = "total", n = 1000),
  base_resolution_row("fixt-rt-excluded", "unresolved", notes = "no usable metadata")
))
applied <- apply_resolution(candidates, resolution)
tmp_path <- tempfile(fileext = ".tsv")
fwrite(applied$analyses, tmp_path, sep = "\t", na = "")
reread_fixed <- fread(tmp_path, sep = "\t", na.strings = "",
                       colClasses = list(character = "exclude_from_build"))
check(is.character(reread_fixed$exclude_from_build),
      "round trip: exclude_from_build should read back as character with colClasses set, got %s",
      class(reread_fixed$exclude_from_build))
buildable_fixed <- reread_fixed[exclude_from_build != "true" | is.na(exclude_from_build)]
check(nrow(buildable_fixed) == 1 && buildable_fixed$analysis_id == "fixt-rt-ok",
      "round trip: expected exactly fixt-rt-ok to be buildable with the colClasses fix, got %s",
      paste(buildable_fixed$analysis_id, collapse = ","))

# Demonstrates the bug the fix above prevents: without colClasses, the
# excluded row is wrongly readmitted as buildable.
reread_unfixed <- fread(tmp_path, sep = "\t", na.strings = "")
check(is.logical(reread_unfixed$exclude_from_build),
      "round trip: exclude_from_build should be mistyped as logical without colClasses (sanity check on the bug itself)")
buildable_unfixed <- reread_unfixed[exclude_from_build != "true" | is.na(exclude_from_build)]
check(nrow(buildable_unfixed) == 2,
      "round trip: sanity check that the unfixed read reproduces the bug (both rows wrongly buildable), got %d buildable",
      nrow(buildable_unfixed))
unlink(tmp_path)

# --- #58: the dense generator delegates shared-core validation to
# opengwasdb. Use one real, conforming emitted row, then prove failures in a
# shared-only vocabulary/required column are caught (neither is in the dense
# generator's registry-only structural checks).
schema_tmp <- tempfile(pattern = "dense-schema-")
dir.create(schema_tmp)
schema_config <- file.path(schema_tmp, "config.yaml")
writeLines(c("output:", paste0("  release_dir: ", schema_tmp)), schema_config)
valid_schema_row <- fread(
  "families/ukb-b/releases/dense-observed-vcf-c128-resolved/analyses.tsv",
  sep = "\t", na.strings = "", colClasses = list(character = "exclude_from_build")
)[analysis_id == "ukb-b-19953"]
fwrite(valid_schema_row, file.path(schema_tmp, "analyses.tsv"), sep = "\t", na = "")
writeLines(c("checks:", "  schema: not_run", "warnings: []", "errors: []"),
           file.path(schema_tmp, "validation.yaml"))
run_dense_validate <- function() {
  output <- suppressWarnings(system2("Rscript", c(
    "resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/generate.R",
    paste0("--config=", schema_config), "--mode=validate"
  ), stdout = TRUE, stderr = TRUE))
  status <- attr(output, "status")
  if (is.null(status)) 0L else as.integer(status)
}
check(run_dense_validate() == 0L, "dense schema: conforming manifest should pass")

bad_vocab <- copy(valid_schema_row)
bad_vocab[, original_sd_method := "invented_method"]
fwrite(bad_vocab, file.path(schema_tmp, "analyses.tsv"), sep = "\t", na = "")
check(run_dense_validate() != 0L, "dense schema: shared out-of-vocabulary value should fail")
check(grepl("schema: failed", paste(readLines(file.path(schema_tmp, "validation.yaml")), collapse = "\n")),
      "dense schema: validation.yaml should record checks.schema=failed")

missing_shared <- copy(valid_schema_row)[, original_effect_scale := NULL]
fwrite(missing_shared, file.path(schema_tmp, "analyses.tsv"), sep = "\t", na = "")
check(run_dense_validate() != 0L, "dense schema: missing shared required column should fail")

# --- apply_trait_ontology_mapping(): opengwas-gwas-vcf-dense supplies no
# ontology mapping of its own -- with no canonical table declared, every row
# resolves to an explicit unmapped, never a silently blank/absent column
# (issue: opengwasdb-stores#63 gap analysis).
candidates <- base_candidate_row("fixt-ontology-unmapped")
mapped <- apply_trait_ontology_mapping(candidates, canonical_table = NULL)
row <- mapped[analysis_id == "fixt-ontology-unmapped"]
check(row$trait_ontology_mapping_method == "unmapped" && is.na(row$trait_ontology_id),
      "ontology mapping: no source ID and no table declared should resolve to unmapped, got %s",
      row$trait_ontology_mapping_method)

# A Canonical Trait Mapping Table match resolves the row instead.
fixture_table_path <- tempfile(fileext = ".tsv")
writeLines(c(
  "trait_label\ttrait_ontology_id\ttrait_ontology_label",
  "Fixture fixt-ontology-matched\tEFO:9999001\tFixture ontology label"
), fixture_table_path)
canonical_table <- load_canonical_trait_table(fixture_table_path)
candidates <- base_candidate_row("fixt-ontology-matched")
mapped <- apply_trait_ontology_mapping(candidates, canonical_table = canonical_table)
row <- mapped[analysis_id == "fixt-ontology-matched"]
check(row$trait_ontology_mapping_method == "canonical_table_lookup" && row$trait_ontology_id == "EFO:9999001",
      "ontology mapping: a canonical-table match should resolve via lookup, got method=%s id=%s",
      row$trait_ontology_mapping_method, row$trait_ontology_id)
unlink(fixture_table_path)

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
