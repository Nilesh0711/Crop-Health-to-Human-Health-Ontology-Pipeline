# Checkpoint save retries with the existing temporary-file format

Crop Health job `6383db82b09944ec9b62c99bfffd8df8` failed during source
validation when Windows refused replacing `candidates.jsonl`. The model call
had completed successfully. The saved snapshot had 124 claims and 70 source
results; its complete pending snapshot contained a 71st result.

The previously requested rollback remains respected for unique temporary files
and automatic source-ledger reconciliation: neither feature is restored.
The minimal fix keeps the existing `filename.tmp` convention. Publication
retries only Windows errors 5, 32 and 33, with eight delays totalling about
3.6 seconds. Other errors fail immediately; persistent locks fail after the
bounded attempts, leaving the original destination and complete pending file.
No LLM recovery or scientific changes occur during these retries.

The application previously held JSONL read handles open throughout parsing.
Readers now read the text, close the handle and then parse it. This reduces
the replacement-blocking interval and preserves a consistent snapshot.
Tests verify actual Windows sharing locks, publication while JSON parsing is
underway, bounded failure, non-retryable errors, and unchanged temporary paths.
The exact process holding the file during the reported failure was not identified.

The pending result was recovered after checking candidate IDs, matching PDF,
unchanged scientific fields and model/Thinking input signatures. Original
files were backed up under `reports/windows_checkpoint_retry_2026-10-25/`.
The extraction checkpoint is compatible with v36 and resume validation passes.
Use Resume run; no new extraction or inference was started by the repair.

`GET /api/health` exposes
`pipeline.artifact_write_policy = bounded-replace-retry-existing-temp-v1`
so the active backend can be checked without invalidating scientific checkpoints.
