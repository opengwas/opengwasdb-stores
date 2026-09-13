#!/usr/bin/env Rscript

suppressPackageStartupMessages({
  library(data.table)
  library(curl)
})

repo_root <- normalizePath(getwd(), mustWork = TRUE)
parse_args <- function(args) {
  out <- list()
  for (arg in args) {
    if (!grepl("^--[^=]+=", arg)) {
      stop("Arguments must use --name=value form: ", arg, call. = FALSE)
    }
    key <- sub("^--([^=]+)=.*$", "\\1", arg)
    key <- gsub("-", "_", key)
    value <- sub("^--[^=]+=", "", arg)
    out[[key]] <- value
  }
  out
}

args <- parse_args(commandArgs(trailingOnly = TRUE))

`%||%` <- function(x, y) {
  if (is.null(x) || !length(x) || is.na(x) || !nzchar(x)) y else x
}

candidate_path <- args$input %||%
  file.path(repo_root, "resources/data/derived/store-candidates-analyses.tsv")
somascan_targets_path <- args$somascan_targets %||%
  file.path(repo_root, "resources/somascan/somascan-targets.tsv")
pubmed_id <- args$pubmed_id %||% "29875488"
selected_store_key <- args$store_key %||% NA_character_
out_path <- args$output %||%
  file.path(repo_root, "resources/somascan/sun-2018-analysis-targets.tsv")
dir.create(dirname(out_path), recursive = TRUE, showWarnings = FALSE)

if (!file.exists(candidate_path)) {
  stop("Missing candidate analyses table: ", candidate_path, call. = FALSE)
}
if (!file.exists(somascan_targets_path)) {
  stop("Missing SomaScan target resource: ", somascan_targets_path, call. = FALSE)
}

ensembl_mart_url <- Sys.getenv(
  "ENSEMBL_MART_URL",
  "https://www.ensembl.org/biomart/martservice"
)

parse_trait_symbol <- function(x) {
  sub(".*\\(([^.]+).*", "\\1", x)
}

parse_seqid <- function(x) {
  seqid <- sub(".*?([0-9]{4,5}[.][0-9]{1,3}([.][0-9]{1,3})?)\\)\\s*$", "\\1", x)
  seqid <- sub("[.]", "-", seqid)
  sub("[.].*$", "", seqid)
}

biomart_symbol_query <- function(symbols) {
  values <- paste(symbols, collapse = ",")
  sprintf('<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE Query>
<Query virtualSchemaName="default" formatter="TSV" header="1" uniqueRows="1" count="" datasetConfigVersion="0.6">
  <Dataset name="hsapiens_gene_ensembl" interface="default">
    <Filter name="hgnc_symbol" value="%s"/>
    <Attribute name="hgnc_symbol"/>
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

fetch_biomart_symbols_once <- function(symbols) {
  h <- new_handle()
  handle_setform(h, query = biomart_symbol_query(symbols))
  resp <- curl_fetch_memory(ensembl_mart_url, handle = h)
  txt <- rawToChar(resp$content)
  if (!nzchar(trimws(txt))) {
    return(data.table())
  }
  out <- fread(text = txt, sep = "\t", na.strings = c("", "NA"))
  if (ncol(out) != 8L) {
    stop(
      "Unexpected Ensembl BioMart response for symbols ",
      paste(head(symbols, 5), collapse = ","),
      ": ",
      substr(gsub("[\r\n]+", " ", txt), 1, 500),
      call. = FALSE
    )
  }
  setnames(out, c(
    "source_target_symbol",
    "ensembl_gene_id",
    "gene_name",
    "chromosome",
    "gene_start",
    "gene_end",
    "strand",
    "gene_biotype"
  ))
  out[]
}

fetch_biomart_symbols <- function(symbols) {
  symbols <- unique(na.omit(symbols))
  if (!length(symbols)) {
    return(data.table())
  }

  last_error <- NULL
  for (attempt in seq_len(2L)) {
    res <- tryCatch(fetch_biomart_symbols_once(symbols), error = identity)
    if (!inherits(res, "error")) {
      return(res)
    }
    last_error <- res
    Sys.sleep(attempt)
  }

  if (length(symbols) > 1L) {
    mid <- ceiling(length(symbols) / 2L)
    message(sprintf("    splitting failed symbol batch of %s", length(symbols)))
    return(rbindlist(list(
      fetch_biomart_symbols(symbols[seq_len(mid)]),
      fetch_biomart_symbols(symbols[(mid + 1L):length(symbols)])
    ), fill = TRUE))
  }

  stop(last_error)
}

analyses <- fread(candidate_path, sep = "\t", na.strings = "")
if (!is.na(pubmed_id)) {
  analyses <- analyses[PUBMED.ID == as.integer(pubmed_id)]
}
if (!is.na(selected_store_key)) {
  analyses <- analyses[store_key == selected_store_key]
}
if (!nrow(analyses)) {
  stop("No analyses selected from ", candidate_path, call. = FALSE)
}

selected <- analyses
selected[, `:=`(
  source_analysis_id = STUDY.ACCESSION,
  source_target_symbol = parse_trait_symbol(DISEASE.TRAIT),
  source_seqid = parse_seqid(DISEASE.TRAIT)
)]
selected <- selected[, .(
  source_analysis_id,
  source_seqid,
  source_target_symbol,
  source_label = DISEASE.TRAIT,
  trait_ontology_name = MAPPED_TRAIT,
  trait_ontology_id = MAPPED_TRAIT_URI
)]

soma <- fread(somascan_targets_path, sep = "\t", na.strings = "")
soma <- soma[is_primary_chromosome == TRUE & mapping_status == "mapped_to_ensembl"]
soma[, source_seqid := seqid]

direct <- merge(selected, soma, by = "source_seqid", allow.cartesian = TRUE)
direct[, `:=`(
  target_resolution_method = "somascan_db_seqid",
  matched_seqid = seqid,
  target_resolution_notes = ""
)]

direct_ids <- unique(direct$source_analysis_id)
needs_fallback <- selected[!source_analysis_id %in% direct_ids]

soma_symbol <- copy(soma)
soma_symbol[, source_target_symbol := gene_name]
soma_symbol <- soma_symbol[, !"source_seqid"]
symbol_from_soma <- merge(
  needs_fallback,
  soma_symbol,
  by = "source_target_symbol",
  allow.cartesian = TRUE
)
symbol_from_soma[, `:=`(
  target_resolution_method = "somascan_db_gene_name",
  matched_seqid = seqid,
  target_resolution_notes = paste(
    "source SeqId absent from current SomaScan.db;",
    "coordinate resolved by matching GWAS Catalog trait symbol to SomaScan.db gene_name"
  )
)]

fallback_ids <- unique(symbol_from_soma$source_analysis_id)
needs_ensembl <- needs_fallback[!source_analysis_id %in% fallback_ids]

message(sprintf(
  "Resolving %s target symbols through Ensembl BioMart...",
  uniqueN(needs_ensembl$source_target_symbol)
))
symbols_to_resolve <- sort(unique(needs_ensembl$source_target_symbol))
if (length(symbols_to_resolve)) {
  symbol_batches <- split(
    symbols_to_resolve,
    ceiling(seq_along(symbols_to_resolve) / 200L)
  )
  ensembl_symbols <- rbindlist(lapply(seq_along(symbol_batches), function(i) {
    message(sprintf("  symbol batch %s/%s", i, length(symbol_batches)))
    fetch_biomart_symbols(symbol_batches[[i]])
  }), fill = TRUE)
} else {
  ensembl_symbols <- data.table(
    source_target_symbol = character(),
    ensembl_gene_id = character(),
    gene_name = character(),
    chromosome = character(),
    gene_start = integer(),
    gene_end = integer(),
    strand = integer(),
    gene_biotype = character()
  )
}
ensembl_symbols <- unique(ensembl_symbols)
ensembl_symbols[, `:=`(
  genome_build = "GRCh38",
  is_primary_chromosome = !is.na(chromosome) &
    chromosome %in% c(as.character(1:22), "X", "Y", "MT"),
  mhc = !is.na(chromosome) & chromosome == "6" &
    !is.na(gene_start) & !is.na(gene_end) &
    gene_start <= 34000000L & gene_end >= 25000000L,
  mapping_status = "mapped_to_ensembl",
  mapping_source = "GWAS Catalog trait symbol",
  somascan_db_version = NA_character_,
  coordinate_source = "Ensembl BioMart hsapiens_gene_ensembl",
  coordinate_source_url = ensembl_mart_url,
  seqid = NA_character_,
  target_full_name = NA_character_,
  entrez_id = NA_character_,
  somascan_is_multiple = NA_integer_,
  menu_v4_0 = NA_integer_,
  menu_v4_1 = NA_integer_,
  menu_v5_0 = NA_integer_
)]

symbol_from_ensembl <- merge(
  needs_ensembl,
  ensembl_symbols[is_primary_chromosome == TRUE],
  by = "source_target_symbol",
  allow.cartesian = TRUE
)
symbol_from_ensembl[, `:=`(
  target_resolution_method = "ensembl_hgnc_symbol",
  matched_seqid = NA_character_,
  target_resolution_notes = paste(
    "source SeqId absent from current SomaScan.db;",
    "coordinate resolved by matching GWAS Catalog trait symbol to Ensembl HGNC symbol"
  )
)]

resolved_ids <- unique(c(
  direct$source_analysis_id,
  symbol_from_soma$source_analysis_id,
  symbol_from_ensembl$source_analysis_id
))
unresolved <- selected[!source_analysis_id %in% resolved_ids]
if (nrow(unresolved)) {
  unresolved[, `:=`(
    seqid = NA_character_,
    target_full_name = NA_character_,
    entrez_id = NA_character_,
    ensembl_gene_id = NA_character_,
    gene_name = NA_character_,
    chromosome = NA_character_,
    gene_start = NA_integer_,
    gene_end = NA_integer_,
    strand = NA_integer_,
    gene_biotype = NA_character_,
    genome_build = "GRCh38",
    is_primary_chromosome = NA,
    mhc = NA,
    somascan_is_multiple = NA_integer_,
    menu_v4_0 = NA_integer_,
    menu_v4_1 = NA_integer_,
    menu_v5_0 = NA_integer_,
    mapping_status = "unresolved",
    mapping_source = NA_character_,
    somascan_db_version = NA_character_,
    coordinate_source = NA_character_,
    coordinate_source_url = NA_character_,
    target_resolution_method = "unresolved",
    matched_seqid = NA_character_,
    target_resolution_notes = "source SeqId absent from current SomaScan.db and trait symbol did not resolve to a primary Ensembl gene"
  )]
}

out <- rbindlist(list(
  direct,
  symbol_from_soma,
  symbol_from_ensembl,
  unresolved
), fill = TRUE)

out <- out[, .(
  source_analysis_id,
  source_seqid,
  matched_seqid,
  source_target_symbol,
  source_label,
  trait_ontology_name,
  trait_ontology_id,
  target_full_name,
  entrez_id,
  ensembl_gene_id,
  gene_name,
  chromosome,
  gene_start,
  gene_end,
  strand,
  gene_biotype,
  genome_build,
  is_primary_chromosome,
  mhc,
  target_resolution_method,
  target_resolution_notes,
  mapping_status,
  mapping_source,
  somascan_db_version,
  coordinate_source,
  coordinate_source_url
)]

setorder(out, source_analysis_id, gene_name, ensembl_gene_id)
fwrite(out, out_path, sep = "\t", na = "")

message(sprintf("Wrote %s rows for %s analyses to %s",
                nrow(out), uniqueN(selected$source_analysis_id), out_path))
print(out[, .(n_rows = .N, n_analyses = uniqueN(source_analysis_id)),
          by = target_resolution_method][order(target_resolution_method)])
