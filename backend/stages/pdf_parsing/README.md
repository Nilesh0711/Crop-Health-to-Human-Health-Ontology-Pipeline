# PDF parsing

`structure.py` inspects pages, chapters and bibliography regions. `packets.py`
builds the exact numbered reading context, source targets and attached citations.
`layout.py` owns versioned text order:

- Fresh runs use `column-aware-text-v1`. Clear two-column structure **and repeated
  column interleaving** are required before coordinate reordering. Font fragments
  on the same printed line preserve original word/identifier boundaries.
- Pages already in column order, ambiguous coordinates and character-preservation
  failures retain plain text with a diagnostic reason. Page reports describe
  the method; they do not certify reading order or OCR accuracy.
- Existing extraction checkpoints retain their recorded parser and saved reading
  packets. Older checkpoints use `plain-text-v1`, preserving evidence numbering.

External public PDFs use the same text-order helper with a separately versioned
material cache. The offline `audit-layout` command compares reference-count and
page diagnostics across PDFs without modifying the sources. Visual/human review
remains necessary; higher reference counts do not establish higher accuracy.

See [v49](../../../docs/protocols/proof_audit_atomic_reextraction_and_layout.md).
