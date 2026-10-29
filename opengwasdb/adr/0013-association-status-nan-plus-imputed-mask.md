# Encode association status with NaN and an imputed mask

Reference-Completed Stores encode Association Status using canonical NaN values in the statistic arrays plus an imputed mask aligned to the dense grid or ragged sequence. Finite Z and SE with `imputed=false` means observed; finite Z and SE with `imputed=true` means imputed; NaN Z and SE means missing.

This avoids a separate status array while retaining the three required states. Builders and validators must reject inconsistent states such as only one statistic being NaN, imputed values with NaN statistics, or non-canonical NaN payloads that would harm compression.

The imputed mask is a dense boolean or uint8 Zarr array for Dense grids and an association-aligned boolean or uint8 array for Ragged Reference-Completed sequences. It is not a sparse offsets index like the top-hit significance index and is chunk-aligned with Z and SE arrays because most statistic reads also need association provenance.

