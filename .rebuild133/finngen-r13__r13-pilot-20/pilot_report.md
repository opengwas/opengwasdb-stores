# FinnGen R13 20-Analysis pilot assessment

## Observed build

- Analyses: 20
- GRCh38 union variants: 21,230,615
- Source downloads: 15.59 GB
- Store size: 4.11 GB
- Build wall time: 4058.1 seconds
- Peak observed RSS: 43.95 GB
- Store validation: passed
- Binary query probe: finngen-r13-RX_PARACETAMOL_NSAID (21,230,615 finite associations)
- Quantitative query probe: finngen-r13-BMI_IRN (21,228,482 finite associations)

## Release evidence

- Checks: schema=passed, files=passed, reader_smoke_test=passed, ancestry=passed, effect_scale=failed, sd_estimation=failed, selection=passed, metadata_resolution=passed, store=passed
- Failed SD analyses: finngen-r13-HEIGHT_IRN
- Named warnings: 1

## Scale-up risks

- Resolve the quantitative-trait scale discrepancy before treating every IRN endpoint as declared-standardised.
- Linear source-volume extrapolation is 2.15 TB for 2,754 endpoints; confirm provider and local capacity.
- A full dense axis at the observed 21,230,615 variants would contain 58,469,113,710 variant-by-Analysis cells before considering array fields, compression, indexes, or staging overhead.
- OpenGWASDB's union-axis Pass 1 is intentionally serial, so build wall time will not scale only with the configured Pass 2 worker count.

## Decision

Blocking reasons:

- required release check effect_scale=failed
- required release check sd_estimation=failed
- effect-scale evidence failed for finngen-r13-HEIGHT_IRN

The native FinnGen build/query path is operational, but the complete R13 collection should not be onboarded until the blocking evidence is resolved.

Recommendation: **NO-GO**
