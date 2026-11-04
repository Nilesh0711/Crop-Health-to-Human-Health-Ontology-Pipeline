# Backend configuration

Current application: v50. `paths.py` owns repository-relative resource/output
paths. `settings.py` defines the frozen `Settings` dataclass, approved primary model routes
and application budgets. Environment defaults are read at import time: set
variables before starting the backend and restart after changing them.
The application does not automatically load `.env` files.

## Models and Thinking

| Environment variable | Default | Work |
| --- | --- | --- |
| `EXTRACTION_MODEL` | `opencode-go/deepseek-v4.1-flash` | Direct claim extraction |
| `OPEN_CODE_SOURCE_MODEL` | `opencode-go/deepseek-v4.1-flash` | PDF fidelity and separate project-scope decisions |
| `OPEN_CODE_QUERY_MODEL` | `opencode-go/mimo-v2.6-flash` | Neighboring-claim query planning |
| `OPEN_CODE_PATHWAY_ASSEMBLY_MODEL` | `opencode-go/mimo-v2.6-flash` | Provisional global entity identity |
| `OPEN_CODE_EXTERNAL_PAPER_MODEL` | `opencode-go/deepseek-v4.1-flash` | Each paper against two neighboring claims |
| `OPEN_CODE_EXTERNAL_MODEL` | `opencode-go/muse-spark-1.3-contributor` | Cross-paper local-connection synthesis and quotation audit |

The five runtime Thinking fields are `extraction`, `source`, `query`, `external`
and `pathway_assembly`, all Off by default. The external field covers both paper
assessment, synthesis and quotation auditing. Explicit model overrides must be approved Go IDs; there
is no automatic fallback or alternate transport. The live pipeline is fixed to
`pathway_only` as a compatibility metadata label; its assessment target is `neighboring_claims`. Historical trust/claim mode selectors and their settings exist
only in [research modules](../../archive/legacy_pipeline/README.md).

## Selection and execution budgets

| Environment variable | Default | Meaning / configured range |
| --- | --- | --- |
| `EXTRACTION_MODE` | `target_context` | Also accepts `whole_packet` |
| `EXTRACTION_TARGET_WORDS` | 800 | Target size; 200-2,000 |
| `EXTRACTION_CLAIMS_PER_PACKET` | 0 | No numeric claim cap; optional 1-8 maximum per call |
| `EXTRACTION_MIN_OUTPUT_BUDGET` | 24,576 | Initial reasoning-plus-answer allowance |
| `VALIDATION_BATCH_SIZE` | 3 | Source claims per shared source call; 1-8 |
| `PATHWAY_ASSEMBLY_BATCH_SIZE` | 24 | Incoming endpoint occurrences per assembly batch; 1-64 |
| `PATHWAY_ASSEMBLY_INPUT_CHARACTERS` | 220,000 | Assembly application input budget |
| `NEIGHBOR_MAX_PAIRS` | 100 | Selected neighboring pairs involving the document; 1-500 |
| `NEIGHBOR_WORKERS` | 2 | Concurrent pair assessments; 1-2 |
| `SOURCE_WORKERS` | 2 | Independent source batches; 1-4 |
| `EXTERNAL_TOP_K` | 8 | Search papers per neighboring pair; 1-30, plus usable attached citations |
| `EXTERNAL_TOP_CHUNKS` | 4 | Retained chunks per paper; 1-12 |
| `PATHWAY_FOLLOWUP_PAPERS` | 3 | Extra search-paper budget for one unresolved-evidence round; 0-10 |
| `EXTERNAL_PAPER_WORKERS` | 3 | Concurrent paper requests; 1-4 |
| `RETRIEVAL_WORKERS` | 4 | Concurrent search/fetch preparation workers; 1-8 |
| `EXTERNAL_INPUT_MAX_CHARACTERS` | 240,000 | External application input budget; evidence is not silently dropped |
| `EXTERNAL_MIN_OUTPUT_BUDGET` | 28,192 | Initial external reasoning-plus-answer allowance |

Generation values are minimum allowances; request-specific headroom can raise
the actual requested output limit. Character and token budgets are application controls, not verified provider
context limits or guarantees of completion. Runtime API options expose extraction
mode/target/claim/generation limits and neighboring-pair count/paper limits. Not every
environment setting is an editable browser control. The API extraction generation
option accepts 8,192-32,768 tokens. See `backend/api/contracts.py` for request ranges.

Timeout defaults are `EXTRACTION_TIMEOUT_SECONDS=420`,
`EXTERNAL_TIMEOUT_SECONDS=1200` and `VALIDATION_TIMEOUT_SECONDS=300` for other
requests. `SEARCH_WAIT_SECONDS=20` and `METADATA_TIMEOUT_SECONDS=20` bound metadata
waiting; unfinished searches/citations remain pending and resumable.

## Local retrieval models and reuse

BGE (`BAAI/bge-base-en-v1.5`) supplies dense retrieval, MedCPT
(`ncbi/MedCPT-Cross-Encoder`) reranks, and exact query/text cross-encoder scores are cached using identifiable model weights.
NLI is removed from active execution and settings.

`EMBEDDING_BATCH_SIZE=16` and `CROSS_ENCODER_BATCH_SIZE=8` each allow 1-128.
`CROSS_ENCODER_CANDIDATES=20` controls the metadata shortlist. Bare Settings
defaults devices to CPU; the launcher selects CUDA when available unless an
explicit device was supplied. Compatible model instances are reused in-process.

The persistent index defaults to enabled, 512 MB and a 168-hour refresh interval:
`EVIDENCE_INDEX_ENABLED`, `EVIDENCE_INDEX_MAX_MB`, `EVIDENCE_INDEX_TTL_HOURS`.
Current neighboring-pair targets still determine passage selection. Cache reuse does not
supply scientific support or make previous verdicts compatible with changed input.

## Credentials and structured responses

Provide `OPENCODE_API_KEY` to the backend or use
`python -m backend.cli.configure_api_key` on Windows. The current user's Credential
Manager target is `crop-to-human-health/OpenCodeInference`. Keys are never stored
in Settings or sent to the browser. Local `.env` files stay ignored.

`OPENCODE_RESPONSE_FORMAT=model_default` requests native JSON mode for DeepSeek
and schema formatting for MiMo, reflecting the configured route probes.
Application schema and evidence checks always run.

`JSON_REPAIR_ATTEMPTS=1` permits same-model syntax-edit proposals;
`JSON_REGENERATION_ATTEMPTS=1` permits regeneration of incomplete/invalid content
from original evidence. Each accepts 0 or 1, with at most one extra call in total.
`REGENERATION_MAX_OUTPUT_TOKENS=32768` controls additional headroom; a higher
original budget is preserved. No local JSON correction, recovery chaining,
provider-error retry or model substitution occurs. See
[provider recovery](../providers/README.md).

Removed live controls include `QUERY_BATCH_SIZE`, `EXTERNAL_PREFETCH`,
`PAPER_READY_JUDGEMENT`, `ENABLE_NLI_PROMOTION` and the shared trust-claim batch size.
`BACKEND_RELOAD=1` is the launcher's default. It watches backend Python and
Markdown files; editing them interrupts active requests. Use `BACKEND_RELOAD=0`
to disable reload. Vite retains its separate development hot reload.

## Assembly candidate retrieval

`PATHWAY_CANDIDATE_STRATEGY=indexed` enables hybrid contextual candidate selection;
`exhaustive` keeps the reference full scan. `PATHWAY_CANDIDATE_TOP_K=10` and
`PATHWAY_CANDIDATE_EXPANSION_K=30` allow `1 <= top-k <= expansion-k <= 500`.
`PATHWAY_CANDIDATE_SMALL_REGISTRY=64` allows 0-1,000 and avoids index warmup when
that complete registry also fits the cheap-request character threshold. These
are backend settings exposed in health metadata, not extra run-mode selectors.
See [indexed assembly](../../docs/protocols/indexed_pathway_assembly.md), introduced
in v45 and retained in v48 before local-pair validation.

V49 keeps extraction schema v36 and assembly v2, versions source fidelity/scope to v5 and external evidence to v4. Fresh PDFs record `column-aware-text-v1`; legacy resumptions keep their prior parser and sentence IDs. Supported graph admission now also requires a matching passed quote audit.

The external selector also supplies the quotation audit. The Go Muse Contributor route requires enabling endpoints that train on request data in OpenCode workspace privacy settings. This setting is never changed by the application. Explicit environment overrides remain available; no fallback is used. See [v50](../../docs/protocols/muse_external_model.md).

Muse cannot disable reasoning completely. For the synthesis/audit model, the unchecked external switch requests minimal effort, and checked requests high effort. DeepSeek per-paper requests still use disabled/enabled thinking. This mapping is explicit in the UI, health reasoning policy and request metrics; provider reasoning tokens count toward output usage.
