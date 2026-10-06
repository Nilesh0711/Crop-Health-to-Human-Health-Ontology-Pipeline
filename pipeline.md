# Crop to Human Health Ontology Pipeline

## Overview
This pipeline builds a research-grade knowledge graph linking agricultural factors to human health outcomes. It processes pre-extracted 6-tuples (from ChatGPT) through validation, graph construction, and multi-hop reasoning to discover hidden causal chains.

## Pipeline Architecture (4 Steps)

### Step 1 — Load Raw Tuples
- **Source**: 6-tuples extracted using ChatGPT 5 from scientific PDFs
- **Location**: Each PDF's tuples are stored in `ontology/<pdf_name>/raw_triplets.txt`
- **Format**: `<Subject, Relation, Object, SubjectType, ObjectType, Confidence>`

### Step 2 — Deduplicate & Validate
- **Deduplication**: Case-insensitive exact-match removal of duplicate tuples
- **Validation**: Each tuple is checked against:
  - `VALID_RULES` matrix (90+ valid type-relation-type combinations)
  - `ENTITY_TYPES` whitelist (7 types: Crop, PlantDisease, Nutrient, SoilCondition, ClimateFactor, HumanDisease, AgriculturalPractice)
  - `RELATIONS` whitelist (8 relations: affects, causes, reduces, increases, has_nutrient, leads_to, associated_with, deficiency_causes)
  - Confidence threshold >= 0.7
- **Entity Normalization**: Dictionary-based merging of synonymous concepts (e.g., "antioxidant activity" → "antioxidant")

### Step 3 — Build Knowledge Graph
- **Engine**: NetworkX DiGraph
- **Master Graph**: Persists across runs in `ontology/master_knowledge_graph.graphml` — each new PDF's data is appended
- **Local Graph**: Per-PDF isolated graph saved to `ontology/<pdf_name>/knowledge_graph.graphml`
- **Reverse Edges**: Automatically generated (e.g., `affects` → `affected_by`)

### Step 4 — Multi-hop Reasoning
Two-stage path traversal algorithm:

**Stage 1 — Direct Paths**: Source nodes (ClimateFactor, SoilCondition, AgriculturalPractice) → HumanDisease, up to 6 hops.

**Stage 2 — Partial Path Stitching**: If no direct paths exist, finds Source → Nutrient and Nutrient → HumanDisease paths separately, then stitches them at the overlapping Nutrient node.

## Workflow

1. Upload PDF to ChatGPT 5 web
2. Paste the prompt from `prompt/ChatGPT Extraction Prompt`
3. Copy the output tuples into `ontology/<pdf_name>/raw_triplets.txt`
4. Run `python pdf_to_antology.py`
5. Check `reasoning_output.txt` for discovered causal chains

## Dependencies
- `networkx >= 3.0` (graph construction and path traversal)
- No LLM API, no PDF extraction libraries needed

## External Research Validation (Separate, In Progress)

The graph pipeline above remains unchanged while external validation is
developed. `external_validation.py` reads each `raw_triplets.txt` file and
creates a stable candidate record with originating-paper metadata (paper title,
tuple file/line, and local PDF hash where available).

For each candidate, it queries Europe PMC for a small set of open-access
external research articles. Full text is retrieved into memory only, split and
ranked there, then discarded. The persistent validation packet contains only
the selected evidence chunks and bibliographic metadata; external PDFs, XML,
and complete articles are never written to the repository.

The next step is one-document-at-a-time external entailment using
`prompt/External Entailment Validator Prompt.md`. Decisions are validated
against stored chunks before `external_validation.py ingest-decisions` writes
the external evidence ledger. Source-paper entailment is intentionally deferred
and is not part of this milestone.

## Output Files (per PDF)
- `raw_triplets.txt` — Input from ChatGPT (manually placed)
- `validated_triplets.txt` — Tuples that passed validation
- `knowledge_graph.graphml` — Local graph for this PDF
- `reasoning_output.txt` — Discovered multi-hop reasoning chains

## Output Files (shared)
- `ontology/master_knowledge_graph.graphml` — Cumulative graph across all PDFs
