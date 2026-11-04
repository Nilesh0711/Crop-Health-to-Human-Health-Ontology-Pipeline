# Tests

Run `python -m pytest` from the repository root after installing
`requirements-dev.txt`. `pyproject.toml` sets discovery to `tests/`, adds the
repository to the import path and uses package-based import mode.

Inference tests use mocked transports. An automatic fixture blocks live HTTP
transport and supplies a dummy key, so the suite cannot spend API credits.
Direct API, model-proposed parsing edits, incomplete-response regeneration and
checkpoint recovery tests exercise the only provider transport. Tests explicitly reject any automatic
local formatting correction or chained recovery attempts.

- `backend/`: API, source fidelity, ontology constraints, repairs and evaluation.
- `external/`: retrieval, neighboring-pair assessment, provider adapter and historical regression contracts.
- `project/`: architecture, launcher reload and maintenance-command compatibility.
- `regression/`: versioned pipeline regressions kept to protect earlier fixes.

Run a group with `python -m pytest tests/backend`, for example. Frontend checks
live in `frontend/tests/` and run with `npm test` from `frontend/`.
Tests use synthetic/local evidence and mocked providers; they do not establish
scientific precision/recall or require paid model calls.

Historical per-claim/trust executors are imported explicitly from
`archive/legacy_pipeline/research_historical/`, using `HistoricalSettings`. Live backend imports
of that research package are forbidden by architecture tests. Saved-result
readers and exact-text evidence helpers remain in production packages.

## Current verification

On 1 November 2026, v50 passed **921 Python tests**, **59 frontend tests** and the
production build after the proof-audit, atomic re-extraction and conservative-layout update. Contract coverage includes source/manual
decisions, exact proof, local-pair support, evidence reuse, redo namespaces,
stop/resume, historical viewing, early paper checkpoints, automatic reload,
idempotent retained proof echoes, conflicting selections, review holds and
out-of-order paper completions, exact/partial/contextual proposition matches,
correction provenance, focused follow-ups, synthesis reuse and cross-contract
invalidation. Synthetic declaration tests check admissibility, not live-model
scientific accuracy. A local export of the prior eight Crop Health pairs was saved
for paired review under `reports/benchmarks/qualified_neighbor_v47_baseline.json`.
That generated, Git-ignored baseline is not a test dependency or part of a fresh
checkout; use it only if retained with the original run data.
The six-pair immediate v47 baseline is separately exported at
`reports/benchmarks/neighbor_v47_before_v48_20261101T101507Z.json`. Offline snapshot/
comparison commands and the named-reviewer/evidence-signature requirement are
documented in [the v48 protocol](../docs/protocols/identity_atomicity_and_qualified_support.md).
New contracts cover inconsistent declared merges, stale identity-map rebuilding,
contextual correction proof, exclusion versus host context, strict unknown
applicability, qualified-support display and benchmark source immutability.
Redo-display checks cover historical/current result separation, out-of-order
refreshes, retained historical access and exclusion of retired results from
current benchmark counts. Partial-match tests retain strict paper stance enums.
The suite checks active backend undefined names
with Ruff; run `python -m ruff check backend` independently when editing Python.

If Windows denies access to its system temporary directory, choose a fresh
workspace-local pytest directory, for example:

```powershell
.venv/Scripts/python.exe -m pytest -q --basetemp reports/pytest-local-run
```

Use a new disposable directory for `--basetemp`; pytest manages its contents.
The existing Starlette/httpx deprecation warning and Vite bundle-size advisory
are separate from test failures. No test success substitutes for independent
scientific review or a real-document precision/recall benchmark.

V49 tests additionally cover literal arithmetic, signed/partial numeric-token rejection, quote-audit evidence binding and holds, audit invalidation and resume scheduling, atomic parent/child provenance, interruption recovery, source/provider failure before and after publication, column-order preservation, alternate OA-copy access, and offline corpus manifests. See [v49](../docs/protocols/proof_audit_atomic_reextraction_and_layout.md).

V50 additionally checks the Muse assignment for synthesis and audit, the preserved DeepSeek paper route, single-attempt privacy-policy errors without fallback, and extraction reuse when only the external model changes. Historical comparison configuration remains pinned to the original MiMo Pro route. After account access was enabled, authenticated synthetic JSON, synthesis and quote-audit calls succeeded. Muse uses schema-format requests and explicit minimal/high reasoning controls. These contract checks and synthetic calls do not establish scientific accuracy. See [v50](../docs/protocols/muse_external_model.md).

Synthesis-consistency regressions verify that partial component findings cannot grant connection_supported, that repeated inconsistent verdicts preserve the unadmitted response and paper checkpoints without poisoning the client or cancelling sibling pairs, and that automatic cancellation retains its actual cause. Provider failures remain fatal and visible.
