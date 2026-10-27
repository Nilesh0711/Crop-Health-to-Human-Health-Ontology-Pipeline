# Generated per-PDF findings

Each `ontology/<PDF stem>/` folder holds candidates, selected evidence, source and
external validations, graphs, logs, summaries, revisions and evaluation samples.
Generated findings remain ignored by Git. Preserve human-labelled evaluation
fields and historical run provenance. Do not put application source or original
PDFs here; original PDFs belong in `pdf/`.

`ontology/.pathways/` holds the global Pathway Assembly snapshot and resumable
entity-alignment checkpoint. It is separate from each PDF's original claims and
validation records. Rebuilding pathways does not rewrite those source records.
