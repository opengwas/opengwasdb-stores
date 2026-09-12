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
    "Rscript", c("resources/generators/gwas-ssf-ragged/generate.R", args)
  )
  if (status != 0) fail("generate.R --mode=%s exited non-zero", mode)
}

run_mode("emit")
run_mode("validate")
run_mode("filter")

analyses <- fread(file.path(output_dir, "analyses.tsv"), sep = "\t", na.strings = "")
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

# The Store is built by the production `opengwasdb` CLI from the same fixed
# input the generator emitted -- the command `workflow/phase.py` shells out to.
# The retired build-store.py adapter used to do this with an in-process call and
# a read-back smoke test that unconditionally required a non-empty cis region
# (opengwasdb-stores#101 follow-up): every no-target family's build failed with
# "No non-empty cis region found in sparse_regions.tsv" right after the real
# OpenGWASDB store had already been built successfully. Issue #103 deleted that
# adapter, so the no-target (trans-only) release must now succeed through the
# shared CLI path -- the production build seam -- and validate there.
opengwasdb <- Sys.which("opengwasdb")
if (!nzchar(opengwasdb)) fail("opengwasdb is not on PATH; run via `pixi run test`")

store_dir <- file.path(output_dir, "store", "ragged__pmid-99999999__European")
unlink(store_dir, recursive = TRUE)
build_status <- system2(
  opengwasdb,
  c(
    "build-ragged-ssf",
    shQuote(file.path(output_dir, "analyses.tsv")),
    shQuote(file.path(output_dir, "filtered")),
    shQuote(store_dir),
    "--store-id", "ragged__pmid-99999999__European",
    "--release-id", "fixture-release",
    "--stored-effect-scale", "sd",
    "--overwrite"
  )
)
check(build_status == 0, "opengwasdb build-ragged-ssf should succeed for a no-cis (trans-only) release bundle")
check(file.exists(file.path(store_dir, "manifest.json")), "the no-cis build should produce a Store envelope")

store_analyses <- fread(file.path(store_dir, "analyses.tsv"), sep = "\t", na.strings = "")
check(nrow(store_analyses) == 1, "expected 1 analysis in the built Store")
check(identical(store_analyses$analysis_id[1], "GCST900001"), "the built Store carries the wrong analysis")

validate_status <- system2(opengwasdb, c("validate", shQuote(store_dir)))
check(validate_status == 0, "the no-cis (trans-only) Store should pass opengwasdb validate")

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
