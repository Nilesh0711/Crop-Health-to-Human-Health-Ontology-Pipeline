# Evidence ranking

Ranking allocates retrieval effort and selects passages for neighboring-pair
assessment. It does not determine scientific support. The source and external
models judge meaning from supplied evidence; exact-text checks preserve provenance.

## Publication ranking

`articles._hybrid_order` combines BGE dense similarity, BM25 lexical matching,
metadata anchor coverage, scholarly-API order and accessible-full-text signals.
MedCPT reranks a shortlist, with rank fusion retaining the other retrieval signals.
Metadata-only title relevance remains uncertain; it is not paper evidence.

`pathways.balanced_articles` runs this selection against the two claim-link and
local-connection targets, interleaving their rankings to avoid relying on one topic
score. This improves coverage allocation but does not guarantee recall. All usable
resolved source-cited leads receive slots outside the independent-search budget.

## Passage ranking

After retrieval fetches full text or a usable abstract, `passages.SemanticReranker`
selects exact chunks against each target's complete wording and a neutral entity/
qualifier query. `pathways.balanced_passages` interleaves those ranked lists within
the per-paper budget, preserves original chunk identity/text and annotates which
targets selected a chunk. The default budget is four chunks per paper.

Shared cache/index text is reranked for the current pair. Follow-up ranking focuses
on unresolved links, local conditions and proposed corrections rather than
reserving the budget for already supported links. A selected passage
retains its paper, section and numbered sentences. The judge selects exact proof
references; a ranking score alone never grants support. Abstract-only evidence
retains its access label and cannot supply missing full-text measurements.

## Execution controls

- `EMBEDDING_BATCH_SIZE=16` and `CROSS_ENCODER_BATCH_SIZE=8`, each 1-128.
- `CROSS_ENCODER_CANDIDATES=20` for the metadata shortlist.
- `EXTERNAL_TOP_CHUNKS=4`, configurable 1-12 per paper.
- Bounded fetch/CPU preparation workers hand papers to one ranking owner while
  hosted paper assessments can proceed concurrently.
- Compatible local models remain loaded between resumes in one backend process;
  selected text and vectors can be reused through the bounded evidence index.

Bare configuration defaults to CPU. The launcher chooses CUDA when available;
explicit device settings remain authoritative. Larger batches require measurement
and can exhaust device memory. Local acceleration affects retrieval and reranking,
not the hosted model's generation speed. See [configuration](../../config/README.md)
and [retrieval](../literature_retrieval/README.md).
NLI is not part of active execution.
