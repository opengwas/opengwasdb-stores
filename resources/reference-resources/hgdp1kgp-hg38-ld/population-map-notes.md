# Population mapping provenance

This mapping enumerates the 79 population labels (plus the synthetic truth
control) in the pinned gnomAD v3.1.2 HGDP+1kGP metadata. Every label is either
included in exactly one of AFR, EAS, SAS, or EUR, or carries an exclusion reason.

The ancestry-assignment fixture currently defines only the four broad groups
`AFR_fine`, `EAS_fine`, `SAS_fine`, and `EUR_fine`; it has no population-level
labels to reconcile. Consequently, all named HGDP populations are an explicit
granularity mismatch with that reference, rather than being silently dropped.
`ACB` and `ASW` are deliberately excluded despite their source region being AFR.
AMR, Middle Eastern/North African, and Oceanian groups are excluded because no
corresponding panel is declared.
