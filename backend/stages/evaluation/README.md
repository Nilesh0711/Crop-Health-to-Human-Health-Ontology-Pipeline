# Evaluation

`metrics.py` builds annotation samples and scores independently supplied labels.
`neighbors.py` freezes local-pair assessments and compares versioned runs without
inventing gold or transferring labels to changed evidence. `parsing.py` compares
PDF text-order policies using observable page/bibliography diagnostics.

Commands live in `python -m backend.cli.evaluation`:

- `snapshot-neighbors`: export a new immutable comparison snapshot outside the
  source ontology; existing outputs cannot be overwritten.
- `compare-neighbors`: compare the same source document before/after, separating
  changed inputs, removed/new pairs, technical holds and reviewed scientific errors.
- `compare-corpus`: load a manifest of before/after snapshots, preserving separate
  document results instead of double-counting shared cross-PDF pairs.
- `audit-layout`: compare multiple source PDFs without editing them; report
  reference counts and page-layout signals, not scientific accuracy.

These commands make no LLM calls. Gold requires an independent reviewer and the
exact saved assessment/evidence signature. See [v49](../../../docs/protocols/proof_audit_atomic_reextraction_and_layout.md)
for commands, manifest format and limitations.
