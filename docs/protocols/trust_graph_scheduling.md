# Shared assessment reliability and scheduling

Updated 2026-10-27. Applies to Trust graph mode. Scientific judgement remains
DeepSeek Flash per paper and MiMo Pro for cross-paper synthesis; Thinking remains
Off by default. The previous validated-first mode remains available.

## Observed failure

The saved grouped-paper response repeated `candidate_id` at the beginning and
end of each result object. The same pattern recurred during the one permitted
regeneration. Both requests finished normally, with zero reported reasoning
tokens. This was neither truncation nor a timeout. Strict duplicate-property
parsing correctly refused to choose one identity silently.

The grouped prompt inherited a one-claim instruction and then appended a
multi-claim instruction. The response schema placed the identity last while the
natural result format suggested placing it first. These conflicting cues are
plausible contributors; a model's internal cause cannot be established from its
output alone.

Multi-claim shared-paper requests use this wire contract:

```json
{
  "results": {
    "claim-A": {"decision": {}, "finding": {}},
    "claim-B": {"decision": {}, "finding": {}}
  }
}
```

The empty objects above illustrate envelope ownership only; actual decision and
finding values must satisfy the full scientific schema. Identity occurs once
as the key. Missing/unknown keys, duplicate properties and extra candidate_id
fields are invalid. A validated keyed object is converted to the existing
internal rows; this is a contract adapter, not a local JSON repair. Recovery
still permits at most one same-model correction call, with no fallback model.

A subsequent resumed run repeated a whole claim-keyed value three times on a
one-claim request. Single-claim paper requests now use the ordinary root
`decision`/`finding` contract with no collection or claim-ID keys. The caller
already fixes that one claim and paper, and adds its known identity to the
internal record only after schema/proof checks. No attribution is guessed.
True multi-claim requests retain explicit keys and exactly-once checking.

Generation allowance is also separated from desired answer length. Previously
the large safety allowance was included in the prompt as an approximate answer
length. Trust requests now provide an independent concise-answer hint (2600
tokens per paper-claim assessment, 3000 per synthesis claim). The prompt says
this is not a target to fill and must not cause omission of required evidence.
The provider's configured generation allowance remains unchanged. This removes
a potentially misleading instruction; it cannot guarantee that a JSON-mode
provider will never produce malformed output.

## Changes

| Before | Now | Purpose |
| --- | --- | --- |
| Paper calls sequential inside a group | Up to 3 independent paper requests concurrently | Reduce cloud wait time without removing papers |
| Each group finishes retrieval and judgement before the next starts | One retrieval owner prepares one future group during current judgement | Overlap network/ranking work with cloud calls |
| Prepared text persisted only after assessment begins | Signed selected packets checkpointed as soon as prepared | Preserve lookahead work on interruption |
| Full-text cache key depends on indexed abstract | Additional exact XML/PDF access-identity cache | Reuse the same parsed copy across metadata variations |
| Concurrent identical cache misses can repeat a download | One computation per in-flight cache key | Avoid duplicated I/O; return isolated copies |
| Identical section context repeated in Pro input | Exact shared source-context pool with per-claim references | Reduce input repetition without summarizing evidence |
| Latest completed sibling can hide another waiting request | Keep an active request visible and show concurrent count | Make parallel execution understandable |
| Only provider-attempt timings | Group preparation/wait/paper/synthesis wall timings | Distinguish I/O from model latency |

`EXTERNAL_PAPER_WORKERS` defaults to 3, allows 1..4, and can be set to 1 for
serial comparison. This differs from runtime **Claims per shared assessment**,
which controls how many claims share one assessment, not parallel request count.
`EXTERNAL_PREFETCH=0` disables retrieval lookahead. GPU/ranking still has one
retrieval owner. Bounded CPU/network fetch workers remain unchanged.

Completed paper results are checkpointed by one owner and reordered into the
original paper order before synthesis. Pro waits for all selected papers in its
group. On failure, no further paper requests are submitted; already submitted
successes are saved. Lookahead inherits user cancellation and is cancelled when
its owner exits. Blocking network operations may still take their configured
timeout to return. Provider rate/usage failures remain visible without hidden
retries or a smaller model.

## Published search-system ideas

These are documented patterns, not a claim to reproduce private search-engine
implementations or their scientific accuracy:

- Google describes splitting questions into subtopics and issuing searches
  simultaneously. This supports overlapping independent retrieval work.
  [Google AI Mode](https://blog.google/products-and-platforms/products/search/google-search-ai-mode-update/)
- Microsoft documents LLM query planning, parallel subqueries, semantic
  reranking and unified grounding references. It also acknowledges that extra
  agentic planning adds latency. Our existing complementary-query/reranker flow
  stays; this update avoids adding a mandatory new planning LLM pass.
  [Azure agentic retrieval](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-overview)
- Bing describes combining retrieval with LLM generation and source links.
  This supports preserving the source-evidence relationship through synthesis.
  Edge is a browser; Bing/Copilot are the relevant search/answer services.
  [Bing generative search](https://blogs.bing.com/search/2024/7/generativesearch/)
- Brave describes ranked extracted chunks, source metadata and configurable
  context budgets. Our application keeps traceable selected passages and
  removes exact repetition rather than replacing evidence with free summaries.
  [Brave LLM Context](https://api-dashboard.search.brave.com/documentation/services/llm-context)
- DuckDuckGo documents linked sources for Search Assist answers. Source links
  make an answer inspectable but do not themselves establish scientific support.
  [DuckDuckGo Search Assist](https://duckduckgo.com/duckduckgo-help-pages/results/ai-assisted-answers)

Search-platform documentation is research inspiration for our own
[evidence-reuse and paper-ready update](validation_speed.md); commercial search
connectors are not part of the pipeline. Existing
scholarly retrieval, every usable attached citation, selected paper budgets,
per-edge decisions and strict literal provenance checks remain in place. The
LLMs interpret scientific meaning; scheduling and caching do not vote on truth.

## Verification and remaining work

Offline tests cover concurrent execution, paper order and attribution, strict
keyed responses, source-context preservation, partial checkpoint recovery,
prepared-packet reuse, cancellation and shared source downloads. An isolated
live check, when performed, is recorded under the dated optimization report;
it is not a full-document accuracy or latency benchmark.

Actual production speed depends on provider queuing, source-download delays,
cache hits and recovery rates. Parallelism reduces waiting, not inherently the
number of required scientific assessments. A future benchmark can compare
retrieval-aware claim grouping and bounded missing-evidence searches, while
retaining per-edge evidence and explicitly recording unassessed connections.
