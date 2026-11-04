# Source PDFs

Place original input PDFs here. The API discovers `*.pdf` files. Preserve names
when saved results refer to them; generated findings belong in
`ontology/<PDF stem>/`.

Existing tracked PDFs remain tracked. The current `.gitignore` ignores new files
under `pdf/`, so adding a new PDF to the working directory does not automatically
add it to Git. Publishing original PDFs is a separate explicit snapshot-export
option (`--include-pdfs`); see the [viewer guide](../docs/deployment/render_viewer.md).
