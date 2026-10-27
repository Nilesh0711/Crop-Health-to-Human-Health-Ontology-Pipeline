# Stop a pipeline run

The Stop pipeline button appears beside the run controls for the selected active job. POST /api/jobs/{job_id}/stop is idempotent and job-scoped. Unknown IDs return 404; a terminal/old job cannot stop a newer run.

Active runs transition queued/running ? stopping ? stopped. Stopping remains active until the worker and its retrieval/lookahead pools release their work; new runs are blocked during this interval. The terminal state and activity are persisted. Stopped runs expose the existing recovery controls. Resume is available when extraction has a complete compatible checkpoint; stopping incomplete extraction requires a fresh run. Saved results are not deleted.

Job-scoped cancellation follows work into bounded retrieval pools. No new LLM request or structured-output recovery starts after cancellation is observed. An asynchronous HTTP wait inside a job is cancelled locally, closing its connection instead of waiting for the model timeout. Remote provider processing/cost cancellation is unconfirmed. Local PDF parsing, GPU inference and blocking article downloads finish their current operation before their next cancellation boundary; the UI remains Stopping until resources are released.

Cancellation is control flow, not a provider failure or a scientific verdict. It cannot become needs_review, unsupported, or contradicted. Cancelled model waits are recorded separately in request metrics. Partial external evidence and completed stage checkpoints remain available. Assembly stops preserve the last complete global graph and shard checkpoint; claim action history records stopped rather than failed.

Verification uses mock transports only: queued/running cancellation, idempotency, isolated job IDs, resource lockout while stopping, worker-context propagation, local HTTP cancellation without regeneration, checkpoint preservation, frontend button states and continued polling through Stopping.
