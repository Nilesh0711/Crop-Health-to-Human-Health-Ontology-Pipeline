# Trust graph knowledge building

Web runs default to `knowledge_building_mode=trust_graph`, with up to three
claims per shared assessment (runtime range 1..6). Choose `validated_first` to
compare with the previous flow. Existing Thinking defaults remain Off and model
assignments remain MiMo Flash for identity assembly, DeepSeek Flash for paper
assessment, and MiMo Pro for cross-paper synthesis. There is no model failover.
The direct Python `run_pipeline` entry point preserves its previous mode unless
the caller explicitly selects the new one. Extraction checkpoint version v36 is
retained because the saved extraction contract is compatible.

## Flow and ownership

1. Extract independently measurable, in-scope edges with source context and
   actual restrictions. A metric belongs to the measured endpoint, not a nearby
   consequence. A yield-only number does not become a new eligible pathway edge.
2. Check PDF fidelity, then reassess each complete non-positive result once in
   this mode. `source_validation/reassessment.py` preserves both readings, permits
   confirmed rejection and does not rewrite the scientific assertion. Existing
   source-defined name corrections still require literal definitions. Provider
   format/schema recovery retains its separate one-extra-call limit.
3. Build `.provisional_pathways` over source-entailed edges across all documents.
   This does not require external support. Unassessed, conflicted and out-of-scope
   edges remain explicitly provisional. Source-review and unsupported edges stay
   in the claim audit with a zero unassessed evidence balance.
4. `grouping.py` forms bounded graph-neighbour groups for the active document.
   Standalone claims remain in the queue. Global identity alignment spans PDFs;
   external judgement groups currently operate within the document being run.
5. Reuse query translation, full-text and chunk caches; merge selected text from
   the same paper across group members without changing paper/chunk identities.
   Citation route and evidence-origin metadata remain claim-local. Conflicting
   source identities fail visibly. A group's paper union preserves every selected
   paper and usable citation; it does not reduce their configured budgets.
6. `judgement.py` sends the shared paper once with the member claims. A one-claim
   request uses the ordinary root decision/finding contract with no results or
   identity envelope; its identity is fixed by the request. A multi-claim wire
   response is an object keyed by candidate ID; each value contains only decision
   and finding. IDs are not repeated inside result values. Strict duplicate-key
   parsing still applies, and complete validated wire results are adapted to the
   existing internal row format. Each result independently passes `paper_proofs.py` provenance
   checks. A valid clause cannot supply an unrelated claim's missing endpoint.
7. Send one compact Pro synthesis for a group, with deduplicated exact excerpts,
   per-claim findings and explicit source-only context. Each claim receives its
   own type/scope assessment and paper dispositions. Existing guards recheck the
   response; synthesis cannot invent, promote or reattribute invalid proof.
8. Pro also assesses actual adjacent connections within that group as compatible,
   incompatible or unresolved, citing its input papers. Unassessed cross-group
   and cross-document connections remain unassessed. Compatibility does not prove
   causation or create a collapsed A-to-C edge.
9. `ledger.py` recomputes each claim's evidence balance from current admitted
   findings. `projection.py` reuses provisional identity alignment to publish the
   validated-only subset without another assembly model request.

`backend/pipelines/trust.py` owns retrieval orchestration and document artifacts;
this stage owns grouped evidence assessment, scoring and graph projection.
Stages do not import API or pipeline entry points.

## Scheduling and latency

Independent paper requests run with at most `EXTERNAL_PAPER_WORKERS=3` concurrent
requests (allowed 1..4; use 1 for serial comparison). This is separate from the
runtime claims-per-assessment setting. Pro synthesis waits for every selected
paper in its group. No paper budget, finding or evidence guard is removed.
The large provider generation allowance is separate from a smaller answer-length
hint. Prompts explicitly treat the hint as a brevity guide, never a target to
fill by repeating results or a reason to omit scientific evidence.
`scheduling.py` stops submitting after a failure, drains already submitted
successes to the single checkpoint owner, then raises the failure. Paper order
is restored before synthesis so completion order cannot change attribution.

With `EXTERNAL_PREFETCH=1`, one retrieval worker prepares the next claim group
while the current group is judged in the cloud. It remains the sole retrieval
GPU/ranker owner; only one future group is kept ahead. Selected packets persist
under `trust_prepared/<candidate_id>.json`, with retrieval and scientific input
signatures, including prefetched packets not yet judged. Resume reuses compatible
packets. A local cancellation scope stops lookahead when judgement fails or the
user stops; it inherits user cancellation without cancelling the parent job.

Identical original-source context is stored once in the Pro input's
`source_context_pool`, with explicit per-claim references. This is lossless text
deduplication, not a summary or a new external source. Shared run caches coalesce
simultaneous requests for the same key. Parsed XML/PDF material is keyed by exact
access identifier/URL so different indexed abstracts do not trigger another copy
of the same full-text fetch. The shared persistent index and its limits are
documented in [the speed protocol](../../../docs/protocols/validation_speed.md).
`PAPER_READY_JUDGEMENT=1` also starts paper calls within an unfinished group.
If later group members expand a paper's input, its earlier subset result is
reassessed; reuse requires an exact compatible input signature.

Group preparation, retrieval wait, paper judgement and synthesis wall timings
are recorded in `trust_summary.json`, including interrupted runs. Model elapsed
seconds summed across parallel requests are not the group's wall time. The
frontend keeps showing a waiting request when its sibling completes and reports
how many provider requests are active. Actual speed/cost/accuracy improvements
still require a document benchmark; concurrency can encounter provider limits.

## Meaning of the numbers

Initial balance is zero with `not_assessed`, not false or 50% probability.
For distinct original-result study groups, score = supports minus contradictions.
Background/review summaries are retained as context, not new original-study votes.
An explicit study group is used when known; publication identity is a conservative
fallback. Independence is not guaranteed just because two papers have different
DOIs. Model-assessed evidence origin remains inspectable and fallible.

Same-study duplicates and repeated chunks, paths, queries or retries do not add
votes. A study group with opposing admitted findings is internally disputed and
contributes no directional vote. A positive net balance with a valid contradiction
still has `contested` status. Missing evidence and technical failures do not add
contradictions. In-flight/failed assessments can retain provisional findings with
`assessment_complete=false`; they do not enter the validated graph.

Graph contribution is the count of adjacent directed two-edge connections, plus
the number of distinct adjacent claim edges. It is independent of path pagination,
separate from the evidence balance, and never raises that balance. A numerical
threshold does not decide validated graph admission: PDF fidelity, completed
scope/type/evidence assessment and existing independent-support requirements do.

## Correction, recovery and persistence

- `trust_assessments/<signature>.json`: accepted or reviewable paper assessments;
  synthesis records use the `synthesis-` prefix. Input signatures cover claim,
  source context/revision, evidence, policy, assigned model and Thinking.
- `validation_runs.jsonl`: existing per-claim evidence/status contract, augmented
  with `trust`; checkpoints are saved after every completed paper batch.
- `trust_scores.jsonl`: all current extracted claims, including those blocked
  before external assessment, with honest initial/review states.
- `trust_connections.jsonl`: grouped compatibility assessments with claim and
  evidence fingerprints. Changed claims or findings invalidate old connections.
- `trust_summary.json`: group/cache counts, primary calls, observed provider
  attempts, and a hypothetical unbatched call count for the assessed packets.
  Recovery attempts are included only in observed provider attempts.
- `ontology/.trust_graph/snapshot.json`: annotated provisional graph.
- `ontology/.pathways/snapshot.json`: compatible validated-only projection.

The existing claim repair action retains original candidate/validation history,
proposes one extraction correction, checks its PDF fidelity and assesses its
external evidence. Corrected assertions invalidate prior evidence inputs rather
than incrementing historical votes. Retry reuses matching paper assessments and
reruns only unfinished synthesis. A correction does not silently change unrelated
claims. Resume can select a different external-building mode while preserving
compatible source/extraction work; older runs default to their previous mode in
recovery metadata. Trust mode refetches legacy evidence packets under its table-
aware retrieval signature rather than assuming tables were previously retained.

## UI and limits

Run settings expose mode and group size. Claims show evidence balance and its
study ledger. Paths -> Trust network shows provisional claims, sortable evidence
balance and graph contribution, including standalone edges. Validated pathways
remain a separate view. GraphML exports retain score, state and original-study
counts. GET routes never start model requests or rewrite scientific results.

Tables from XML retain source cell order, headers, spans, units and footnotes in
explicitly labeled serializations; figure captions and PDF caption text are kept.
These are not continuous prose quotations. Complex table layout is not verified
automatically; image-only figure values are not read by a new vision model.
Parsed article text can persist in the bounded shared index (default seven-day
TTL); original downloaded PDF/XML bytes stay transient. Claim audit evidence
is independent of cache expiry.

Oversized packets fail before a provider request without silently dropping
claims/papers. Transport, truncation and unusable schema errors remain visible
after bounded same-model recovery. Grouping does not guarantee lower total token
cost or wall time. Public search/RAG documentation informs the scheduling and
reuse design; no commercial search-platform connector is included. Automatic
multiround literature search is not added; insufficient evidence remains unresolved.

The offline suite covers shared calls and real adapter payloads with mocked HTTP,
paper/claim attribution, duplicate studies, conflicts, cache invalidation, failed
paper/synthesis recovery, source reassessment, empty retrieval, table evidence,
graph isolation and the previous flow. No live full-document accuracy or cost
benchmark is implied by the mocked call-count reduction.
