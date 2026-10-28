export default function SavedRunDetails({ jobs = [], job, jobId, onSelect, pipeline }) {
  return <section className="saved-run-details context-packet" aria-label="Saved run details">
    <h3>Saved run details</h3>
    <p>These are archived settings and logs. Claims and graphs show the published result snapshot, not a reconstruction of the selected historical run.</p>
    <label>Historical run <select value={jobId || ''} onChange={event => onSelect(event.target.value)}>
      <option value="">Document activity</option>
      {jobs.map(row => <option key={row.id} value={row.id}>{row.started_at || row.id} · {row.recorded_status || row.status} · {row.action || 'pipeline'}</option>)}
    </select></label>
    {job && <><dl><dt>Job ID</dt><dd>{job.id}</dd><dt>Recorded status</dt><dd>{job.recorded_status || job.status}</dd>
      <dt>Started</dt><dd>{job.started_at || 'Not recorded'}</dd><dt>Finished</dt><dd>{job.finished_at || 'Not recorded'}</dd></dl>
      {job.error && <p className="inline-error">{job.error}</p>}
      <details><summary>Recorded runtime settings</summary><pre>{JSON.stringify(job.options || {}, null, 2)}</pre></details></>}
    <details><summary>Published model assignments</summary><pre>{JSON.stringify(pipeline || {}, null, 2)}</pre></details>
  </section>
}
