# Backend configuration

- `paths.py` owns repository-relative PDF, output, prompt and frontend-build paths. CLI artifacts use
  `reports/external_validation/`; prompt resources use the extraction/external-validation stage folders.
  Paths are based on the module's location, not the current working directory.
- `backend/config/settings.py` owns `Settings`, model routing and validation
  budgets. It re-exports existing path names for compatibility.
- Startup environment defaults are documented in `scripts/run_backend.sh` and
  `backend/stages/external_validation/README.md`. Set overrides before starting the process;
  restart it when changing import-time environment settings.

Do not put credentials into committed configuration. Local `.env` files remain
ignored; the application does not automatically load them.

The backend calls OpenCode inference directly. On this
Windows machine the key is stored under `crop-to-human-health/OpenCodeInference`
in the current user's Windows Credential Manager. Replace it with
`python -m backend.cli.configure_api_key`, or supply `OPENCODE_API_KEY` in the
backend environment. Keys are never sent to the browser or stored in settings.

`OPENCODE_RESPONSE_FORMAT=model_default` requests native JSON mode for DeepSeek
and schema format for MiMo Flash/Pro, based on successful configuration probes
(DeepSeek rejected schema mode). Application-side schema and evidence checks
always run. `JSON_REPAIR_ATTEMPTS=1` enables model-proposed syntax edits;
`JSON_REGENERATION_ATTEMPTS=1` enables regeneration of incomplete responses from
the original evidence. Either can be disabled with `0`. There is at most one
extra call per request, choosing one branch, and no local JSON correction.
`REGENERATION_MAX_OUTPUT_TOKENS=32768` caps additional regeneration headroom;
an original request's higher budget is preserved. There is no model or transport
substitution. The five stage selectors are extraction, source, query, external
and pathway assembly. `OPEN_CODE_EXTERNAL_PAPER_MODEL` separately assigns per-paper
judgement (DeepSeek Flash); `OPEN_CODE_EXTERNAL_MODEL` assigns synthesis (MiMo Pro).
These phases share the external Thinking control, default Off. Retired scope/semantic/priority/helper settings and agent CLI profiles
have been removed.

`EXTERNAL_MIN_OUTPUT_BUDGET=28192` reserves generation headroom for joint external
assessment from the first attempt; the allowance covers answer and reasoning.
Extraction/source/query budgets retain their existing defaults. One regeneration
may increase the allowance up to `REGENERATION_MAX_OUTPUT_TOKENS`, while preserving
an original request's larger budget. Recovery attempts are warnings until recovery
is exhausted or disabled; their raw failed-attempt status and cause remain logged.
