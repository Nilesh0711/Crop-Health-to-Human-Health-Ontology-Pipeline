import { ExternalLink, Network } from 'lucide-react'
import GraphView, { GraphSelectionDetails } from './GraphView.jsx'

export default function GraphWorkspace({ graph, mode, onModeChange, scope, onScopeChange, filter, onFilter,
  pathwayOnly, selected, element, onSelect, onInspectPathway, onInspectClaim, onViewClaims, api, assembly }) {
  return <>
    <div className="graph-controls">
      <div className="segmented" aria-label="Network evidence view">
        {(pathwayOnly || assembly?.graph?.edges?.length > 0) && <button type="button" className={mode === 'assembled' ? 'active' : ''}
          onClick={() => onModeChange('assembled')}>Assembled network</button>}
        <button type="button" className={mode === 'main' ? 'active' : ''} onClick={() => onModeChange('main')}>
          {pathwayOnly ? 'Supported pathway network' : 'Historical external graph'}</button>
        <button type="button" className={mode === 'source' ? 'active' : ''} onClick={() => onModeChange('source')}>Source-supported</button>
        <button type="button" className={mode === 'audit' ? 'active' : ''} onClick={() => onModeChange('audit')}>Audit view</button>
      </div>
      <div className="segmented" aria-label="Network document scope">
        <button type="button" className={scope === 'paper' ? 'active' : ''} onClick={() => onScopeChange('paper')}>This paper</button>
        <button type="button" className={scope === 'all' ? 'active' : ''} onClick={() => onScopeChange('all')}>All papers</button>
      </div>
      <select value={filter} onChange={event => onFilter(event.target.value)} aria-label="Filter graph edges">
        <option value="all">All evidence states</option>
        {mode === 'main' && !pathwayOnly && <><option value="corroborated">Independently corroborated</option><option value="single_source">Independent article</option></>}
        {mode === 'source' && <option value="source_supported">Source supported</option>}
        {mode === 'audit' && <><option value="rejected">Rejected</option><option value="semantic_rejected">Semantic rejected</option>
          <option value="semantic_needs_review">Review semantics</option><option value="source_unsupported">Source unsupported</option>
          <option value="source_needs_review">Review source</option><option value="source_reference_supported">Source reference support</option>
          <option value="extracted">Awaiting validation</option><option value="not_found">External evidence not found</option>
          <option value="contested">Contested</option><option value="contradicted">Contradicted</option></>}
      </select>
    </div>
    {mode === 'assembled' && <p className="graph-view-note" role="note"><Network size={15} />
      {graph?.edges?.length || 0} original claims connected through aligned entities. This candidate network is not a validated complete mechanism.</p>}
    {mode === 'assembled' && assembly?.stale && <p className="graph-view-note">Saved claims have changed. Resume assembly to update the remaining connections.</p>}
    {mode === 'assembled' && assembly?.error && <p className="graph-view-note" role="alert">Assembly: {assembly.error}</p>}
    <GraphView key={`${selected?.id}:${mode}:${scope}`} graph={graph} activeFilter={filter} onSelect={onSelect} onViewClaims={onViewClaims}
      emptyTitle={mode === 'main' && pathwayOnly ? 'No fully supported pathways yet' : undefined}
      emptyDescription={mode === 'main' && pathwayOnly ? 'Component-supported and incomplete assessments do not enter this supported-only network. The assembled candidate network remains available.' : undefined}
      emptyAction={mode === 'main' && pathwayOnly ? <button type="button" className="button secondary" onClick={() => onModeChange('assembled')}>View assembled network</button> : undefined} />
    {mode === 'main' && scope === 'paper' && ['completed', 'completed_with_warnings'].includes(selected?.status) && <div className="graph-export">
      <a href={`${api}/pdfs/${selected.id}/graph.graphml`} download><ExternalLink size={13} /> Download GraphML</a></div>}
    <GraphSelectionDetails element={element} onClose={() => onSelect(null)} onInspectPathway={onInspectPathway} onInspectClaim={onInspectClaim} />
    <div className="graph-legend">
      {mode === 'assembled' ? <span>Source-grounded candidate connections. See Pathway assessments for each route's external evidence.</span>
        : mode === 'main' && pathwayOnly ? <span>Members of evidence-supported pathways; inspect each complete route and its conditions.</span>
          : mode === 'main' ? <><span><i className="legend-dot corroborated" />Independently corroborated</span><span><i className="legend-dot single" />Independent article</span></>
            : mode === 'source' ? <span><i className="legend-dot single" />Faithful to source PDF</span>
              : <><span><i className="legend-dot extracted" />Awaiting validation or source-reference only</span><span><i className="legend-dot contested" />Contested</span><span><i className="legend-dot insufficient" />External evidence not found</span></>}
    </div>
  </>
}
