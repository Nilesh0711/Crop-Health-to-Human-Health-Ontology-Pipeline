const speed = value => Number.isFinite(value) ? Number(value).toFixed(2) : 'N/A'
const shortModel = model => String(model || '').split('/').pop()

export default function ModelPerformance({ requests = [], summary, policy, routing }) {
  if (!requests.length && !summary?.llm_calls) return null
  const taskModels = summary?.task_models || routing
  const roles = taskModels ? Object.entries(taskModels).map(([task, model]) => [({scope: 'Document screening', extraction: 'Extraction', semantic: 'Type checking', source: 'Source verification', query: 'Search planning', external: 'External evidence', external_priority: 'Top independent article'})[task] || task, model, task]) : [
    ['Extraction', summary?.extraction_model, 'extraction'],
    ['Scope / typing / query planning', summary?.helper_model || summary?.validation_model, 'helper'],
    ['Source / external evidence', summary?.validation_model, 'validation'],
  ]
  const policies = summary ? summary.llm_reasoning_policy || {} : policy || {}
  return <details className="model-performance"><summary>Model activity &amp; token accounting · {requests.length || summary?.llm_calls?.total_requests || 0} calls</summary>
    <p>Final-output TPS measures useful response tokens divided by the full request time. Output + reasoning TPS includes reported thinking work. Both include queue, prefill and network overhead; neither is an engine-only decode benchmark.</p>
    {summary?.models_used?.length ? <p>Models attempted: {summary.models_used.map(shortModel).join(", ")}. Role cards show configured primaries; request rows show actual attempts.</p> : null}
    {summary?.external_phase_models && <p>Per-paper judgement: {shortModel(summary.external_phase_models.paper)}. Cross-paper synthesis: {shortModel(summary.external_phase_models.synthesis)}.</p>}
    <div className="model-role-grid">{roles.map(([label, model, role]) => <div key={role}><span>{label}</span><strong>{shortModel(model) || 'Not recorded'}</strong><small>Requested effort: {policies[role]?.requested_reasoning_effort || 'Not recorded for this run'} · thinking: {policies[role]?.requested_thinking || 'provider default / not recorded'}{policies[role]?.requested_thinking_budget ? ` · requested budget ${policies[role].requested_thinking_budget}` : ''}</small></div>)}</div>
    {requests.length ? <div className="diagnostic-table-scroll"><table className="diagnostic-table"><thead><tr><th>Task / model</th><th>Time</th><th>Input tokens</th><th>Final tokens</th><th>Reasoning tokens</th><th>Final TPS</th><th>Output + reasoning TPS</th><th>Requested effort</th></tr></thead><tbody>{requests.map((item, index) => <tr key={index}><td><details><summary>{(item.task || 'Model request').split(':')[0]}</summary><small>{item.task}</small><br /><small>{item.error || ""}{item.next_model ? `; switching to ${item.next_model}` : ""}</small><br /><small>{item.request_model || item.model} · {item.status}</small></details></td><td>{item.elapsed_seconds?.toFixed(1)}s</td><td>{(item.input_tokens_reported ?? item.usage_reported) && Number.isInteger(item.input_tokens) && item.input_tokens >= 0 ? item.input_tokens.toLocaleString() : 'N/A'}</td><td>{item.usage_reported ? item.output_tokens : 'N/A'}</td><td>{item.reasoning_usage_reported ? item.reasoning_tokens : 'N/A'}</td><td>{speed(item.effective_output_tps)}</td><td>{speed(item.effective_generated_tps)}</td><td>{item.requested_reasoning_effort || 'provider default'}</td></tr>)}</tbody></table></div> : <p>This older run has aggregate counters only. Extraction: {summary?.llm_calls?.extraction?.reasoning_tokens ?? 'N/A'} reasoning tokens / {summary?.llm_calls?.extraction?.output_tokens ?? 'N/A'} final tokens. Rerun to record per-call details.</p>}
  </details>
}
