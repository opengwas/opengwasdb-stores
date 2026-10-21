## Ragged-store spike — combined download + sparse-region filter (ticket #6).
##
## Per analyte: download the harmonised GWAS-SSF `.h.tsv.gz`, filter to the
## sparse regions defined in ticket #5, write (a) the filtered rows as a
## GWAS-SSF-shaped `.tsv.gz` for the opengwasdb adapter (#7) to ingest, and
## (b) a `.ranges.tsv` of the genomic intervals kept (the coverage-map raw
## material), then delete the full download so peak disk stays ~one file.
##
## Filter policy (#5), on the file's real p_value (GRCh38, no liftover):
##   cis         : gene(s) ±1 Mb — kept in FULL (not p-filtered)
##   sig trans   : p<=5e-8 outside cis — ±1 Mb around each, merged
##   suggestive  : 5e-8<p<=1e-5 outside cis & sig-trans — lead SNPs only
##   MHC analytes: standard window + mhc=true flag
##
## Provisional location: final home decided by the interface ticket (#8).
## Run: Rscript resources/scripts/ragged-spike/download-filter.R
##   MAX_ANALYTES=3 Rscript ...   # process only the first N (for a quick pass)

suppressPackageStartupMessages(library(data.table))

CIS_FLANK      <- 1e6
TRANS_FLANK    <- 1e6
SIG_P          <- 5e-8
SUGGESTIVE_P   <- 1e-5
SUGG_MERGE_KB  <- 1e5          # min spacing between kept suggestive leads
FTP_BASE       <- "http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics"

out_dir  <- "resources/data/ragged-spike/filtered"
work_dir <- "resources/data/ragged-spike/work"
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(work_dir, recursive = TRUE, showWarnings = FALSE)

## Analyte manifest — GRCh38 gene coords from ticket #4 (Ensembl REST).
analytes <- fread(text = "
gcst,gene_name,gene_id,chr,gene_start,gene_end,mhc
GCST90240122,YWHAE,ENSG00000108953,17,1344275,1400326,FALSE
GCST90240124,SFN,ENSG00000175793,1,26863149,26864456,FALSE
GCST90240120,PDK2,ENSG00000005882,17,50094737,50120276,FALSE
GCST90240127,HPGD,ENSG00000164120,4,174453668,174523154,FALSE
GCST90241928,MICB,ENSG00000204516,6,31494881,31511124,TRUE
GCST90240770,C4A;C4B,ENSG00000244731;ENSG00000224389,6,31981991,32035975,TRUE
GCST90240126,YWHAZ,ENSG00000164924,8,100914526,100953397,FALSE
GCST90242530,SERPINA10,ENSG00000140093,14,94278767,94293320,FALSE
GCST90243230,USP25,ENSG00000155313,21,15729898,15880068,FALSE
GCST90240121,PLCG1,ENSG00000124181,20,41136931,41196801,FALSE
", colClasses = list(character = "chr"))

max_n <- suppressWarnings(as.integer(Sys.getenv("MAX_ANALYTES", "")))
if (!is.na(max_n)) analytes <- analytes[seq_len(min(max_n, .N))]
only <- Sys.getenv("ONLY_GCST", "")
if (nzchar(only)) analytes <- analytes[gcst %in% strsplit(only, ",")[[1]]]

## FTP bucket for an accession: 1,000-accession blocks (GCST9024012x -> 90240001-90241000).
bucket_of <- function(gcst) {
  n <- as.numeric(sub("GCST", "", gcst))
  lo <- floor((n - 1) / 1000) * 1000 + 1
  sprintf("GCST%d-GCST%d", lo, lo + 999)
}

## Merge overlapping [lo,hi] intervals within one chromosome.
merge_intervals <- function(lo, hi) {
  if (length(lo) == 0) return(data.table(lo = numeric(0), hi = numeric(0)))
  o <- order(lo); lo <- lo[o]; hi <- hi[o]
  out_lo <- lo[1]; out_hi <- hi[1]; res_lo <- c(); res_hi <- c()
  for (i in seq_along(lo)[-1]) {
    if (lo[i] <= out_hi) { out_hi <- max(out_hi, hi[i]) }
    else { res_lo <- c(res_lo, out_lo); res_hi <- c(res_hi, out_hi); out_lo <- lo[i]; out_hi <- hi[i] }
  }
  data.table(lo = c(res_lo, out_lo), hi = c(res_hi, out_hi))
}

## Greedy distance-pruned leads: lowest-p first, drop anything within min_kb.
pick_leads <- function(dt, min_bp) {
  if (nrow(dt) == 0) return(integer(0))
  dt <- dt[order(p_value)]
  kept <- logical(nrow(dt)); taken <- list()
  chrpos <- split(seq_len(nrow(dt)), dt$chromosome)
  keptpos <- new.env()
  keep_idx <- integer(0)
  for (i in seq_len(nrow(dt))) {
    ch <- dt$chromosome[i]; pos <- dt$base_pair_location[i]
    prev <- get0(ch, envir = keptpos, ifnotfound = numeric(0))
    if (length(prev) == 0 || min(abs(prev - pos)) > min_bp) {
      keep_idx <- c(keep_idx, i)
      assign(ch, c(prev, pos), envir = keptpos)
    }
  }
  dt[keep_idx, row_id]
}

process_one <- function(a) {
  gcst <- a$gcst
  url <- sprintf("%s/%s/%s/harmonised/%s.h.tsv.gz", FTP_BASE, bucket_of(gcst), gcst, gcst)
  gz <- file.path(work_dir, paste0(gcst, ".h.tsv.gz"))

  t0 <- Sys.time()
  options(timeout = 3600)
  download.file(url, gz, mode = "wb", quiet = TRUE)
  t_dl <- as.numeric(difftime(Sys.time(), t0, units = "secs"))
  dl_mb <- file.size(gz) / 1e6

  t1 <- Sys.time()
  hdr <- names(fread(gz, nrows = 0))
  want <- c("chromosome", "base_pair_location", "effect_allele", "other_allele",
            "beta", "standard_error", "p_value", "effect_allele_frequency",
            "variant_id", "rsid")
  sel <- intersect(want, hdr)
  d <- fread(gz, select = sel, colClasses = list(character = "chromosome"),
             showProgress = FALSE)
  d <- d[!is.na(base_pair_location) & !is.na(p_value)]
  n_in <- nrow(d)

  ## cis window (union over the analyte's gene(s))
  cis_chr <- a$chr
  cis_lo <- a$gene_start - CIS_FLANK
  cis_hi <- a$gene_end + CIS_FLANK
  d[, is_cis := chromosome == cis_chr & base_pair_location >= cis_lo & base_pair_location <= cis_hi]

  ## significant trans -> merged ±1 Mb windows (per chromosome)
  sig <- d[p_value <= SIG_P & !is_cis]
  trans_ranges <- data.table(chromosome = character(0), lo = numeric(0), hi = numeric(0))
  d[, in_trans := FALSE]
  if (nrow(sig) > 0) {
    for (ch in unique(sig$chromosome)) {
      m <- merge_intervals(sig[chromosome == ch, base_pair_location - TRANS_FLANK],
                           sig[chromosome == ch, base_pair_location + TRANS_FLANK])
      trans_ranges <- rbind(trans_ranges, data.table(chromosome = ch, lo = m$lo, hi = m$hi))
    }
    setkey(d, chromosome)
    for (i in seq_len(nrow(trans_ranges))) {
      ch <- trans_ranges$chromosome[i]
      d[chromosome == ch & base_pair_location >= trans_ranges$lo[i] &
          base_pair_location <= trans_ranges$hi[i], in_trans := TRUE]
    }
  }

  ## suggestive -> lead SNPs only (outside cis & sig-trans)
  d[, row_id := .I]
  sugg <- d[p_value > SIG_P & p_value <= SUGGESTIVE_P & !is_cis & !in_trans]
  sugg_lead_ids <- pick_leads(sugg[, .(chromosome, base_pair_location, p_value, row_id)], SUGG_MERGE_KB)
  d[, is_sugg_lead := row_id %in% sugg_lead_ids]

  keep <- d[is_cis | in_trans | is_sugg_lead]
  n_kept <- nrow(keep)

  ## write filtered rows (GWAS-SSF-shaped) for the adapter (#7)
  out_gz <- file.path(out_dir, sprintf("%s_%s.filtered.tsv.gz", a$gene_name_slug, gcst))
  fwrite(keep[, ..sel], out_gz, sep = "\t")

  ## write kept ranges (coverage-map material): cis + merged trans + suggestive points
  ranges <- rbindlist(list(
    data.table(chromosome = cis_chr, start = cis_lo, end = cis_hi, tier = "cis"),
    if (nrow(trans_ranges)) trans_ranges[, .(chromosome, start = lo, end = hi, tier = "sig_trans")],
    if (length(sugg_lead_ids)) d[row_id %in% sugg_lead_ids,
        .(chromosome, start = base_pair_location, end = base_pair_location, tier = "suggestive")]
  ), use.names = TRUE)
  fwrite(ranges, file.path(out_dir, sprintf("%s_%s.ranges.tsv", a$gene_name_slug, gcst)), sep = "\t")

  unlink(gz)  # delete the full download; peak disk stays ~one file
  t_filter <- as.numeric(difftime(Sys.time(), t1, units = "secs"))

  data.table(
    gcst = gcst, gene = a$gene_name, mhc = a$mhc,
    n_in = n_in, n_cis = keep[, sum(is_cis)],
    n_sig_trans_regions = nrow(trans_ranges),
    n_trans_kept = keep[, sum(in_trans)],
    n_suggestive = length(sugg_lead_ids),
    n_kept = n_kept, kept_frac = round(n_kept / n_in, 5),
    dl_mb = round(dl_mb, 1), out_kb = round(file.size(out_gz) / 1e3, 1),
    t_download_s = round(t_dl, 1), t_filter_s = round(t_filter, 1),
    t_total_s = round(t_dl + t_filter, 1)
  )
}

analytes[, gene_name_slug := gsub("[^A-Za-z0-9]+", "-", gene_name)]

summary_rows <- list()
for (i in seq_len(nrow(analytes))) {
  a <- analytes[i]
  cat(sprintf("[%d/%d] %s (%s)%s ...\n", i, nrow(analytes), a$gene_name, a$gcst,
              if (a$mhc) " [MHC]" else ""))
  r <- tryCatch(process_one(a), error = function(e) {
    cat("  ERROR:", conditionMessage(e), "\n"); NULL
  })
  if (!is.null(r)) {
    summary_rows[[length(summary_rows) + 1]] <- r
    cat(sprintf("  in=%s kept=%s (%.3f%%) | %s trans-regions, %s suggestive | dl %.0fMB in %.0fs, filter %.0fs -> out %.0fKB\n",
                format(r$n_in, big.mark = ","), format(r$n_kept, big.mark = ","),
                100 * r$kept_frac, r$n_sig_trans_regions, r$n_suggestive,
                r$dl_mb, r$t_download_s, r$t_filter_s, r$out_kb))
  }
}

summ <- rbindlist(summary_rows)
fwrite(summ, "resources/data/ragged-spike/filter-summary.tsv", sep = "\t")
cat("\n=== filter summary ===\n"); print(summ)
if (nrow(summ)) cat(sprintf(
  "\nTotals: %s analytes | %s -> %s rows (%.3f%% kept) | mean %.0fs/analyte (dl %.0fs + filter %.0fs)\n",
  nrow(summ), format(sum(summ$n_in), big.mark = ","), format(sum(summ$n_kept), big.mark = ","),
  100 * sum(summ$n_kept) / sum(summ$n_in), mean(summ$t_total_s),
  mean(summ$t_download_s), mean(summ$t_filter_s)))
