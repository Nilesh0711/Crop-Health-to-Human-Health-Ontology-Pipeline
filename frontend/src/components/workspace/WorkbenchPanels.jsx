import { THINKING_STAGES, defaultThinking } from '../../lib/thinking.js'
import { activityStage, activityStageLabel, filterActivity, ACTIVITY_STAGES } from '../../lib/activity.js'
import { useEffect, useRef, useState } from 'react'
import { Activity, ArrowRight, Check, CheckCircle2, ChevronDown, CircleAlert, Clock3, FileText, GitBranch, Layers3, LoaderCircle, Network, PanelLeftClose, PanelLeftOpen, Play, Search, Settings2, Sparkles, Square } from 'lucide-react'

export function StopRunButton({ active, stopping, onStop }) {
  if (!active) return null
  return <button className="button stop-button" type="button" disabled={stopping} onClick={onStop}
    title="Stop this run and preserve saved progress">{stopping ? <LoaderCircle size={15} className="spin" /> : <Square size={14} />}
    {stopping ? 'Stopping?' : 'Stop pipeline'}</button>
}

export function ResumeRunButton({ canResume, disabled, onResume }) {
  if (!canResume) return null
  return <button className="button secondary" type="button" disabled={disabled} onClick={onResume}
    title="Reuse extracted claims and completed checks; continue unfinished work"><Play size={15} /> Resume run</button>
}

export function SidebarToggle({ hidden, onToggle }) {
  const Icon = hidden ? PanelLeftOpen : PanelLeftClose
  const label = hidden ? 'Show paper library' : 'Hide paper library'
  return <button type="button" className="sidebar-toggle icon-button" title={label} aria-label={label}
    aria-expanded={!hidden} aria-controls="paper-library" onClick={onToggle}><Icon size={19} /></button>
}

export function PaperRunStatus({ paper }) {
  const status = paper.status || 'not_started'
  const [label, Icon] = ({
    completed: ['Run completed', CheckCircle2],
    completed_with_warnings: ['Completed with warnings', CircleAlert],
    running: ['Running', LoaderCircle],
    stopping: ['Stopping', LoaderCircle],
    stopped: ['Stopped', Square],
    queued: ['Queued', Clock3],
    failed: ['Run failed', CircleAlert],
    blocked_text_quality: ['Blocked: text quality', CircleAlert],
    not_started: ['Not run yet', Clock3],
  })[status] || [status.replaceAll('_', ' '), CircleAlert]
  return <span className={`paper-run-status ${status}`}><Icon size={12} aria-hidden="true" className={status === 'running' ? 'spin' : undefined} /><span>{label}</span></span>
}

export function WorkspaceNavigation({ view, onChange, claims, paths, errors }) {
  return <nav className="tabs" aria-label="Paper workspace views">
    {[
      ['claims', 'Claims', Layers3, claims],
      ['graph', 'Graph', Network],
      ['paths', 'Paths', GitBranch, paths],
      ['activity', 'Activity', Activity, errors || undefined],
      ['details', 'Run details', Settings2],
    ].map(([key, label, Icon, count]) => <button type="button" key={key}
      className={view === key ? 'active' : ''} aria-current={view === key ? 'page' : undefined}
      onClick={() => onChange(key)}><Icon size={17} />{label}
      {count != null && <span className={key === 'activity' ? 'tab-alert' : ''}>{count}</span>}
    </button>)}
  </nav>
}

export function FindingsToolbar({ search, onSearch, filter, onFilter, counts, shown }) {
  return <div className="findings-toolbar">
    <div className="findings-search"><Search size={17} /><input type="search" aria-label="Search claims and evidence"
      placeholder="Search a claim, subject, or paper…" value={search} onChange={event => onSearch(event.target.value)} /></div>
    <div className="claim-filters" aria-label="Filter claims by decision">
      {[['all', 'All claims'], ['source', 'Source supported'], ['supported', 'External support'], ['review', 'Review / pending'], ['rejected', 'Rejected']].map(([key, label]) =>
        <button key={key} type="button" className={filter === key ? 'active' : ''} aria-pressed={filter === key}
          onClick={() => onFilter(key)}>{label}<span>{counts[key]}</span></button>)}
    </div>
    <p className="results-caption">Showing {shown} of {counts.all} claims. Expand a claim to inspect its evidence and validation.</p>
  </div>
}

export function ActivityLog({ logs, running, progress }) {
  const [filter, setFilter] = useState('all')
  const [follow, setFollow] = useState(true)
  const [stage, setStage] = useState('all')
  const end = useRef(null)
  const visible = filterActivity(logs, stage, filter)
  useEffect(() => {
    if (running && follow) end.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [logs.length, running, follow, filter, stage])
  return <section className="activity-panel" aria-label="Pipeline activity log">
    <RequestProgress progress={progress || [...logs].reverse().find(entry => entry.progress)?.progress} running={running} />
    <div className="activity-toolbar"><div className="activity-summary"><Clock3 size={17} />
      <span>{running ? 'Running · updates appear automatically' : `${logs.length} recorded events`}</span>
      <span className={`live-indicator ${running ? 'on' : ''}`}><span />{running ? 'Live' : 'Idle'}</span>
    </div><div className="activity-options"><label className="stage-filter">Stage <select aria-label="Filter activity by stage" value={stage} onChange={event => setStage(event.target.value)}>
      <option value="all">All stages</option>{ACTIVITY_STAGES.map(key => <option key={key} value={key}>{activityStageLabel(key)}</option>)}
    </select></label><div className="segmented" aria-label="Filter activity events">
      <button type="button" className={filter === 'all' ? 'active' : ''} aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>All events</button>
      <button type="button" className={filter === 'attention' ? 'active' : ''} aria-pressed={filter === 'attention'} onClick={() => setFilter('attention')}>Errors &amp; warnings</button>
    </div><label className="follow-toggle"><input type="checkbox" checked={follow} onChange={event => setFollow(event.target.checked)} />Follow live updates</label></div></div>
    <div className="log-list" role="log" aria-label="Run events" aria-live="off">
      {visible.length ? visible.map((entry, index) => <div className={`log-entry ${entry.level}`} key={`${entry.at}-${index}`}>
        <span className="log-rail"><i /></span><div className="log-content"><div className="log-entry-meta">
          <time className="log-time" dateTime={entry.at}>{entry.at ? new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(entry.at)) : ''}</time>
          <span className="log-level">{entry.level}</span><span className="log-stage">{activityStageLabel(activityStage(entry))}</span>{entry.candidate_id && <span className="log-candidate" title={entry.candidate_id}>Claim {entry.candidate_id.replace(/^candidate-/, "").slice(0, 8)}</span>}</div>
          {entry.historical_error_notice && <p className="soft-note">{entry.historical_error_notice}</p>}
          <p>{entry.message}</p></div>
      </div>) : <div className="log-empty"><Activity size={26} /><strong>{logs.length ? 'No matching events' : 'Activity will appear here'}</strong>
        <p>{logs.length ? 'Choose All stages and All events to see the complete run.' : 'Start the pipeline to follow extraction, evidence retrieval, and validation.'}</p></div>}
      <div ref={end} />
    </div>
  </section>
}

export function RequestProgress({ progress, running }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [running])
  if (!progress) return null
  const repairing = progress.status === 'repairing_format' || (progress.recovery_pending && progress.recovery_action === 'format_repair')
  const regenerating = progress.status === 'regenerating_response' || (progress.recovery_pending && progress.recovery_action === 'response_regeneration')
  const active = running && (progress.status === 'waiting_for_provider' || repairing || regenerating)
  const hasInputTokens = progress.input_tokens_reported === true && Number.isInteger(progress.input_tokens) && progress.input_tokens >= 0
  const elapsed = active ? Math.max(0, Math.floor((now - Date.parse(progress.started_at)) / 1000)) : progress.elapsed_seconds
  return <div className="request-progress" role="status" aria-label="Model request progress">
    <strong>{active ? regenerating ? 'Regenerating response (1 attempt)' : repairing ? 'Repairing response formatting (1 attempt)' : 'Waiting for model response' : 'Latest model request'} · {progress.model}</strong>
    <p>{progress.task}</p>
    <div className="request-metrics">
      {active && progress.active_request_count > 1 && <span><b>{progress.active_request_count}</b> model requests running in parallel; showing one active request</span>}
      {progress.requested_thinking && <span>Thinking <b>{({ enabled: 'On', disabled: 'Off' })[progress.requested_thinking] || progress.requested_thinking}</b></span>}
      {progress.paper_count != null && <span><b>{progress.paper_count}</b> papers</span>}
      {progress.claim_count != null && <span><b>{progress.claim_count}</b> claims sharing this request</span>}
      {progress.chunk_count != null && <span><b>{progress.chunk_count}</b> chunks</span>}
      {progress.output_limit_tokens != null && <span><b>{Number(progress.output_limit_tokens).toLocaleString()}</b> generation token limit (answer + reasoning)</span>}
      <span><b>{hasInputTokens ? progress.input_tokens.toLocaleString() : active ? 'Pending' : 'Unavailable'}</b> input tokens{hasInputTokens ? ' (provider-reported)' : ''}</span>
      <span><b>{elapsed == null ? 'Unavailable' : `${Math.round(elapsed)} s`}</b> elapsed / {progress.timeout_seconds} s limit</span>
    </div>
    <small>{progress.provider_status}. Request status: {progress.status.replaceAll('_', ' ')}.</small>
    {progress.historical_error_notice && <small>{progress.historical_error_notice}</small>}
  </div>
}

export function PdfQualityReport({ quality }) {
  if (!quality) return null
  const flagLabel = flag => ({
    no_extracted_text_ocr_or_blank_page: 'No text: blank page or OCR needed',
    sparse_text_possible_figure_or_scan: 'Very little text: check figure or scanned page',
    replacement_characters: 'Unreadable characters',
    possible_reading_order_or_split_words: 'Possible reading-order or split-word problem',
  })[flag] || flag.replaceAll('_', ' ')
  return <section className="quote-box" aria-label="PDF page quality report">
    <h3>PDF extraction coverage</h3>
    <p>{quality.pages_with_text} / {quality.total_pages} pages contain extractable text. {quality.selected_pages} pages selected; {quality.pages_with_warnings} pages flagged.</p>
    <small>This measures text availability, not OCR accuracy or verified reading order. Sparse pages may contain figures or be intentionally blank.</small>
    {quality.warnings?.map(warning => <p key={warning}>{warning}</p>)}
    <details className="context-packet"><summary>Per-page text, bibliography and reading-order diagnostics</summary>
      <div className="diagnostic-table"><table><thead><tr><th>PDF page</th><th>Words</th><th>Body / reference characters</th><th>Reading order</th><th>Flags</th></tr></thead>
        <tbody>{quality.pages?.map(page => <tr key={page.page}><td>{page.page}{!page.within_page_limit && ' (outside limit)'}</td><td>{page.word_count}</td><td>{page.body_characters} / {page.reference_characters}</td><td>{page.reading_order.replaceAll('_', ' ')}</td><td>{page.flags.map(flagLabel).join('; ') || 'No automatic flags'}</td></tr>)}</tbody></table></div>
    </details>
  </section>
}

const modelName = value => {
  const id = String(value || '').split('/').pop().split('#')[0]
  return ({ 'mimo-v2.6-flash': 'MiMo Flash', 'deepseek-v4.1-flash': 'DeepSeek Flash', 'mimo-v2.6-pro': 'MiMo Pro' })[id] || id || 'Assigned model'
}

export function RunSetup({ health, options, onOptions, open, onToggle }) {
  const tasks = health?.pipeline?.task_models || {}
  const phases = health?.pipeline?.external_phase_models || {}
  return <section className="workflow-card" aria-label="Pipeline and run settings">
    <div className="section-head compact"><div><span className="eyebrow">RUN CONFIGURATION</span><h2>From paper to evidence</h2></div>
      <button type="button" className="button secondary" aria-expanded={open} aria-controls="run-settings" onClick={onToggle}>
        <Settings2 size={16} />{open ? 'Hide settings' : 'Run settings'}<ChevronDown size={15} className={open ? 'chevron open' : 'chevron'} /></button></div>
    <div className="steps">{[
      ['Extract claims', FileText, modelName(tasks.extraction)], ['Check source', Check, modelName(tasks.source)],
      ...(options.knowledge_building_mode !== 'validated_first' ? [['Candidate pathways', GitBranch, modelName(tasks.pathway_assembly)]] : []),
      ['Find papers', Search, modelName(tasks.query)], ['Assess evidence', Sparkles, phases.paper ? `${modelName(phases.paper)} → ${modelName(phases.synthesis)}` : modelName(tasks.external)],
      ...(options.knowledge_building_mode === 'validated_first' ? [['Assemble pathways', GitBranch, modelName(tasks.pathway_assembly)]] : []), ['Build graph', GitBranch, 'Evidence states'],
    ].map(([label, Icon, detail], index) => <div className="step-group" key={label}>
      {index > 0 && <ArrowRight size={15} className="step-arrow" />}<div className="step"><span className="step-icon"><Icon size={18} /></span><span><strong>{label}</strong><small>{detail}</small></span></div>
    </div>)}</div>
    {open && <div id="run-settings" className="run-settings">
      <div className="settings-section-title"><h3>Knowledge building</h3><p>Compare grouped trust assessment with the existing claim-first flow.</p></div>
      <label>Knowledge building mode<select aria-label="Knowledge building mode" value={options.knowledge_building_mode ?? 'trust_graph'}
        onChange={event => onOptions({ ...options, knowledge_building_mode: event.target.value })}>
        <option value="trust_graph">Trust graph: assemble first, assess groups</option>
        <option value="validated_first">Previous: validate claims, then assemble</option>
      </select><small>Trust graph keeps per-edge verdicts, shares paper requests and records support minus contradiction. The score is not a probability.</small></label>
      {options.knowledge_building_mode !== 'validated_first' && <label>Claims per shared assessment<input type="number" min={1} max={6}
        aria-label="Claims per shared assessment" value={options.trust_batch_size ?? 3}
        onChange={event => onOptions({ ...options, trust_batch_size: Number(event.target.value) })} />
        <small>Default 3. Smaller groups reduce context and output pressure. Disconnected claims are assessed too. Up to {health?.pipeline?.external_paper_workers ?? 3} independent paper requests run in parallel; this is separate from the claim group size.</small></label>}
      {options.knowledge_building_mode !== 'validated_first' && <p className="soft-note">Paper assessments appear as papers become ready. Compatible source text and local models are reused across runs. If a search service misses its deadline, available findings are saved and missing sources stay pending for resume.</p>}
      <div className="settings-section-title"><h3>Claim extraction</h3><p>Choose what each extraction call is responsible for.</p></div>
      <label>Extraction mode<select value={options.extraction_mode ?? 'target_context'}
        onChange={event => onOptions({ ...options, extraction_mode: event.target.value })}>
        <option value="target_context">Target / context (default)</option>
        <option value="whole_packet">Whole packet (previous approach)</option>
      </select><small>{options.extraction_mode === 'whole_packet'
        ? 'Select claims from anywhere in each reading packet.'
        : 'Extract from smaller assigned regions; read the surrounding packet for context. More calls may be needed.'}</small></label>
      {options.extraction_mode !== 'whole_packet' && <label>Target size (words)<input type="number" min={200} max={2000}
        value={options.extraction_target_words ?? 800}
        onChange={event => onOptions({ ...options, extraction_target_words: Number(event.target.value) })} />
        <small>Approximate maximum; sentence boundaries are preserved. Paragraphs stay together when they fit.</small></label>}
      <label>Generation budget (tokens)<input type="number" min={8192} max={32768} step={1024}
        value={options.extraction_generation_budget ?? 24576}
        onChange={event => onOptions({ ...options, extraction_generation_budget: Number(event.target.value) })} />
        <small>Shared by reasoning and the answer. A larger allowance can increase request time.</small></label>
      <div className="settings-section-title"><h3>Run limits</h3><p>Choose how much of this paper to process.</p></div>
      {[
        ['max_pages', 'PDF pages', 0, 1000, '0 = all text pages'],
        ['claims_per_packet', 'Claims per extraction call', 0, 8, '0 = all eligible claims; 1–8 = optional maximum per call'],
        ['max_claims', 'Claims to validate', 0, 500, '0 = all extracted claims'],
        ['articles_per_claim', 'Search papers per claim', 1, 30, 'All usable attached citations are additional'],
      ].map(([key, label, min, max, hint]) => <label key={key}>{label}<input type="number" min={min} max={max} value={options[key]}
        onChange={event => onOptions({ ...options, [key]: Number(event.target.value) })} /><small>{hint}</small></label>)}
      <div className="settings-section-title"><h3>Assigned models</h3><p>Each task uses its configured primary model. Thinking defaults to Off for every stage.</p></div>
      {THINKING_STAGES.map(([key, label]) => {
        const enabled = options.thinking?.[key] ?? false
        return <div className="assigned-model" role="group" aria-label={`${label} model settings`} key={key}>
          {key === 'external' && <label>Per-paper judgement model<input value={phases.paper || ''} readOnly placeholder="Loading model assignment…" /><small>Assesses each paper; Trust graph mode groups related claims within the request</small></label>}
          <label>{key === 'external' ? 'Cross-paper synthesis' : label} model<input value={tasks[key] || ''} readOnly title={tasks[key] || 'Model assignment loading'}
            placeholder="Loading model assignment…" /><small>Configured on the backend</small></label>
          <label className="thinking-toggle"><span>Thinking</span>
            <input type="checkbox" role="switch" aria-label={`${label} thinking`} aria-checked={enabled} checked={enabled}
              onChange={event => onOptions({ ...options, thinking: { ...defaultThinking(), ...options.thinking, [key]: event.target.checked } })} />
            <span className="thinking-state">{enabled ? 'On' : 'Off'}</span></label>
        </div>
      })}
      <p className="settings-note">Thinking and the answer share the generation budget. Off keeps the same evidence input. Changing extraction thinking requires a new run; other changed stages recheck their cached judgements on resume.</p>
      <p className="settings-note">{modelName(phases.paper)} judges each paper; {modelName(phases.synthesis || tasks.external)} synthesizes their findings and exact evidence. External Thinking controls both phases. Model errors appear in Activity. External timeout: {health?.pipeline?.external_timeout_seconds ?? 1200} seconds.</p>
    </div>}
    <p className="soft-note">Source fidelity checks the original PDF. External validation assesses retrieved research, contextual types, and pathway relevance. Its Thinking switch applies to both paper judgements and cross-paper synthesis.</p>
  </section>
}
