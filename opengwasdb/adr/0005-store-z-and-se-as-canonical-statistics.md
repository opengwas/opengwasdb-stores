# Store Z and SE as canonical association statistics

OpenGWASDB stores Z and SE as the canonical logical statistics for each retained association, with beta derived as `z × se` when needed. SE is always non-negative; effect direction is carried by signed Z.

This preserves source uncertainty directly, avoids the undefined `beta / z` case when Z is zero, and keeps reconstruction independent of allele frequency and sample size.

