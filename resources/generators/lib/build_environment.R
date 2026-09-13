# Build-environment provenance capture (issue #41), R side.
#
# Mirrors resources/generators/lib/build_environment.py: complements the existing
# generator-script-hash provenance with the software environment that
# actually ran the generator (repo commit, pixi.lock hash, Pixi environment
# name, tool versions). Best-effort — any field that can't be determined is
# recorded as "unavailable" rather than aborting the build.

UNAVAILABLE <- "unavailable"

.bee_run <- function(args) {
  result <- tryCatch(
    system2(args[1], args[-1], stdout = TRUE, stderr = TRUE),
    error = function(e) NULL,
    warning = function(w) NULL
  )
  status <- attr(result, "status")
  if (is.null(result) || (!is.null(status) && status != 0)) return(NULL)
  result
}

.bee_repo_commit <- function(root) {
  out <- .bee_run(c("git", "-C", root, "rev-parse", "HEAD"))
  if (is.null(out)) UNAVAILABLE else out[1]
}

.bee_pixi_lock_sha256 <- function(root) {
  lock <- file.path(root, "pixi.lock")
  if (!file.exists(lock)) return(UNAVAILABLE)
  out <- .bee_run(c("sha256sum", lock))
  if (is.null(out)) UNAVAILABLE else strsplit(out[1], "\\s+")[[1]][1]
}

.bee_tool_version <- function(cmd, flag = "--version") {
  path <- Sys.which(cmd)
  if (identical(unname(path), "")) return(UNAVAILABLE)
  out <- .bee_run(c(cmd, flag))
  if (is.null(out) || length(out) == 0) UNAVAILABLE else out[1]
}

#' Capture this process's build-environment provenance, rooted at `root`
#' (the opengwasdb-stores repository root).
build_environment_info <- function(root) {
  list(
    repo_commit = .bee_repo_commit(root),
    pixi_lock_sha256 = .bee_pixi_lock_sha256(root),
    pixi_environment = { v <- Sys.getenv("PIXI_ENVIRONMENT_NAME", unset = NA); if (is.na(v) || v == "") UNAVAILABLE else v },
    platform = R.version$platform,
    r_version = R.version.string
  )
}
