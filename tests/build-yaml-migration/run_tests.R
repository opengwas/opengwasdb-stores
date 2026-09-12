#!/usr/bin/env Rscript
## Generator / executable-schema agreement (issue #97, ADR 0022).
##
## Issue #97 migrates the seven Trial Store Releases' `build.yaml` to the
## executable schema and updates the generators that emit it, so a freshly
## generated release and a migrated one are identical in schema. This suite
## proves that directly for every R generator and config that produced one of
## the seven: it loads each generator's own `write_build_yaml`, emits a
## `build.yaml` into a temp directory, and compares that emitted document's
## `build` block and fixed-input `source` keys against the checked-in migrated
## release's.
##
## `eqtlgen-besd-ragged` is a Python generator; its half of the contract is
## covered by tests/build-yaml-migration/run_tests.py.
##
## The generator files run their CLI `main` at source time, so this loads only
## the function definitions (everything before the CLI argument parse) -- that
## still exercises the generator's real emitter, not a copy of it. Run from the
## repository root:
##
##     pixi run test-r
suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
})

root <- normalizePath(
  system2("git", c("rev-parse", "--show-toplevel"), stdout = TRUE)[[1]],
  winslash = "/"
)

n_checks <- 0L
check <- function(condition, message) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(condition)) stop(message, call. = FALSE)
}

## Everything before the CLI entry point is the generator's function library.
load_generator <- function(path) {
  lines <- readLines(path)
  cut <- grep("args <- parse_args\\(commandArgs", lines)
  check(length(cut) > 0L, sprintf("cannot locate the CLI entry point in %s", path))
  env <- new.env(parent = globalenv())
  script <- tempfile(fileext = ".R")
  writeLines(lines[seq_len(cut[1] - 1L)], script)
  sys.source(script, envir = env)
  env
}

emit_build_yaml <- function(env, cfg, release_dir) {
  if (is.null(env$artifact_paths)) {
    env$write_build_yaml(cfg, release_dir)
  } else {
    env$write_build_yaml(cfg, root, release_dir, env$artifact_paths(cfg, root))
  }
}

compare_with_migrated <- function(label, generated_path, migrated_path) {
  generated <- read_yaml(generated_path)
  migrated <- read_yaml(migrated_path)
  check(is.null(generated$builder), sprintf("%s: generator still emits a builder block", label))
  check(
    identical(generated$build, migrated$build),
    sprintf("%s: generator and migrated build block differ (%s vs %s)",
            label, paste(capture.output(str(generated$build)), collapse = " "),
            paste(capture.output(str(migrated$build)), collapse = " "))
  )
  for (key in c("root", "analyses")) {
    check(
      identical(generated$source[[key]], migrated$source[[key]]),
      sprintf("%s: generator and migrated source.%s differ", label, key)
    )
  }
  ## The two optional workflow branches (issue #101) are part of the emitted
  ## build recipe, not curation: a regenerate must not silently drop them.
  for (key in c("rho", "reference_completion")) {
    check(
      identical(generated[[key]], migrated[[key]]),
      sprintf("%s: generator and migrated %s block differ (%s vs %s)",
              label, key, paste(capture.output(str(generated[[key]])), collapse = " "),
              paste(capture.output(str(migrated[[key]])), collapse = " "))
    )
  }
}

## (generator, config, migrated release, extra libraries the emitter needs)
cases <- list(
  list(
    generator = "resources/generators/finngen-r13-dense/generate.R",
    config = "families/finngen-r13/generators/config-pilot-20.yaml",
    release = "families/finngen-r13/releases/r13-pilot-20",
    libs = character()
  ),
  list(
    generator = "resources/generators/gwas-ssf-ragged/generate.R",
    config = "families/metabolome-plasma-2023/generators/config-european.yaml",
    release = "families/metabolome-plasma-2023/releases/2023-chen-full-european",
    libs = c("resources/lib/effect_scale_validation.R", "resources/lib/effect_scale_stage_yaml.R")
  ),
  list(
    generator = "resources/generators/gwas-ssf-ragged/generate.R",
    config = "families/pqtl-interval-2018/generators/config-pilot-10.yaml",
    release = "families/pqtl-interval-2018/releases/2018-sun-pilot-10",
    libs = c("resources/lib/effect_scale_validation.R", "resources/lib/effect_scale_stage_yaml.R")
  ),
  list(
    generator = "resources/generators/gwas-ssf-hybrid/generate.R",
    config = "families/gwas-catalog-eur-hybrid/generators/config-pilot-10.yaml",
    release = "families/gwas-catalog-eur-hybrid/releases/eur-hybrid-pilot-10",
    libs = c("resources/lib/effect_scale_validation.R", "resources/lib/effect_scale_stage_yaml.R")
  ),
  list(
    generator = "resources/generators/gwas-ssf-hybrid/generate.R",
    config = "families/gwas-catalog-eur-hybrid/generators/config-quant-pilot-10.yaml",
    release = "families/gwas-catalog-eur-hybrid/releases/eur-hybrid-quant-pilot-10",
    libs = c("resources/lib/effect_scale_validation.R", "resources/lib/effect_scale_stage_yaml.R")
  )
)

for (case in cases) {
  label <- case$release
  env <- load_generator(file.path(root, case$generator))
  for (lib in case$libs) sys.source(file.path(root, lib), envir = env)
  cfg <- read_yaml(file.path(root, case$config))
  release_dir <- tempfile("build-yaml-")
  dir.create(release_dir)
  emit_build_yaml(env, cfg, release_dir)
  compare_with_migrated(label, file.path(release_dir, "build.yaml"), file.path(root, case$release, "build.yaml"))
  cat(sprintf("  ok  %s (%s)\n", label, case$generator))
}

cat(sprintf("%d generator/schema agreement checks passed\n", n_checks))
