#!/usr/bin/env Rscript
# The shared R side of the one candidate emitter (#248): a legacy generator's
# work root, and the projection of its working bundle into the current Release
# Bundle contract through src/manifest_generation/emit.py.

suppressPackageStartupMessages({
  library(yaml)
})

fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(condition, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(condition)) fail(...)
}

root <- normalizePath(getwd(), winslash = "/")
source(file.path(root, "resources/generators/lib/emit_registry.R"))

tmp <- tempfile("emit-registry-")
dir.create(tmp, recursive = TRUE)
on.exit(unlink(tmp, recursive = TRUE), add = TRUE)
Sys.setenv(OPENGWASDB_ARTIFACT_ROOT = file.path(tmp, "artifacts"))
registry_root <- file.path(tmp, "registry")

cfg <- list(
  label = "fixture-legacy",
  access_posture = "public",
  description = "A legacy generator's fixture candidate",
  notes = "Fixture notes",
  source = list(source_snapshot_id = "fixture-snapshot-v1"),
  output = list(store_id = "OGS-99011", registry_root = registry_root),
  build = list(
    layout = "dense",
    completion_state = "observed_only",
    command = "build-dense-vcf",
    options = list(`source-reader-capability` = "opengwasdb.gwas-vcf", `source-assembly` = "hg38"),
    post = list(top_hits = TRUE, overview = TRUE)
  )
)

# The work root is <artifact-root>/<id>/generate, resolved by Python.
work_root <- generation_work_root(cfg)
check(identical(work_root, file.path(tmp, "artifacts", "OGS-99011", "generate")),
      "work root should be <artifact-root>/<id>/generate, got %s", work_root)

# A working bundle, as a legacy generator's modes leave it.
dir.create(file.path(work_root, "sidecars"), recursive = TRUE)
analyses_text <- paste0(
  "analysis_id\tstored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t",
  "original_effect_scale\toriginal_sd_method\tassigned_ancestry\tancestry_assignment_method\t",
  "checksum\tchecksum_algorithm\tsource_file\n",
  "FIXTURE_1\tsd\ttotal\tanalysis_level\t1000\tsd\tdeclared_standardised\tEUR\taf_assigned\t",
  strrep("a", 64), "\tsha256\t/data/source/fixture.vcf.gz\n"
)
writeLines(sub("\n$", "", analyses_text), file.path(work_root, "analyses.tsv"))
writeLines("analysis_id\tselected\nFIXTURE_1\ttrue", file.path(work_root, "sidecars", "selection.tsv"))
write_yaml(list(status = "passed", validated_at = NULL, checks = list(schema = "passed")),
           file.path(work_root, "validation.yaml"))

# The working bundle's own command log, as a legacy writer records it.
write_yaml(list(generator = list(command = "Rscript fixture/generate.R --mode=emit")),
           file.path(work_root, "release.yaml"))
script <- "resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R"
cfg$.config_path <- "fixture/config.yaml"
emit_registry(cfg, root, script, also_ran = "python fixture/acquire.py")
commands <- c("Rscript fixture/generate.R --mode=emit", "python fixture/acquire.py",
              paste("Rscript", script, "--config=fixture/config.yaml --mode=emit-registry"))

bundle_dir <- file.path(registry_root, "OGS-99011")
release <- read_yaml(file.path(bundle_dir, "release.yaml"))
check(identical(release$store_id, "OGS-99011"), "release store_id mismatch")
check(identical(release$status, "candidate"), "an emitted bundle is a candidate")
check(identical(unlist(release$generator$commands), commands),
      "the working log, then also_ran, then this emit, got:\n%s", paste(unlist(release$generator$commands), collapse = "\n"))
check(grepl("^sha256:[0-9a-f]{64}$", release$generator$version), "the version is the generator script's sha256")
check(identical(release$label, "fixture-legacy"), "label comes from the config")
check(identical(release$source_snapshot_id, "fixture-snapshot-v1"), "source_snapshot_id comes from the config")
build <- read_yaml(file.path(bundle_dir, "build.yaml"))
check(identical(build$store_id, "OGS-99011"), "build store_id mismatch")
check(identical(build$build$command, "build-dense-vcf"), "build command comes from the config build: block")
check(identical(build$indexed_subsets[[1]]$name, "hm3"), "a Dense release declares the default hm3 subset (#210)")
check(identical(readLines(file.path(bundle_dir, "analyses.tsv")), readLines(file.path(work_root, "analyses.tsv"))),
      "analyses.tsv is the working bundle's, unchanged")
check(file.exists(file.path(bundle_dir, "sidecars", "selection.tsv")), "sidecars are carried over")
check(file.exists(file.path(bundle_dir, "summary.yaml")), "the emitter writes summary.yaml")
validation <- read_yaml(file.path(bundle_dir, "validation.yaml"))
check(identical(validation$validated_at, release$created_at), "validated_at is the emit's created_at")

# Emitting over a bundle that has been accepted is refused, and it is untouched.
release_p <- file.path(bundle_dir, "release.yaml")
writeLines(sub("^status: candidate$", "status: accepted", readLines(release_p)), release_p)
accepted <- readLines(release_p)
refused <- tryCatch(
  { emit_registry(cfg, root, script); NULL },
  error = function(e) conditionMessage(e)
)
check(!is.null(refused) && grepl("Release Status 'accepted'", refused),
      "emitting over an accepted bundle should be refused, got: %s", refused %||% "no error")
check(identical(readLines(release_p), accepted), "the accepted bundle should be untouched")

# A config without a Store Release id has no work root.
no_id <- cfg
no_id$output$store_id <- NULL
missing <- tryCatch({ generation_work_root(no_id); NULL }, error = function(e) conditionMessage(e))
check(!is.null(missing) && grepl("output.store_id", missing), "a missing output.store_id should be named")

# Each legacy generator's --mode=emit-registry emits its working bundle through
# the shared helper (#248). Their own suites cover how the bundle is made.
generators <- list(
  `gwas-ssf-hybrid` = list(id = "OGS-99012", layout = "hybrid", command = "build-hybrid",
                           options = list(`source-reader-capability` = "opengwasdb.gwas-ssf", `source-assembly` = "hg38")),
  `gwas-ssf-ragged` = list(id = "OGS-99013", layout = "ragged", command = "build-ragged-ssf",
                           options = list(`stored-effect-scale` = "sd")),
  `opengwas-gwas-vcf-dense` = list(id = "OGS-99014", layout = "dense", command = "build-dense-vcf",
                                   options = list(`source-reader-capability` = "opengwasdb.gwas-vcf"))
)
for (format in names(generators)) {
  g <- generators[[format]]
  generator_cfg <- cfg[!startsWith(names(cfg), ".")]
  generator_cfg$output <- list(store_id = g$id, registry_root = registry_root)
  generator_cfg$build <- list(layout = g$layout, completion_state = "observed_only", command = g$command,
                              options = g$options, post = list(top_hits = FALSE, overview = FALSE))
  generator_work <- generation_work_root(generator_cfg)
  dir.create(generator_work, recursive = TRUE)
  file.copy(file.path(work_root, c("analyses.tsv", "validation.yaml")), generator_work)
  config_path <- file.path(tmp, paste0(format, ".yaml"))
  write_yaml(generator_cfg, config_path)
  output <- suppressWarnings(system2("Rscript", c(
    file.path("resources/generators/lib/source-formats", format, "generate.R"),
    paste0("--config=", config_path), "--mode=emit-registry"
  ), stdout = TRUE, stderr = TRUE))
  status <- attr(output, "status")
  check(is.null(status) || status == 0L, "%s --mode=emit-registry failed:\n%s", format, paste(output, collapse = "\n"))
  emitted_release <- read_yaml(file.path(registry_root, g$id, "release.yaml"))
  check(identical(emitted_release$status, "candidate"), "%s should emit a candidate", format)
  check(any(grepl(paste0(format, "/generate.R --config=.* --mode=emit-registry"), unlist(emitted_release$generator$commands))),
        "%s should record its own emit-registry command", format)
  emitted_build <- read_yaml(file.path(registry_root, g$id, "build.yaml"))
  check(identical(emitted_build$layout, g$layout), "%s build.yaml layout should come from its config", format)
}

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
