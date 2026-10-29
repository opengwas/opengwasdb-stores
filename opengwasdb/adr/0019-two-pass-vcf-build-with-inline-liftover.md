# Two-pass VCF build path with inline liftover

GWAS-VCF sources (e.g. IEU OpenGWAS UKB files in GRCh37) are ingested via a dedicated two-pass dense builder rather than the existing single-pass `build_dense_observed_store`. Pass 1 streams all VCFs to collect the union hg38 ALID set, lifting each hg19 position inline using pyliftover; variants that fail liftover are dropped, and the build fails if the failure rate exceeds a configurable threshold (default 1%). Pass 2 streams each VCF again trait-by-trait, reusing the hg19→hg38 lookup built in pass 1, and fills one zarr column at a time so peak memory stays O(n_variants) rather than O(n_variants × n_analyses).

The single-pass in-memory path was rejected because loading all associations as Python objects before writing zarr is untenable at full-genome scale (~1B objects for 100 traits). Pre-processing VCFs to hg38 before ingestion was rejected to avoid modifying or duplicating source files. Liftover failures are tolerated up to a threshold rather than silently dropped because a wrong chain file would otherwise produce a valid-looking but nearly empty store.

`stored_effect_scale` is inferred from the `StudyType` field in the `##SAMPLE` VCF meta-line (`CaseControl → LOG_OR`, `Continuous → SD_UNITS`) rather than carried in the manifest, consistent with the GWAS-VCF spec.

**VCF reader: bcftools (updated from cyvcf2).** The initial implementation used cyvcf2. Benchmarking on the chr1 × 100-analysis UKB dataset showed bcftools is 2.26× faster for building (876.6s → 388.5s). cyvcf2 was replaced by `bcftools query` and `bcftools view -h` subprocesses in `opengwasdb/build/vcf_source.py`. See `docs/benchmark-output/opengwasdb_vcf_ukb_chr1_bcftools_benchmark.json` for the measured build times.

The output format is identical to `build_dense_observed_store` — same zarr layout, SQLite schema, manifest, and tabix variant axis — so all existing queries work unchanged.
