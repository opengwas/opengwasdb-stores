- **Click a component on the architecture pages to see its details (issue #435).**
  On Pipeline, Structure and health and the Work map, a component's box in a
  diagram, or its name in a list or table, opens a panel with:
  - the paths it claims, linked to GitHub at the commit read;
  - its size and stages;
  - the work on it now, by owner, with each item's state;
  - its imports each way, with counts, and the upward ones marked;
  - its ADRs, with how each was placed.

  This is the architecture map prototype's panel (#417), which the merged pages
  had dropped without a decision. The claimed paths and per-component imports
  were on no page before.

  `#component-<id>` opens the panel from a link. Work that could not be
  measured is shown as unknown, never as none. Each page embeds only the fields
  the panel shows, so the collected data's host paths are never published.
  Checked on the 4 Nov render of `main`: all 25 diagram boxes and every named
  component open their panel.
