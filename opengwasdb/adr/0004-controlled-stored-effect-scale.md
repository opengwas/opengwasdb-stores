# Use a controlled stored effect scale and loose original effect scale

OpenGWASDB stores effects on a standardised Stored Effect Scale with a strict controlled vocabulary. The initial vocabulary is SD Units, Log Odds, and Log Hazard; there is no `other`, `unknown`, or original-units stored scale.

The source unit is retained separately as Original Effect Scale provenance and is not forced into an ontology because source units are heterogeneous and often free text. Sample size semantics remain separate from effect scale.

