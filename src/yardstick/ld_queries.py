"""The LD section's queries and their shapes (#504): the one table its names come from.

The workload reads the shapes (how many variants a `matrix_N` or `proxies_N` reads, how wide
a region is), and the result schema reads the names, in this order, which og-dev-hub's
Stores view reads too (`yardstick.result.LD_TIMED`). It imports nothing, so the schema's
readers can import it.
"""

from __future__ import annotations

#: `matrix_N` reads N of the workload's 500 variants (`yardstick.ld.matrix_inputs`).
LD_MATRIX = {"matrix_10": 10, "matrix_500": 500}
#: A region's width; the query is `region(chromosome, centre, width // 2)`, and the reader refuses a
#: radius over far_bp / 2, so a width over the Store's far_bp is refused before anything runs.
LD_REGIONS = {"region_1mb": 1_000_000, "region_3mb": 3_000_000}
#: `proxies_N` reads the workload's list of N variants.
LD_PROXIES = {"proxies_1": 1, "proxies_10": 10, "proxies_100": 100}
#: Every timed LD query, in the order a result lists them.
LD_TIMED = ("open", *LD_MATRIX, *LD_REGIONS, *LD_PROXIES, "clump")
