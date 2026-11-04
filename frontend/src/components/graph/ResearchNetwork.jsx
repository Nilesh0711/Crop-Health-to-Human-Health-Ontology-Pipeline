import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowRight, BookOpen, CircleAlert, Download, ExternalLink, GitBranch, LoaderCircle, Network, Search, SlidersHorizontal, X } from 'lucide-react'
import { downloadGraphJson } from '../../lib/graphDownload.js'
import GraphView, { GraphSelectionDetails } from './GraphView.jsx'
import { PathwayAssessmentCard } from './PathwayAssessments.jsx'
import PathwayPanel from './PathwayPanel.jsx'
import { CorroboratedPaths, EvidenceBadge, PathwayCard } from './PathwayCards.jsx'
import { PATHWAY_OUTCOMES, entityLabel, evidenceState, matchesEdge, readable } from '../../lib/pathways.js'
import { combineRoutes, filterRoutes, graphForRoutes, includeRouteContext, routeGroup, routeHighlights, routeLabel, splitLocalAssessments } from '../../lib/researchNetwork.js'
import { scopeAssemblyGraph } from '../../lib/graphNetwork.js'

const EMPTY_GRAPH = { nodes: [], edges: [] }

function RouteSequence({ edges }) {
  return <span className="network-route-sequence"><span>{entityLabel(edges[0], 'source')}</span>
    {edges.map(edge => <span key={edge.id}><ArrowRight size={12} aria-hidden="true" /><span>{entityLabel(edge, 'target')}</span></span>)}</span>
}

export default function ResearchNetwork({ assembly, assessments, graph, dataMode = 'assembled', onDataMode,
  scope = 'paper', onScope, selected, selectedElement, onSelect, onInspectClaim, canInspectClaim,
  onViewClaims, onAssemble, busy, readOnly, pathwayOnly, onDepthChange, onLoadMore, onOpenSettings,
  focusedPathwayId, onClearFocus, historicalAssembly, historicalPaths, onHistoricalDepthChange, onHistoricalLoadMore, api }) {
  const local = assessments?.policy === 'neighbor-pair-v1'
  const [query, setQuery] = useState('')
  const [group, setGroup] = useState('all')
  const [outcome, setOutcome] = useState('all')
  const [sort, setSort] = useState('outcome')
  const [edgeFilter, setEdgeFilter] = useState('all')
  const [claimQuery, setClaimQuery] = useState('')
  const [claimState, setClaimState] = useState('all')
  const [activeKey, setActiveKey] = useState(null)
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [legacyView, setLegacyView] = useState('trust')
  const evidenceRef = useRef(null)
  const selectionRef = useRef(null)
  const scrollSelection = useRef(false)
  const scrollEvidence = useRef(false)
  const handledFocus = useRef(null)
  const completeGraph = assembly?.graph || EMPTY_GRAPH
  const assessmentSets = useMemo(() => splitLocalAssessments(assessments, assembly), [assessments, assembly])
  const allRows = useMemo(() => combineRoutes(assembly?.paths, assessmentSets.current), [assembly?.paths, assessmentSets.current])
  const historicalRows = useMemo(() => combineRoutes([], assessmentSets.historical)
    .map(row => ({ ...row, key: `previous:${row.record.pathway_id}` })), [assessmentSets.historical])
  const paperKey = scope === 'paper' ? selected?.title : null
  const scopedRows = useMemo(() => filterRoutes(allRows, { paperKey }), [allRows, paperKey])
  const visibleRows = useMemo(() => filterRoutes(allRows, { paperKey, group, outcome, query, sort }), [allRows, paperKey, group, outcome, query, sort])
  const historicalScoped = useMemo(() => filterRoutes(historicalRows, { paperKey }), [historicalRows, paperKey])
  const active = [...allRows, ...historicalRows].find(row => row.key === activeKey)
  const scopedGraph = useMemo(() => scopeAssemblyGraph(completeGraph, paperKey), [completeGraph, paperKey])
  const registerClaims = useMemo(() => scopedGraph.edges.filter(edge => matchesEdge(edge, claimQuery, claimState)), [scopedGraph, claimQuery, claimState])
  const filtered = group !== 'all' || outcome !== 'all' || Boolean(query.trim())
  const displayedGraph = useMemo(() => {
    if (dataMode !== 'assembled') return graph || { nodes: [], edges: [] }
    const filteredGraph = filtered ? graphForRoutes(scopedGraph, visibleRows) : scopedGraph
    return includeRouteContext(filteredGraph, completeGraph, active)
  }, [dataMode, graph, filtered, scopedGraph, visibleRows, completeGraph, active])
  const highlights = useMemo(() => routeHighlights(displayedGraph, active), [displayedGraph, active])
  const counts = scopedRows.reduce((value, row) => { value[routeGroup(row)] += 1; return value }, { supported: 0, partial: 0, review: 0, excluded: 0 })
  const staleCount = scopedRows.filter(row => row.record?.stale).length
  const selectedPairs = assessments?.coverage?.selected_pairs ?? assessments?.coverage?.selected_pathways ?? 0
  const reusedPairs = assessments?.coverage?.reused_pair_ids?.length ?? 0
  const crossPaperContext = displayedGraph.edges.some(edge => paperKey && edge.document_key && edge.document_key !== paperKey)
  const layerLabels = { assembled: 'Aligned candidate network', main: local ? 'Locally supported connection graph' : 'Saved supported graph', source: 'Source PDF relations', audit: 'All extracted relations · audit' }

  function choose(row, openEvidence = false) {
    setActiveKey(row.key)
    setEdgeFilter('all')
    onDataMode?.('assembled')
    onSelect?.(null)
    setEvidenceOpen(openEvidence)
    if (openEvidence) {
      if (activeKey === row.key && evidenceOpen) evidenceRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      else scrollEvidence.current = true
    }
  }
  function clearRoute() { setActiveKey(null); setEvidenceOpen(false); onSelect?.(null); onClearFocus?.() }
  function clearFilters() { setQuery(''); setGroup('all'); setOutcome('all'); setEdgeFilter('all'); clearRoute() }
  function changeGroup(value) { setGroup(value); setOutcome('all'); setEdgeFilter('all'); clearRoute(); onDataMode?.('assembled') }
  async function changeDepth(value) {
    setLoading(true)
    try { await onDepthChange?.(value) } finally { setLoading(false) }
  }
  async function loadMore() {
    setLoading(true)
    try { await onLoadMore?.() } finally { setLoading(false) }
  }
  useEffect(() => {
    if (scrollEvidence.current && evidenceOpen) {
      scrollEvidence.current = false
      evidenceRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [activeKey, evidenceOpen])
  useEffect(() => {
    if (scrollSelection.current && selectedElement && !evidenceOpen) {
      scrollSelection.current = false
      selectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [selectedElement, evidenceOpen])
  useEffect(() => { setActiveKey(null); setEvidenceOpen(false); setQuery(''); setGroup('all'); setOutcome('all') }, [selected?.id])
  useEffect(() => {
    if (!focusedPathwayId) { handledFocus.current = null; return }
    if (handledFocus.current === focusedPathwayId) return
    const row = [...allRows, ...historicalRows].find(value => value.record?.pathway_id === focusedPathwayId)
    if (!row) return
    handledFocus.current = focusedPathwayId
    setQuery(''); setGroup('all'); setOutcome('all')
    onScope?.('all'); onDataMode?.('assembled')
    choose(row, true)
  }, [focusedPathwayId, allRows, historicalRows])

  if (!assembly && !assessments) return <div className="network-loading-loop" role="status"><LoaderCircle size={26} className="spin" /><p>Loading saved network and pathway findings…</p></div>
  return <section className="research-network" aria-label="Research network and pathways">
    <div className="network-heading">
      <div><h3>Explore connections. Review the evidence.</h3><p>{local ? 'Select a chain or neighboring pair to highlight its claims and inspect the evidence.' : 'Select a pathway to highlight its route, then open its evidence.'}</p></div>
      <div className="network-heading-controls">
        <div className="network-scope-toggle" role="group" aria-label="Research network scope selector">
          <button type="button" className={`segmented-btn ${scope === 'paper' ? 'active' : ''}`} onClick={() => { clearRoute(); onScope?.('paper') }}>
            This paper
          </button>
          <button type="button" className={`segmented-btn ${scope === 'all' ? 'active' : ''}`} onClick={() => { clearRoute(); onScope?.('all') }}>
            All papers
          </button>
        </div>
        <label style={{ display: 'none' }}>Scope<select aria-label="Research network scope" value={scope} onChange={event => { clearRoute(); onScope?.(event.target.value) }}>
          <option value="paper">This paper</option><option value="all">All papers</option></select></label>
      </div>
    </div>
    <dl className="network-metrics" aria-label="Network overview">
      {[[scopedGraph.edges.length, 'Source-grounded claims'], [scopedGraph.nodes.length, 'Aligned entities'], [scopedRows.length, local ? 'Chains and local pairs' : 'Loaded pathways'], [counts.supported, local ? 'Locally supported pairs' : 'Currently fully supported']].map(([value, label]) =>
        <div key={label}><dd>{value}</dd><dt>{label}</dt></div>)}
    </dl>
    {staleCount > 0 && <div className="network-alert" role="status"><CircleAlert size={17} /><p><strong>{staleCount} saved assessment{staleCount === 1 ? ' needs' : 's need'} refresh.</strong> The network changed after assessment. Earlier findings remain available, but are not counted as currently supported.</p></div>}
    {(assembly?.stale || assessments?.assembly_stale) && <p className="network-alert" role="status"><CircleAlert size={16} />Saved claims have changed since assembly. Resume the pipeline to update connections.</p>}
    {(assembly?.error || assessments?.error) && <p className="network-alert" role="alert"><CircleAlert size={16} />{[assembly?.error, assessments?.error].filter(Boolean).join(' · ')}</p>}
    {assembly?.status === 'running' && <p className="network-alert" role="status">Connections are being rebuilt. The last complete saved network remains visible.</p>}
    <div className="network-explorer">
      <div className="network-map">
        <div className="network-map-caption">
          <div className="network-map-caption-left">
            <span><Network size={15} /><strong>Data layer:</strong></span>
            <select aria-label="Network data layer quick select" className="network-layer-select" value={dataMode} onChange={event => { clearRoute(); setEdgeFilter('all'); onDataMode?.(event.target.value) }}>
              {Object.entries(layerLabels).map(([key, labelText]) => <option key={key} value={key}>{labelText}</option>)}
            </select>
            <span className="network-scope-indicator">· {scope === 'paper' ? (selected?.title ? `Paper: ${selected.title}` : 'This paper') : 'All workspace papers'}</span>
          </div>
          <div className="network-map-caption-right">
            {active && <button type="button" className="text-button" onClick={clearRoute}><X size={13} />Clear pathway</button>}
            <div className="network-download-bar">
              <button type="button" className="tool-button" onClick={() => downloadGraphJson(displayedGraph, `network-${scope}-${dataMode}.json`)} title="Download displayed graph as JSON">
                <Download size={13} />JSON
              </button>
              {selected && ['completed', 'completed_with_warnings'].includes(selected.status) && (
                <a className="tool-button" href={`${api}/pdfs/${selected.id}/graph.graphml`} download title="Download saved GraphML">
                  <Download size={13} />GraphML
                </a>
              )}
            </div>
          </div>
        </div>
        <GraphView key={`${selected?.id}:${dataMode}:${scope}`} graph={displayedGraph} activeFilter={edgeFilter} onSelect={(value, event) => { onSelect?.(value); if (value && !event?.refresh) setEvidenceOpen(false) }}
          compactControls highlightedEdgeIds={highlights} onViewClaims={onViewClaims}
          emptyTitle={filtered ? 'No network routes match this filter' : dataMode === 'main' ? local ? 'No locally supported connections yet' : 'No fully supported network yet' : 'No saved connections yet'}
          emptyDescription={filtered ? 'Clear the pathway filters to see all source-grounded claims, including standalone relations.' : dataMode === 'main' ? 'Use the aligned candidate network to explore source-grounded connections while whole routes remain unresolved.' : 'Saved source-grounded claims and connections appear here after assembly.'}
          emptyAction={<button type="button" className="button secondary" onClick={() => { clearFilters(); onDataMode?.('assembled') }}>Show all candidate connections</button>} />
        <p className="network-map-note">{active?.record?.stale ? 'This saved assessment belongs to an earlier network and is not highlighted as a current result.'
          : crossPaperContext ? 'Includes connected papers so the selected route stays complete.'
            : filtered && dataMode === 'assembled' ? 'Showing connections in matching loaded pathways. Clear filters to include standalone claims.'
              : dataMode !== 'assembled' ? 'This saved data layer is independent of the pathway list filters. Switch to the candidate network to explore aligned routes.'
              : local ? 'Blue highlights show a selected route. Local connection support does not establish a complete-chain mechanism.' : 'Connections are candidates. Only a whole-pathway assessment can establish route support.'}</p>
      </div>
      <aside className="network-routes" aria-label="Pathways in this network">
        <div className="network-routes-heading"><h4>{local ? 'Connections & chains' : 'Pathways'} <span>{visibleRows.length}</span></h4>
          <label className="network-route-search"><Search size={14} /><input type="search" aria-label="Search network pathways" placeholder="Find an entity or pathway" value={query}
            onChange={event => { setQuery(event.target.value); clearRoute() }} /></label>
          <select aria-label="Pathway evidence filter" value={group} onChange={event => changeGroup(event.target.value)}>
            <option value="all">All pathways</option><option value="supported">{local ? 'Locally supported pairs' : 'Fully supported'} ({counts.supported})</option>
            <option value="partial">Parts supported ({counts.partial})</option><option value="review">Needs review / unassessed ({counts.review})</option>
            {!!counts.excluded && <option value="excluded">Opposing / excluded ({counts.excluded})</option>}</select>
          {(filtered || active) && <button type="button" className="text-button" onClick={clearFilters}>Clear filters and selection</button>}
        </div>
        <div className="network-route-list">{visibleRows.map((row, n) => <article className={`network-route ${activeKey === row.key ? 'active' : ''}`} key={row.key}>
          <button type="button" className="network-route-select" aria-pressed={activeKey === row.key} onClick={() => choose(row)}>
            <span className="network-route-meta"><span>{row.record?.assessment_scope === 'neighboring_claims' || row.record?.target?.policy === 'neighbor-pair-v1' ? 'Neighbor pair' : 'Candidate chain'} {n + 1}</span><span className={`network-route-state ${routeGroup(row)}`}>{routeLabel(row)}</span></span>
            <RouteSequence edges={row.edges} />{row.localCoverage && <small>{row.localCoverage.supported}/{row.localCoverage.total} local connections supported; whole chain not assessed</small>}<small>{row.edges.length} relations · {row.paperCount != null && row.paperCount > 0 ? `${row.paperCount} external papers across ${row.pairs?.length || 0} pairs` : `${row.record?.paper_assessments?.length || 0} external papers assessed`}</small>
          </button>
          <button type="button" className="network-evidence-link text-button" onClick={() => choose(row, true)}><BookOpen size={13} />View evidence <ArrowDown size={12} /></button>
        </article>)}</div>
        {!visibleRows.length && <div className="network-list-empty"><GitBranch size={22} /><p>{scopedRows.length ? 'No pathways match these filters.' : 'No continuous pathways are loaded. Individual source claims are still available in the network and Claims.'}</p></div>}
        {assembly?.has_more && <button type="button" className="button secondary network-load-more" disabled={loading} onClick={loadMore}>{loading ? 'Loading…' : 'Load more candidate pathways'}</button>}
      </aside>
    </div>
    {selectedElement && !evidenceOpen && <div ref={selectionRef}><GraphSelectionDetails element={selectedElement} onClose={() => onSelect?.(null)}
      onInspectClaim={!canInspectClaim || canInspectClaim(selectedElement.candidate_id) ? onInspectClaim : undefined}
      onInspectPathway={id => { const row = allRows.find(value => value.record?.pathway_id === id); if (row) choose(row, true) }} /></div>}
    {active && evidenceOpen && <section ref={evidenceRef} tabIndex={-1} className="network-evidence" aria-label="Selected pathway evidence">
      <div className="network-evidence-heading"><h4>Evidence for the selected connection or chain</h4><button type="button" className="button secondary" onClick={() => setEvidenceOpen(false)}><X size={13} />Close evidence</button></div>
      {active.record ? <PathwayAssessmentCard record={active.record} paperKey={selected?.title} onInspectClaim={onInspectClaim} />
        : active.pairs?.length ? <>
            <div className="network-reading-guide" style={{ margin: '0 0 1rem 0' }}>
              <p><strong>Composite pathway ({active.edges.length} steps):</strong> Connects <strong>{active.pairs.length} evaluated local pairs</strong> across <strong>{active.paperCount} external papers</strong>. In the neighbor validation framework, individual 2-step links are assessed for local validity without asserting a whole-chain direct effect. Inspect the evidence for each local pair below:</p>
            </div>
            {active.pairs.map(pairRecord => <PathwayAssessmentCard key={pairRecord.pathway_id} record={pairRecord} paperKey={selected?.title} onInspectClaim={onInspectClaim} />)}
          </>
        : <><p className="soft-note">This candidate route has no saved external assessment. Its source passages are available below.</p><PathwayCard path={active.edges} number={allRows.indexOf(active) + 1} provisional pathwayOnly onSelect={value => { scrollSelection.current = true; setEvidenceOpen(false); onSelect?.(value) }} /></>}
    </section>}
    <details className="network-reading-guide"><summary>How to read support and coverage</summary><div>
      {local ? <p><strong>Locally supported:</strong> both qualified claims have applicable external support and their shared connection is compatible. Longer chains remain unassessed as complete mechanisms. Parts supported means the local assessment is incomplete scientifically. Pending and needs-refresh states are not rejection.</p> : <p><strong>Fully supported:</strong> applicable evidence supports the connected route. <strong>Parts supported:</strong> at least one component has evidence; the whole route is not established. Incomplete, unassessed and needs-refresh states are not scientific rejection.</p>}
      {assessments?.coverage && <p>Latest run selection{assessments.coverage.document_key ? ` (${assessments.coverage.document_key})` : ''}: {local
        ? `${selectedPairs} neighboring ${selectedPairs === 1 ? 'pair' : 'pairs'} selected for assessment; each contains two claims and one shared connection. ${reusedPairs} compatible completed pair ${reusedPairs === 1 ? 'assessment' : 'assessments'} reused.`
        : `${assessments.coverage.selected_pathways ?? assessments.records?.length ?? 0} pathways, up to ${assessments.coverage.max_hops ?? assembly?.max_hops ?? 6} edges per chain.`} Saved assessments can include earlier selections across the workspace.</p>}
      {assembly?.candidate_selection?.exhaustive === false && <p>Entity retrieval used a bounded shortlist. Additional connections may remain undiscovered; similarity alone does not establish identity.</p>}
      {local && assessments?.coverage?.has_more && <p>The assessment budget leaves additional neighboring pairs pending. Resume the saved run to continue.</p>}
      {(assembly?.has_more || (!local && (assessments?.coverage?.has_more || assessments?.coverage?.depth_limited))) && <p>Additional candidate chains remain outside the loaded or assessed selection. Display limits do not change saved pair findings.</p>}
      {!!assessments?.coverage?.claims_outside_selected_pathways?.length && <p>{assessments.coverage.claims_outside_selected_pathways.length} source claims are outside the latest selected chains and remain individually externally unvalidated.</p>}
      <p>Only specific assessed pairs carry local verdicts. Other paths through the union graph are not automatically validated.</p>
    </div></details>
    <details className="network-claim-register"><summary>Browse all network claims ({scopedGraph.edges.length})</summary>
      <div className="network-register-filters"><label className="network-route-search"><Search size={14} /><input type="search" aria-label="Search all network claims" value={claimQuery} onChange={event => setClaimQuery(event.target.value)} placeholder="Find an entity, relation or paper" /></label>
        <select aria-label="Network claim evidence state" value={claimState} onChange={event => setClaimState(event.target.value)}><option value="all">All evidence states</option>
          {[...new Set(scopedGraph.edges.map(evidenceState))].sort().map(state => <option key={state} value={state}>{readable(state)}</option>)}</select></div>
      <p className="results-caption">{registerClaims.length} of {scopedGraph.edges.length} source claims</p><div className="pathway-claims-table"><table>
      <thead><tr><th>Source claim</th><th>Source paper</th><th>Evidence state</th><th>Evidence</th></tr></thead><tbody>{registerClaims.map(edge => <tr key={edge.id}>
        <td>{edge.subject || entityLabel(edge, 'source')} <span>{readable(edge.relation)}</span> {edge.object || entityLabel(edge, 'target')}</td><td>{edge.paper || edge.document_key}</td>
        <td><EvidenceBadge edge={edge} /><small>Source: {readable(edge.source_status) || 'Not recorded'}</small><small>External: {readable(edge.external_status) || 'Not assessed'}</small></td>
        <td><button type="button" className="text-button" onClick={() => { scrollSelection.current = true; setEvidenceOpen(false); onSelect?.({ kind: 'edge', ...edge }) }}>Inspect source</button></td>
      </tr>)}</tbody></table></div></details>
    {!!assessments?.historical_records?.length && <details className="network-history"><summary>Historical whole-route assessments ({assessments.historical_records.length})</summary>
      <p className="soft-note">These saved v45 outcomes concern their original complete routes. They do not count as current local-pair support.</p>
      {assessments.historical_records.map(record => <PathwayAssessmentCard key={record.pathway_id} record={record} paperKey={selected?.title} onInspectClaim={onInspectClaim} />)}
    </details>}
    {!!historicalScoped.length && <details className="network-history"><summary>Earlier local assessments ({historicalScoped.length})</summary>
      <p className="soft-note">These records use an earlier evidence contract or a connection no longer present in the current network. They are preserved for audit and excluded from the current list and counts.</p>
      {historicalScoped.map(row => <article key={row.key} className="pathway-proof-item">
        <RouteSequence edges={row.edges} /><p className="soft-note">Previous result: {readable(row.record.scientific_verdict || row.record.verdict)} · {row.record.target?.assessment_contract || 'earlier contract'}</p>
        <button type="button" className="text-button" onClick={() => choose(row, true)}><BookOpen size={13} />Review previous evidence</button>
      </article>)}
    </details>}
    {!pathwayOnly && <details className="network-history"><summary>Historical research views</summary>
      <label>Saved view<select aria-label="Historical pathway view" value={legacyView} onChange={event => setLegacyView(event.target.value)}>
        <option value="trust">Historical trust network</option><option value="assembled">Historical validated assembly</option><option value="health">Historical health paths</option></select></label>
      {legacyView === 'health' ? <CorroboratedPaths paths={historicalPaths || []} /> : <PathwayPanel data={legacyView === 'trust' ? assembly : historicalAssembly}
        readOnly={readOnly} busy={busy} onAssemble={onAssemble} onDepthChange={legacyView === 'trust' ? onDepthChange : onHistoricalDepthChange}
        onLoadMore={legacyView === 'trust' ? onLoadMore : onHistoricalLoadMore} />}
    </details>}
  </section>
}
