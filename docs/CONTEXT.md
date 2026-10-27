# Project context: soil-to-human-health research workbench

Last updated: 2026-10-27. Pipeline contract: `external-validation-v36`.

## Scientific boundary

Extract direct soil/crop/contaminant/nutrient/exposure/health pathway edges with
exact PDF provenance. Source fidelity, retrieved literature support and scientific
truth are separate. Unlocated evidence means evidence not found, not false.
Source-cited articles are tracked separately from independent corroboration.
Never infer a direct A-to-C finding merely from separate A-to-B and B-to-C edges.
Accepted graph edges remain provisional research findings.

## Active model routes

| Task | Assigned primary |
| --- | --- |
| Claim extraction | `opencode-go/deepseek-v4.1-flash` |
| PDF source fidelity | `opencode-go/deepseek-v4.1-flash` |
| Literature query planning | `opencode-go/mimo-v2.6-flash` |
| Per-paper external judgement | `opencode-go/deepseek-v4.1-flash` |
| Cross-paper synthesis | `opencode-go/mimo-v2.6-pro` |
| Global Pathway Assembly | `opencode-go/mimo-v2.6-flash` |

Only direct OpenCode HTTP inference remains. There are no model fallback pools,
agent CLI transports, local generative clients, standalone scope/semantic gates
or separate priority-paper judges. BGE embeddings, MedCPT reranking and optional
DeBERTa NLI remain local specialist models. NLI promotion is disabled by default.
The stage selectors may be explicitly configured among approved Go model IDs.

The backend reads its API key from `OPENCODE_API_KEY` or the current Windows
user's Credential Manager entry `crop-to-human-health/OpenCodeInference`.
Configure the vault using `python -m backend.cli.configure_api_key`.
Never expose credentials to the frontend, source files or request logs.

MiMo requests native schema formatting; DeepSeek requests JSON mode because
configuration probes rejected its schema format. Application schema validation
and scientific proof checks always run. See [model requests and recovery](../backend/providers/README.md).

## Current flow

1. Parse PDF pages, chapter boundaries, headings, sentences and chapter-scoped
   bibliographies. Record page quality, missing text and unresolved citation links.
   Heading recognition uses numbering, typography and isolated title/subtitle
   syntax, without disease-name or geographic-ending lists. Fragmented bold
   titles can retain quoted/hyphenated wording across punctuation font changes.
   Deterministic text-quality checks stop unreadable inputs; they do not judge
   topic relevance or scientific correctness.
2. Default to target/context direct extraction: deterministically assign every
   distinct parsed sentence to a region of about 800 words, retaining the original
   packet as reading context. Extract all eligible atomic claims by default and
   reject direct evidence outside its sentence IDs. Run settings retain the
   previous whole-packet mode. Validate JSON structure, provisional label names,
   sentence IDs and exact source anchors. Up to three non-adjacent evidence
   sentences may be selected within a target; gaps and all intervening text
   remain explicit for source review. Relations are open; no type-pair rule
   decides eligibility. Semantic heuristics record review notes only, without
   rewriting entities, restrictions or modality. Processing all targets does not
   imply exhaustive claim recall. Candidate IDs include retained source context
   so otherwise identical geographic/temporal variants cannot share an ID.
   Runtime examples use abstract entities. There are no case-derived endpoint
   or editorial phrase lists; real claim examples remain in tests/audit data.
3. Check source fidelity in independent small batches. Preserve relation,
   direction, certainty, entity identity and essential qualifiers. The source
   model owns fidelity decisions; lexical hints cannot overturn its judgement.
   Same-section/chapter numbered PDF context supplies explicit antecedents and
   definitions; selected fragments must still state the effect. Context-bound
   canonical names need not appear verbatim in a single definition quotation.
   Redundant name-expansion annotations never become fictitious edits.
   A provider
   failure stops visibly; it cannot become a scientific rejection.
4. Generate complementary search queries. Discover literature through Europe
   PMC/OpenAlex and resolve attached bibliography references through DOI lookup
   or conservative Crossref matching. Accessible PDF-only records remain eligible.
5. Rank metadata to allocate retrieval effort, fetch accessible research text,
   chunk passages, then rerank using the full claim/qualifiers and a neutral
   exposure/outcome query. Metadata scores and retrieval hints are not proof.
6. Select up to eight search-discovered papers by default (configurable 1–30),
   plus all usable resolved attached citations. Retain four chunks per paper by
   default. Preserve citation lifecycle and paper contribution ledgers.
7. Judge each selected paper separately with DeepSeek Flash, then
   synthesize the saved findings with MiMo Pro. Both phases inherit the
   external Thinking toggle, including Off. Exact finding sentences and separate
   paper-local context establish definitions/conditions without literal canonical
   name matching. An alignment may use the expanded name from an explicitly
   selected definition while its finding uses the abbreviation; the finding's
   own endpoint mentions and trigger still require literal proof. Decisive paper
   decisions must return `context_selections` (possibly `[]`); nondecisive ones
   may omit unused context without admitting additional evidence or inventing a
   model field. Each completed paper is checkpointed. The synthesis compares
   exact excerpts plus findings, cannot promote invalid paper proof, and accounts
   for every paper. `EXTERNAL_JUDGEMENT_MODE=joint` retains the former single-call
   method for controlled comparison. The default is `per_paper_synthesis`.
   Assess support/conflict, contextual types and pathway scope together. Derive
   exact quotes from selected sentence IDs and validate each paper's row locally.
   One paper cannot supply another paper's missing endpoint or condition; synthesis
   cannot override invalid quotations. Oversized packets fail visibly.
   Evidence sentence IDs are exact lists: `[4,6,8]` selects those three sentences,
   not the entire interval. Separated fragments retain explicit gaps; intervening
   sentences accompany synthesis as labeled context, not automatic proof.
   Complete, schema-valid responses with unresolved proof selections receive
   one bounded correction attempt, then remain visible as paper/type review
   without stopping other assessments. Invalid proof is never admitted as support.
   Provider failures, truncated answers and unusable structured output still stop.
8. Run global Pathway Assembly with MiMo Flash: align endpoint occurrences using
   source context and combine the original edges as A-to-B-to-C, without causal
   chain validation or a new A-to-C edge. Assembly admits only source-entailed,
   explicitly in-scope claims with a completed independent-supported or
   corroborated external result and accepted independent paper support.
   Pending, conflicted and source-review claims remain in document audit/source
   views. Reuse unchanged occurrence mappings and
   scan all registry shards for new/changed occurrences. Keep evidence statuses.
9. Save findings, graphs, request diagnostics, PDF quality and review reasons.
   The UI supports source-grounded claim repair and unfinished external retry.
   Verified external contradiction can enable repair; insufficient evidence alone
   does not. Repaired assertions must pass source and joint external checks again.

The v36 extraction policy needs a fresh document run: old completed checkpoints
cannot recover claims omitted by the former relation/type gates. Historical
findings and response artifacts are preserved. See
[open relations and semantic review](protocols/open_relations_and_semantic_review.md)
for the stripe-rust audit and evidence sources.

External requests default to 1,200 seconds. Extraction defaults to 420 seconds;
other requests use 300 seconds. The joint packet application limit is 240,000
characters, not a verified provider context limit. Fetching remains concurrent;
external judgement uses smaller per-paper calls followed by a compact synthesis
packet of exact findings and binding context. No paper is omitted from the ledger.

Extraction's initial generation allowance defaults to 24,576 tokens shared by
reasoning and answer, with the existing bounded regeneration cap of 32,768.
Run settings expose the mode, target size and generation allowance. Checkpoints
retain the original extraction policy; historical runs without a mode field are
whole-packet runs. Saved reading packets support single-claim repair. See the
[extraction mode contract](../backend/stages/claim_extraction/README.md).

## Recovery and saved work

Parse exactly the supplied JSON; do not correct it locally. For syntax errors,
the same model may propose one bounded set of before/after edits. Validate that
the proposal preserves scientific values, quotations, identifiers and item binding.
For empty/truncated/schema-invalid answers or missing required response content,
the same model may instead regenerate once from the original input and evidence.
Content after a prematurely closed root object or multiple root values also
regenerates: separator edits cannot safely establish which object owns a field.
An invalid model patch reports broken syntax before testing value preservation.
There is at most one extra call in total, without recovery chaining or retries
seeking a favorable verdict. Provider errors and refusals stop without substitution.

Final model answers, edit proposals and accepted objects are archived in each
run's `model_responses/`; hidden reasoning and keys are excluded. Request metrics
record actual model, purpose, attempt, usage, elapsed time and artifact path.
Token speed divides reported tokens by full request time, not engine decode time.

**Resume failed run** retains saved findings and reuses only compatible extraction,
completed checks and retained evidence packets. Compatibility checks include PDF
hash, models, extraction settings, pipeline version and input signatures. Imported
historical checkpoints require matching provenance and coverage. Saved runs are
never automatically rejudged or rewritten by a configuration change.

Job status/options/progress/logs are persisted under each run's `jobs/` folder;
`run_state.json` records the latest job ID. The single-process backend restores
terminal jobs after restart and marks missing workers as interrupted. It never
restarts a scientific request from status polling. The frontend stops polling
unknown IDs and reloads saved findings. The launcher disables automatic reload
by default; `BACKEND_RELOAD=1` is an explicit development option that interrupts
active work when code changes.

Older saved errors can contain a stale parser model name. The API corrects this
wording only for display, labels it historical and shows the recorded model when
available. Original activity/request files remain unchanged.

## Ownership, launch and verification

See [backend architecture](../backend/README.md), [stage ownership](../backend/stages/README.md),
[configuration](../backend/config/README.md), and [external protocol](../backend/stages/external_validation/README.md).
`backend/app.py` forwards the FastAPI app in `backend/api/app.py`. Pipelines
coordinate stage packages; stages/domain/providers/shared/config cannot import
API, pipelines or CLI adapters. Package initializers do not load models.

Run `bash scripts/run_backend.sh` and `bash scripts/run_frontend.sh` in separate
Git Bash terminals. A built frontend can also be served by FastAPI. PDFs remain
under `pdf/`, saved evidence under `ontology/<PDF stem>/`, and developer outputs
under `reports/`. Resource paths resolve independently of the current directory.
Deleted archive/scratch directories are not required or recreated.

Run the offline Python suite with `python -m pytest`; provider tests block live
HTTP and use a dummy key. Run `npm test` and `npm run build` in `frontend/`.
Architecture tests guard ownership, removed execution paths and import cycles.
Mocked checks do not establish scientific precision/recall or live provider reliability.

The [26 October source-fidelity audit](../reports/source_fidelity_audit_2026-10-26/README.md)
records all 89 Crop Health claims. Source-only reassessment resolved 29 review
holds, yielding 84 entailed, 4 needing review and 1 unsupported, with original
assertions, infection restrictions and external results preserved. The stopped
checkpoint still needs external work; these are not 84 independently corroborated
findings. Original files and provider responses are retained beside the report.
