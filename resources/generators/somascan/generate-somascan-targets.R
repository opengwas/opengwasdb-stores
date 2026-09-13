#!/usr/bin/env Rscript

suppressPackageStartupMessages({
  library(data.table)
  library(curl)
})

repo_root <- normalizePath(file.path(getwd()), mustWork = TRUE)
out_dir <- file.path(repo_root, "resources/somascan")
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

somascan_db_url <- Sys.getenv(
  "SOMASCAN_DB_SQLITE_URL",
  "https://raw.githubusercontent.com/SomaLogic/SomaScan.db/main/inst/extdata/SomaScan.sqlite"
)
somascan_db_version <- Sys.getenv("SOMASCAN_DB_VERSION", "0.99.10")
ensembl_mart_url <- Sys.getenv(
  "ENSEMBL_MART_URL",
  "https://www.ensembl.org/biomart/martservice"
)

cache_dir <- file.path(repo_root, "resources/cache/somascan-db")
dir.create(cache_dir, recursive = TRUE, showWarnings = FALSE)
sqlite_path <- file.path(cache_dir, "SomaScan.sqlite")

if (!file.exists(sqlite_path) || isTRUE(as.logical(Sys.getenv("REFRESH_SOMASCAN_DB", "FALSE")))) {
  message("Downloading SomaScan SQLite annotation database...")
  curl_download(somascan_db_url, sqlite_path, quiet = FALSE)
}

sqlite3 <- Sys.which("sqlite3")
if (!nzchar(sqlite3)) {
  stop("sqlite3 is required to export SomaScan.db tables", call. = FALSE)
}

sqlite_query <- function(sql) {
  sql <- gsub("[\r\n]+", " ", sql)
  cmd <- c("-header", "-csv", shQuote(sqlite_path), shQuote(sql))
  txt <- system2(sqlite3, cmd, stdout = TRUE, stderr = TRUE)
  if (!length(txt)) {
    return(data.table())
  }
  fread(text = paste(txt, collapse = "\n"), sep = ",", na.strings = c("", "NA"))
}

soma <- sqlite_query("
  SELECT
    p.probe_id AS seqid,
    p.gene_id AS entrez_id,
    p.is_multiple AS somascan_is_multiple,
    t.target_full_name AS target_full_name,
    m.v4_0 AS menu_v4_0,
    m.v4_1 AS menu_v4_1,
    m.v5_0 AS menu_v5_0
  FROM probes p
  LEFT JOIN target_names t ON p.probe_id = t.probe_id
  LEFT JOIN map_menu m ON p.probe_id = m.probe_id
  ORDER BY p.probe_id, p.gene_id
")

soma[, entrez_id := as.character(entrez_id)]
entrez <- sort(unique(na.omit(soma$entrez_id)))

biomart_query <- function(entrez_ids) {
  values <- paste(entrez_ids, collapse = ",")
  sprintf('<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE Query>
<Query virtualSchemaName="default" formatter="TSV" header="1" uniqueRows="1" count="" datasetConfigVersion="0.6">
  <Dataset name="hsapiens_gene_ensembl" interface="default">
    <Filter name="entrezgene_id" value="%s"/>
    <Attribute name="entrezgene_id"/>
    <Attribute name="ensembl_gene_id"/>
    <Attribute name="external_gene_name"/>
    <Attribute name="chromosome_name"/>
    <Attribute name="start_position"/>
    <Attribute name="end_position"/>
    <Attribute name="strand"/>
    <Attribute name="gene_biotype"/>
  </Dataset>
</Query>', values)
}

fetch_biomart_once <- function(entrez_ids) {
  h <- new_handle()
  handle_setform(h, query = biomart_query(entrez_ids))
  resp <- curl_fetch_memory(ensembl_mart_url, handle = h)
  txt <- rawToChar(resp$content)
  if (!nzchar(trimws(txt))) {
    return(data.table())
  }
  out <- fread(text = txt, sep = "\t", na.strings = c("", "NA"))
  if (ncol(out) != 8L) {
    stop(
      "Unexpected Ensembl BioMart response for Entrez IDs ",
      paste(head(entrez_ids, 5), collapse = ","),
      ": ",
      substr(gsub("[\r\n]+", " ", txt), 1, 500),
      call. = FALSE
    )
  }
  setnames(out, c(
    "entrez_id",
    "ensembl_gene_id",
    "gene_name",
    "chromosome",
    "gene_start",
    "gene_end",
    "strand",
    "gene_biotype"
  ))
  out[, entrez_id := as.character(entrez_id)]
  out[]
}

fetch_biomart <- function(entrez_ids) {
  last_error <- NULL
  for (attempt in seq_len(2L)) {
    res <- tryCatch(fetch_biomart_once(entrez_ids), error = identity)
    if (!inherits(res, "error")) {
      return(res)
    }
    last_error <- res
    Sys.sleep(attempt)
  }

  if (length(entrez_ids) > 1L) {
    mid <- ceiling(length(entrez_ids) / 2L)
    message(sprintf(
      "    splitting failed batch of %s Entrez IDs",
      length(entrez_ids)
    ))
    return(rbindlist(list(
      fetch_biomart(entrez_ids[seq_len(mid)]),
      fetch_biomart(entrez_ids[(mid + 1L):length(entrez_ids)])
    ), fill = TRUE))
  }

  stop(last_error)
}

message(sprintf("Fetching Ensembl coordinates for %s Entrez IDs...", length(entrez)))
batches <- split(entrez, ceiling(seq_along(entrez) / 400L))
coords <- rbindlist(lapply(seq_along(batches), function(i) {
  message(sprintf("  batch %s/%s", i, length(batches)))
  fetch_biomart(batches[[i]])
}), fill = TRUE)

coords <- unique(coords)
manifest <- merge(soma, coords, by = "entrez_id", all.x = TRUE, sort = FALSE)

manifest[, `:=`(
  genome_build = "GRCh38",
  somascan_db_version = somascan_db_version,
  mapping_source = "SomaScan.db",
  coordinate_source = "Ensembl BioMart hsapiens_gene_ensembl",
  coordinate_source_url = ensembl_mart_url,
  is_primary_chromosome = !is.na(chromosome) &
    chromosome %in% c(as.character(1:22), "X", "Y", "MT"),
  mhc = !is.na(chromosome) & chromosome == "6" &
    !is.na(gene_start) & !is.na(gene_end) &
    gene_start <= 34000000L & gene_end >= 25000000L,
  mapping_status = fifelse(
    is.na(entrez_id), "no_entrez_id",
    fifelse(is.na(ensembl_gene_id), "no_ensembl_match", "mapped_to_ensembl")
  )
)]

setcolorder(manifest, c(
  "seqid",
  "target_full_name",
  "entrez_id",
  "ensembl_gene_id",
  "gene_name",
  "chromosome",
  "gene_start",
  "gene_end",
  "strand",
  "gene_biotype",
  "genome_build",
  "is_primary_chromosome",
  "mhc",
  "somascan_is_multiple",
  "menu_v4_0",
  "menu_v4_1",
  "menu_v5_0",
  "mapping_status",
  "mapping_source",
  "somascan_db_version",
  "coordinate_source",
  "coordinate_source_url"
))

out_path <- file.path(out_dir, "somascan-targets.tsv")
fwrite(manifest, out_path, sep = "\t", na = "")

summary <- manifest[, .N, by = mapping_status][order(mapping_status)]
message(sprintf("Wrote %s rows to %s", nrow(manifest), out_path))
print(summary)
