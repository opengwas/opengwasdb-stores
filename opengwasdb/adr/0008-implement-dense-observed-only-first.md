# Implement Dense Observed-Only first

The first production implementation slice is Dense Observed-Only storage. This validates the common Store envelope, manifest, SQLite metadata/index, Zarr Z and SE arrays, layout-independent query API, top-hit index, and release validation without taking on reference completion or ragged-region complexity at the same time.

Reference completion, Ragged layout, and Dense Ragged-Overflow support build on this slice after the core Store contract is exercised end to end.

