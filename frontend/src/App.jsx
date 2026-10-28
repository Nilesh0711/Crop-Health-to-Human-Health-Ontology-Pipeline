import { defaultThinking, resumeWithThinking } from './lib/thinking.js'
import { useEffect, useRef, useState } from 'react'
import {
  Activity, ArrowRight, BookOpen, Check, ChevronDown, CircleAlert,
  ExternalLink, FileText, Filter, FolderOpen, GitBranch, Layers3, Leaf, LoaderCircle,
  Network, Play, Plus, Search, Settings2, X,
} from 'lucide-react'
import GraphView from './components/graph/GraphView.jsx'
import PathwayPanel from './components/graph/PathwayPanel.jsx'
import PathwayNavigation from './components/graph/PathwayNavigation.jsx'
import { CorroboratedPaths } from './components/graph/PathwayCards.jsx'
import TrustEvidence from './components/claims/TrustEvidence.jsx'
import { CitationTracking, JointAssessment, ClaimActions, ClaimContext, ValidationTrace, ExternalDecisionDetails, ExtractionDiscards } from './components/claims/ClaimDetails.jsx'
import ModelPerformance from './components/diagnostics/ModelPerformance.jsx'
import { watchJob } from './lib/jobPolling.js'
import { canExecute as executionAllowed, isViewer } from './lib/capabilities.js'
import SavedRunDetails from './components/workspace/SavedRunDetails.jsx'
import RunRecoveryNotice from './components/workspace/RunRecoveryNotice.jsx'
import { PdfQualityReport, ActivityLog, FindingsToolbar, PaperRunStatus, ResumeRunButton, StopRunButton, RunSetup, SidebarToggle, WorkspaceNavigation } from './components/workspace/WorkbenchPanels.jsx'

const API = '/api'

async function request(path, options) {
  const response = await fetch(`${API}${path}`, options)
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try { detail = (await response.json()).detail || detail } catch { /* response has no JSON body */ }
    const error = new Error(detail)
    error.status = response.status
    throw error
  }
  return response.json()
}

const statusLabels = {
  not_started: 'Ready to run', running: 'In progress', completed: 'Completed',
  completed_with_warnings: 'Completed with warnings', failed: 'Run failed', stopping: 'Stopping', stopped: 'Stopped',
  external_in_progress: 'External check in progress',
  retrieval_pending: 'Retrieval incomplete - resume missing sources',
  external_support_needs_review: 'Support found - review needed',
  external_assessment_needs_review: 'Evidence assessment needs review',
  external_needs_review: 'Review external evidence', out_of_scope: 'Outside project scope',
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
  retrieval_pending: 'warning',
  external_support_needs_review: 'warning', external_assessment_needs_review: 'warning', external_needs_review: 'warning',
  completed_with_warnings: 'warning', failed: 'negative', stopping: 'running', stopped: 'muted',
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
const formatDuration = value => value == null ? 'Not recorded' : value >= 60 ? `${(value / 60).toFixed(1)} min` : `${Math.round(value)} sec`

function claimStatus(record) {
  const { candidate, validation } = record
  const semanticDecision = candidate.semantic_validation?.decision
  const sourceDecision = candidate.source_validation?.decision
  return candidate.schema_error ? 'schema_rejected' : candidate.scope_error ? 'rejected'
    : semanticDecision === 'rejected' ? 'semantic_rejected' : semanticDecision === 'needs_review' ? 'semantic_needs_review'
    : sourceDecision === 'unsupported' ? 'source_unsupported' : sourceDecision === 'needs_review' ? 'source_needs_review'
    : validation?.validation_status || (sourceDecision === 'entailed' ? 'source_entailed' : semanticDecision === 'accepted' ? 'semantic_accepted' : 'extracted')
}

const supportedStatuses = new Set(['single_source', 'corroborated', 'world_supported_pending_corroboration',
  'world_corroborated_pending_review', 'world_independent_supported_pending_corroboration',
  'world_independently_corroborated_pending_review'])
const rejectedStatuses = new Set(['schema_rejected', 'rejected', 'semantic_rejected', 'source_unsupported',
  'contradicted', 'world_contradicted_or_contested'])
const claimGroup = record => {
  const status = claimStatus(record)
  if (supportedStatuses.has(status)) return 'supported'
  if (rejectedStatuses.has(status)) return 'rejected'
  return 'review'
}

export function matchesClaim(record, query) {
  const needle = query.trim().toLocaleLowerCase()
  if (!needle) return true
  const candidate = record.candidate
  const paperTitles = record.validation?.attempts?.flatMap(attempt => attempt.evidence_documents?.map(paper => paper.title) || []) || []
  return [candidate.subject, candidate.object, candidate.relation?.replaceAll('_', ' '), candidate.claim_text,
    candidate.source_evidence_span, candidate.candidate_id, ...Object.values(candidate.qualifiers || {}), ...paperTitles].filter(Boolean).join(' ').toLocaleLowerCase().includes(needle)
}

export function initialWorkspaceView(hash) {
  const view = String(hash || '').replace(/^#/, '')
  return ['claims', 'graph', 'paths', 'activity', 'details'].includes(view) ? view : 'claims'
}

function RunBreakdown({ summary }) {
  if (!summary || summary.extracted == null) return null
  const stages = [
    ['Extracted', summary.extracted],
    ['Vocabulary', summary.schema_valid],
    ['Source evidence', summary.source_entailed],
    ['External support', summary.graph?.supported],
  ]
  const max = Math.max(1, summary.extracted)
  const calls = summary.llm_calls
  const requestSeconds = Object.keys(summary.task_models || { extraction: null, validation: null, helper: null })
    .reduce((seconds, task) => seconds + (calls?.[task]?.elapsed_seconds ?? 0), 0)
  const coverageUnit = summary.extraction_coverage?.unit || 'packets'
  return <section className="run-breakdown" aria-label="Latest run breakdown">
    {summary.extraction_coverage?.complete === false && <div className="extraction-coverage-warning" role="alert"><strong>Partial extraction: {summary.extraction_coverage.completed_packets}/{summary.extraction_coverage.total_packets} {coverageUnit} completed</strong><p>Claims in missing regions were not extracted or evaluated. The counts below do not describe the whole paper.</p><details><summary>Inspect incomplete regions</summary>{summary.extraction_coverage.failed_packets?.map(item => <p key={item.target_id || item.packet_number}>{item.target_id ? `Target ${item.target_number}` : `Packet ${item.packet_number}`}{item.page_start != null ? ` · pages ${item.page_start}–${item.page_end}` : ''} · {item.status}: {item.error || 'Extraction did not complete'}</p>)}</details></div>}
    <div className="run-breakdown-heading"><div><span className="eyebrow">LATEST RUN</span><h2>Where claims stand</h2></div><span className="run-version">{summary.schema_version || 'Legacy run'}</span></div>
    <div className="run-stage-grid">{stages.map(([label, count], index) => <div className="run-stage" key={label}>
      <span className="run-stage-label">{label}</span><strong>{count ?? '—'}</strong>
      <span className="run-stage-track"><span style={{ width: `${Math.max(0, Math.min(100, ((count || 0) / max) * 100))}%` }} /></span>
      {index < stages.length - 1 && <ArrowRight size={14} className="run-stage-arrow" />}
    </div>)}</div>
    <div className="run-breakdown-footer"><span>{summary.semantic_stage === 'removed' ? 'Joint evidence assessment' : (summary.semantic_needs_review ?? 0) + ' semantic review'} · {summary.source_needs_review ?? 0} source review · {summary.audit_graph?.edges ?? 0} audit edges</span>
      {calls && <span title="Sum of individual model-request durations, not wall-clock runtime">OpenCode calls: {calls.total_requests ?? '—'} · extraction {calls.extraction?.requests ?? '—'} / validation {calls.validation?.requests ?? '—'}{calls.helper?.requests ? ` / helper ${calls.helper.requests}` : ''} · request time {formatDuration(requestSeconds)}</span>}
    </div>
    {summary.extraction_mode && <p className="run-breakdown-note">Extraction: {summary.extraction_mode === 'target_context' ? 'Target / context' : 'Whole packet'}
      {' · '}{summary.claims_per_packet ? `${summary.claims_per_packet} claims maximum per call` : 'All eligible claims per call'}
      {summary.extraction_generation_budget ? ` · ${summary.extraction_generation_budget.toLocaleString()} generation tokens per initial call` : ''}.
      Completed regions measure processing coverage, not exhaustive claim recall.</p>}
    <p className="run-breakdown-note">External evidence not found means the search did not establish support; it does not prove the claim false. Only independently supported claims enter the external graph.</p>
    {summary.stage_timings_seconds && <details className="context-packet"><summary>Runtime breakdown · {formatDuration(summary.wall_elapsed_seconds)} total</summary><div>{Object.entries(summary.stage_timings_seconds).map(([stage, seconds]) => <p key={stage}>{stage.replaceAll('_', ' ')}: {formatDuration(seconds)}</p>)}<p>{summary.canonical_duplicates_removed ?? 0} duplicate claims merged · {summary.retrieval_cache?.hits ?? 0} retrieval cache hits · batches of {summary.validation_batch_size}</p></div></details>}
  </section>
}

function Empty({ icon: Icon, title, children }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={29} /></span><h3>{title}</h3><p>{children}</p></div>
}

function StatusPill({ value, children }) {
  return <span className={`status-pill ${colorClass(value)}`}><span className="status-dot" />{children || statusLabels[value] || value}</span>
}

const documentScopeCopy = {
  not_screened: { label: 'Text quality checked', detail: 'Parsed text passed the readability check. Original passages remain available alongside each claim.' },
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
      <p>{scope === 'not_screened' ? quality === 'usable' ? copy.detail : quality === 'unusable' ? 'Too little readable text was extracted for claim validation.' : 'Parsed text contains quality warnings. Review the original PDF alongside each passage.' : documentScope.rationale || copy.detail}</p>
      {quality === 'degraded' && <small>The extracted PDF text contains broken words or characters; review source evidence carefully.</small>}
      {quality === 'unusable' && <small>A cleaner PDF or OCR is required before faithful extraction.</small>}
    </div>
  </section>
}

export function ClaimCard({ record, index, busy, onClaimAction, defaultExpanded = false, readOnly = false }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const { candidate, validation } = record
  const decisions = validation?.attempts?.flatMap(attempt => attempt.decisions || []) || []
  const articles = validation?.attempts?.flatMap(attempt => attempt.evidence_documents || []) || []
  const semanticDecision = candidate.semantic_validation?.decision
  const sourceDecision = candidate.source_validation?.decision
  const resultStatus = claimStatus(record)
  const preview = candidate.schema_error || validation?.error
    || (['unsupported', 'needs_review'].includes(sourceDecision) ? candidate.source_validation.rationale : '')
    || candidate.claim_text || candidate.source_evidence_span || ''
  return <article className={`claim-card ${expanded ? 'expanded' : ''}`}>
    <button type="button" className="claim-main" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
      <span className="claim-number">{String(index + 1).padStart(2, '0')}</span>
      <span className="claim-content"><span className="claim-line"><strong>{candidate.subject}</strong><span className="relation-chip">{candidate.relation.replaceAll('_', ' ')}</span><strong>{candidate.object}</strong></span>
        <span className="claim-meta">Page {candidate.source_page ?? '—'} · {candidate.subject_type} → {candidate.object_type}</span>
        {preview && <span className="claim-preview">{preview}</span>}</span>
      <StatusPill value={resultStatus} /><ChevronDown size={17} className={`chevron ${expanded ? 'open' : ''}`} />
    </button>
    {expanded && <div className="claim-expanded">
      <ClaimActions readOnly={readOnly} candidate={candidate} validation={validation} repairEligibility={record.repair_eligibility} revisions={record.revisions}
        busy={busy} onAction={action => onClaimAction(candidate.candidate_id, action)} />
      <TrustEvidence trust={record.trust || validation?.trust} />
      <ClaimContext candidate={candidate} />
      <ValidationTrace candidate={candidate} validation={validation} />
      <JointAssessment validation={validation} />
      <CitationTracking candidate={candidate} validation={validation} />
      {(candidate.source_evidence_span || candidate.source_evidence_quote) && <div className="quote-box"><span className="eyebrow">{candidate.source_context_packet ? `DIRECT EVIDENCE ${candidate.source_evidence_is_contiguous === false ? 'FRAGMENTS' : 'SPAN'} · SENTENCES ${(candidate.source_evidence_sentence_numbers || []).join(', ')}` : 'SOURCE CONTEXT'}</span>{candidate.source_evidence_is_contiguous === false ? <>
        <p>These source sentences are separated in the PDF. Their connection requires source validation.</p>
        {candidate.source_evidence_fragments?.map(fragment => <div key={fragment.sentence_number}><small>Sentence {fragment.sentence_number} · PDF page {fragment.page}</small><blockquote>{fragment.text}</blockquote></div>)}
        <details><summary>Intervening source text · retained for review</summary>{candidate.source_intervening_context?.map(fragment => <p key={fragment.sentence_number}>[{fragment.sentence_number}] Page {fragment.page}: {fragment.text}</p>)}</details>
      </> : <p>“{candidate.source_evidence_span || candidate.source_evidence_quote}”</p>}</div>}
      {candidate.source_context_packet && <details className="context-packet"><summary>Section context · {candidate.source_section_heading || 'Unlabelled paper text'} · pages {candidate.source_page_start}-{candidate.source_page_end}{candidate.source_context_estimated_tokens ? ` · ~${candidate.source_context_estimated_tokens} tokens` : ''}</summary><p>{candidate.source_context_packet}</p></details>}
      {candidate.schema_error && <p className="inline-error"><CircleAlert size={16} /> Vocabulary check: {candidate.schema_error}</p>}
      {candidate.scope_error && <p className="inline-error"><CircleAlert size={16} /> {candidate.scope_error}</p>}
      {candidate.semantic_validation?.type_disagreement && <p className="decision-note">Type disagreement recorded: extractor chose {candidate.semantic_validation.type_disagreement.extracted?.subject_type} → {candidate.semantic_validation.type_disagreement.extracted?.object_type}; classifier suggested {candidate.semantic_validation.type_disagreement.classified?.subject_type} → {candidate.semantic_validation.type_disagreement.classified?.object_type}. Source evidence still decides whether the claim is faithful.</p>}
      {candidate.semantic_validation?.normalization_applied && <p className="decision-note">Entity types normalized after semantic review; original types are retained in the candidate record.</p>}
      {candidate.source_context && <p className="soft-note"><strong>Source setting:</strong> {candidate.source_context} (provenance, not an external-study requirement)</p>}
      {!!candidate.qualifier_adjustments?.length && <details className="context-packet"><summary>Qualifier adjustments · {candidate.qualifier_adjustments.length}</summary><div>{candidate.qualifier_adjustments.map((adjustment, adjustmentIndex) => <p key={adjustmentIndex}>{adjustment.field}: {adjustment.reason || adjustment.action || 'Adjusted during extraction'}</p>)}</div></details>}
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
            <ExternalDecisionDetails decision={decision} />
            {article.passage_selection && <details className="context-packet"><summary>Retrieved passages / {article.passage_selection.retained_chunks}/{article.passage_selection.candidate_chunks}</summary>
              <p>Ranking claim: {article.passage_selection.query}</p><p>Neutral exposure/outcome query: {article.passage_selection.neutral_query}</p>
              {article.evidence_chunks?.map(chunk => <div key={chunk.chunk_id}><strong>{chunk.section_heading || chunk.chunk_id}</strong><p>{chunk.text}</p><small>Dense: {chunk.dense_score} / Cross-encoder: {chunk.cross_encoder_score} / Neutral: {chunk.neutral_cross_encoder_score}</small></div>)}
            </details>}
          </div>
        })}</div>}
      {!validation && !candidate.schema_error && !candidate.scope_error && semanticDecision !== 'rejected' && semanticDecision !== 'needs_review' && sourceDecision !== 'unsupported' && sourceDecision !== 'needs_review' && <p className="soft-note">External evaluation has not run for this claim yet.</p>}
    </div>}
  </article>
}

export default function App() {
  const [pdfs, setPdfs] = useState([])
  const [selectedId, setSelectedId] = useState(() => typeof window === 'undefined' ? null : new URLSearchParams(window.location.search).get('paper'))
  const [health, setHealth] = useState(null)
  const [savedJobs, setSavedJobs] = useState([])
  const readOnly = isViewer(health)
  const canExecute = executionAllowed(health)
  const [graph, setGraph] = useState({ nodes: [], edges: [], counts: { nodes: 0, edges: 0, corroborated: 0 } })
  const [claims, setClaims] = useState([])
  const [discards, setDiscards] = useState([])
  const [modelRequests, setModelRequests] = useState([])
  const [paths, setPaths] = useState([])
  const [logs, setLogs] = useState([])
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [search, setSearch] = useState('')
  const [libraryOpen, setLibraryOpen] = useState(false)
  const [sidebarHidden, setSidebarHidden] = useState(() => {
    try { return typeof window !== 'undefined' && window.localStorage.getItem('soil-health.sidebar-hidden') === 'true' }
    catch { return false }
  })
  const [view, setView] = useState(() => initialWorkspaceView(typeof window === 'undefined' ? '' : window.location.hash))
  const [graphScope, setGraphScope] = useState('paper')
  const [graphMode, setGraphMode] = useState('main')
  const [filter, setFilter] = useState('all')
  const [claimFilter, setClaimFilter] = useState('all')
  const [claimSearch, setClaimSearch] = useState('')
  const [selectedElement, setSelectedElement] = useState(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [options, setOptions] = useState({ knowledge_building_mode: 'trust_graph', trust_batch_size: 3, max_pages: 0, max_claims: 0, articles_per_claim: 8, claims_per_packet: 0,
    extraction_mode: 'target_context', extraction_target_words: 800, extraction_generation_budget: 24576, thinking: defaultThinking() })
  const [trustData, setTrustData] = useState(null)
  const [pathwayData, setPathwayData] = useState(null)
  const [pathView, setPathView] = useState('assembled')
  const [message, setMessage] = useState(null)
  const [recoveryEvent, setRecoveryEvent] = useState(null)
  const [recoveryInfo, setRecoveryInfo] = useState(null)
  const [recoveryRefresh, setRecoveryRefresh] = useState(0)
  const [confirmingRestart, setConfirmingRestart] = useState(false)
  const [starting, setStarting] = useState(false)
  const uploadRef = useRef(null)

  const selected = pdfs.find(item => item.id === selectedId)
  const filteredPdfs = pdfs.filter(item => item.title.toLowerCase().includes(search.toLowerCase()))
  const isRunning = canExecute && ['running', 'queued', 'stopping'].includes(job?.status)
  const [stopPending, setStopPending] = useState(false)
  const needsRecovery = canExecute && !!selected && !isRunning && (['failed', 'stopped'].includes(selected.status) || recoveryEvent?.pdfId === selectedId)
  const recoveryData = recoveryInfo?.pdfId === selectedId ? recoveryInfo : null
  const runDisabled = !canExecute || starting || isRunning || pdfs.some(item => ['running', 'queued', 'stopping'].includes(item.status))
  const claimCounts = { all: claims.length, source: claims.filter(record => record.candidate.source_validation?.decision === 'entailed').length, supported: 0, review: 0, rejected: 0 }
  claims.forEach(record => { claimCounts[claimGroup(record)] += 1 })
  const visibleClaims = claims.filter(record => (claimFilter === 'all' || (claimFilter === 'source' ? record.candidate.source_validation?.decision === 'entailed' : claimGroup(record) === claimFilter)) && matchesClaim(record, claimSearch))
  const errorCount = logs.filter(entry => entry.level === 'error').length

  function showClaims(group = 'all') {
    setClaimFilter(group)
    setClaimSearch('')
    setView('claims')
  }

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
    const [graphData, claimData, logData, pathData, assembledData, trustGraphData] = await Promise.all([
      request(graphPath),
      request(`/pdfs/${pdfId}/claims`), request(`/pdfs/${pdfId}/logs`), request(`/pdfs/${pdfId}/paths`),
      request('/pathways').catch(error => ({ status: 'failed', error: error.message })),
      request('/pathways?view=provisional').catch(error => ({ status: 'failed', error: error.message })),
    ])
    setGraph(graphData)
    setClaims(claimData.claims)
    setDiscards(claimData.extraction_rejections || [])
    setModelRequests(claimData.model_requests || [])
    setLogs(logData)
    setPaths(pathData)
    setPathwayData(assembledData)
    setTrustData(trustGraphData)
  }

  useEffect(() => {
    refreshPdfs().catch(error => setMessage(error.message))
    request('/health').then(data => {
      setHealth(data)
      setOptions(previous => ({ ...previous, articles_per_claim: data.pipeline?.external_top_k ?? previous.articles_per_claim,
        knowledge_building_mode: data.pipeline?.knowledge_building_mode ?? previous.knowledge_building_mode,
        trust_batch_size: data.pipeline?.trust_batch_size ?? previous.trust_batch_size,
        extraction_mode: data.pipeline?.extraction_mode ?? previous.extraction_mode,
        extraction_target_words: data.pipeline?.extraction_target_words ?? previous.extraction_target_words,
        extraction_generation_budget: data.pipeline?.extraction_generation_budget ?? previous.extraction_generation_budget }))
      setOptions(previous => ({ ...previous, claims_per_packet: data.pipeline?.extraction_claims_per_packet ?? previous.claims_per_packet }))
    }).catch(() => setHealth({ extraction: { ready: false }, validation: { ready: false } }))
  }, [])

  useEffect(() => {
    if (!selectedId) return
    const location = new URL(window.location.href)
    location.searchParams.set('paper', selectedId)
    location.hash = view
    window.history.replaceState(null, '', location)
  }, [selectedId, view])

  useEffect(() => {
    try { window.localStorage.setItem('soil-health.sidebar-hidden', String(sidebarHidden)) }
    catch { /* The toggle still works when browser storage is unavailable. */ }
  }, [sidebarHidden])

  useEffect(() => { setConfirmingRestart(false) }, [selectedId])

  useEffect(() => {
    if (!readOnly || !selectedId) return
    let cancelled = false
    setSavedJobs([])
    request(`/pdfs/${selectedId}/jobs`).then(rows => { if (!cancelled) setSavedJobs(rows) })
      .catch(error => { if (!cancelled) setMessage(error.message) })
    return () => { cancelled = true }
  }, [readOnly, selectedId])

  useEffect(() => {
    if (!needsRecovery) return
    let cancelled = false
    setRecoveryInfo({ pdfId: selectedId, loading: true })
    request(`/pdfs/${selectedId}/recovery`).then(data => {
      if (!cancelled) setRecoveryInfo({ pdfId: selectedId, data, loading: false })
    }).catch(error => {
      if (!cancelled) setRecoveryInfo({ pdfId: selectedId, loading: false, error: error.message })
    })
    return () => { cancelled = true }
  }, [needsRecovery, selectedId, selected?.latest_job_id, recoveryEvent?.jobId, recoveryRefresh])

  useEffect(() => {
    if (!selectedId) return
    setSelectedElement(null)
    setFilter('all')
    refreshDetails(selectedId, graphScope, graphMode).catch(error => setMessage(error.message))
  }, [selectedId, graphScope, graphMode])

  useEffect(() => {
    const paper = pdfs.find(item => item.id === selectedId)
    setJob(null)
    setJobId(paper?.active_job_id || paper?.latest_job_id || null)
  }, [selectedId, selected?.id])

  useEffect(() => {
    if (!jobId || !health) return
    let cancelled = false
    if (readOnly) {
      request(`/jobs/${jobId}`).then(current => { if (!cancelled) setJob(current) })
        .catch(error => { if (!cancelled) setMessage(error.message) })
      return () => { cancelled = true }
    }
    if (!canExecute) return
    const stop = watchJob({
      fetchJob: () => request(`/jobs/${jobId}`),
      onJob: async current => {
        if (cancelled) return
        setJob(current)
        if (current.pdf_id === selectedId) setLogs(current.logs)
        if (['running', 'queued', 'stopping'].includes(current.status)) {
          await refreshPdfs()
          if (current.pdf_id === selectedId) refreshDetails(selectedId, graphScope, graphMode).catch(() => {})
        } else {
          await refreshPdfs()
          if (current.pdf_id === selectedId) await refreshDetails(selectedId, graphScope, graphMode)
          if (!cancelled && ['failed', 'stopped'].includes(current.status) && current.pdf_id === selectedId) {
            setRecoveryEvent({ pdfId: selectedId, jobId: current.id, kind: current.status === 'stopped' ? 'stopped' : current.interrupted ? 'interrupted' : 'failed', error: current.error })
            setMessage(current.status === 'stopped' ? 'Pipeline stopped. Saved progress is available.' : current.error || 'Pipeline failed')
          }
        }
      },
      onMissing: async () => {
        if (cancelled) return
        setJob(null)
        setRecoveryEvent({ pdfId: selectedId, kind: 'missing', jobId })
        try {
          const items = await refreshPdfs()
          if (cancelled) return
          const paper = items.find(item => item.id === selectedId)
          if (paper?.active_job_id && paper.active_job_id !== jobId) {
            setRecoveryEvent(null)
            setJobId(paper.active_job_id)
            setMessage('Reconnected to the current run.')
            return
          }
          await refreshDetails(selectedId, graphScope, graphMode)
          if (!cancelled) setMessage(paper?.status === 'completed' || paper?.status === 'completed_with_warnings'
            ? 'Loaded the saved results. The previous live job is no longer available.'
            : 'Live monitoring was interrupted, possibly by a backend restart. Saved findings are preserved. Use Resume failed run when available.')
        } finally {
          if (!cancelled) setJobId(current => current === jobId ? null : current)
        }
      },
      onError: error => { if (!cancelled) setMessage(error.message) },
    })
    return () => { cancelled = true; stop() }
  }, [jobId, selectedId, graphScope, graphMode, readOnly, canExecute, health])

  async function assembleSavedPaths() {
    if (!canExecute || !selected || runDisabled) return
    setStarting(true)
    try {
      const started = await request(`/pdfs/${selected.id}/assemble-pathways`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(options) })
      setJob(started)
      setJobId(started.id)
      setLogs(started.logs || [])
      setMessage(null)
    } catch (error) {
      setMessage(error.message)
    } finally {
      setStarting(false)
    }
  }

  async function stopRun() {
    if (!canExecute || !job?.id || job.pdf_id !== selectedId || !isRunning || stopPending || job.status === 'stopping') return
    setStopPending(true)
    try {
      const updated = await request(`/jobs/${job.id}/stop`, { method: 'POST' })
      setJob(updated)
      setLogs(updated.logs || [])
      setView('activity')
    } catch (error) { setMessage(error.message) }
    finally { setStopPending(false) }
  }

  async function startRun(resume = false, runOptions = options) {
    if (!canExecute || !selected || runDisabled) return
    setStarting(true)
    setMessage(null)
    try {
      const started = await request(`/pdfs/${selected.id}/${resume ? 'resume' : 'run'}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(runOptions) })
      setRecoveryEvent(null)
      setRecoveryInfo(null)
      setConfirmingRestart(false)
      if (resume) setOptions(runOptions)
      setJob(started)
      setJobId(started.id)
      setLogs(started.logs || [])
      if (!resume) {
        setClaims([])
        setDiscards([])
        setModelRequests([])
      }
      setView('activity')
    } catch (error) {
      setMessage(error.message)
      setLogs(previous => [...previous, { at: new Date().toISOString(), level: 'error', message: error.message }])
      setRecoveryRefresh(value => value + 1)
    } finally {
      setStarting(false)
    }
  }

  function continueSavedRun() {
    if (recoveryData?.data?.can_continue) startRun(true, resumeWithThinking(recoveryData.data.options, options))
  }

  async function runClaimAction(candidateId, action) {
    if (!canExecute || !selected) return
    setMessage(null)
    try {
      const started = await request(`/pdfs/${selected.id}/claims/${candidateId}/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(options),
      })
      setJob(started)
      setJobId(started.id)
      setLogs(started.logs || [])
    } catch (error) {
      setMessage(error.message)
      setLogs(previous => [...previous, { at: new Date().toISOString(), level: 'error', message: error.message }])
    }
  }

  async function upload(event) {
    if (!canExecute) return
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
    <a className="skip-link" href="#workspace-content">Skip to findings</a>
    <aside id="paper-library" className={`sidebar ${libraryOpen ? 'library-open' : ''}`} aria-label="Paper library" hidden={sidebarHidden}>
      <div className="brand"><span className="brand-mark"><Leaf size={23} /></span><span><strong>Soil to Health</strong><small>Research workbench</small></span></div>
      <button type="button" className="library-toggle" aria-expanded={libraryOpen} aria-controls="paper-library-list" onClick={() => setLibraryOpen(!libraryOpen)}><FolderOpen size={16} /> Paper library <span>{pdfs.length}</span><ChevronDown size={15} className={libraryOpen ? 'chevron open' : 'chevron'} /></button>
      <div className="sidebar-section-header"><span className="eyebrow">PAPER LIBRARY</span><span className="count-badge">{pdfs.length}</span></div>
      <div className="search-field"><Search size={17} /><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search papers…" aria-label="Search papers" /></div>
      <div className="pdf-list" id="paper-library-list">
        {filteredPdfs.map(pdf => <button type="button" key={pdf.id} className={`pdf-card run-${pdf.status} ${selectedId === pdf.id ? 'selected' : ''}`} aria-pressed={selectedId === pdf.id} title={pdf.title}
          onClick={() => { setSelectedId(pdf.id); showClaims(); setLibraryOpen(false) }}>
          <span className="pdf-icon"><FileText size={19} /></span><span className="pdf-card-copy"><strong>{pdf.title}</strong>
            <small>{formatSize(pdf.size_bytes)} · {pdf.summary?.extracted ?? 'No'} claims</small>
            <PaperRunStatus paper={pdf} /></span></button>)}
        {!filteredPdfs.length && <div className="sidebar-empty">No papers match your search.</div>}
      </div>
      <div className="sidebar-footer">{canExecute && <><button className="upload-card" type="button" onClick={() => uploadRef.current?.click()}><span><Plus size={18} /></span><div><strong>Add a PDF</strong><small>Upload a research paper</small></div></button>
        <input ref={uploadRef} type="file" accept="application/pdf,.pdf" hidden onChange={upload} /></>}
        <div className="local-status"><span className={`connection-dot ${readOnly || (health?.extraction?.ready && health?.validation?.ready) ? 'online' : ''}`} />
          <span>{readOnly ? 'Published results' : !health ? 'Connecting' : health?.extraction?.ready && health?.validation?.ready ? 'Models connected' : 'Models unavailable'}</span><span className="local-label">{readOnly ? 'Read only' : 'Local'}</span></div>
      </div>
    </aside>

    <main className="main-area" id="workspace-content" tabIndex={-1}>
      <header className="topbar"><div className="topbar-left"><SidebarToggle hidden={sidebarHidden} onToggle={() => setSidebarHidden(previous => !previous)} />
        <div className="breadcrumbs"><span>Research</span><ArrowRight size={14} /><strong>Paper workspace</strong></div></div>
        <div className="topbar-right"><span className="topbar-date">Soil · Crop · Human health</span><span className="workspace-badge"><Leaf size={13} /> {readOnly ? 'Results viewer' : 'Local workspace'}</span><span className="workspace-version" aria-label="Running pipeline version" title={health?.pipeline?.schema_version || 'Backend version unavailable'}>{health?.pipeline?.schema_version?.replace(/^external-validation-/, '') || 'Version unavailable'}</span></div></header>
      <div className="page-content">
        {message && <div className="toast" role="alert"><CircleAlert size={18} /><span>{message}</span><button type="button" aria-label="Dismiss notification" onClick={() => setMessage(null)}><X size={17} /></button></div>}
        {readOnly && <div className="snapshot-banner" role="status"><strong>Published research snapshot · Read only</strong><span>{new Date(health.snapshot.created_at).toLocaleString()} · {health.snapshot.id}</span><p>Explore saved findings, evidence and run history. Pipeline execution is available only in the local workspace.</p>{health.snapshot.warnings?.map((warning, index) => <p key={index}>{warning}</p>)}</div>}
        {!selected ? <Empty icon={FolderOpen} title={readOnly ? 'No published papers' : 'Your research workspace is ready'}>{readOnly ? 'No saved results are available in this snapshot.' : 'Add a PDF to begin extracting and validating claims.'}</Empty> : <>
          <div className="page-heading"><div className="paper-heading-copy"><div className="paper-heading-meta"><span className="eyebrow accent">PAPER WORKSPACE</span><StatusPill value={selected.status} /></div>
            <h1>{selected.title}</h1><p>Review the claims, follow the evidence, and explore how findings connect.</p></div>
            <div className="heading-actions">{selected.pdf_available !== false && <a className="button secondary" href={`${API}/pdfs/${selected.id}/file`} target="_blank" rel="noreferrer"><BookOpen size={16} /> View PDF</a>}
              <button className="button secondary" type="button" onClick={() => { setView('details'); setSettingsOpen(true) }}><Settings2 size={16} /> {readOnly ? 'Run details' : 'Run settings'}</button>
              {canExecute && <><StopRunButton active={isRunning && job?.pdf_id === selectedId} stopping={stopPending || job?.status === 'stopping'} onStop={stopRun} />
              <ResumeRunButton canResume={selected.can_resume} disabled={runDisabled || !recoveryData?.data?.can_continue} onResume={continueSavedRun} />
              <button className="button primary" type="button" disabled={runDisabled} onClick={() => needsRecovery ? setConfirmingRestart(true) : startRun(false)}>
                {isRunning || starting ? <LoaderCircle className="spin" size={16} /> : <Play size={15} fill="currentColor" />}{isRunning ? 'Running pipeline' : starting ? 'Starting pipeline' : needsRecovery ? 'Restart pipeline' : selected.status === 'completed' ? 'Run again' : selected.status === 'blocked_text_quality' ? 'Retry pipeline' : 'Run pipeline'}</button></>}</div>
          </div>

          {needsRecovery && <RunRecoveryNotice kind={recoveryEvent?.pdfId === selectedId ? recoveryEvent.kind : recoveryData?.data?.job_missing ? 'missing' : selected.status === 'stopped' ? 'stopped' : recoveryData?.data?.interrupted ? 'interrupted' : 'failed'}
            jobId={recoveryEvent?.pdfId === selectedId ? recoveryEvent.jobId : recoveryData?.data?.job_id}
            error={recoveryEvent?.pdfId === selectedId ? recoveryEvent.error : recoveryData?.data?.error}
            recovery={recoveryData?.data} loading={!recoveryData || recoveryData.loading} checkError={recoveryData?.error}
            disabled={runDisabled} confirmingRestart={confirmingRestart} onContinue={continueSavedRun}
            onAskRestart={() => setConfirmingRestart(true)} onCancelRestart={() => setConfirmingRestart(false)}
            onRestart={() => startRun(false)} onViewActivity={() => setView('activity')}
            onCheckAgain={() => setRecoveryRefresh(value => value + 1)} />}

          <div className="overview-grid" aria-label="Paper findings overview">
            <button type="button" className="overview-card" onClick={() => showClaims()}><span className="metric-icon"><Layers3 size={19} /></span><span><span className="eyebrow">EXTRACTED CLAIMS</span><strong>{selected.summary?.extracted ?? claims.length}</strong><small>Review all claims <ArrowRight size={12} /></small></span></button>
            <button type="button" className="overview-card" onClick={() => showClaims('source')}><span className="metric-icon"><Check size={19} /></span><span><span className="eyebrow">SOURCE SUPPORTED</span><strong>{selected.summary?.source_entailed ?? (claims.some(item => item.candidate.source_validation) ? claims.filter(item => item.candidate.source_validation?.decision === 'entailed').length : '—')}</strong><small>Faithful to the source PDF <ArrowRight size={12} /></small></span></button>
            <button type="button" className="overview-card" onClick={() => { setGraphMode('main'); setView('graph') }}><span className="metric-icon"><Network size={19} /></span><span><span className="eyebrow">GRAPH RELATIONS</span><strong>{selected.summary?.graph?.edges ?? graph?.counts?.edges ?? 0}</strong><small>{selected.summary?.graph?.corroborated ?? 0} corroborated · Explore graph <ArrowRight size={12} /></small></span></button>
            <button type="button" className="overview-card" onClick={() => setView('activity')}><span className="metric-icon"><Activity size={19} /></span><span><span className="eyebrow">RUN STATUS</span><strong className="metric-status">{statusLabels[selected.status] || selected.status}</strong><small>{selected.summary?.completed_at || selected.summary?.updated_at ? `Updated ${new Date(selected.summary.completed_at || selected.summary.updated_at).toLocaleDateString()}` : 'Open activity log'} <ArrowRight size={12} /></small></span></button>
          </div>

          {!needsRecovery && (isRunning || selected.summary?.external_incomplete > 0 || selected.summary?.extraction_coverage?.complete === false || selected.status === 'failed' || selected.document_scope?.text_quality === 'unusable') &&
            <div className={`findings-notice ${isRunning ? 'in-progress' : ''}`} role="status">
              {isRunning ? <LoaderCircle size={18} className="spin" /> : <CircleAlert size={18} />}
              <div><strong>{isRunning ? 'Analysis in progress' : selected.document_scope?.text_quality === 'unusable' ? 'The PDF needs clearer text' : 'Some findings need attention'}</strong>
                <p>{isRunning ? 'You can explore available claims while the pipeline continues.' : selected.document_scope?.text_quality === 'unusable' ? (readOnly ? 'The saved PDF has text-quality issues. Inspect Run details for the recorded diagnostics.' : 'Use a cleaner PDF or OCR, then retry the pipeline. Details are available in Run details.') : selected.summary?.external_incomplete > 0 ? `${selected.summary.external_incomplete} claims await completed external validation. An unfinished check is not a rejected claim.` : selected.summary?.extraction_coverage?.complete === false ? 'Some extraction regions were not processed. Check Run details for coverage.' : 'The last run stopped. Open the activity log to inspect the error.'}</p></div>
              <button type="button" className="text-button" onClick={() => setView('activity')}>View activity <ArrowRight size={15} /></button>
            </div>}

          <section className="analysis-panel" aria-label="Paper analysis">
            <WorkspaceNavigation view={view} onChange={setView} claims={claims.length} paths={pathView === 'trust' ? (trustData?.paths?.length ?? 0) : pathView === 'assembled' ? (pathwayData?.paths?.length ?? 0) : paths.length} errors={errorCount} />
            <div className="section-head"><div><span className="eyebrow">{view === 'details' ? 'CONFIGURATION & DIAGNOSTICS' : view === 'activity' ? 'RUN HISTORY' : 'RESEARCH FINDINGS'}</span>
              <h2>{({ claims: 'Extracted claims', graph: 'Knowledge graph', paths: 'Research pathways', activity: 'Activity log', details: 'Run details' })[view]}</h2>
              <p>{({ claims: 'Inspect each claim, its source passage, and the external evidence.', graph: 'Explore source-supported, externally supported, and audit relations.', paths: 'Connect original triplets across papers using shared entities and source context.', activity: 'Follow every stage and inspect warnings or model errors.', details: readOnly ? 'Inspect saved settings, coverage and model activity.' : 'Adjust the run, review coverage, and inspect model activity.' })[view]}</p></div>
              {view === 'claims' && <span className="subtle-count">{claims.length} claims</span>}</div>
            {view === 'claims' && <>
              <FindingsToolbar search={claimSearch} onSearch={setClaimSearch} filter={claimFilter} onFilter={setClaimFilter} counts={claimCounts} shown={visibleClaims.length} />
              <div className="claim-list">{visibleClaims.length ? visibleClaims.map(item => <ClaimCard readOnly={!canExecute} key={item.candidate.candidate_id} record={item} index={claims.indexOf(item)} busy={isRunning || !!pdfs.find(pdf => ['running', 'queued', 'stopping'].includes(pdf.status))} onClaimAction={runClaimAction} />)
                : <Empty icon={Layers3} title={claims.length ? 'No matching claims' : 'No claims extracted yet'}>{claims.length ? 'Try a different search or decision filter.' : readOnly ? 'No claims were included in this publication.' : 'Run the PDF to see its claims, source quotes, and article decisions.'}</Empty>}</div>
              <ExtractionDiscards records={discards} />
            </>}
            {view === 'graph' && <><div className="graph-controls"><div className="segmented"><button type="button" className={graphMode === 'main' ? 'active' : ''} onClick={() => setGraphMode('main')}>External graph</button><button type="button" className={graphMode === 'source' ? 'active' : ''} onClick={() => setGraphMode('source')}>Source-supported</button><button type="button" className={graphMode === 'audit' ? 'active' : ''} onClick={() => setGraphMode('audit')}>Audit view</button></div><div className="segmented"><button type="button" className={graphScope === 'paper' ? 'active' : ''} onClick={() => setGraphScope('paper')}>This paper</button><button type="button" className={graphScope === 'all' ? 'active' : ''} onClick={() => setGraphScope('all')}>All papers</button></div><select value={filter} onChange={event => setFilter(event.target.value)} aria-label="Filter graph edges"><option value="all">All evidence states</option>{graphMode === 'main' && <><option value="corroborated">Independently corroborated</option><option value="single_source">Independent article</option></>}{graphMode === 'source' && <option value="source_supported">Source supported</option>}{graphMode === 'audit' && <><option value="rejected">Rejected</option><option value="semantic_rejected">Semantic rejected</option><option value="semantic_needs_review">Review semantics</option><option value="source_unsupported">Source unsupported</option><option value="source_needs_review">Review source</option><option value="source_reference_supported">Source reference support</option><option value="extracted">Awaiting validation</option><option value="not_found">External evidence not found</option><option value="contested">Contested</option><option value="contradicted">Contradicted</option></>}</select></div>
              <GraphView graph={graph} activeFilter={filter} onSelect={setSelectedElement} onViewClaims={() => showClaims()} />
              {graphMode === 'main' && graphScope === 'paper' && ['completed', 'completed_with_warnings'].includes(selected.status) && <div className="graph-export"><a href={`${API}/pdfs/${selected.id}/graph.graphml`} download><ExternalLink size={13} /> Download GraphML</a></div>}
              {selectedElement && <div className="graph-selection"><button type="button" className="close-mini" aria-label="Close selected graph details" onClick={() => setSelectedElement(null)}><X size={16} /></button><span className="eyebrow">{selectedElement.kind === 'edge' ? 'SELECTED RELATION' : 'SELECTED ENTITY'}</span><strong>{selectedElement.kind === 'edge' ? `${selectedElement.source?.split(':').slice(1).join(':')} → ${selectedElement.relation} → ${selectedElement.target?.split(':').slice(1).join(':')}` : selectedElement.label}</strong><p>{selectedElement.kind === 'edge' ? `${statusLabels[selectedElement.status] || selectedElement.status} · ${selectedElement.support_count} supporting article(s) · ${selectedElement.paper}` : selectedElement.type}</p>{selectedElement.rejection_reason && <p>{selectedElement.rejection_reason}</p>}{selectedElement.quote && <blockquote>“{selectedElement.quote}”</blockquote>}</div>}
              <div className="graph-legend">{graphMode === 'main' ? <><span><i className="legend-dot corroborated" />Independently corroborated</span><span><i className="legend-dot single" />Independent article</span></> : graphMode === 'source' ? <span><i className="legend-dot single" />Faithful to source PDF</span> : <><span><i className="legend-dot extracted" />Awaiting validation or source-reference only</span><span><i className="legend-dot contested" />Contested</span><span><i className="legend-dot insufficient" />External evidence not found</span></>}</div></>}
            {view === 'paths' && <><PathwayNavigation value={pathView} onChange={setPathView}
              trustCount={trustData?.paths?.length} assembledCount={pathwayData?.paths?.length} healthCount={paths.length} />
            {pathView !== 'corroborated' ? <PathwayPanel readOnly={!canExecute} key={pathView} data={pathView === 'trust' ? trustData : pathwayData} busy={runDisabled} onAssemble={assembleSavedPaths}
              onDepthChange={async maxHops => {
                try { (pathView === 'trust' ? setTrustData : setPathwayData)(await request(`/pathways?view=${pathView === 'trust' ? 'provisional' : 'validated'}&max_hops=${maxHops}`)) }
                catch (error) { setMessage(error.message) }
              }}
              onLoadMore={async () => {
                try {
                  const current = pathView === 'trust' ? trustData : pathwayData
                  const next = await request(`/pathways?view=${pathView === 'trust' ? 'provisional' : 'validated'}&offset=${current.paths.length}&max_hops=${current.max_hops}`);
                  (pathView === 'trust' ? setTrustData : setPathwayData)(previous => ({ ...next, paths: [...previous.paths, ...next.paths] }))
                } catch (error) { setMessage(error.message) }
              }} /> : <CorroboratedPaths paths={paths} />}</>}
            {view === 'activity' && <>{readOnly && <SavedRunDetails jobs={savedJobs} job={job?.pdf_id === selectedId ? job : null} jobId={jobId} onSelect={value => { setJob(null); setJobId(value) }} pipeline={health.pipeline} />}<ActivityLog key={selected.id} logs={readOnly && job?.pdf_id === selectedId ? job.logs || [] : logs} running={isRunning && job?.pdf_id === selectedId} progress={!readOnly && job?.pdf_id === selectedId ? job.request_progress : null} /></>}
            {view === 'details' && <div className="run-details">{readOnly ? <SavedRunDetails jobs={savedJobs} job={job?.pdf_id === selectedId ? job : null} jobId={jobId} onSelect={value => { setJob(null); setJobId(value) }} pipeline={health.pipeline} /> : canExecute && <RunSetup health={health} options={options} onOptions={setOptions} open={settingsOpen} onToggle={() => setSettingsOpen(!settingsOpen)} />}
              <DocumentScopeNotice documentScope={selected.document_scope || selected.summary?.document_scope} />
              <PdfQualityReport quality={(selected.document_scope || selected.summary?.document_scope)?.pdf_quality} />
              <RunBreakdown summary={selected.summary} />
              <ModelPerformance requests={modelRequests} summary={selected.summary} policy={health?.pipeline?.reasoning_policy} routing={health?.pipeline?.task_models} />
            </div>}
          </section>
          <footer className="workspace-footer"><Leaf size={14} /><span>Research evidence remains provisional. Review source passages and study conditions alongside every finding.</span></footer>
        </>}
      </div>
    </main>
  </div>
}
