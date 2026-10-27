# Backend architecture

Stage functions perform work; pipelines coordinate them; API/CLI adapters expose
those operations. Shared scientific rules and infrastructure sit outside stages.

```text
backend/
  app.py                         Stable ASGI launch entry point only
  api/app.py                     HTTP routes, uploads, run actions and job snapshots
  pipelines/
    document.py                  Complete PDF-run orchestration and checkpoints
    claim_repair.py              Bounded repair and external-only retry workflows
  stages/
    pdf_parsing/                 Chapters, bibliography, sentences and text quality
    claim_extraction/            Prompt, JSON contract, extraction and deduplication
    source_validation/           Original-PDF fidelity and qualifier checks
    query_planning/              Complementary literature queries
    literature_retrieval/        Citation resolution, indexes, fetching and coverage
    evidence_ranking/            Metadata fusion and passage reranking
    external_validation/         Joint Pro judgement, proof guards and aggregation
    pathway_assembly/            Global MiMo Flash entity alignment and path assembly
    graph_construction/         Graph snapshots, exports and provisional paths
    evaluation/                 Human-annotation samples and metrics
  domain/                        Ontology, vocabulary, alignment, context and modality
  providers/                     LLM adapter and optional NLI model
  shared/                        Storage, atomic artifacts, cache, batching and text
  config/                        Resource paths and model/settings contract
  cli/                           External validation, tuple ingestion and evaluation
```

See [stage inputs, outputs and ownership](stages/README.md) for the detailed flow.

## Entry points

- Startup remains `bash scripts/run_backend.sh` or
  `python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000`.
  The small `app.py` forwards the same FastAPI app from `backend.api.app`.
- Complete PDF run: `backend.pipelines.document.run_pipeline`.
- Standalone external validation: `python -m backend.cli.external_validation`.
- Offline evaluation: `python -m backend.cli.evaluation`.

Internal imports use the owning stage or shared package. The former flat helper
modules and `backend.external_validator` package have been replaced by these
explicit owners; repository scripts and tests use the new paths.

## Dependency rules

- API/CLI adapters may call pipelines or stage interfaces.
- Pipelines may coordinate stages. Stages must not import API handlers,
  pipelines or CLI entry points.
- Domain rules, configuration and shared infrastructure do not depend on stage
  execution or HTTP routes. Reusable modality/search-term helpers live in domain.
- Package initializers do not construct services or load models. Architecture
  tests check dependency direction and import cycles.
- Each stage owns its prompt/schema resources. The extraction template is
  `stages/claim_extraction/prompt.md`; external Markdown is a reference, while
  runtime joint instructions/schema live in `stages/external_validation/joint.py`.
- Standalone scope/semantic model gates, priority-paper routing, the agent CLI
  transport and the local generative client have been removed. All active tasks
  call the direct inference API with their assigned primary model.

## Resource and output management

`config/paths.py` resolves resources independently of the working directory.
PDFs remain in `pdf/`; per-PDF evidence, findings, logs, revisions and graphs
remain in `ontology/<PDF stem>/`. CLI JSONL stays under
`reports/external_validation/`, created only when output is written. Deleted
root archive/scratch folders are not required or recreated.

Project dependencies, scripts and tests stay at the root. Runtime model settings
live in `config/settings.py`; the retired agent CLI configuration is removed.

```bash
python -m backend.cli.evaluation score "ontology/<PDF stem>/evaluation_sample.jsonl"
python -m backend.cli.evaluation sample "ontology/<PDF stem>" --limit 80 --output reports/evaluation_sample.jsonl
```

Sampling refuses to replace existing output unless `--overwrite` is given.
Run `python -m pytest` for pipeline/API/scientific guard and architecture checks.
Frontend checks remain in `frontend/`.

## Structured model responses

The default OpenCode adapter calls the inference API directly with the selected
primary model. It requests native JSON/schema formatting and validates the
complete response schema locally. No local JSON corrections are performed.
Syntax errors go to the same model for an explicit edit proposal; code validates
and applies only those proposed edits, preserving scientific values and evidence.
Empty/truncated answers or missing required response content instead trigger one
fresh generation from the original input and evidence. Regeneration does not
force a favorable judgement. All normal scientific evidence guards still run.
There is at most one extra call in total, with no recovery chaining, provider-error
retry or model substitution. Failed recovery is visible in the activity log.

API final answers and accepted repairs are archived under each run's
`model_responses/`, with request metrics pointing to their artifacts. Credentials
and hidden reasoning are excluded. See [provider behavior](providers/README.md).

## Resume failed work

The frontend's **Resume failed run** action preserves findings and logs. It
reuses completed extraction after checking PDF hash, pipeline version, model
choices and extraction settings. Source/query checks require matching input
signatures; completed external checks require matching validation signatures.
An interrupted external judgement can reuse its retained paper packet.
Extraction that has not produced a checkpoint requires a new run. A complete
historical extraction is admitted only with matching PDF/model provenance and page
coverage; missing historical extraction-limit metadata is explicitly reported.

## Job monitoring across restarts

The local backend runs one worker process. Job metadata, options, progress and
logs are saved atomically in `ontology/<PDF stem>/jobs/<job ID>.json`, with the
latest job identity/status in `run_state.json`. Saved terminal jobs remain
queryable after a restart. If a saved running/queued job has no registered worker,
it becomes interrupted/failed with a recovery message. Its saved scientific work
is retained; status polling never restarts a model request. Recovering an older
job cannot replace a newer run's state or logs.

The frontend stops polling missing jobs, reloads saved findings, and can reconnect
to a replacement active job. Transient connection failures continue monitoring;
polls do not overlap. Backend code reload is off by default. Set
`BACKEND_RELOAD=1` only for development; reloading terminates in-flight work.

Lost jobs and failed/interrupted runs display a persistent recovery notice with
**Continue saved run** and **Restart from beginning**. The read-only recovery
endpoint validates the actual checkpoint and restores the original extraction
settings before enabling Continue. Missing job identity alone does not prevent
checkpoint recovery. Restart requires an explicit confirmation that it replaces
current claims, validations and activity logs. Neither action starts automatically.
