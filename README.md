# Crop To Human Health Ontology

## Overview

This project converts agriculture, crop, soil, food-security, and human-health research PDFs into structured ontology outputs. The pipeline extracts text from a PDF, generates subject-relation-object triplets, cleans and normalizes those triplets, classifies entities into ontology classes, builds class hierarchy information, extracts entity properties, and writes a final ontology JSON file.

The current implementation uses:

- `docling` for PDF text extraction
- an OpenAI-compatible local API endpoint at `http://localhost:11434/v1`
- a local Ollama model configured in the script as `gemma4-gpu`
- prompt files in the `prompt/` directory for the ontology-building stages

## What We Have Done

The project has been organized as a reproducible ontology-generation pipeline rather than a one-off script. The work completed so far includes:

- Created a PDF-to-text extraction flow using Docling.
- Added chunking so long PDFs can be processed in smaller sections.
- Added triplet extraction from PDF text.
- Added cleaning and deduplication of generated triplets.
- Added entity-name normalization and triplet validation.
- Added relation normalization so relation labels are more atomic and consistent.
- Added property extraction for entities.
- Added a fixed high-level ontology class schema.
- Added entity classification into ontology classes.
- Added hierarchy generation for `is-a` style subclass relationships.
- Added deterministic ontology assembly into `ontology.json`.
- Added checkpoint-style reuse of existing intermediate files so reruns do not always need to repeat every expensive step.
- Added output folders under `ontology/` so each processed PDF gets its own result directory.

## Repository Structure

```text
.
pdf_to_antology.py      Main pipeline script
requirements.txt        Python package dependencies
README.md               Project documentation
.env.example            Template for local environment variables
.env                    Local environment file created by the user; not committed to Git
prompt/                 Prompt templates used by the pipeline
pdf/                    Place input PDF files here
ontology/               Generated output appears here after running the script
```


## Prerequisites

Before running the project, make sure you have:

- Python 3.11 or newer
- `pip`
- Ollama, or another OpenAI-compatible local server available at `http://localhost:11434/v1`
- The model configured in `pdf_to_antology.py`

The script currently uses this model name:

```python
MODEL = "gemma4-gpu"
```

If your local model has a different name, change `MODEL` in `pdf_to_antology.py` before running.

Useful checks:

```bash
python --version
pip --version
ollama --version
ollama list
```

## Setup

Clone the repository, enter the project folder, create a virtual environment, and install the dependencies.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\requirements.txt
```

### Ubuntu / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

Create a local `.env` file from the example file if you need to store local keys or configuration.

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Ubuntu / macOS:

```bash
cp .env.example .env
```

Do not commit `.env` to GitHub.

Put your input PDF inside the `pdf/` folder.

By default, the script is configured to process:

```text
pdf/The links between agriculture and health.pdf
```

To process a different PDF, update this line in `pdf_to_antology.py`:

```python
PDF_PATH = "pdf/your_file_name.pdf"
```

## Run The Pipeline

Start the local model server if it is not already running:

```bash
ollama run gemma4-gpu
```

Then run the script.

Windows PowerShell:

```powershell
python .\pdf_to_antology.py
```

Ubuntu / macOS:

```bash
python3 pdf_to_antology.py
```

## Pipeline Steps

When the script runs, it performs these main stages:

1. Extract text from the configured PDF.
2. Split the extracted text into chunks.
3. Extract raw subject-relation-object triplets from each chunk.
4. Clean and deduplicate triplets.
5. Normalize entity names.
6. Normalize relation labels.
7. Extract entity properties.
8. Generate ontology classes, classify entities, and build hierarchy data.
9. Build the final ontology JSON.
10. Infer indirect relationships and write expanded triplets.

## Output Folder Explained

After a successful run, the script creates a folder inside `ontology/` using the PDF filename without `.pdf`.

For the default PDF, the output folder is:

```text
ontology/The links between agriculture and health/
```

Expected files inside that folder:

```text
raw_triplets.txt
cleaned_triplets.txt
normalized_triplets.txt
relation_normalized_triplets.txt
properties.json
classes.json
classified_entities.json
hierarchy.json
ontology.json
expanded_triplets.txt
```

### Output File Meanings

`raw_triplets.txt`

Contains the first generated subject-relation-object triplets extracted from the PDF chunks. This is the least cleaned version and may contain duplicates, inconsistent names, or noisy relations.

`cleaned_triplets.txt`

Contains triplets after cleaning and deduplication. This file should be easier to read than `raw_triplets.txt` and is used as a stronger base for later stages.

`normalized_triplets.txt`

Contains triplets after entity normalization and validation. Entity names are made more consistent so the same concept is less likely to appear under multiple spellings.

`relation_normalized_triplets.txt`

Contains triplets after relation labels are normalized into cleaner, more atomic relation names. This helps the final ontology avoid messy relation phrases.

`properties.json`

Contains extracted properties for entities. A property describes an attribute of an entity rather than a relation between two separate entities.

`classes.json`

Contains the high-level ontology class schema used by the project. These are the broad categories into which entities can be classified.

`classified_entities.json`

Contains specific entities found in the PDF and the class assigned to each entity.

`hierarchy.json`

Contains subclass or `is-a` style hierarchy information. This file is used to represent parent-child class relationships.

`ontology.json`

This is the main final ontology output. It combines classes, instances, subclass relationships, relations, and properties into one structured JSON file.

Expected top-level sections include:

```json
{
  "classes": [],
  "instances": [],
  "subclasses": [],
  "relations": [],
  "properties": []
}
```

`expanded_triplets.txt`

Contains inferred or indirect relationships generated after the main ontology-building step. This can help reveal relationships that are not directly stated in the original extracted triplets.
