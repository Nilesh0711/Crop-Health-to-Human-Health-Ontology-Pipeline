# External model assignments and concise research activity

Per-paper external assessments use `opencode-go/deepseek-v4.1-flash` by default.
The final cross-paper synthesis uses `opencode-go/mimo-v2.6-pro`.
Both use the existing external Thinking switch (default Off), evidence selection,
bounded same-model recovery, cancellation and per-paper checkpointing. Changing
the paper model rejudges papers; it cannot reuse a Pro judgement as a DeepSeek
judgement. Compatible downloaded evidence and extraction/source checkpoints remain
reusable. Original run decisions and human labels are not rewritten automatically.

Native requests use JSON mode for DeepSeek and schema formatting for Pro.
Both receive application schema/provenance validation. Shared accounting retains
every call with its actual model, usage, response artifact and recovery history.
There are still normally N+1 calls for N papers; model assignment changes do not
establish scientific accuracy equivalence or guarantee latency/cost savings.

## Removed from Activity

Routine messages below are no longer appended to new Activity logs. The same
policy hides historical routine rows without deleting the original saved log:

- Full search queries, returned-result counts and retrieval lookahead chatter.
- Metadata/article ranking notifications and candidate counts.
- Paper-fetch/preparation waves and worker counts.
- Per-paper ranked-passage counts and passage-preparation completion/timing rows.
- Per-paper assessment start, saved/reused and retained-evidence cache notices.
- Duplicate model request progress rows: waiting, completion and intermediate states.
- Successful request token/TPS diagnostic rows; successful recovery remains visible.
- Individual source-validation and query-planning batch-start messages.
- Individual extraction section/target starts and running accepted-candidate totals.
- Startup model assignments, extraction-budget configuration, scope-not-screened
  and disabled-semantic-stage notices; model loading/readiness messages.
- Pathway endpoint batch/registry-scan progress.

Stage milestones, aggregate extraction/source/reuse summaries, current claim,
claim decisions, source name/provenance corrections, synthesis milestones,
graph/pathway completion, stop/resume events and every warning/error remain.
Warnings are retained even if tagged as retrieval/fetch/ranking, including missing
citations, unavailable models, incomplete coverage and recovery errors.

The live request panel now reads the job's separately persisted `request_progress`
rather than relying on progress log rows. Model activity retains full token,
timing, model and request diagnostics; evidence details retain fetched papers,
selected passages, citation tracking and review reasons. Stage filters remain.
