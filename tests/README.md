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
- `external/`: retrieval, joint evidence assessment and provider adapter.
- `project/`: resource and maintenance-command compatibility.
- `regression/`: versioned pipeline regressions kept to protect earlier fixes.

Run a group with `python -m pytest tests/backend`, for example. Frontend checks
live in `frontend/tests/` and run with `npm test` from `frontend/`.
Tests use synthetic/local evidence and mocked providers; they do not establish
scientific precision/recall or require paid model calls.
