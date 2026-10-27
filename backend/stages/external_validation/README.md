# External evidence validation

The default is DeepSeek Flash per-paper judgements followed by MiMo Pro cross-paper
synthesis, sharing one external Thinking toggle. `EXTERNAL_JUDGEMENT_MODE=joint`
retains the previous single-call method for comparison. See the
[current contract](../../../docs/protocols/per_paper_synthesis.md).
For a complete PDF run, use the web application. The CLI reads prepared
candidates and writes scratch JSONL under `reports/external_validation/`.

```text
backend/stages/external_validation/
  workflow.py        bounded claim assessment, checkpoints and evidence aggregation
  joint.py           one multi-paper Pro call and contextual role grounding
  synthesis.py       default per-paper calls, resumable findings and synthesis
  paper_proofs.py    exact sentence/context provenance without lexical synonym gates
  proofs.py          per-paper quote/entity/relation/direction/qualifier invariants
  prompt_reference.md human-readable reference; runtime schema lives in joint.py
```

Literature search/fetching lives in `../literature_retrieval/`; queries live in
`../query_planning/`; ranking lives in `../evidence_ranking/`. LLM/NLI clients are
in `backend/providers/`, while settings and shared storage/cache live in their
own packages. The standalone entry point is `python -m backend.cli.external_validation`.


## Validation flow

1. Resolve ALL bibliography entries attached to the exact source evidence. Use embedded DOIs directly; otherwise accept Crossref matches only with compatible title, first author and year. Look up the exact identifier in Europe PMC/OpenAlex. Unresolved references stay visible.
2. Batch query planning on the query model (default Go MiMo-V2.6-Flash), producing Europe PMC Boolean and concise OpenAlex plain-text queries. Search indexes concurrently. A stable claim-based sentence feeds ranking; no model-generated HyDE claim is used.
3. Deduplicate publications and rank metadata using BM25 plus BGE-base reciprocal-rank fusion. Missing abstracts no longer exclude potentially accessible PDF-only works from discovery; reserve a small search-selection allowance for them.
4. Rerank the leading candidates jointly with `ncbi/MedCPT-Cross-Encoder` before transient full-text retrieval.
5. Prefer Europe PMC XML, fall back to an explicit OA PDF, then an indexed abstract. Remove references and other back matter where possible. Keep page numbers for PDF passages.
6. Fetch top-k search papers (default 8) plus all usable attached citations. Rank sentence-aligned passages with the full claim/qualifiers AND a neutral exposure/outcome query, using BGE and MedCPT. Retain 4 chunks per paper by default. Parsed material is cached transiently within a bounded 32 MiB run cache.
7. Judge each paper separately with DeepSeek Flash. Save each completed finding, then use MiMo Pro to synthesize all findings plus exact binding excerpts. Both phases honor external Thinking, including Off. The optional joint comparison mode uses one multi-paper Pro call.
8. Require actual paper/chunk/sentence IDs and quotations. The model judges entity, relation, direction, certainty and qualifier agreement using explicitly selected paper-local context. NLI is diagnostic in the new flow and cannot promote or overturn a finding.
9. Keep source-reference evidence distinct from search-discovered evidence. Track supporting document groups separately from original-result groups; several reviews/background summaries do not establish new experimental replication. Scope/type review preserves paper findings but excludes the unresolved claim from validated assembly.

Paper endpoint alignments may quote an explicitly selected definition or
antecedent when the finding uses an abbreviation or contextual reference. The
finding's own endpoint mentions and relation trigger must still occur literally
in its selected sentences. The model establishes identity; code verifies only
the supplied paper-local text and identifiers. `context_selections` is required
for supported/contradicted decisions (use `[]` for self-contained findings).
Insufficient/review responses may omit this unused field; omission admits no
extra context, does not invent an empty model field and never promotes support.
Any supplied context selections still undergo provenance checks.

The originating PDF is excluded from external corroboration. Whole external articles are processed in memory and discarded; only selected passages, page metadata, bibliographic metadata, access/license information, and decisions persist.

Query planning receives the source-local heading and bounded neighboring
sentences to resolve references before searching. An extracted `claim_text`
is checked for faithfulness during source validation; it never counts as
external proof. Milling/processing recovery and food-quality mechanisms are
in scope, while aggregate farm productivity alone remains excluded.

OpenCode request logs distinguish final-output TPS from reported
output-plus-reasoning TPS, both over complete request time. These are not
engine decode benchmarks. FastAPI runs save per-call counters and requested
effort in `llm_requests.jsonl` for the frontend diagnostics table. The default Go
routes honor their explicit Thinking settings. Separate calls to the same model
do not constitute independent model opinions.

V26 separates sampling/farming occurrence provenance from biological conditions
through an evidence-grounded role classification in the existing source check.
It preserves original values in audit metadata; it does not erase all qualifiers.
The editable extraction template is the actual runtime prompt. Extraction has
its own 420-second timeout and 12,288-token requested output headroom. Packet
coverage is recorded explicitly; incomplete extraction is never reported as
complete paper coverage. v31 removes model failover and recovery waiting.

## Models and error behavior

The external flow uses these **Go** model assignments:

| Task | Primary | Primary override |
| --- | --- | --- |
| Extraction | `opencode-go/deepseek-v4.1-flash` | `EXTRACTION_MODEL` |
| Source fidelity | `opencode-go/deepseek-v4.1-flash` | `OPEN_CODE_SOURCE_MODEL` |
| Query planning | `opencode-go/mimo-v2.6-flash` | `OPEN_CODE_QUERY_MODEL` |
| Per-paper judgement | `opencode-go/deepseek-v4.1-flash` | `OPEN_CODE_EXTERNAL_PAPER_MODEL` |
| Cross-paper synthesis | `opencode-go/mimo-v2.6-pro` | `OPEN_CODE_EXTERNAL_MODEL` |

Each task uses only its configured primary model. Standalone scope/semantic
model calls and priority-paper allocation are removed from active runs. The
former priority model/count selectors do not allocate calls. A Pro failure
stops the run; Flash never substitutes. Go allowance is shared.

Unresolved evidence selections in complete, schema-valid JSON are reviewable
findings, not provider failures. One same-model correction is allowed; afterward
invalid paper proof is withheld as `needs_review`, or classification remains
unresolved, and assessment continues. Activity logs show `evidence_needs_review`
warnings and retain the unchanged model proposal. Transport failures, truncated
answers and unusable JSON/schema still stop after bounded recovery.

`EXTERNAL_TOP_K=8` sets the default search-paper budget (1..30).
`EXTERNAL_TOP_CHUNKS=4` sets chunks per paper (1..12).
`EXTERNAL_INPUT_MAX_CHARACTERS=240000` bounds each serialized request input; it is
an application limit, not a verified provider context-window size. Oversized
packs fail visibly before requesting the model; papers are not silently dropped.

Rate limits, timeouts, missing models, region/access errors and unrecoverable
response errors stop the run. The frontend activity log shows the error with the
stage and model. Failed requests retain their status/error in `llm_requests.jsonl`.
The job is marked failed; a service error never becomes a scientific rejection.
Already in-flight requests in other pipeline stages may finish before the worker exits.

External requests default to 1200 seconds (`EXTERNAL_TIMEOUT_SECONDS`); other requests default to 300 seconds. The selected packet and completed paper judgements are checkpointed. Retry reuses compatible papers and reruns missing judgements plus synthesis. Source corrections remain bounded and must pass source and external checks. Verified contradictions can enable repair; insufficient evidence alone does not. Completed source-entailed claims also expose external reassessment without changing their original PDF extraction.

The default transport is direct OpenCode inference. Native JSON/schema format
requests and local schema validation precede evidence checks. Parsing errors
receive one same-model edit proposal, without local correction. Missing required
response content instead triggers regeneration from the original evidence.
Only one extra call is permitted, followed by the full evidence guards. See the
[bounded recovery contract](../../providers/README.md). There are no fallback
pools, cooldown waits or automatic provider-error retries. **Resume failed run**
preserves saved extraction and compatible completed checks and paper packets.
The former `OPEN_CODE_FALLBACK_MODELS`, `OPEN_CODE_<TASK>_FALLBACK_MODELS`,
`OPEN_CODE_MODEL_COOLDOWN_SECONDS` and `OPEN_CODE_WAIT_FOR_MODELS` variables have
no effect. Readiness requires the assigned primary for each task. Startup logs
show one model per task, without arrows. Restart a running backend to load this
policy when reload is disabled.

Retired helper/validation aliases, scope/semantic/priority model settings and
agent CLI server configuration have been removed. Use the four explicit task
selectors above. Routes use native provider settings. Scientific quote,
direction, certainty and qualifier guards
continue to apply after a successful response.

The UI health/settings panels and summaries expose `model_policy: primary_only`,
`task_models`, readiness and per-task counters.
Paper ledger rows retain their actual judge and evidence origin. Summary `llm_calls.total_requests`
counts requests once across four clients; helper/validation aliases must not be
summed again. `VALIDATION_BATCH_SIZE=3` controls source batches,
`QUERY_BATCH_SIZE=6` controls query batches, and `RETRIEVAL_WORKERS=4` controls
fetching. NLI promotion remains opt-in (`ENABLE_NLI_PROMOTION=0`).

The embedding and cross-encoder loader tries `sentence-transformers` first and uses direct PyTorch/Transformers loading if that stack is unavailable. NLI is an additional conservative conflict check, not the final judge.

## CLI

```bash
python -m backend.cli.external_validation health-check
python -m backend.cli.external_validation build-candidates --tuple-file raw_triplets.txt
python -m backend.cli.external_validation retrieve --limit 3
python -m backend.cli.external_validation validate --limit 3
```

The standalone CLI assumes its candidates are already faithful to their source. In the web pipeline, `backend/stages/source_validation/checker.py` performs source entailment before this package runs.

Current v34 additions: [chapter parsing, citation tracking, progress and quoted type evidence](../README.md).
