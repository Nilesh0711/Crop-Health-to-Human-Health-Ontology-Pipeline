# External validation package

The package supplies the application validator and a standalone CLI, invoked with `python -m external_validator.cli`. The implementation is split by responsibility:

For a fresh end-to-end PDF run, use the web app described in `README.md`. The CLI reads manually prepared `raw_triplets.txt` files and writes scratch JSONL under `rag/`; it is not required by the web app. Example: `python -m external_validator.cli health-check`.

```
external_validator/
  candidates.py  # raw tuple/evidence-comment parsing and provenance
  llm.py         # local Ollama / configured Qwen JSON client (default: qwen3:8b)
  workflow.py    # query → HyDE → retrieve → rerank → judge orchestration
  retrieval.py   # Europe PMC requests and in-memory XML parsing
  rerank.py      # BAAI/bge-small-en-v1.5 semantic ranking
  decision.py    # per-paper decision contract and quote/entity/relation guardrails
  claim_matching.py # conservative contiguous entity phrase checks
  storage.py     # JSONL and stable IDs
  cli.py         # build-candidates, health-check, retrieve, validate
```

The production path is `validate`, not `retrieve`:

1. `qwen3:8b` turns the candidate entities, types, and qualifiers into a concept-only Europe PMC query. The originating paper title is deliberately excluded.
2. Qwen writes a one-sentence HyDE hypothesis.
3. Europe PMC returns open-access candidates; BGE-small ranks their title/abstract previews before any full-text request.
4. XML for only the top article candidates is held in memory. BGE-small ranks its chunks and only the top chunks are retained.
5. The configured Qwen model judges each external paper separately and must cite one or two complete consecutive sentences from a retained chunk.
6. Guardrails require the cited text to name the precise entities and compatible relation, direction, certainty, and available crop/population qualifiers. Ambiguous synonyms are sent to review rather than auto-accepted.
7. Each claim is validated in one bounded pass. Insufficient claims remain in the audit view; they are not automatically retried.

In the web app, `backend/source_validation.py` runs before this package. The standalone CLI does not perform that source-PDF entailment step; it assumes independently prepared candidates.

On this workstation, Windows application control blocks a SciPy DLL required by `sentence-transformers`. `rerank.py` tries `SentenceTransformer('BAAI/bge-small-en-v1.5')` first, then uses the same BGE model through PyTorch/Transformers as a documented fallback. It never falls back to lexical ranking.
