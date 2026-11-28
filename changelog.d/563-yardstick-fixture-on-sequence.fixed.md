- **The yardstick's fixture Store no longer places a variant in chr1's leading assembly gap (opengwasdb#410, issue #563).**
  `rs1` was at 1:10000, inside the gap (1-10,000) that every build now drops, so the Store held one variant and the
  `resolve` measure found no rows. It is at 1:15000.
