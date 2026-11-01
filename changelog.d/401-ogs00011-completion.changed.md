- **OGS-00011 is Reference-Completed and validated (issues #401, #332 and #334,
  epic #371).** The MAF-fixed build was completed against the UKB EUR LD panel
  at opengwasdb `v0.3.0`: it imputed 6,898,789,430 cells, and the Store now
  holds 48,069,918 variants, 14,267,257 of them on the Dense axis. Its
  Observed-View Check passed at `v0.3.2` and the Store was published on
  2026-11-01. A Release Erratum records eight associations whose source SE
  exceeds float16's 65,504 and is stored as `+inf`, the same eight in the
  Store published before (opengwasdb#338). `STORES.md` no longer shows
  OGS-00011 as completion pending.
