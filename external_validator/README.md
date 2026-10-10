# External validation package

This package supplies the application's external validator and a standalone CLI. For a complete PDF run, use the web application. The CLI reads prepared candidates and writes scratch JSONL under `rag/`.

```text
external_validator/
  candidates.py       raw tuple/evidence parsing
  llm.py              local Ollama structured-JSON client
  workflow.py         citation and broad retrieval, fusion, reranking, judging
  retrieval.py        Crossref, Europe PMC, OpenAlex, XML/PDF passage extraction
  rerank.py           BGE-large dense retrieval and MedCPT cross-encoder reranking
  nli.py              DeBERTa NLI conflict detector
  decision.py         per-document decision and quote/entity/relation guardrails
  claim_matching.py   conservative contiguous entity matching
  storage.py          JSONL and stable identifiers
  cli.py              build-candidates, health-check, retrieve, validate
```

## Validation flow

1. Resolve bibliography entries cited by the exact source evidence span through Crossref and look them up in Europe PMC. If needed, use only an explicit public OpenAlex PDF for that same work.
2. Ask Qwen for a concept-only broad Europe PMC query and a neutral HyDE passage.
3. Rank up to 100 metadata candidates using BM25 plus BGE-large reciprocal-rank fusion.
4. Rerank the leading candidates jointly with `ncbi/MedCPT-Cross-Encoder` before transient full-text retrieval.
5. Prefer Europe PMC XML, fall back to an explicit OA PDF, then an indexed abstract. Remove references and other back matter where possible. Keep page numbers for PDF passages.
6. Rerank passages and retain only the configured top chunks.
7. Ask Qwen to judge each article separately and quote one or two complete consecutive sentences.
8. Require precise entity, relation, direction, certainty, and qualifier agreement. DeBERTa NLI may veto a supported decision on strong contradiction but cannot create support.
9. Keep source-reference evidence distinct from independent external evidence. Two independent support groups are required for the independently corroborated state.

The originating PDF is excluded from external corroboration. Whole external articles are processed in memory and discarded; only selected passages, page metadata, bibliographic metadata, access/license information, and decisions persist.

## Models and fallback behavior

Defaults are configured in `external_validator/config.py` and can be overridden through environment variables:

- `EMBEDDING_MODEL=BAAI/bge-large-en-v1.5`
- `CROSS_ENCODER_MODEL=ncbi/MedCPT-Cross-Encoder`
- `NLI_MODEL=cross-encoder/nli-deberta-v3-large`
- `ENABLE_NLI_VALIDATOR=1`

The embedding and cross-encoder loader tries `sentence-transformers` first and uses direct PyTorch/Transformers loading if that stack is unavailable. NLI is an additional conservative conflict check, not the final judge.

## CLI

```bash
python -m external_validator.cli health-check
python -m external_validator.cli build-candidates --tuple-file raw_triplets.txt
python -m external_validator.cli retrieve --limit 3
python -m external_validator.cli validate --limit 3
```

The standalone CLI assumes its candidates are already faithful to their source. In the web pipeline, `backend/source_validation.py` performs source entailment before this package runs.
