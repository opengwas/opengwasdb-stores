# Use store-local variant indices

Each Store Release contains its own Store Variant Table and assigns compact local Variant Indices to its rows. Indices need not remain stable across Stores or releases; they connect association arrays to that release's variant table.

Cross-Store matching uses canonical variant identity, qualified by Reference Assembly, rather than a global numeric variant dictionary. Reference-Completed releases also record Reference Panel Membership as variant metadata so query results can distinguish LD-panel variants from observed off-panel variants without adding per-association fields.

