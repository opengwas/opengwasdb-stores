#!/usr/bin/env Rscript
# Unit-level smoke test for the `opengwas-gwas-vcf` metadata resolver
# (issue #49, corrected by issue #50). Run from the repository root:
#
#   Rscript tests/metadata-resolvers/opengwas-api/run_tests.R
#
# Exercises resolve_opengwas_api_metadata() directly against fixture
# "gwasinfo" records. Every fixture below is a real record captured from a
# live, authenticated call to the OpenGWAS API (`POST /api/gwasinfo`) during
# issue #50's implementation -- not hand-invented -- except the final three
# structural edge cases (unresolvable/zero/NULL), which do not occur as a
# distinguishable real dataset shape to capture. This repository's test
# suites are themselves documented as data-only and network-free (see
# resources/scripts/run_all_tests.py), so fetch_opengwas_gwasinfo() (the
# live HTTP transport) is not itself exercised here -- only the resolver
# logic it feeds, using its captured output as fixtures.
#
# The resolver itself never sees the source GWAS-VCF file or its ##SAMPLE
# header (per the resolver contract in docs/release-metadata-schema.md,
# input is the provider's own metadata only); VCF-header framing in the
# comments below is narrative context, not an input to the function under
# test.

suppressPackageStartupMessages(library(data.table))

source("generators/lib/metadata_resolvers/opengwas_api.R")

fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(cond, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(cond)) fail(...)
}

# --- ieu-a-7 (issue #15's motivating example): the source GWAS-VCF
# ##SAMPLE header declares StudyType=Continuous with no case/control
# counts, but the OpenGWAS API's own gwasinfo record for the same study
# correctly reports it as case-control, unit="log odds".
r <- resolve_opengwas_api_metadata(list(
  id = "ieu-a-7", trait = "Coronary heart disease", category = "Disease",
  unit = "log odds", ncase = 60801, ncontrol = 123504, sample_size = 184305,
  consortium = "CARDIoGRAMplusC4D", pmid = 26343387
))
check(r$resolution_status == "resolved", "ieu-a-7: expected resolved, got %s", r$resolution_status)
check(r$stored_effect_scale == "log_or", "ieu-a-7: expected stored_effect_scale=log_or, got %s", r$stored_effect_scale)
check(r$sample_size_kind == "case_control", "ieu-a-7: expected sample_size_kind=case_control, got %s", r$sample_size_kind)
check(r$n_cases == 60801, "ieu-a-7: expected n_cases=60801, got %s", r$n_cases)
check(r$n_controls == 123504, "ieu-a-7: expected n_controls=123504, got %s", r$n_controls)

# --- ukb-b-10001 (issue #50's key finding): a Binary-category ukb-b trait
# with real ncase/ncontrol -- naively inferring stored_effect_scale from
# ncase/ncontrol presence alone would wrongly call this log_or. The ukb-b
# batch's BOLT-LMM/PHESANT pipeline reports every trait, binary or not, on
# the SD scale (unit="SD"), so the `unit` field must override that
# inference. This is exactly the ukb-b generator's (issue #50) motivating
# correctness case.
r <- resolve_opengwas_api_metadata(list(
  id = "ukb-b-10001", trait = "Operative procedures - secondary OPCS: Z84.6 Knee joint",
  category = "Binary", unit = "SD", ncase = 6661, ncontrol = 456349,
  sample_size = 463010, consortium = "MRC-IEU"
))
check(r$resolution_status == "resolved", "ukb-b-10001: expected resolved, got %s", r$resolution_status)
check(r$stored_effect_scale == "sd", "ukb-b-10001: expected stored_effect_scale=sd (not log_or), got %s", r$stored_effect_scale)
check(r$sample_size_kind == "case_control", "ukb-b-10001: expected sample_size_kind=case_control, got %s", r$sample_size_kind)
check(r$n_cases == 6661 && r$n_controls == 456349, "ukb-b-10001: n_cases/n_controls mismatch")
check(r$sample_size == 463010, "ukb-b-10001: expected sample_size=463010, got %s", r$sample_size)

# --- ukb-b-19953 (BMI): Continuous-category ukb-b trait, no ncase/ncontrol.
r <- resolve_opengwas_api_metadata(list(
  id = "ukb-b-19953", trait = "Body mass index (BMI)", category = "Continuous",
  unit = "SD", sample_size = 461460, consortium = "MRC-IEU"
))
check(r$resolution_status == "resolved", "ukb-b-19953: expected resolved, got %s", r$resolution_status)
check(r$stored_effect_scale == "sd", "ukb-b-19953: expected stored_effect_scale=sd, got %s", r$stored_effect_scale)
check(r$sample_size_kind == "total", "ukb-b-19953: expected sample_size_kind=total, got %s", r$sample_size_kind)
check(r$sample_size == 461460, "ukb-b-19953: expected sample_size=461460, got %s", r$sample_size)
check(is.na(r$n_cases) && is.na(r$n_controls), "ukb-b-19953: n_cases/n_controls should be NA")

# --- finn-b-I9_CHD: a Binary FinnGen trait whose `unit` field is the API's
# own literal placeholder string "NA" (not JSON null) and has no
# `sample_size` field at all -- exercises both the "NA"-is-not-a-real-unit
# normalisation and the secondary ncase/ncontrol-presence fallback (FinnGen
# uses logistic regression for binary traits, unlike ukb-b, so log_or is
# the correct fallback here), plus sample_size being computed as
# n_cases + n_controls when the API doesn't supply it directly.
r <- resolve_opengwas_api_metadata(list(
  id = "finn-b-I9_CHD", trait = "Major coronary heart disease event",
  category = "Binary", unit = "NA", ncase = 21012, ncontrol = 197780
))
check(r$resolution_status == "resolved", "finn-b-I9_CHD: expected resolved, got %s", r$resolution_status)
check(r$stored_effect_scale == "log_or", "finn-b-I9_CHD: expected stored_effect_scale=log_or, got %s", r$stored_effect_scale)
check(r$sample_size_kind == "case_control", "finn-b-I9_CHD: expected sample_size_kind=case_control, got %s", r$sample_size_kind)
check(r$sample_size == 21012 + 197780, "finn-b-I9_CHD: expected sample_size computed as ncase+ncontrol, got %s", r$sample_size)

# --- Unresolvable: no usable ncase/ncontrol pair and no usable sample_size
# (e.g. a withdrawn or placeholder dataset record). Must resolve to an
# explicit unresolved/unavailable state, never a defaulted 0.
r <- resolve_opengwas_api_metadata(list(id = "fixt-unresolvable", trait = "Fixture withdrawn dataset"))
check(r$resolution_status == "unresolved", "unresolvable: expected unresolved, got %s", r$resolution_status)
check(!is.na(r$resolution_notes) && nzchar(r$resolution_notes), "unresolvable: resolution_notes should explain the failure")
check(is.na(r$stored_effect_scale) && is.na(r$sample_size_kind) && is.na(r$sample_size),
      "unresolvable: resolved fields should all be NA, never a silent default")

# --- Unresolvable variant: a zero/placeholder ncase/ncontrol/sample_size
# (observed API convention for "not applicable", distinct from a genuinely
# missing field) must not be treated as a usable count.
r <- resolve_opengwas_api_metadata(list(id = "fixt-zero", trait = "Fixture zeroed counts", ncase = 0, ncontrol = 0, sample_size = 0))
check(r$resolution_status == "unresolved", "zero-counts: expected unresolved, got %s", r$resolution_status)

# --- Defensive: no record at all (API returned nothing for the id).
r <- resolve_opengwas_api_metadata(NULL)
check(r$resolution_status == "unresolved", "NULL record: expected unresolved, got %s", r$resolution_status)

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
