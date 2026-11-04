# Model requests and recovery

The transport is direct OpenCode inference. Stage selectors
use DeepSeek Flash for extraction/source/per-paper external judgement, MiMo Flash
for queries and pathway assembly, and Muse Spark 1.3 Contributor for cross-paper synthesis. `opencode-go/<model>` selects
`https://opencode.ai/inference/go/openai/v1/chat/completions`. The request body uses the bare model
ID. Authorization stays in the backend, using an environment key or the current
Windows user's Credential Manager. There is no automatic transport fallback.

The implementation follows [OpenCode inference](https://opencode.ai/v2/docs/console/api/inference/)
and [Go endpoints](https://opencode.ai/v2/docs/console/go/#endpoints).
Small authenticated configuration probes found that MiMo Flash/Pro and Muse accept
`json_schema`, while DeepSeek rejects it and accepts `json_object`. Native
format requests supplement local validation; gateway acceptance alone does not
prove every schema keyword is enforced upstream.

## Bounded recovery

External neighboring-pair requests reserve at least **28,192 generation tokens** on the
first attempt (`EXTERNAL_MIN_OUTPUT_BUDGET`). This is shared headroom for the
answer and reasoning, while per-request headroom can raise the requested limit. The concise final-answer
target is separate from this generation budget. Larger original request allowances
are preserved by recovery. This application setting is not a
claim about the provider's maximum limit and does not guarantee completion.

1. Make the scientific request with its selected model and original evidence.
2. Parse the supplied JSON strictly. No local correction is attempted, including
   removal of Markdown/prose or automatic movement of an annotation brace.
3. If response content is missing (empty/token-truncated answer, unfinished value,
   schema violation, duplicate JSON properties, or missing required proof/item coverage), regenerate ONCE
   with the same model, original input, evidence selection and schema. The old
   partial answer is not reconstructed or used as new scientific evidence.
   A missing closing brace after complete values is a syntax issue instead.
   Repeated `candidate_id` fields within one result are duplicate properties,
   even if both values match. Never silently keep the first/last occurrence or
   ask the separator-only patcher to delete a field. The same key appearing once
   in each separate batch result is valid. Conflicting duplicates also regenerate.
   Data after a prematurely closed root object, including a second root value,
   also uses this regeneration path: field ownership cannot be established by
   separator-only edits. The new call uses the original evidence and schema,
   not the malformed response. Structure errors do not increase the generation
   budget merely to correct nesting. Extra terminal closing delimiters alone
   remain eligible for the formatting path.
4. For syntax errors, ask the same model ONCE to propose up to eight unique
   before/after edits. Code applies only the model's proposed text. It validates
   preservation of keys, scientific values, quotations and item binding. Permitted
   punctuation edits include separators, outer fences, terminal closing delimiters
   and a model-proposed adjacent brace/quote swap in an explanation/rationale.
   Ambiguous restructuring, rewritten evidence and invented fields are rejected.
   No-op patches, missing/non-unique targets and repeated edit targets have
   distinct error messages; none is applied as an automatic local correction.
   The proposed result must parse as complete JSON before value preservation is
   checked. A patch that still has broken syntax reports that specific problem
   instead of claiming that scientific evidence changed.
5. Validate the final schema and response completeness, then run the original
   scientific proof guards. A regenerated answer can still be unsupported,
   insufficient or need review. Complete negative findings never trigger retry.
6. Allow at most ONE extra call in total: edit OR regenerate. A failed recovery
   cannot start the other branch. Surface the error and retain checkpoints.

`JSON_REPAIR_ATTEMPTS` and `JSON_REGENERATION_ATTEMPTS` are independently `0` or
`1`. Regeneration requests extra output headroom up to
`REGENERATION_MAX_OUTPUT_TOKENS=32768`; an original request's higher budget
is preserved. This is an application budget, not a verified provider limit.
HTTP/access/rate-limit failures and refusals are not content-recovery events.

An incomplete/malformed initial attempt scheduled for bounded recovery logs a
**warning** stating that regeneration or formatting repair is pending. Raw attempt
status, error and token usage remain recorded. Successful recovery is identified
explicitly. Exhausted/disabled JSON/schema recovery and provider failures log
errors and stop the run. A schema-valid but ungrounded external proof proposal
instead holds the affected pair as `assessment_incomplete`, preserving the
proposal separately and allowing other pairs to continue. It cannot enter the
supported graph. Source/assembly failures remain visible through their owning
stage. Document runs and claim actions use the same request severity policy. Progress
also shows the generation token limit separately from provider input usage.
Neither model substitution nor repeated recovery is allowed. The retired agent
CLI adapter, local generative client and model-profile configuration are removed.
Standalone application commands under `backend.cli` use this same HTTP adapter.

Original final answers, repair patches and accepted objects are stored in
`ontology/<paper>/model_responses/` (standalone CLI: its output directory).
Request logs include purpose, attempt, model, format mode, duration, provider
token usage and artifact path. They never include credentials or reasoning
text. API output counts separate reported reasoning from completion totals.

Request activity and the frontend show provider-reported input tokens for the
full request. Counts remain pending until the provider responds, and unavailable
if it returns no input usage. Input usage is retained even when output usage is
missing. Character lengths are not converted into guessed token counts; older
character-only activity events display token usage as unavailable.

The frontend distinguishes edit proposals from regeneration and shows warning/error events. **Resume failed
run** preserves saved findings and reuses only work with matching provenance
and signatures. It does not retry models until a desired verdict appears.

Saved parser errors from older runs may name an unrelated model. The API labels
this historical wording for display and shows the recorded request model when
available. Original log records and saved findings remain unchanged.

## What current responses describe

Each source result assesses one fixed atomic claim, with three claims batched
per request by default. A paper request assesses two neighboring claims and their
local connection against one retrieved paper. Pro synthesis reconciles the
paper-bound findings for that same pair. It does not assess an entire long chain.
Request-bound identities are attached from input instead of asking models to
repeat them. See the [source contract](../stages/source_validation/README.md)
and [neighboring-pair contract](../stages/neighbor_validation/README.md).

| Information | Purpose |
| --- | --- |
| Source fidelity and project scope | Separate whether the PDF states the claim from whether it fits the project |
| Alignment and qualifiers | Bind a source mention to its endpoint while preserving dose, species, material and other restrictions |
| Edge and connection references | Identify the two original edges and their shared-entity junction |
| Paper-local proof references | Select exact chunk/sentence IDs, from which code reconstructs literal quotations |
| `claim_match` and `conditions_match` | Distinguish the full original proposition from partial/contextual evidence and assess applicability |
| Local-pair synthesis | Reconcile grounded papers without transferring quotations or assuming causal transitivity |
| Correction proposals | Preserve original wording alongside a proof-bound proposed revision for researcher review |
| Scientific and execution status | Separate support/uncertainty from pending retrieval, invalid proof or technical failure |

Abstract example: Paper A's sentence 4 concerns `Factor A -> Process B`.
A model can paraphrase that finding, but its proof must still identify Paper A,
the actual chunk and sentence 4. It cannot assign the quotation to Paper B.
These deterministic checks protect source traceability. Scientific support and
connection compatibility remain model judgements from supplied text.

## Historical NLI diagnostics

No NLI model is loaded or called by the current pipeline. The retired DeBERTa
implementation lives under `archive/legacy_pipeline/`; older diagnostic fields
remain readable in saved results. They do not grant current support. BGE and
MedCPT are still active local retrieval/ranking models, while DeepSeek and MiMo
make the scientific assessments.

Muse uses schema-format requests plus application schema/proof validation. After the user enabled workspace access, a small authenticated application-client probe succeeded. Muse sends reasoning_effort=minimal for the unchecked external switch and high when checked; the generic thinking parameter is unsupported on its translated Responses route. Logs show required minimal reasoning rather than claiming it is disabled. See [v50 access and compatibility](../../docs/protocols/muse_external_model.md).
