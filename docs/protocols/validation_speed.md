# Reusing evidence and judging ready papers

Implemented 2026-10-27; scope clarified 2026-10-28. Trust graph mode retains DeepSeek Flash paper assessments
and MiMo Pro cross-paper synthesis. Thinking defaults remain Off. The previous
validated-first mode remains available. This changes scheduling and reuse,
not source-grounding or scientific evidence requirements.

## Overlapping work

One retrieval owner prepares the active group and at most one future group.
Bounded network/CPU workers fetch and parse papers. Each ranked document enters
a ready queue immediately. Up to three DeepSeek paper requests run while later
claim packets are still being prepared. Queued claims that already share that
paper can share its assessment request.

Completed, checked findings are checkpointed by the main owner and appear through
frontend polling. They remain provisional until the complete group receives Pro
synthesis. Pro waits for every selected paper. Raw partial JSON is never shown
as an accepted scientific finding.

Early input can be smaller than the eventual union of selected chunks for a
shared paper. Final assessment reuses a result only if its exact paper/claim
signature still matches. Expanded inputs are assessed again. This can add calls
in exchange for earlier findings; `trust_summary.json` records early calls,
final paper calls, synthesis calls, retrieval wait and early-drain wall time.
Set `PAPER_READY_JUDGEMENT=0` for a group-barrier comparison.

## Slow search sources

Europe PMC/OpenAlex searches and attached-citation resolution run under a search
deadline for each claim in trust mode. Default:
`SEARCH_WAIT_SECONDS=20`; individual metadata requests use
`METADATA_TIMEOUT_SECONDS=20`. Completed sources are retained. HTTP metadata I/O
is cooperatively cancellable; transient 429/5xx errors permit one bounded retry
within that deadline. Original PDF/XML downloads retain their existing timeouts
and may still be slow. This is not live reranking of every search hit on arrival;
available metadata is ranked together when sources finish or the deadline ends.

Unfinished/failed sources are recorded under `pending_sources`, with provider,
query or reference identity and reason. `retrieval_pending` claims retain their
findings but cannot enter the validated graph. The frontend explains this state
and offers resume/retry. Resume reruns incomplete retrieval while reusing
successful query results, source text and compatible assessments. Pending work
never becomes an empty successful search or a scientific contradiction. A
permanently unavailable configured service may need its configuration corrected.

## Shared index and warm models

`ontology/.evidence_index/evidence.sqlite3` is a reconstructible shared cache,
independent of permanent per-claim proof. It contains bibliographic identities,
successful parsed source text, sentence-indexed chunks and normalized embeddings.
Original downloaded PDF/XML bytes remain transient.

- Parsed text uses exact XML source/ID or PDF URL, a parser-policy version, a
  stored content hash and a seven-day freshness limit. Changed fetched text
  creates new chunk keys. A source can change during the TTL; no ETag or
  Last-Modified change-notification service is implemented.
- Chunk keys include exact text content and chunk-policy version. Stored chunks
  are reranked for each claim; old relevance or truth decisions are not reused
  as retrieval scores.
- Vectors include exact input text, backend, model name and resolved weight
  revision. Local model paths use file size/mtime fingerprints. Unidentifiable
  weights use process-local vectors, not persistent cross-run reuse.
- Bibliographic/full-text-preview FTS supplies additional candidate papers;
  ordinary identity exclusion, ranking and paper budgets still apply. Bounded
  previews are retrieval aids, never proof quotations.
- Successful queries are reused for at most one hour. Failed fetches do not
  become persistent missing-evidence records.
- The logical compressed-entry budget defaults to 512 MiB with LRU eviction;
  FTS is separately limited to 4,096 previews of at most 18,000 characters.
  SQLite/WAL files can occupy more disk space than the compressed-entry budget.

`EVIDENCE_INDEX_ENABLED=0` disables shared persistence. Adjust
`EVIDENCE_INDEX_MAX_MB` and `EVIDENCE_INDEX_TTL_HOURS` to set limits. Cache expiry
never deletes stored claim proof. Corrupt cached content fails visibly rather
than becoming scientific absence.

Embedding, cross-encoder and NLI instances are reused within the backend process
for compatible model/device configurations. Device inference locks serialize
shared GPU use; CPU parsing and cloud judgement continue concurrently. A backend
restart still needs initial loading. Models are not all preloaded at startup.

## Smaller output and correction

Paper and synthesis prompts ask for short explanations without repeating claims
or quotations. Exact identifiers, conditions and evidence requirements stay.
Generation budgets remain available; brevity hints are not evidence caps.

If grouped Pro synthesis has invalid role-proof selections, its one permitted
recovery receives only that claim's admitted text and fixed assessment. The model
returns replacement `role_evidence` only. Code inserts that field at the
identity-matched row and rechecks the entire response. Decisions, types, other
claims and pathway connections stay fixed. This is model-made proof correction,
not local scientific repair. A failed correction starts no third call and uses
no substitute model. Other schema/content errors retain bounded regeneration.

## Research inspiration, implemented within this project

The user requested research into search/RAG architecture, not integrations with
commercial search platforms. The initially added optional Brave/Perplexity
connectors, credentials configuration and routing have been removed. No live
calls were made to those services. Scholarly retrieval remains Europe PMC,
OpenAlex and bibliography/Crossref resolution.

These mappings are our engineering adaptations of public documentation; they
do not claim access to any company's complete private implementation:

| Published principle | Application here | Boundary |
| --- | --- | --- |
| Google separates crawling/indexing from serving results | Persist parsed text, indexed previews, chunks and vectors for reuse across runs | Our index fills after first encounters; it is not a pre-crawled web index |
| Microsoft describes parallel subqueries, semantic reranking and merged grounding | Keep complementary scholarly queries, hybrid ranking and exact source identities; overlap ready-paper judgement with later retrieval | Search metadata still waits for completion or the bounded deadline before ranking |
| Brave describes ranked extracted chunks packaged for LLM grounding | Prepare reusable sentence-indexed chunks and compact exact evidence packets within our own retrieval code | Measurements, conditions and opposing findings are not removed merely to shorten input |
| Perplexity describes hybrid retrieval and multistage ranking | Retain keyword/dense retrieval followed by cross-encoder ranking and separate scientific judgement | Relevance is not evidence of truth; scientific decisions remain DeepSeek/MiMo's evidence-bound task |

Sources checked 2026-10-28:
[Google search stages](https://developers.google.com/search/docs/fundamentals/how-search-works),
[Microsoft agentic retrieval](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-overview),
[Brave extracted context](https://api-dashboard.search.brave.com/documentation/services/llm-context),
and [Perplexity's architecture overview](https://www.perplexity.ai/en-GB/hub/blog/architecting-and-evaluating-an-ai-first-search-api).
The Perplexity architecture summary was available in its publisher search
listing; the full article timed out, so no detailed internal claims are inferred.

Paper-ready scheduling, warm model reuse, source deadlines and field-scoped Pro
correction are project-specific engineering decisions. Public documentation
motivates the general patterns, not their particular settings or a guaranteed
speedup. Extra autonomous search loops and aggressive evidence compression were
not added without a quality/cost benchmark.

## Verification and comparison

Run the offline benchmark without paid inference:

```powershell
.venv/Scripts/python.exe -m scripts.research.validation_speed --output reports/validation_speed_comparison --requests "ontology/<paper>/llm_requests.jsonl"
```

It checks identical fixed proof outputs for barrier/ready schedules, exercises
persistent source reuse across new index instances, and replays saved successful
primary-call durations for three/four workers. Replay assumes papers are ready
together and excludes retrieval, recovery and provider queuing; it does not
predict total run time. Defaults stay at three workers until a live comparison
establishes a benefit. A full-document independent quality/cost benchmark has not
been performed by this change.

Regression tests cover actual orchestration overlap, progressive checkpoints,
expanded-input invalidation, partial-source resume, cancellable HTTP metadata,
model and vector-version reuse, bounded proof correction, unchanged decisions
and graph isolation for incomplete retrieval.
