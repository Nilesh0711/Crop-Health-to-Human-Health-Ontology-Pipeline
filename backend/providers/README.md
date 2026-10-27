# Model requests and recovery

The transport is direct OpenCode inference. Stage selectors
use DeepSeek Flash for extraction/source/per-paper external judgement, MiMo Flash
for queries and pathway assembly, and MiMo Pro for cross-paper synthesis. `opencode-go/<model>` selects
`https://opencode.ai/inference/go/openai/v1/chat/completions`. The request body uses the bare model
ID. Authorization stays in the backend, using an environment key or the current
Windows user's Credential Manager. There is no automatic transport fallback.

The implementation follows [OpenCode inference](https://opencode.ai/v2/docs/console/api/inference/)
and [Go endpoints](https://opencode.ai/v2/docs/console/go/#endpoints).
Small authenticated configuration probes found that MiMo Flash/Pro accept
`json_schema`, while DeepSeek rejects it and accepts `json_object`. Native
format requests supplement local validation; gateway acceptance alone does not
prove every schema keyword is enforced upstream.

## Bounded recovery

Joint external requests reserve at least **28,192 generation tokens** on the
first attempt (`EXTERNAL_MIN_OUTPUT_BUDGET`). This is shared headroom for the
answer and reasoning, chosen from the allowance already accepted by a successful
eight-paper recovery. The concise final-answer target is unchanged. Larger
calculated request budgets are preserved. This application setting is not a
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
explicitly. Exhausted/disabled recovery and provider failures log errors and stop
the run. Document runs and claim actions use the same severity policy. Progress
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

## Why the response contains more than a triplet

These examples describe the data contract, not findings from a real paper.

| Field | Meaning and example |
| --- | --- |
| Claim identifier and decision | `candidate-17` identifies one extracted assertion. Source `entailed` means the PDF supports it; external `supported` refers to admitted external evidence. IDs prevent batch answers being assigned to another claim. |
| Entity alignments and qualifiers | An alignment links the canonical entity `cadmium` to a paper's mention `Cd`, with supplied text grounding that identity. Qualifiers retain restrictions such as the tested dose, crop or duration; a high-dose result cannot silently support an unrestricted claim. |
| Paper and chunk identifiers | `paper-2` and `paper-2:chunk-4` identify the exact external document and retained passage. A quotation from that paper cannot migrate to another paper's judgement. |
| Exact evidence quotations | The selected source wording is checked against the supplied passage. A model-written summary cannot replace a missing quotation. |
| Explanations and review reasons | An explanation records why evidence applies. Review reasons expose missing endpoints, unmatched conditions or ungrounded identity. Explanations alone cannot override failed evidence checks. |
