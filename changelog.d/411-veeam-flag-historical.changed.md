- **The yardstick no longer flags a run for the Veeam backup window, and the
  runner no longer warns about it (issue #411).** Since 2026-11-02 the nightly
  snapshot skips `/data/opengwasdb`, so a run at 03:00 is as comparable as one
  at 15:00 and records `veeam_window: false`. The field stays in Result schema
  1: a result measured before that date keeps the flag that was true of it, and
  the site still rings those points and marks them `†`.
