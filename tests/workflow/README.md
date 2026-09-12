# Workflow behaviour

The three things the DAG is responsible for beyond issuing correct commands:

1. conditional branches and resumption produce the right step set -- rho off,
   no completion child, a partial set of records;
2. a failed step cannot damage a live Store or a good `validation.yaml`;
3. records merge into `validation.yaml` correctly, and `register` fails when a
   record's executed argv differs from the planned argv -- except for the
   `complete-dense-resume` substitution, which it accepts and records.

One fixture-scale end-to-end smoke test lives here too. Source formats, Store
contents, layouts, queries and scientific invariants are tested once, in
`opengwasdb`.

Empty until Phase A is implemented.
