import { useEffect, useRef, useState } from 'react'
import {
  Activity, ArrowRight, BookOpen, Check, ChevronDown, CircleAlert, Clock3,
  ExternalLink, FileText, Filter, FolderOpen, GitBranch, Layers3, Leaf, LoaderCircle,
  Network, Play, Plus, Search, Settings2, Sparkles, UploadCloud, X,
} from 'lucide-react'
import GraphView from './GraphView.jsx'

const API = '/api'

async function request(path, options) {
  const response = await fetch(`${API}${path}`, options)
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try { detail = (await response.json()).detail || detail } catch { /* response has no JSON body */ }
    throw new Error(detail)
  }
  return response.json()
}

const statusLabels = {
  not_started: 'Ready to run', running: 'In progress', completed: 'Completed',
  blocked_text_quality: 'Blocked: text quality',
  extracted: 'Extracted', single_source: 'Independent article', corroborated: 'Independently corroborated',
  source_reference_supported: 'Source reference support',
  contested: 'Contested', contradicted: 'Contradicted', insufficient: 'Evidence not found', not_found: 'Evidence not found', error: 'Needs attention',
  rejected: 'Rejected', pipeline_error: 'Pipeline error', schema_rejected: 'Vocabulary check failed',
  world_supported_pending_corroboration: 'One article',
  world_corroborated_pending_review: 'Corroborated', world_contested_pending_review: 'Contested',
  world_independent_supported_pending_corroboration: 'Independent article',
  world_independently_corroborated_pending_review: 'Independently corroborated',
  source_reference_supported_pending_independent_corroboration: 'Source reference support',
  world_insufficient: 'Evidence not found', world_insufficient_after_retry: 'Evidence not found', external_evidence_not_found: 'Evidence not found', retrieval_failed_or_no_open_access_evidence: 'No evidence found',
  world_contradicted_or_contested: 'Contradicted',
  semantic_accepted: 'Semantically accepted', semantic_rejected: 'Semantic rejected', semantic_needs_review: 'Review semantics',
  source_entailed: 'Source supported', source_supported: 'Source supported', source_unsupported: 'Source unsupported', source_needs_review: 'Review source',
}

const colorClass = value => ({
  completed: 'positive', corroborated: 'positive', single_source: 'warning', source_reference_supported: 'warning',
  world_supported_pending_corroboration: 'warning', running: 'running',
  world_corroborated_pending_review: 'positive', world_contested_pending_review: 'negative',
  world_independent_supported_pending_corroboration: 'warning', world_independently_corroborated_pending_review: 'positive',
  source_reference_supported_pending_independent_corroboration: 'warning',
  contested: 'negative', contradicted: 'negative', world_contradicted_or_contested: 'negative',
  error: 'negative', pipeline_error: 'negative', schema_rejected: 'muted', rejected: 'muted',
  blocked_text_quality: 'negative',
  insufficient: 'muted', not_found: 'muted', world_insufficient: 'muted', world_insufficient_after_retry: 'muted', external_evidence_not_found: 'muted',
  semantic_accepted: 'warning', semantic_rejected: 'negative', semantic_needs_review: 'muted',
  source_entailed: 'warning', source_supported: 'warning', source_unsupported: 'negative', source_needs_review: 'muted',
}[value] || 'neutral')

const formatSize = size => `${(size / 1024 / 1024).toFixed(1)} MB`
const formatTime = value => value ? new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value)) : ''

function Empty({ icon: Icon, title, children }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={29} /></span><h3>{title}</h3><p>{children}</p></div>
}

function StatusPill({ value, children }) {
  return <span className={`status-pill ${colorClass(value)}`}><span className="status-dot" />{children || statusLabels[value] || value}</span>
}

const documentScopeCopy = {
  in_scope: {
    label: 'Pathway-relevant',
    detail: 'This paper can contribute evidence to the soil-to-human-health pathway.',
  },
  upstream_only: {
    label: 'Upstream evidence',
    detail: 'Retained for agriculture, soil, crop, or disease bridge evidence; it is not treated as a health-outcome paper.',
  },
  out_of_scope: {
    label: 'Outside project scope',
    detail: 'Extraction was skipped because no usable biological or chemical pathway evidence was found.',
  },
  uncertain: {
    label: 'Scope uncertain',
    detail: 'The paper was retained because the scope check could not decide safely.',
  },
}

const textQualityCopy = {
  usable: 'Text usable',
  degraded: 'Text quality degraded',
  unusable: 'Text unusable',
}

function DocumentScopeNotice({ documentScope }) {
  if (!documentScope) return null
  const scope = documentScope.scope || 'uncertain'
  const quality = documentScope.text_quality || 'usable'
  const copy = documentScopeCopy[scope] || documentScopeCopy.uncertain
  const qualityLabel = textQualityCopy[quality] || 'Text quality unknown'
  const isWarning = scope === 'out_of_scope' || quality === 'unusable'
  const Icon = isWarning || quality === 'degraded' ? CircleAlert : scope === 'uncertain' ? Filter : Check
  return <section className={`document-scope-notice scope-${scope} quality-${quality}`} aria-label="Document scope and text quality">
    <span className="document-scope-icon"><Icon size={18} /></span>
    <div className="document-scope-copy">
      <div className="document-scope-labels"><strong>{copy.label}</strong><span className="text-quality-badge">{qualityLabel}</span></div>
      <p>{documentScope.rationale || copy.detail}</p>
      {quality === 'degraded' && <small>The extracted PDF text contains broken words or characters; review source evidence carefully.</small>}
      {quality === 'unusable' && <small>A cleaner PDF or OCR is required before faithful extraction.</small>}
    </div>
  </section>
}

function ClaimCard({ record, index }) {
  const [expanded, setExpanded] = useState(false)
  const { candidate, validation } = record
  const decisions = validation?.attempts?.flatMap(attempt => attempt.decisions || []) || []
  const articles = validation?.attempts?.flatMap(attempt => attempt.evidence_documents || []) || []
  const semanticDecision = candidate.semantic_validation?.decision
  const sourceDecision = candidate.source_validation?.decision
  const resultStatus = candidate.schema_error ? 'schema_rejected' : candidate.scope_error ? 'rejected'
    : semanticDecision === 'rejected' ? 'semantic_rejected' : semanticDecision === 'needs_review' ? 'semantic_needs_review'
    : sourceDecision === 'unsupported' ? 'source_unsupported' : sourceDecision === 'needs_review' ? 'source_needs_review'
    : validation?.validation_status || (sourceDecision === 'entailed' ? 'source_entailed' : semanticDecision === 'accepted' ? 'semantic_accepted' : 'extracted')
  return <article className={`claim-card ${expanded ? 'expanded' : ''}`}>
    <button type="button" className="claim-main" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
      <span className="claim-number">{String(index + 1).padStart(2, '0')}</span>
      <span className="claim-content"><span className="claim-line"><strong>{candidate.subject}</strong><span className="relation-chip">{candidate.relation.replaceAll('_', ' ')}</span><strong>{candidate.object}</strong></span>
        <span className="claim-meta">Page {candidate.source_page} · {candidate.subject_type} → {candidate.object_type}</span></span>
      <StatusPill value={resultStatus} /><ChevronDown size={17} className={`chevron ${expanded ? 'open' : ''}`} />
    </button>
    {expanded && <div className="claim-expanded">
      {(candidate.source_evidence_span || candidate.source_evidence_quote) && <div className="quote-box"><span className="eyebrow">{candidate.source_context_packet ? `DIRECT EVIDENCE SPAN · SENTENCES ${(candidate.source_evidence_sentence_numbers || []).join(', ')}` : 'SOURCE CONTEXT'}</span><p>“{candidate.source_evidence_span || candidate.source_evidence_quote}”</p></div>}
      {candidate.source_context_packet && <details className="context-packet"><summary>Section context · {candidate.source_section_heading || 'Unlabelled paper text'} · pages {candidate.source_page_start}-{candidate.source_page_end}{candidate.source_context_estimated_tokens ? ` · ~${candidate.source_context_estimated_tokens} tokens` : ''}</summary><p>{candidate.source_context_packet}</p></details>}
      {candidate.schema_error && <p className="inline-error"><CircleAlert size={16} /> Vocabulary check: {candidate.schema_error}</p>}
      {candidate.scope_error && <p className="inline-error"><CircleAlert size={16} /> {candidate.scope_error}</p>}
      {candidate.semantic_validation && <p className="soft-note">Semantic gate: {statusLabels[`semantic_${candidate.semantic_validation.decision}`] || candidate.semantic_validation.decision}. {candidate.semantic_validation.rationale}{(candidate.semantic_validation.reasons || candidate.semantic_validation.guardrail_reasons)?.length ? ` ${(candidate.semantic_validation.reasons || candidate.semantic_validation.guardrail_reasons).join('; ')}` : ''}</p>}
      {candidate.source_validation && <p className="soft-note">Source check: {statusLabels[`source_${candidate.source_validation.decision}`] || candidate.source_validation.decision}. {candidate.source_validation.rationale}{(candidate.source_validation.reasons || candidate.source_validation.guardrail_reasons)?.length ? ` ${(candidate.source_validation.reasons || candidate.source_validation.guardrail_reasons).join('; ')}` : ''}</p>}
      {candidate.certainty && <p className="soft-note">Claim wording: {candidate.certainty}</p>}
      {!!candidate.source_references?.length && <details className="context-packet"><summary>Cited evidence candidates · {candidate.source_references.length} reference(s)</summary><div>{candidate.source_references.map(reference => <p key={reference.reference_number}><strong>[{reference.reference_number}]</strong> {reference.citation_text}</p>)}</div></details>}
      {validation?.error && <p className="inline-error"><CircleAlert size={16} /> {validation.error}</p>}
      {articles.length > 0 && <div className="evidence-list"><span className="eyebrow">EXTERNAL ARTICLES · {articles.length}</span>
        {articles.map((article, articleIndex) => {
          const decision = decisions.find(item => item.evidence_document_id === article.evidence_document_id)
          return <div className="evidence-item" key={`${article.evidence_document_id}-${articleIndex}`}>
            <div className="evidence-item-head"><span>{article.title?.replace(/<[^>]+>/g, '') || 'Untitled article'}</span><StatusPill value={decision?.decision || 'insufficient'}>{decision?.decision || 'pending'}</StatusPill></div>
            <span className="evidence-detail">{article.publication_year} · {article.doi ? <a href={`https://doi.org/${article.doi}`} target="_blank" rel="noreferrer">DOI {article.doi} <ExternalLink size={12} /></a> : 'No DOI'} · {article.evidence_level === 'source_reference' ? `source reference ${article.source_reference_number || ''}` : 'independent search'} · {article.evidence_access === 'abstract' ? 'abstract' : 'full text'}{article.open_access_license ? ` · ${article.open_access_license}` : ''}</span>
            {decision?.evidence?.[0]?.quoted_span && <blockquote>“{decision.evidence[0].quoted_span}”</blockquote>}
            {decision?.rationale && <p>{decision.rationale}</p>}
          </div>
        })}</div>}
      {!validation && !candidate.schema_error && !candidate.scope_error && semanticDecision !== 'rejected' && semanticDecision !== 'needs_review' && sourceDecision !== 'unsupported' && sourceDecision !== 'needs_review' && <p className="soft-note">External evaluation has not run for this claim yet.</p>}
    </div>}
  </article>
}

export default function App() {
  const [pdfs, setPdfs] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [health, setHealth] = useState(null)
  const [graph, setGraph] = useState({ nodes: [], edges: [], counts: { nodes: 0, edges: 0, corroborated: 0 } })
  const [claims, setClaims] = useState([])
  const [paths, setPaths] = useState([])
  const [logs, setLogs] = useState([])
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [search, setSearch] = useState('')
  const [view, setView] = useState('graph')
  const [graphScope, setGraphScope] = useState('paper')
  const [graphMode, setGraphMode] = useState('main')
  const [filter, setFilter] = useState('all')
  const [selectedElement, setSelectedElement] = useState(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [options, setOptions] = useState({ max_pages: 0, max_claims: 0, articles_per_claim: 3, claims_per_packet: 4 })
  const [message, setMessage] = useState(null)
  const uploadRef = useRef(null)
  const logEndRef = useRef(null)

  const selected = pdfs.find(item => item.id === selectedId)
  const filteredPdfs = pdfs.filter(item => item.title.toLowerCase().includes(search.toLowerCase()))
  const isRunning = job?.status === 'running' || job?.status === 'queued'

  async function refreshPdfs() {
    const items = await request('/pdfs')
    setPdfs(items)
    setSelectedId(current => current && items.some(item => item.id === current) ? current : items[0]?.id || null)
    return items
  }

  async function refreshDetails(pdfId = selectedId, scope = graphScope, mode = graphMode) {
    if (!pdfId) return
    const graphPath = mode === 'audit'
      ? (scope === 'all' ? '/audit-graph' : `/pdfs/${pdfId}/audit-graph`)
      : mode === 'source'
        ? (scope === 'all' ? '/source-graph' : `/pdfs/${pdfId}/source-graph`)
        : (scope === 'all' ? '/graph' : `/pdfs/${pdfId}/graph`)
    const [graphData, claimData, logData, pathData] = await Promise.all([
      request(graphPath),
      request(`/pdfs/${pdfId}/claims`), request(`/pdfs/${pdfId}/logs`), request(`/pdfs/${pdfId}/paths`),
    ])
    setGraph(graphData)
    setClaims(claimData.claims)
    setLogs(logData)
    setPaths(pathData)
  }

  useEffect(() => {
    refreshPdfs().catch(error => setMessage(error.message))
    request('/health').then(data => {
      setHealth(data)
    }).catch(() => setHealth({ extraction: { ready: false }, validation: { ready: false } }))
  }, [])

  useEffect(() => {
    if (!selectedId) return
    setSelectedElement(null)
    setFilter('all')
    refreshDetails(selectedId, graphScope, graphMode).catch(error => setMessage(error.message))
    const existingJob = pdfs.find(item => item.id === selectedId)?.active_job_id
    if (existingJob) setJobId(existingJob)
  }, [selectedId, graphScope, graphMode])

  useEffect(() => {
    if (!jobId) return
    let cancelled = false
    const poll = async () => {
      try {
        const current = await request(`/jobs/${jobId}`)
        if (cancelled) return
        setJob(current)
        if (current.pdf_id === selectedId) setLogs(current.logs)
        if (current.status === 'running' || current.status === 'queued') {
          await refreshPdfs()
          if (current.pdf_id === selectedId) refreshDetails(selectedId, graphScope, graphMode).catch(() => {})
        } else {
          await refreshPdfs()
          if (current.pdf_id === selectedId) await refreshDetails(selectedId, graphScope, graphMode)
          if (current.status === 'failed') setMessage(current.error || 'Pipeline failed')
          clearInterval(timer)
        }
      } catch (error) { if (!cancelled) setMessage(error.message) }
    }
    poll()
    const timer = setInterval(poll, 2000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [jobId, selectedId, graphScope, graphMode])

  useEffect(() => { logEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [logs.length])

  async function startRun() {
    if (!selected) return
    setMessage(null)
    try {
      const started = await request(`/pdfs/${selected.id}/run`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(options) })
      setJob(started)
      setJobId(started.id)
      setLogs([])
      setView('graph')
    } catch (error) { setMessage(error.message) }
  }

  async function upload(event) {
    const file = event.target.files?.[0]
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    try {
      const result = await request('/pdfs', { method: 'POST', body: form })
      await refreshPdfs()
      setSelectedId(result.id)
      setMessage(`${result.name} is ready to run`)
    } catch (error) { setMessage(error.message) }
    event.target.value = ''
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Leaf size={22} strokeWidth={2.3} /></span><span><strong>Crop Health</strong><small>RESEARCH WORKBENCH</small></span></div>
      <div className="sidebar-section-header"><span className="eyebrow">YOUR LIBRARY</span><span className="count-badge">{pdfs.length}</span></div>
      <div className="search-field"><Search size={17} /><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Search papers" aria-label="Search papers" /></div>
      <div className="pdf-list">
        {filteredPdfs.map(pdf => <button type="button" key={pdf.id} className={`pdf-card ${selectedId === pdf.id ? 'selected' : ''}`} onClick={() => { setSelectedId(pdf.id); setView('graph') }}>
          <span className="pdf-icon"><FileText size={19} /></span><span className="pdf-card-copy"><strong>{pdf.title}</strong><small>{formatSize(pdf.size_bytes)} <span>·</span> {pdf.summary?.extracted ?? 'New'} claims</small></span>
          <span className={`pdf-state-dot ${pdf.status}`} /></button>)}
        {!filteredPdfs.length && <div className="sidebar-empty">No papers match your search.</div>}
      </div>
      <div className="sidebar-footer"><button className="upload-card" type="button" onClick={() => uploadRef.current?.click()}><span><Plus size={18} /></span><div><strong>Add a PDF</strong><small>Upload a research paper</small></div></button>
        <input ref={uploadRef} type="file" accept="application/pdf,.pdf" hidden onChange={upload} />
        <div className="local-status"><span className={`connection-dot ${health?.extraction?.ready && health?.validation?.ready ? 'online' : ''}`} /><span>{health?.extraction?.ready && health?.validation?.ready ? 'OpenCode models ready' : 'OpenCode model unavailable'}</span><strong>LongCat → Step 5</strong></div>
      </div>
    </aside>

    <main className="main-area">
      <header className="topbar"><div className="breadcrumbs"><span>Workspace</span><ArrowRight size={14} /><strong>{selected ? selected.title : 'Overview'}</strong></div><div className="topbar-right"><span className="topbar-date">CROP → HUMAN HEALTH</span><span className="avatar">CH</span></div></header>
      <div className="page-content">
        {message && <div className="toast" role="alert"><CircleAlert size={17} /><span>{message}</span><button type="button" onClick={() => setMessage(null)}><X size={16} /></button></div>}
        {!selected ? <Empty icon={FolderOpen} title="Your research workspace is ready">Add a PDF to begin extracting and validating claims.</Empty> : <>
          <div className="page-heading"><div><span className="eyebrow accent">PAPER WORKSPACE</span><h1>{selected.title}</h1><p>Extract claims, examine research evidence, and trace the resulting knowledge graph.</p></div>
            <div className="heading-actions"><a className="button secondary" href={`${API}/pdfs/${selected.id}/file`} target="_blank" rel="noreferrer"><BookOpen size={17} /> View PDF</a><button className="button primary" type="button" disabled={isRunning || !!pdfs.find(item => item.status === 'running')} onClick={startRun}>{isRunning ? <LoaderCircle className="spin" size={17} /> : <Play size={16} fill="currentColor" />}{isRunning ? 'Running pipeline' : selected.status === 'completed' ? 'Run again' : selected.status === 'blocked_text_quality' ? 'Retry pipeline' : 'Run pipeline'}</button></div>
          </div>

          <div className="overview-grid">
            <div className="overview-card highlighted"><div className="metric-icon green"><Layers3 size={21} /></div><div><span className="eyebrow">EXTRACTED CLAIMS</span><strong>{selected.summary?.extracted ?? claims.length}</strong><small>From this source PDF</small></div></div>
            <div className="overview-card"><div className="metric-icon sand"><Check size={21} /></div><div><span className="eyebrow">SOURCE SUPPORTED</span><strong>{selected.summary?.source_entailed ?? (claims.some(item => item.candidate.source_validation) ? claims.filter(item => item.candidate.source_validation?.decision === 'entailed').length : '—')}</strong><small>{selected.summary && selected.summary.semantic_accepted == null ? 'Legacy run: rerun for semantic and source checks' : `${selected.summary?.semantic_accepted ?? 0} passed the semantic gate`}</small></div></div>
            <div className="overview-card"><div className="metric-icon plum"><Network size={21} /></div><div><span className="eyebrow">GRAPH RELATIONS</span><strong>{selected.summary?.graph?.edges ?? graph?.counts?.edges ?? 0}</strong><small>{selected.summary?.graph?.corroborated ?? 0} corroborated</small></div></div>
            <div className="overview-card"><div className="metric-icon blue"><Activity size={21} /></div><div><span className="eyebrow">RUN STATUS</span><strong className="metric-status">{statusLabels[selected.status]}</strong><small>{selected.summary?.completed_at ? `Last run ${new Date(selected.summary.completed_at).toLocaleDateString()}` : 'Waiting for first run'}</small></div></div>
          </div>

          <DocumentScopeNotice documentScope={selected.document_scope || selected.summary?.document_scope} />

          <section className="workflow-card"><div className="section-head compact"><div><span className="eyebrow">PIPELINE</span><h2>From paper to evidence</h2></div><button type="button" className="text-button" onClick={() => setSettingsOpen(!settingsOpen)}><Settings2 size={16} /> Run settings <ChevronDown size={15} className={settingsOpen ? 'chevron open' : 'chevron'} /></button></div>
            <div className="steps"><div className="step"><span className="step-icon"><FileText size={19} /></span><span><strong>Extract</strong><small>LongCat reads rich PDF context</small></span></div><ArrowRight size={17} className="step-arrow" /><div className="step"><span className="step-icon"><Filter size={19} /></span><span><strong>Semantic gate</strong><small>Step 5 checks scope &amp; types</small></span></div><ArrowRight size={17} className="step-arrow" /><div className="step"><span className="step-icon"><Check size={19} /></span><span><strong>Check source</strong><small>Step 5 vs PDF evidence</small></span></div><ArrowRight size={17} className="step-arrow" /><div className="step"><span className="step-icon"><Sparkles size={19} /></span><span><strong>Validate</strong><small>Step 5 + hybrid search</small></span></div><ArrowRight size={17} className="step-arrow" /><div className="step"><span className="step-icon"><GitBranch size={19} /></span><span><strong>Map graph</strong><small>Evidence states</small></span></div></div>
            {settingsOpen && <div className="run-settings"><label>Extraction model <input value={health?.extraction?.model || 'opencode/longcat-2.5-preview-free'} readOnly /><small>OpenCode LongCat; receives ~4,000-word evidence packets.</small></label><label>Evidence-validation model <input value={health?.validation?.model || 'opencode/step-5-preview-free'} readOnly /><small>OpenCode Step 5 independently checks scope, source and external evidence.</small></label><label>PDF pages <input type="number" min="0" max="1000" value={options.max_pages} onChange={event => setOptions({ ...options, max_pages: Number(event.target.value) })} /><small>0 = all pages</small></label><label>Claims per packet <input type="number" min="1" max="8" value={options.claims_per_packet} onChange={event => setOptions({ ...options, claims_per_packet: Number(event.target.value) })} /><small>Compare 2, 4, or 6 during evaluation</small></label><label>Claims to validate <input type="number" min="0" max="500" value={options.max_claims} onChange={event => setOptions({ ...options, max_claims: Number(event.target.value) })} /><small>0 = all claims</small></label><label>Independent articles per claim <input type="number" min="1" max="8" value={options.articles_per_claim} onChange={event => setOptions({ ...options, articles_per_claim: Number(event.target.value) })} /><small>One cited lead is checked separately and does not consume this budget</small></label></div>}
            <p className="soft-note">The vocabulary check only verifies approved entity and relation labels. Scientific correctness is assessed by the semantic, source, and external-evidence stages.</p>
          </section>

          <div className="content-grid"><section className="analysis-panel"><div className="section-head"><div><span className="eyebrow">ANALYSIS</span><h2>{view === 'graph' ? 'Knowledge graph' : view === 'claims' ? 'Extracted claims' : 'Reasoning paths'}</h2></div><StatusPill value={selected.status} /></div>
            <div className="tabs"><button type="button" className={view === 'graph' ? 'active' : ''} onClick={() => setView('graph')}><Network size={17} /> Graph</button><button type="button" className={view === 'claims' ? 'active' : ''} onClick={() => setView('claims')}><Layers3 size={17} /> Claims <span>{claims.length}</span></button><button type="button" className={view === 'paths' ? 'active' : ''} onClick={() => setView('paths')}><GitBranch size={17} /> Paths <span>{paths.length}</span></button></div>
              {view === 'graph' && <><div className="graph-controls"><div className="segmented"><button type="button" className={graphMode === 'main' ? 'active' : ''} onClick={() => setGraphMode('main')}>External graph</button><button type="button" className={graphMode === 'source' ? 'active' : ''} onClick={() => setGraphMode('source')}>Source-supported</button><button type="button" className={graphMode === 'audit' ? 'active' : ''} onClick={() => setGraphMode('audit')}>Audit view</button></div><div className="segmented"><button type="button" className={graphScope === 'paper' ? 'active' : ''} onClick={() => setGraphScope('paper')}>This paper</button><button type="button" className={graphScope === 'all' ? 'active' : ''} onClick={() => setGraphScope('all')}>All papers</button></div><select value={filter} onChange={event => setFilter(event.target.value)} aria-label="Filter graph edges"><option value="all">All evidence states</option>{graphMode === 'main' && <><option value="corroborated">Independently corroborated</option><option value="single_source">Independent article</option></>}{graphMode === 'source' && <option value="source_supported">Source supported</option>}{graphMode === 'audit' && <><option value="rejected">Rejected</option><option value="semantic_rejected">Semantic rejected</option><option value="semantic_needs_review">Review semantics</option><option value="source_unsupported">Source unsupported</option><option value="source_needs_review">Review source</option><option value="source_reference_supported">Source reference support</option><option value="extracted">Awaiting validation</option><option value="not_found">External evidence not found</option><option value="contested">Contested</option><option value="contradicted">Contradicted</option></>}</select></div>
              <GraphView graph={graph} activeFilter={filter} onSelect={setSelectedElement} />
              {graphMode === 'main' && graphScope === 'paper' && selected.status === 'completed' && <div className="graph-export"><a href={`${API}/pdfs/${selected.id}/graph.graphml`} download><ExternalLink size={13} /> Download GraphML</a></div>}
              {selectedElement && <div className="graph-selection"><button type="button" className="close-mini" onClick={() => setSelectedElement(null)}><X size={16} /></button><span className="eyebrow">{selectedElement.kind === 'edge' ? 'SELECTED RELATION' : 'SELECTED ENTITY'}</span><strong>{selectedElement.kind === 'edge' ? `${selectedElement.source?.split(':').slice(1).join(':')} → ${selectedElement.relation} → ${selectedElement.target?.split(':').slice(1).join(':')}` : selectedElement.label}</strong><p>{selectedElement.kind === 'edge' ? `${statusLabels[selectedElement.status] || selectedElement.status} · ${selectedElement.support_count} supporting article(s) · ${selectedElement.paper}` : selectedElement.type}</p>{selectedElement.rejection_reason && <p>{selectedElement.rejection_reason}</p>}{selectedElement.quote && <blockquote>“{selectedElement.quote}”</blockquote>}</div>}
              <div className="graph-legend">{graphMode === 'main' ? <><span><i className="legend-dot corroborated" />Independently corroborated</span><span><i className="legend-dot single" />Independent article</span></> : graphMode === 'source' ? <span><i className="legend-dot single" />Faithful to source PDF</span> : <><span><i className="legend-dot extracted" />Awaiting validation or source-reference only</span><span><i className="legend-dot contested" />Contested</span><span><i className="legend-dot insufficient" />External evidence not found</span></>}</div></>}
            {view === 'claims' && <div className="claim-list">{claims.length ? claims.map((item, index) => <ClaimCard key={item.candidate.candidate_id} record={item} index={index} />) : <Empty icon={Layers3} title="No claims extracted yet">Run the PDF to see every claim, source quote, and article decision here.</Empty>}</div>}
            {view === 'paths' && <div className="paths-list">{paths.length ? paths.map((path, index) => <div className="path-card" key={index}><span className="eyebrow">PATH {String(index + 1).padStart(2, '0')}</span><div>{path.map((edge, edgeIndex) => <span key={edge.id}>{edgeIndex === 0 && <strong>{edge.source.split(':').slice(1).join(':')}</strong>}<em>{edge.relation.replaceAll('_', ' ')}</em><strong>{edge.target.split(':').slice(1).join(':')}</strong></span>)}</div></div>) : <Empty icon={GitBranch} title="No supported reasoning paths yet">Paths appear when corroborated relations connect an agricultural factor to a human-health outcome.</Empty>}</div>}
          </section>

          <aside className="activity-panel"><div className="section-head"><div><span className="eyebrow">LIVE PROCESS</span><h2>Activity log</h2></div><span className={`live-indicator ${isRunning ? 'on' : ''}`}><span />{isRunning ? 'LIVE' : 'IDLE'}</span></div><div className="activity-summary"><Clock3 size={16} /><span>{isRunning ? 'Pipeline is running. Updates appear here automatically.' : logs.length ? `${logs.length} events in the latest run` : 'Waiting for first run'}</span></div><div className="log-list">{logs.length ? logs.map((entry, index) => <div className={`log-entry ${entry.level}`} key={`${entry.at}-${index}`}><span className="log-rail"><i /></span><div><span className="log-time">{formatTime(entry.at)}</span><p>{entry.message}</p></div></div>) : <div className="log-empty"><Activity size={27} /><strong>Every step, in one place</strong><p>Start a run to follow extraction, evidence search, decisions, and graph building.</p></div>}<div ref={logEndRef} /></div></aside></div>
        </>}
      </div>
    </main>
  </div>
}
