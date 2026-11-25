- **OGS-00011's rebuilt release is `validated` (#478)**, on the owner's approval of 25 Nov, after its benchmark on the published Store. That benchmark found:
  - query timings within about 10% of 1 Nov, except bulk reads, which are 13–29% slower;
  - the three MR pairs within 0.012 of before;
  - the known loci unchanged.

  58 imputed indel rows in its three regional windows are no longer imputed. Their panel reference allele is unsettled, so completion leaves them out rather than guess.
