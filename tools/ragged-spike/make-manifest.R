## Build the prototype analyses manifest that the opengwasdb ragged adapter (#7)
## consumes — one row per analyte, pairing the filtered file with the metadata
## sourced in #4. A stand-in for the release manifest the interface ticket (#8)
## will formalise.

suppressPackageStartupMessages(library(data.table))

analytes <- fread(text = "
gcst,gene_name,gene_id,chr,gene_start,gene_end,mhc
GCST90240122,YWHAE,ENSG00000108953,17,1344275,1400326,TRUE
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
analytes[gcst == "GCST90240122", mhc := FALSE]   # YWHAE is not MHC; fix stray flag
analytes[gcst %in% c("GCST90241928", "GCST90240770"), mhc := TRUE]

## trait_id = the SomaScan SeqId (parenthesised in the GWAS Catalog trait name).
ana <- fread("resources/data/derived/store-candidates-analyses.tsv", sep = "\t", na.strings = "")
seqid <- ana[PUBMED.ID == 29875488, .(gcst = STUDY.ACCESSION,
             trait_id = sub(".*\\(([^)]*)\\)\\s*$", "\\1", DISEASE.TRAIT))]
analytes <- merge(analytes, seqid, by = "gcst", all.x = TRUE, sort = FALSE)

analytes[, gene_name_slug := gsub("[^A-Za-z0-9]+", "-", gene_name)]
analytes[, `:=`(
  analysis_id  = gcst,
  n            = 3301L,                # from meta.yaml (#4)
  tissue       = "plasma",
  context      = "SomaScan",
  trait_bp     = gene_start,           # gene start (GRCh38); representative cis anchor
  filtered_file = sprintf("%s_%s.filtered.tsv.gz", gene_name_slug, gcst)
)]
setnames(analytes, "chr", "trait_chr")

out <- analytes[, .(analysis_index = .I - 1L, analysis_id, trait_id, gene_id, gene_name,
                    trait_chr, trait_bp, n, tissue, context, mhc, filtered_file)]
fwrite(out, "resources/data/ragged-spike/analyses-manifest.tsv", sep = "\t")
cat("wrote analyses-manifest.tsv:\n"); print(out)
