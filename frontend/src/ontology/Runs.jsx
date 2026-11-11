import { useEffect, useRef, useState } from 'react'
import { request } from '../lib/api.js'
import { watchJob } from '../lib/jobPolling.js'
import { defaultThinking } from '../lib/thinking.js'
import { activityStage, activityStageLabel, filterActivity } from '../lib/activity.js'
import { useData, getPdfUrl, Notice, Pagination } from './useData.jsx'

function SavedClaims({ pdfId }) {
  const [offset, setOffset] = useState(0)
  const data = useData(`/pdfs/${pdfId}/claims?limit=25&offset=${offset}`)
  const rows = data.data?.limit ? data.data.claims : data.data?.claims?.slice(offset, offset + 25)
  return <><Notice {...data} />{rows?.map(record => {
    const c = record.candidate
    return <article className="compact-evidence" key={c.candidate_id}>
      <h3>{c.subject} → {c.relation} → {c.object}</h3><blockquote>{c.source_evidence_span}</blockquote>
      <p className="muted">Source page {c.source_page ?? 'not recorded'}</p>
      {(c.subject_context || c.object_context) && <dl>{['subject', 'object'].filter(role => c[role + '_context']).map(role => <div key={role}><dt>{role} context</dt><dd>{c[role + '_context']}</dd></div>)}</dl>}
      {!!Object.keys(c.qualifiers || {}).length && <details><summary>Conditions and measurements</summary><dl>{Object.entries(c.qualifiers).map(([k, v]) => <div key={k}><dt>{k.replaceAll('_', ' ')}</dt><dd>{String(v)}</dd></div>)}</dl></details>}
      {c.source_validation && <details><summary>Historical source assessment</summary><pre>{JSON.stringify(c.source_validation, null, 2)}</pre></details>}
    </article>
  })}{data.data && <Pagination offset={offset} total={data.data.total || data.data.claims?.length || 0} limit={25} onChange={setOffset} />}</>
}

const activeStatuses = ['queued', 'running', 'stopping']
const statusText = status => (status || 'not_started').replaceAll('_', ' ')

function ActivityLog({ logs, loading, error, active }) {
  const [stage, setStage] = useState('all')
  const [severity, setSeverity] = useState('all')
  const [showAll, setShowAll] = useState(false)
  const stages = [...new Set(logs.map(activityStage))]
  const filtered = filterActivity(logs, stage, severity)
  const visible = showAll ? filtered : filtered.slice(-150)
  return <section className="ontology-run-activity" aria-label="Activity logs">
    <div className="ontology-section-heading"><h3>Activity logs</h3><span className="muted">{active ? 'Live updates' : 'Saved activity'} · {logs.length} entries</span></div>
    <div className="ontology-log-filters">
      <label>Stage<select value={stage} onChange={e => setStage(e.target.value)}><option value="all">All stages</option>
        {stages.map(key => <option key={key} value={key}>{activityStageLabel(key)}</option>)}</select></label>
      <label>Messages<select value={severity} onChange={e => setSeverity(e.target.value)}><option value="all">All messages</option><option value="issues">Warnings and errors</option></select></label>
    </div>
    <Notice loading={loading && !logs.length} error={error} />
    {visible.length ? <ol className="ontology-activity" aria-live="off">{visible.map((entry, i) => <li key={i} className={entry.level === 'error' ? 'ontology-error' : entry.level === 'warning' ? 'ontology-warning' : ''}>
      <div className="ontology-log-meta"><time>{entry.at ? new Date(entry.at).toLocaleTimeString() : ''}</time><span>{activityStageLabel(activityStage(entry))}</span><span>{entry.level || 'info'}</span></div>
      <div>{entry.message}</div></li>)}</ol> : !loading && <p className="muted">{logs.length ? 'No messages match these filters.' : active ? 'Waiting for the first activity message…' : 'No activity recorded for this paper yet.'}</p>}
    {!showAll && filtered.length > 150 && <button onClick={() => setShowAll(true)}>Show all {filtered.length} matching messages</button>}
  </section>
}

function PaperRun({ paper, health, onRefresh, blocked, onBusyChange, options, setOptions }) {
  const discoveredJobId = paper.active_job_id || paper.latest_job_id || null
  const [jobId, setJobId] = useState(discoveredJobId)
  const [job, setJob] = useState(null)
  const [actionError, setActionError] = useState('')
  const [monitorError, setMonitorError] = useState('')
  const [jobMissing, setJobMissing] = useState(false)
  const [logRevision, setLogRevision] = useState(0)
  const [showClaims, setShowClaims] = useState(false)
  const refresh = useRef(onRefresh)
  const mounted = useRef(true)
  refresh.current = onRefresh
  const savedLogs = useData(`/pdfs/${paper.id}/logs`, logRevision)
  const currentJob = job?.id === jobId ? job : null
  const active = !jobMissing && activeStatuses.includes(currentJob?.status || paper.status)
  const canExecute = health?.capabilities?.execute === true
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => { if (discoveredJobId) setJobId(discoveredJobId) }, [discoveredJobId])
  useEffect(() => {
    setMonitorError(''); setJobMissing(false)
    if (!jobId) return
    return watchJob({ fetchJob: () => request(`/jobs/${jobId}`),
      onJob: current => {
        setJob(current); setMonitorError('')
        if (!activeStatuses.includes(current.status)) {
          setLogRevision(n => n + 1)
          refresh.current()
        }
      },
      onMissing: () => {
        setJobMissing(true)
        setMonitorError('The live job is unavailable. Saved logs and claims are still available. Resume saved run or start a new run when no pipeline is active.')
        setLogRevision(n => n + 1)
        refresh.current()
      },
      onError: e => setMonitorError(e.message), intervalMs: 3000 })
  }, [jobId])
  async function run(action) {
    if (!canExecute || blocked || active) return
    onBusyChange(true); setActionError('')
    try {
      let body = options
      if (action === 'resume') {
        const recovery = await request(`/pdfs/${paper.id}/recovery`)
        if (!recovery.can_continue) throw new Error(recovery.reason || 'No compatible checkpoint is available')
        body = { ...recovery.options, thinking: { ...options.thinking, extraction: recovery.options?.thinking?.extraction ?? options.thinking.extraction } }
      }
      const started = await request(`/pdfs/${paper.id}/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      if (mounted.current) {
        setJob(started); setJobId(started.id); setJobMissing(false); setMonitorError('')
      }
      refresh.current()
    } catch (e) { if (mounted.current) setActionError(e.message) } finally { onBusyChange(false) }
  }
  async function stop() {
    try {
      const result = await request(`/jobs/${jobId}/stop`, { method: 'POST' })
      if (mounted.current) setJob(result)
    } catch (e) { if (mounted.current) setActionError(e.message) }
  }
  const logs = currentJob?.logs ?? savedLogs.data ?? []
  const manualGrounding = health?.pipeline?.manual_grounding
  const groundingAvailable = manualGrounding?.enabled ?? (health?.pipeline?.stage_status?.ontology_grounding !== 'paused')
  const extractionReady = paper.summary?.extraction_coverage?.complete === true && paper.summary?.extracted > 0
  return <div className="ontology-run-detail">
    <div className="ontology-section-heading"><h3>{paper.title || paper.name}</h3><span className="ontology-run-status">status: {statusText(currentJob?.status || paper.status)}</span></div>
    {(() => {
      const pdfUrl = getPdfUrl(paper)
      return pdfUrl ? <p><a href={pdfUrl} target="_blank" rel="noreferrer">Open source PDF</a></p> : null
    })()}
    {canExecute && <div className="ontology-run-actions">
      <button className="primary" disabled={blocked || active || !health?.model_ready} onClick={() => run('run')}>Run pipeline</button>
      {groundingAvailable && <button disabled={blocked || active || !extractionReady || manualGrounding?.ready === false}
        title={extractionReady ? 'Ground saved claims without repeating extraction' : 'Complete extraction first'}
        onClick={() => run('ground-claims')}>Run grounded claims</button>}
      <button disabled={blocked || active} onClick={() => run('resume')}>Resume saved run</button>
      {active && jobId && <button disabled={currentJob?.status === 'stopping'} onClick={stop}>Stop pipeline</button>}
    </div>}
    {canExecute && !health?.model_ready && <p className="ontology-warning">One or more active models are unavailable. Browsing and saved activity remain available.</p>}
    {canExecute && blocked && !active && <p className="muted">Another operation is active. You can continue browsing papers and their logs.</p>}
    {actionError && <p className="ontology-error" role="alert">{actionError}</p>}
    {monitorError && <p className="ontology-warning" role="alert">{monitorError}</p>}
    {currentJob?.error && <p className="ontology-error" role="alert">{currentJob.error}</p>}
    <ActivityLog logs={logs} loading={savedLogs.loading && !currentJob} error={savedLogs.error} active={active} />
    {canExecute && <details><summary>Extraction and model settings</summary>
      <div className="ontology-settings"><label>PDF pages (0 = all)<input type="number" min="0" max="1000" value={options.max_pages} onChange={e => setOptions({ ...options, max_pages: Number(e.target.value) })} /></label>
        <label>Target size (words)<input type="number" min="200" max="2000" value={options.extraction_target_words} onChange={e => setOptions({ ...options, extraction_target_words: Number(e.target.value) })} /></label>
        <label>Extraction mode<select value={options.extraction_mode} onChange={e => setOptions({ ...options, extraction_mode: e.target.value })}><option value="target_context">Target / context</option><option value="whole_packet">Whole packet</option></select></label>
        <label>Generation budget<input type="number" min="8192" max="50000" step="1" value={options.extraction_generation_budget} onChange={e => setOptions({ ...options, extraction_generation_budget: Number(e.target.value) })} /></label>
        <label>Claims per call (0 = uncapped)<input type="number" min="0" max="8" value={options.claims_per_packet} onChange={e => setOptions({ ...options, claims_per_packet: Number(e.target.value) })} /></label>
      </div>
      {[['extraction', 'Claim extraction'], ...(groundingAvailable ? [['pathway_assembly', 'Ontology grounding (manual)']] : [])].map(([key, name]) => <div key={key} className="ontology-model-setting"><span>{name}: <code>{(key === 'pathway_assembly' ? manualGrounding?.model : null) || health?.pipeline?.task_models?.[key] || 'Unavailable'}</code></span>
        <label className="inline-check"><input type="checkbox" role="switch" checked={options.thinking[key]} onChange={e => setOptions({ ...options, thinking: { ...options.thinking, [key]: e.target.checked } })} />Thinking</label></div>)}
      <p className="muted">Thinking defaults to Off for extraction and On for manual grounding. Run pipeline stops after extraction.</p>
      <p className="muted">Parallel extraction requests: {health?.pipeline?.extraction_concurrency ?? 2}.</p>
    </details>}
    <details><summary>Saved run details</summary><pre>{JSON.stringify(paper.summary || { status: paper.status }, null, 2)}</pre></details>
    <details onToggle={event => setShowClaims(event.currentTarget.open)}><summary>Saved extracted claims and source passages</summary>
      {showClaims && <SavedClaims pdfId={paper.id} />}</details>
  </div>
}

export default function Runs({ pdfs, health, onRefresh, loading = false, error = '' }) {
  const [selected, setSelected] = useState(pdfs[0]?.id || '')
  const [busy, setBusy] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [options, setOptions] = useState({ max_pages: 0, claims_per_packet: 0, extraction_mode: 'target_context',
    extraction_target_words: 200, extraction_generation_budget: 50000, thinking: defaultThinking() })
  const paper = pdfs.find(p => p.id === selected) || pdfs[0]
  const canExecute = health?.capabilities?.execute === true
  const blocked = busy || pdfs.some(p => activeStatuses.includes(p.status))
  async function upload(event) {
    const file = event.target.files?.[0]
    if (!file) return
    setBusy(true); setUploadError('')
    try {
      const body = new FormData(); body.append('file', file)
      const result = await request('/pdfs', { method: 'POST', body })
      setSelected(result.id); onRefresh()
    } catch (e) { setUploadError(e.message) } finally { setBusy(false) }
    event.target.value = ''
  }
  return <section className="ontology-panel ontology-runs"><div className="ontology-section-heading"><h2>PDF runs</h2><button onClick={onRefresh} disabled={loading}>Refresh papers</button></div>
    <p>{health?.pipeline?.stage_status?.ontology_grounding === 'manual'
      ? 'Run pipeline extracts claims only. Use Run grounded claims to ground a completed extraction separately.'
      : health?.pipeline?.stage_status?.ontology_grounding === 'paused'
      ? 'Extraction-only runs: ontology grounding and later stages are paused while atomic claims are reviewed.'
      : 'Extract claims and ground their concepts. The pipeline stops after ontology grounding.'} Source fidelity and external scientific validation stay paused.</p>
    <Notice loading={loading && !pdfs.length} error={error || uploadError} />
    {canExecute && <details><summary>Add a source PDF</summary><input type="file" accept="application/pdf" disabled={blocked} onChange={upload} /></details>}
    <div className="ontology-runs-layout"><aside className="ontology-paper-list" aria-label="All papers"><h3>All papers ({pdfs.length})</h3>
      {pdfs.map(p => <button key={p.id} className={`ontology-paper ${paper?.id === p.id ? 'selected' : ''}`} aria-pressed={paper?.id === p.id} onClick={() => setSelected(p.id)}>
        <span className="ontology-paper-title">{p.title || p.name}</span><span className="ontology-run-status">({statusText(p.status)})</span></button>)}
      {!loading && !pdfs.length && <p className="muted">No papers found. Add a source PDF to begin.</p>}</aside>
      {paper ? <PaperRun key={paper.id} paper={paper} health={health} onRefresh={onRefresh} blocked={blocked} onBusyChange={setBusy}
        options={options} setOptions={setOptions} />
        : <div className="ontology-run-detail"><p className="muted">Select a paper to view its run controls and activity logs.</p></div>}
    </div>
  </section>
}
