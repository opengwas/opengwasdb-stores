## The R side of the one candidate emitter (#248, ADR 0035).
##
## A legacy generator keeps its working bundle, the family-shaped files its
## modes read and write, under its work root: <artifact-root>/<store-id>/generate.
## Both helpers here ask the Python emitter (`python3 -m manifest_generation.emit`,
## #249), so the artifact-root
## precedence (paths.artifact_root) and the projection into the current Release
## Bundle contract are each written once, in Python. The emitter checks the
## contract, records `status: candidate` and the generator commands, writes
## summary.yaml, and replaces only an earlier candidate.

suppressPackageStartupMessages(library(yaml))

.run_emit_py <- function(root, args) {
  # system2 hands its arguments to a shell, so each is quoted: a command log
  # entry holds spaces.
  out <- suppressWarnings(system2("python3", shQuote(c("-m", "manifest_generation.emit", args)),
                                  stdout = TRUE, stderr = TRUE))
  status <- attr(out, "status")
  list(ok = is.null(status) || status == 0L, output = out)
}

## <artifact-root>/<output.store_id>/generate, where a generator keeps its working files.
generation_work_root <- function(cfg, root) {
  store_id <- cfg$output$store_id
  if (is.null(store_id) || !nzchar(store_id)) {
    stop("output.store_id is required: a generator's work root is <artifact-root>/<store-id>/generate",
         call. = FALSE)
  }
  result <- .run_emit_py(root, c("work-root", store_id))
  if (!result$ok) stop("manifest_generation.emit work-root failed:\n", paste(result$output, collapse = "\n"), call. = FALSE)
  result$output[[length(result$output)]]
}

## A generator's --mode=emit-registry: emit the working bundle under the work
## root as candidate <output.store_id>, its release.yaml and build.yaml
## projected from `cfg` (whose `build:` block is the Build Recipe). `script` is
## the generator's repository-relative path. The candidate records the command
## log the working bundle's own release.yaml holds, then `also_ran` (steps
## that log nothing themselves), then this emit. Stops with the emitter's
## refusal if it is refused.
emit_registry <- function(cfg, root, script, also_ran = character(), source_snapshot = NULL,
                          created_at = NULL) {
  version <- paste0("sha256:", strsplit(system2("sha256sum", shQuote(file.path(root, script)),
                                                 stdout = TRUE), "\\s+")[[1]][1])
  commands <- c(also_ran, paste("Rscript", script, paste0("--config=", cfg$.config_path), "--mode=emit-registry"))
  # Keys starting with "." are this run's own bookkeeping (the config path,
  # the resolved work root), not configuration to project.
  cfg <- cfg[!startsWith(names(cfg), ".")]
  config_path <- tempfile("emit-registry-config-", fileext = ".yaml")
  on.exit(unlink(config_path), add = TRUE)
  write_yaml(cfg, config_path)
  args <- c("project", "--config", config_path, "--generator-version", version,
            as.vector(rbind("--command", commands)))
  if (!is.null(created_at)) args <- c(args, "--created-at", created_at)
  if (!is.null(source_snapshot)) {
    snapshot_path <- tempfile("emit-registry-snapshot-", fileext = ".yaml")
    on.exit(unlink(snapshot_path), add = TRUE)
    write_yaml(source_snapshot, snapshot_path)
    args <- c(args, "--source-snapshot", snapshot_path)
  }
  result <- .run_emit_py(root, args)
  if (!result$ok) stop(paste(result$output, collapse = "\n"), call. = FALSE)
  cat(result$output, sep = "\n")
  invisible(TRUE)
}
