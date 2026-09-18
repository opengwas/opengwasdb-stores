# R bridge to opengwasdb's shared analyses.tsv schema (issue #51). Generator
# scripts shell out to resources/generators/lib/schema_validate.py rather than hand-
# maintaining a duplicate required-column/vocabulary list in R that can
# silently drift from what opengwasdb itself considers valid -- the failure
# mode this repository and opengwasdb independently defining analyses.tsv
# schemas invites (see docs/adr/0017-opengwasdb-owns-shared-analysis-schema.md).

validate_against_opengwasdb_schema <- function(analyses_path, root) {
  python_bin <- Sys.which("python3")
  if (!nzchar(python_bin)) {
    stop("python3 not found on PATH; required to validate analyses.tsv against opengwasdb's shared schema")
  }
  script <- normalizePath(file.path(root, "resources/generators/lib/schema_validate.py"), winslash = "/", mustWork = FALSE)
  output <- suppressWarnings(system2(
    python_bin, c(shQuote(script), shQuote(analyses_path)),
    stdout = TRUE, stderr = TRUE
  ))
  # system2(..., stdout=TRUE) only attaches a "status" attribute when the
  # child process exits non-zero; a clean pass leaves it NULL.
  exit_status <- attr(output, "status")
  passed <- is.null(exit_status) || identical(as.integer(exit_status), 0L)
  list(passed = passed, errors = if (passed) character() else output)
}
