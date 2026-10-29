# Use ragged overflow for off-panel observations in dense completed stores

For Dense Reference-Completed releases, the dense matrix axis contains only Reference Variant Set variants. Observed off-panel associations are stored exclusively in Ragged Overflow so dense axes remain identical across Stores completed with the same LD Reference Panel.

Observed associations outside the Reference Variant Set are retained rather than discarded. The dense completed grid covers the LD panel; overflow preserves source-faithful associations that cannot be placed on that grid.

Observed-Only Dense releases remain source-faithful and do not require an LD Reference Panel or panel-defined dense axis. A later Reference-Completed release may use a different dense axis defined by the LD Reference Panel.

