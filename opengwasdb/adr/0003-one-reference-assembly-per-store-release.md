# Use one reference assembly per Store Release

Every Store Release declares exactly one Reference Assembly. ALID remains the compact `chr:pos:A1:A2` identity within a Store, while federation and cross-Store matching qualify it with the assembly.

Coordinate conversion is an ingestion concern recorded in provenance rather than permitting mixed coordinate systems inside one release.

