# Literature retrieval


Search/citation lookup and paper fetching use bounded `RETRIEVAL_WORKERS` (default 4).
Fetched papers are chunked on those workers and handed to one ranking owner as they become ready.
GPU/model ranking is serialized; final documents return to selection order regardless of download order.
Claim-independent chunks and parsed papers share the bounded run cache. Evidence text and citation budgets do not change.
Per-paper fetch/chunk/rank timings and claim wall timings are saved with retained evidence.

Full document runs use `EXTERNAL_PREFETCH=1` to prepare at most one upcoming claim while the current cloud judge waits.
Set it to 0 to disable lookahead. Only the retrieval worker uses the reranker; no extra judge calls are introduced.
Failures in prefetched work are raised for that claim, and prepared packets are bound to the claim/configuration signature.
On resume, compatible retained evidence bypasses retrieval. Completed external judgements remain cached.
