import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity, ArrowRight, BookOpen, Check, ChevronDown, CircleAlert,
  FileText, FolderOpen, Layers3, Leaf, LoaderCircle,
  Network, Play, Plus, Search, Settings2, X,
} from 'lucide-react'
import ResearchNetwork from './components/graph/ResearchNetwork.jsx'
import ClaimCard from './components/claims/ClaimCard.jsx'
import StatusPill from './components/claims/StatusPill.jsx'
import { ExtractionDiscards } from './components/claims/ClaimDetails.jsx'
import ModelPerformance from './components/diagnostics/ModelPerformance.jsx'
import SavedRunDetails from './components/workspace/SavedRunDetails.jsx'
import RunRecoveryNotice from './components/workspace/RunRecoveryNotice.jsx'
import { RunBreakdown, DocumentScopeNotice } from './components/workspace/RunSummary.jsx'
import { PdfQualityReport, ActivityLog, FindingsToolbar, PaperRunStatus, ResumeRunButton, RedoExternalValidationButton, StopRunButton, RunSetup, SidebarToggle, WorkspaceNavigation } from './components/workspace/WorkbenchPanels.jsx'
import { API, request } from './lib/api.js'
import { claimGroup, matchesClaim, statusLabels, formatSize } from './lib/claimPresentation.js'
import { scopeAssemblyGraph } from './lib/graphNetwork.js'
import { combineRoutes, filterRoutes, routeGroup, splitLocalAssessments } from './lib/researchNetwork.js'
import { useWorkspaceNavigation } from './hooks/useWorkspaceNavigation.js'
import { useWorkspaceData } from './hooks/useWorkspaceData.js'
import { usePipelineRun } from './hooks/usePipelineRun.js'

// Keep existing named imports working for saved-view consumers and tests.
export { claimStatus, claimGroup, matchesClaim } from './lib/claimPresentation.js'
export { initialWorkspaceView } from './hooks/useWorkspaceNavigation.js'
export { default as ClaimCard } from './components/claims/ClaimCard.jsx'

function Empty({ icon: Icon, title, children }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={29} /></span><h3>{title}</h3><p>{children}</p></div>
}

export default function App() {
  const [message, setMessage] = useState(null)
  const { selectedId, setSelectedId, view, setView, sidebarHidden, setSidebarHidden } = useWorkspaceNavigation()
  const [search, setSearch] = useState('')
  const [libraryOpen, setLibraryOpen] = useState(false)
  const [graphScope, setGraphScope] = useState('paper')
  const [graphMode, setGraphMode] = useState('assembled')
  const [claimFilter, setClaimFilter] = useState('all')
  const [claimSearch, setClaimSearch] = useState('')
  const [selectedElement, setSelectedElement] = useState(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [focusedPathwayId, setFocusedPathwayId] = useState(null)
  const [claimsPageSize, setClaimsPageSize] = useState(60)
  const uploadRef = useRef(null)
  const workspace = useWorkspaceData({ selectedId, setSelectedId, graphScope, graphMode, onError: setMessage })
  const { pdfs, selected, health, readOnly, canExecute, savedJobs, graph: savedGraph, claims, discards, modelRequests, paths, logs,
    pathwayData, trustData, pathAssessments, setTrustData, setPathwayData, upload, initialLoading, paperLoading, setPaperLoading } = workspace
  const { job, setJob, jobId, setJobId, options, setOptions, claimReviewBusy, recoveryEvent, recoveryData,
    confirmingRestart, setConfirmingRestart, starting, stopPending, isRunning, needsRecovery, runDisabled,
    assembleSavedPaths, stopRun, startRun, continueSavedRun, redoSavedExternalValidation, runClaimAction, checkRecovery } = usePipelineRun({ workspace, selectedId, setView, setMessage })
  const filteredPdfs = pdfs.filter(item => item.title.toLowerCase().includes(search.toLowerCase()))
  const indexedClaims = useMemo(() => claims.map((record, index) => ({ record, index })), [claims])
  const claimCounts = useMemo(() => {
    const counts = { all: claims.length, source: 0, supported: 0, review: 0, rejected: 0, unvalidated: 0 }
    for (const record of claims) {
      if (!record.validation_stages_paused && (record.source_review?.fidelity_decision || record.candidate.source_validation?.decision) === 'entailed') counts.source += 1
      counts[claimGroup(record)] = (counts[claimGroup(record)] || 0) + 1
    }
    return counts
  }, [claims])
  const visibleClaims = useMemo(() => indexedClaims.filter(({ record }) =>
    (claimFilter === 'all' || (claimFilter === 'source' ? (record.source_review?.fidelity_decision || record.candidate.source_validation?.decision) === 'entailed' : claimGroup(record) === claimFilter)) && matchesClaim(record, claimSearch)
  ), [indexedClaims, claimFilter, claimSearch])
  const pageClaims = useMemo(() => visibleClaims.slice(0, claimsPageSize), [visibleClaims, claimsPageSize])
  const pipelineBusy = claimReviewBusy || isRunning || pdfs.some(pdf => ['running', 'queued', 'stopping'].includes(pdf.status))
  const errorCount = logs.filter(entry => entry.level === 'error').length
  const pathwayOnly = ['pathway_only', 'extraction_assembly'].includes(selected?.summary?.knowledge_building_mode)
  const paperAssembly = useMemo(() => scopeAssemblyGraph(trustData?.graph, selected?.title), [trustData?.graph, selected?.title])
  const assemblyGraph = useMemo(() => graphScope === 'paper' ? paperAssembly : scopeAssemblyGraph(trustData?.graph), [graphScope, paperAssembly, trustData?.graph])
  const graph = graphMode === 'assembled' ? assemblyGraph : savedGraph
  function inspectClaim(candidateId) { setClaimFilter('all'); setClaimSearch(candidateId); setView('claims') }
  function inspectPathway(pathwayId) { setFocusedPathwayId(pathwayId); setView('network') }
  const networkRoutes = useMemo(() => combineRoutes(trustData?.paths,
    splitLocalAssessments(pathAssessments, trustData).current), [trustData, pathAssessments])
  const paperRoutes = useMemo(() => filterRoutes(networkRoutes, { paperKey: selected?.title }), [networkRoutes, selected?.title])
  const supportedRoutes = paperRoutes.filter(row => routeGroup(row) === 'supported').length

  async function updatePathways(provisional, maxHops, append = false) {
    const current = provisional ? trustData : pathwayData
    const setter = provisional ? setTrustData : setPathwayData
    try {
      const next = await request(`/pathways?view=${provisional ? 'provisional' : 'validated'}&max_hops=${maxHops ?? current?.max_hops ?? 6}${append ? `&offset=${current?.paths?.length ?? 0}` : ''}`)
      setter(previous => append ? { ...next, paths: [...(previous?.paths || []), ...next.paths] } : next)
    } catch (error) { setMessage(error.message) }
  }

  function showClaims(group = 'all') {
    setClaimFilter(group)
    setClaimSearch('')
    setView('claims')
  }

  useEffect(() => {
    setSelectedElement(null)
  }, [selectedId, graphScope, graphMode])
  useEffect(() => { setFocusedPathwayId(null) }, [selectedId])
  useEffect(() => { setClaimsPageSize(60) }, [selectedId, claimFilter, claimSearch])

  return <div className="app-shell">
    <a className="skip-link" href="#workspace-content">Skip to findings</a>
    <aside id="paper-library" className={`sidebar ${libraryOpen ? 'library-open' : ''}`} aria-label="Paper library" hidden={sidebarHidden}>
      <div className="brand"><span className="brand-mark"><Leaf size={23} /></span><span><strong>Soil to Health</strong><small>Research workbench</small></span></div>
      <button type="button" className="library-toggle" aria-expanded={libraryOpen} aria-controls="paper-library-list" onClick={() => setLibraryOpen(!libraryOpen)}><FolderOpen size={16} /> Paper library <span>{pdfs.length}</span><ChevronDown size={15} className={libraryOpen ? 'chevron open' : 'chevron'} /></button>
      <div className="sidebar-section-header"><span className="eyebrow">PAPER LIBRARY</span><span className="count-badge">{pdfs.length}</span></div>
      <div className="search-field"><Search size={17} /><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search papers…" aria-label="Search papers" /></div>
      <div className="pdf-list" id="paper-library-list">
        {filteredPdfs.map(pdf => <button type="button" key={pdf.id} className={`pdf-card run-${pdf.status} ${selectedId === pdf.id ? 'selected' : ''}`} aria-pressed={selectedId === pdf.id} title={pdf.title}
          onClick={() => { setSelectedId(pdf.id); setPaperLoading(true); showClaims(); setLibraryOpen(false) }}>
          <span className="pdf-icon"><FileText size={19} /></span><span className="pdf-card-copy"><strong>{pdf.title}</strong>
            <small>{formatSize(pdf.size_bytes)} · {pdf.summary?.extracted ?? 'No'} claims</small>
            {paperLoading && selectedId === pdf.id ? <span className="pdf-loading-tag"><LoaderCircle size={11} className="spin" /> Loading…</span> : <PaperRunStatus paper={pdf} />}</span></button>)}
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
        <div className="topbar-right">{paperLoading && <span className="topbar-loading-pill" role="status"><LoaderCircle size={13} className="spin" /> Loading findings…</span>}<span className="topbar-date">Soil · Crop · Human health</span><span className="workspace-badge"><Leaf size={13} /> {readOnly ? 'Results viewer' : 'Local workspace'}</span><span className="workspace-version" aria-label="Running pipeline version" title={health?.pipeline?.version || health?.pipeline?.schema_version || 'Backend version unavailable'}>{(health?.pipeline?.version || health?.pipeline?.schema_version)?.replace(/^(external|pathway|neighbor)-validation-/, '') || 'Version unavailable'}</span></div></header>
      <div className="page-content">
        {message && <div className="toast" role="alert"><CircleAlert size={18} /><span>{message}</span><button type="button" aria-label="Dismiss notification" onClick={() => setMessage(null)}><X size={17} /></button></div>}
        {readOnly && <div className="snapshot-banner" role="status"><strong>Published research snapshot · Read only</strong><span>{new Date(health.snapshot.created_at).toLocaleString()} · {health.snapshot.id}</span><p>Explore saved findings, evidence and run history. Pipeline execution is available only in the local workspace.</p>{health.snapshot.warnings?.map((warning, index) => <p key={index}>{warning}</p>)}</div>}
        {!selected ? (initialLoading ? <div className="workspace-loading-screen" role="status" aria-live="polite"><div className="loading-loop-spinner"><LoaderCircle size={32} className="spin" /></div><h3>Loading research workbench…</h3><p>Fetching papers and connecting to workspace services.</p></div> : <Empty icon={FolderOpen} title={readOnly ? 'No published papers' : 'Your research workspace is ready'}>{readOnly ? 'No saved results are available in this snapshot.' : 'Add a PDF to begin extracting and validating claims.'}</Empty>) : <>
          {paperLoading && <div className="paper-loading-indicator" role="status" aria-live="polite"><LoaderCircle size={18} className="spin" /><span>Loading findings for <strong>{selected.title}</strong>…</span></div>}
          <div className="page-heading"><div className="paper-heading-copy"><div className="paper-heading-meta"><span className="eyebrow accent">PAPER WORKSPACE</span><StatusPill value={selected.status} /></div>
            <h1>{selected.title}</h1><p>Review the claims, follow the evidence, and explore how findings connect.</p></div>
            <div className="heading-actions">{selected.pdf_available !== false && <a className="button secondary" href={`${API}/pdfs/${selected.id}/file`} target="_blank" rel="noreferrer"><BookOpen size={16} /> View PDF</a>}
              <button className="button secondary" type="button" onClick={() => { setView('details'); setSettingsOpen(true) }}><Settings2 size={16} /> {readOnly ? 'Run details' : 'Run settings'}</button>
              {canExecute && <><StopRunButton active={isRunning && job?.pdf_id === selectedId} stopping={stopPending || job?.status === 'stopping'} onStop={stopRun} />
              <ResumeRunButton canResume={selected.can_resume && needsRecovery} disabled={runDisabled || !recoveryData?.data?.can_continue} onResume={continueSavedRun} />
              {selected.can_resume && !needsRecovery && <button className="button secondary" type="button" disabled={runDisabled || !recoveryData?.data?.can_continue} onClick={continueSavedRun}>Assess saved connections</button>}
              <RedoExternalValidationButton available={selected.can_redo_external_validation} disabled={runDisabled || !recoveryData?.data?.can_continue} onRedo={redoSavedExternalValidation} readOnly={!canExecute} />
              <button className="button primary" type="button" disabled={runDisabled} onClick={() => needsRecovery ? setConfirmingRestart(true) : startRun(false)}>
                {isRunning || starting ? <LoaderCircle className="spin" size={16} /> : <Play size={15} fill="currentColor" />}{isRunning ? 'Running pipeline' : starting ? 'Starting pipeline' : needsRecovery ? 'Restart pipeline' : selected.status === 'completed' ? 'Run again' : selected.status === 'blocked_text_quality' ? 'Retry pipeline' : 'Run pipeline'}</button></>}</div>
          </div>

          {canExecute && selected.can_redo_external_validation && <p className="soft-note">Continue reuses completed checks. Redo repeats neighboring-pair retrieval and external judgements, keeping saved extraction and compatible source checks.</p>}
          {canExecute && !needsRecovery && selected.can_redo_external_validation && recoveryData?.data && !recoveryData.data.can_continue && recoveryData.data.reason && <p className="inline-error">Cannot reuse saved extraction: {recoveryData.data.reason}</p>}
          {needsRecovery && <RunRecoveryNotice kind={recoveryEvent?.pdfId === selectedId ? recoveryEvent.kind : recoveryData?.data?.job_missing ? 'missing' : selected.status === 'stopped' ? 'stopped' : recoveryData?.data?.interrupted ? 'interrupted' : 'failed'}
            jobId={recoveryEvent?.pdfId === selectedId ? recoveryEvent.jobId : recoveryData?.data?.job_id}
            error={recoveryEvent?.pdfId === selectedId ? recoveryEvent.error : recoveryData?.data?.error}
            recovery={recoveryData?.data} loading={!recoveryData || recoveryData.loading} checkError={recoveryData?.error}
            disabled={runDisabled} confirmingRestart={confirmingRestart} onContinue={continueSavedRun}
            onAskRestart={() => setConfirmingRestart(true)} onCancelRestart={() => setConfirmingRestart(false)}
            onRestart={() => startRun(false)} onViewActivity={() => setView('activity')}
            onCheckAgain={checkRecovery} />}

          <div className="overview-grid" aria-label="Paper findings overview">
            <button type="button" className="overview-card" onClick={() => showClaims()}><span className="metric-icon"><Layers3 size={19} /></span><span><span className="eyebrow">EXTRACTED CLAIMS</span><strong>{selected.summary?.extracted ?? claims.length}</strong><small>Review all claims <ArrowRight size={12} /></small></span></button>
            <button type="button" className="overview-card" onClick={() => showClaims('source')}><span className="metric-icon"><Check size={19} /></span><span><span className="eyebrow">SOURCE SUPPORTED</span><strong>{selected.summary?.source_entailed ?? (claims.some(item => item.candidate.source_validation) ? claims.filter(item => item.candidate.source_validation?.decision === 'entailed').length : '—')}</strong><small>{selected.summary?.pathway_eligible_claims != null ? `Faithful to the PDF · ${selected.summary.pathway_eligible_claims} in scope for pathways` : 'Faithful to the source PDF'} <ArrowRight size={12} /></small></span></button>
            <button type="button" className="overview-card" onClick={() => { setGraphMode('assembled'); setView('network') }}><span className="metric-icon"><Network size={19} /></span><span><span className="eyebrow">{pathwayOnly ? 'ASSEMBLED RELATIONS' : 'GRAPH RELATIONS'}</span><strong>{pathwayOnly ? paperAssembly.edges.length : selected.summary?.graph?.edges ?? graph?.counts?.edges ?? 0}</strong><small>{pathwayOnly ? supportedRoutes : selected.summary?.graph?.corroborated ?? 0} {pathwayOnly ? 'locally supported connections' : 'corroborated'} · Explore network & pathways <ArrowRight size={12} /></small></span></button>
            <button type="button" className="overview-card" onClick={() => setView('activity')}><span className="metric-icon"><Activity size={19} /></span><span><span className="eyebrow">RUN STATUS</span><strong className="metric-status">{statusLabels[selected.status] || selected.status}</strong><small>{selected.summary?.completed_at || selected.summary?.updated_at ? `Updated ${new Date(selected.summary.completed_at || selected.summary.updated_at).toLocaleDateString()}` : 'Open activity log'} <ArrowRight size={12} /></small></span></button>
          </div>


          {!needsRecovery && (isRunning || selected.summary?.external_incomplete > 0 || selected.summary?.extraction_coverage?.complete === false || selected.status === 'failed' || selected.document_scope?.text_quality === 'unusable') &&
            <div className={`findings-notice ${isRunning ? 'in-progress' : ''}`} role="status">
              {isRunning ? <LoaderCircle size={18} className="spin" /> : <CircleAlert size={18} />}
              <div><strong>{isRunning ? 'Analysis in progress' : selected.document_scope?.text_quality === 'unusable' ? 'The PDF needs clearer text' : 'Some findings need attention'}</strong>
                <p>{isRunning ? 'You can explore available claims while the pipeline continues.' : selected.document_scope?.text_quality === 'unusable' ? (readOnly ? 'The saved PDF has text-quality issues. Inspect Run details for the recorded diagnostics.' : 'Use a cleaner PDF or OCR, then retry the pipeline. Details are available in Run details.') : selected.summary?.external_incomplete > 0 ? `${selected.summary.external_incomplete} ${selected.summary.external_validation_target === 'neighboring_claims' ? 'neighboring-pair assessments' : 'claim assessments'} await completed external validation. An unfinished check is not a scientific rejection.` : selected.summary?.extraction_coverage?.complete === false ? 'Some extraction regions were not processed. Check Run details for coverage.' : 'The last run stopped. Open the activity log to inspect the error.'}</p></div>
              <button type="button" className="text-button" onClick={() => setView('activity')}>View activity <ArrowRight size={15} /></button>
            </div>}

          <section className="analysis-panel" aria-label="Paper analysis">
            <WorkspaceNavigation view={view} onChange={setView} claims={claims.length} paths={graphScope === 'paper' ? paperRoutes.length : networkRoutes.length} errors={errorCount} />
            <div className="section-head"><div><span className="eyebrow">{view === 'details' ? 'CONFIGURATION & DIAGNOSTICS' : view === 'activity' ? 'RUN HISTORY' : 'RESEARCH FINDINGS'}</span>
              <h2>{({ claims: 'Extracted claims', network: 'Network & pathways', activity: 'Activity log', details: 'Run details' })[view]}</h2>
              <p>{({ claims: pathwayOnly ? 'Inspect each claim, its exact source passage, and its separate PDF-fidelity and project-scope decisions.' : 'Inspect each claim, its source passage, and the external evidence.', network: 'Explore connected claims and review local connection findings in one place.', activity: 'Follow every stage and inspect warnings or model errors.', details: readOnly ? 'Inspect saved settings, coverage and model activity.' : 'Adjust the run, review coverage, and inspect model activity.' })[view]}</p></div>
              {view === 'claims' && <span className="subtle-count">{claims.length} claims</span>}</div>
            {view === 'claims' && <>
              <FindingsToolbar search={claimSearch} onSearch={setClaimSearch} filter={claimFilter} onFilter={setClaimFilter} counts={claimCounts} shown={visibleClaims.length} />
              <div className="claim-list">{paperLoading && !visibleClaims.length ? <div className="claims-loading-loop" role="status"><LoaderCircle size={28} className="spin" /><p>Loading extracted claims for this paper…</p></div> : visibleClaims.length ? pageClaims.map(({ record, index }) => <ClaimCard readOnly={!canExecute} key={record.candidate.candidate_id} record={record} index={index} busy={pipelineBusy} onClaimAction={runClaimAction} onInspectPathway={inspectPathway} />)
                : <Empty icon={Layers3} title={claims.length ? 'No matching claims' : 'No claims extracted yet'}>{claims.length ? 'Try a different search or decision filter.' : readOnly ? 'No claims were included in this publication.' : 'Run the PDF to see its claims, source quotes, and article decisions.'}</Empty>}</div>
              {visibleClaims.length > claimsPageSize && <div className="claim-pagination">
                <span>Showing {pageClaims.length} of {visibleClaims.length} claims</span>
                <button type="button" className="button secondary" onClick={() => setClaimsPageSize(prev => prev + 60)}>Show 60 more</button>
                <button type="button" className="button secondary" onClick={() => setClaimsPageSize(visibleClaims.length)}>Show all ({visibleClaims.length})</button>
              </div>}
              <ExtractionDiscards records={discards} />
            </>}
            {view === 'network' && <ResearchNetwork key={selected.id} assembly={trustData} assessments={pathAssessments} graph={graph}
              dataMode={graphMode} onDataMode={setGraphMode} scope={graphScope} onScope={setGraphScope}
              selected={selected} selectedElement={selectedElement} onSelect={setSelectedElement}
              onInspectClaim={inspectClaim} canInspectClaim={id => claims.some(record => record.candidate.candidate_id === id)}
              onViewClaims={() => showClaims()} onAssemble={assembleSavedPaths} busy={runDisabled} readOnly={!canExecute} pathwayOnly={pathwayOnly}
              onDepthChange={depth => updatePathways(true, depth)} onLoadMore={() => updatePathways(true, null, true)}
              focusedPathwayId={focusedPathwayId} onClearFocus={() => setFocusedPathwayId(null)}
              onOpenSettings={() => { setSettingsOpen(true); setView('details') }}
              historicalAssembly={pathwayData} historicalPaths={paths}
              onHistoricalDepthChange={depth => updatePathways(false, depth)} onHistoricalLoadMore={() => updatePathways(false, null, true)} api={API} />}
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
