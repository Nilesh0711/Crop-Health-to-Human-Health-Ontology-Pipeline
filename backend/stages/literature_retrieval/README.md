# Literature retrieval


Search/citation lookup and paper fetching use bounded `RETRIEVAL_WORKERS` (default 4).
Fetched papers are chunked on those workers and handed to one ranking owner as they become ready.
GPU/model ranking is serialized; final documents return to selection order regardless of download order.
Claim-independent chunks and parsed papers share the bounded run cache and the
optional persistent index. Evidence text and citation budgets do not change.
Per-paper fetch/chunk/rank timings and claim wall timings are saved with retained evidence.

Full document runs use `EXTERNAL_PREFETCH=1` to prepare at most one upcoming claim while the current cloud judge waits.
Set it to 0 to disable lookahead. Only the retrieval worker uses the reranker.
Trust mode also starts paper judgement as each document becomes ready. A later
expansion of that paper's shared input requires reassessment rather than reusing
an incomplete subset; this can add calls. Exact matching inputs are reused.
Failures in prefetched work are raised for that claim, and prepared packets are bound to the claim/configuration signature.
On resume, compatible retained evidence bypasses retrieval. Completed external judgements remain cached.

Search sources use a deadline in trust mode; unfinished sources are saved as
pending rather than empty successful searches. Such claims stay out of the final
graph until retrieval completes. See [the speed protocol](../../../docs/protocols/validation_speed.md)
for index versioning, model reuse, optional raw discovery and configuration.
