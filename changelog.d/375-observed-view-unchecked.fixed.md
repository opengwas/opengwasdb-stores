- **A failed Observed-View Check names an unchecked counter `unchecked`, never
  `None` and never 0 (opengwasdb#375).** A runtime that carries opengwasdb#375 writes
  the counters of a part it did not compare as `null` rather than 0, so a skipped part
  no longer reads as a clean census. The exit-1 message renders such a counter as
  `unchecked`; an integer from the pinned runtime is still read as its count, so the pin
  can move without the message changing shape.
