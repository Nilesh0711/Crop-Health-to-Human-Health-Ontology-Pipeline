# Shared evidence utilities and historical readers

The active pipeline assesses two neighboring claims and their local connection through
[`neighbor_validation/`](../neighbor_validation/) after provisional assembly.
DeepSeek Flash assesses each paper; Muse Spark 1.3 Contributor synthesizes that pair's evidence.
Longer assembled chains remain unassessed as complete mechanisms.
See the [current protocol](../../../docs/protocols/neighboring_claim_validation.md).

This package owns reusable evidence handling:

- `evidence.py`: exact sentence numbering, lossless repeated-text packing and
  reconstruction of selected paper-local quotations.
- `history.py`: verifies stored contradictions for historical result views and
  evidence-bound repair eligibility.
- `proofs.py` and `paper_proofs.py`: historical saved-record schemas and guards.
  These preserve paper/chunk identity, literal evidence and old audit compatibility.

The active neighboring-pair judge uses the exact-text utilities; it does not execute the
retired per-claim, grouped-trust or single-call joint workflows. Those executors,
their old configuration and detailed documentation now live in
[`archive/legacy_pipeline/research_historical`](../../../archive/legacy_pipeline/research_historical/README.md).
The application cannot select them through runtime settings or the CLI.

## Models and error behavior

The selected models remain DeepSeek Flash for extraction, source fidelity and
paper assessment, MiMo Flash for query planning and assembly, and Muse Spark 1.3 Contributor for
local-pair synthesis. Each stage uses only its assigned model. Thinking defaults Off.
No fallback model replaces a failed request. Bounded same-model formatting or
missing-content recovery remains in the provider adapter, with errors recorded
in the activity log. See [provider behavior](../../providers/README.md).

`python -m backend.cli.external_validation validate --pdf <path>` invokes the
same neighboring-pair document pipeline. `--resume` reuses compatible saved
extraction/source inputs, `--papers` accepts 1-30 discovered papers per pair,
and `--pairs` accepts 1-500 assessment targets. `build-candidates` is an offline tuple
import only and `health-check` checks the assigned models. Scratch tuple imports
remain under `reports/external_validation/`.
