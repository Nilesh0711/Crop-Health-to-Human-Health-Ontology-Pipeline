import { useEffect, useRef, useState } from 'react'
import { BookOpen, CircleAlert, GitBranch, Info, Layers3, Network, Search, X } from 'lucide-react'
import GraphView from './GraphView.jsx'
import TrustEvidence from '../claims/TrustEvidence.jsx'
import { EvidenceBadge, PathwayCard, PathwayEmpty } from './PathwayCards.jsx'
import { entityLabel, evidenceState, matchesEdge, matchesPath, orderClaims, readable } from '../../lib/pathways.js'

export default function PathwayPanel({ data, busy, onAssemble, onLoadMore, onDepthChange, readOnly = false }) {
  const [showGraph, setShowGraph] = useState(false)
  const [selection, setSelection] = useState(null)
  const [sortBy, setSortBy] = useState('score')
  const [pane, setPane] = useState('paths')
  const [query, setQuery] = useState('')
  const [state, setState] = useState('all')
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadingDepth, setLoadingDepth] = useState(false)
  const selectedRef = useRef(null)
  const triggerRef = useRef(null)
  const graph = data?.graph || { nodes: [], edges: [] }
  const paths = data?.paths || []
  const provisional = data?.view === 'provisional'
  const states = [...new Set(graph.edges.map(evidenceState))].sort()
  const visibleClaims = orderClaims(graph.edges.filter(edge => matchesEdge(edge, query, state)), sortBy)
  const visiblePaths = paths.map((path, n) => ({ path, number: n + 1 })).filter(({ path }) => matchesPath(path, query, state))
  const links = data?.pathway_connections || []
  useEffect(() => { if (selection) selectedRef.current?.focus() }, [selection])
  useEffect(() => { setSelection(null); setQuery(''); setState('all'); setPane('paths') }, [data?.view])

  function inspect(value) {
    if (value) triggerRef.current = document.activeElement
    setSelection(value)
  }
  function closeSelection() {
    setSelection(null)
    triggerRef.current?.focus({ preventScroll: true })
  }
  async function loadMore() {
    if (loadingMore) return
    setLoadingMore(true)
    try { await onLoadMore?.() } finally { setLoadingMore(false) }
  }
  async function changeDepth(value) {
    setLoadingDepth(true)
    try { await onDepthChange?.(value) } finally { setLoadingDepth(false) }
  }

  if (!data) return <section className="pathway-workspace" aria-label="Global pathway assembly" role="status">
    <PathwayEmpty title="Loading saved pathways">Retrieving the saved network and its evidence.</PathwayEmpty></section>

  return <section className="pathway-workspace" aria-label="Global pathway assembly">
    <div className="pathway-overview">
      <div className="pathway-overview-title"><span className="pathway-overview-icon"><Network size={21} /></span><div>
        <h3>{provisional ? 'Explore the trust network' : 'Explore validated connections'}</h3>
        <p>Across all papers in your workspace</p></div><span className="pathway-badge">{provisional ? 'Provisional network' : 'Validated edges'}</span></div>
      <dl className="pathway-metrics">{[
        [Layers3, data?.counts?.claims ?? graph.edges.length, provisional ? 'Source-grounded claims' : 'Validated claims'],
        [Network, data?.counts?.entities ?? graph.nodes.length, 'Aligned entities'],
        [BookOpen, data?.counts?.papers ?? 0, 'Source papers'],
        [GitBranch, paths.length, 'Loaded paths'],
      ].map(([Icon, value, label]) => <div key={label}><dt><Icon size={14} />{label}</dt><dd>{value}</dd></div>)}</dl>
    </div>
    <div className="pathway-actions"><button type="button" className={`button secondary ${showGraph ? 'is-selected' : ''}`}
      aria-expanded={showGraph} aria-controls="assembled-pathway-graph" onClick={() => setShowGraph(!showGraph)}>
      <Network size={15} />{showGraph ? 'Hide assembled graph' : 'Show assembled graph'}</button>
      {!readOnly && !provisional && <button type="button" className="button secondary" disabled={busy} onClick={onAssemble}>
        <GitBranch size={15} />{busy ? 'Pipeline running' : 'Assemble validated claims'}</button>}
      <label>Path length (edges)<select aria-label="Path length in edges" value={data?.max_hops ?? 6} disabled={loadingDepth || loadingMore}
        onChange={event => changeDepth(Number(event.target.value))}>
        {Array.from({ length: 11 }, (_, n) => n + 2).map(n => <option key={n} value={n}>{n}</option>)}</select></label>
    </div>
    <details className="pathway-guidance"><summary><Info size={14} />How to read these findings</summary>
      <p>{provisional ? 'Candidate connections are assembled before external validation. Evidence balance measures distinct original study support minus contradiction; graph contribution counts adjacent two-edge connections. Neither is a probability or proof of a whole causal pathway.' : 'Only source-entailed, in-scope claims with accepted independent external support enter assembly. MiMo Flash aligns their entities; connections retain conditions and do not establish causal-chain validity.'}</p>
    </details>
    {data?.stale && <p className="pathway-notice" role="status"><CircleAlert size={16} />Saved claims have changed. {readOnly ? 'This snapshot preserves the saved assembly and its stale-data warning.' : provisional ? 'Resume the pipeline to update this network.' : 'Assemble again to update these connections.'}</p>}
    {data?.status === 'failed' && <p className="pathway-notice error" role="alert">Assembly stopped: {data.error}. Saved claims remain available.</p>}
    {data?.status === 'running' && <p className="pathway-notice" role="status">{readOnly ? 'Assembly was recorded as in progress when published. This viewer does not run it.' : 'Global assembly is in progress. Saved findings remain available.'}</p>}
    {showGraph && <div id="assembled-pathway-graph" className="pathway-graph"><GraphView graph={graph} activeFilter="all" onSelect={inspect} /></div>}
    {selection && <section className="pathway-selection" aria-label="Selected pathway evidence" tabIndex={-1} ref={selectedRef}>
      <div className="pathway-section-heading"><span className="eyebrow">{selection.kind === 'edge' ? 'SELECTED CLAIM' : 'ALIGNED ENTITY'}</span>
        <button type="button" className="button secondary" onClick={closeSelection}><X size={14} />Close details</button></div>
      <h3>{selection.kind === 'edge' ? `${entityLabel(selection, 'source')} → ${readable(selection.relation)} → ${entityLabel(selection, 'target')}` : selection.label}</h3>
      {selection.quote && <blockquote>{selection.quote}</blockquote>}
      {selection.paper && <p>{selection.paper}{selection.source_page ? ` · page ${selection.source_page}` : ''} · {readable(selection.external_status)}</p>}
      {selection.aliases && <p>Aligned names: {selection.aliases.join(', ')}</p>}
      {selection.trust && <TrustEvidence trust={selection.trust} />}
    </section>}
    <div className="pathway-tabs" role="group" aria-label="Network findings">
      {[
        ['paths', 'Pathways', paths.length], ['claims', 'Network claims', graph.edges.length],
        ...(provisional ? [['connections', 'Connection assessments', links.length]] : []),
      ].map(([key, label, count]) => <button type="button" key={key} aria-pressed={pane === key} className={pane === key ? 'active' : ''} onClick={() => setPane(key)}>
        {label}<span>{count}</span></button>)}
    </div>
    {pane !== 'connections' && <div className="pathway-searchbar"><label className="pathway-search"><Search size={16} />
      <input type="search" aria-label="Search pathways and claims" placeholder="Search an entity, relation or paper" value={query} onChange={e => setQuery(e.target.value)} /></label>
      {provisional && <label className="pathway-state-filter">Evidence state<select aria-label="Filter pathway evidence state" value={state} onChange={e => setState(e.target.value)}>
        <option value="all">All states</option>{states.map(value => <option key={value} value={value}>{readable(value)}</option>)}</select></label>}
      {(query || state !== 'all') && <button type="button" className="text-button" onClick={() => { setQuery(''); setState('all') }}>Clear filters</button>}
    </div>}
    {pane === 'paths' && <>
      <p className="pathway-results-count" role="status">{visiblePaths.length} of {paths.length} loaded paths{state !== 'all' ? ' · each contains at least one claim in the selected state' : ''}.
        {provisional ? ' Provisional paths may include unfinished or contested claims.' : ' Every edge meets the validated graph requirements.'}</p>
      <div className="research-paths-list">{visiblePaths.length ? visiblePaths.map(({ path, number }) =>
        <PathwayCard key={path.map(e => e.id).join('|')} path={path} number={number} provisional={provisional} onSelect={inspect} />)
        : <PathwayEmpty title={paths.length ? 'No matching pathways' : 'No connected pathways yet'}
          action={<button type="button" className="button secondary" onClick={() => setPane('claims')}>View network claims</button>}>
          {paths.length ? 'Try a different entity or evidence state. Filters search the currently loaded paths.' : readOnly ? 'No connected paths were saved in this snapshot. Explore the available network claims.' : provisional
            ? 'Source-grounded standalone claims remain in the trust table under Network claims. Run or resume the pipeline to build this network.'
            : 'You can assemble eligible saved claims without repeating extraction or external validation.'}</PathwayEmpty>}</div>
      {data?.has_more && <div className="pathway-load-more"><button type="button" className="button secondary" disabled={loadingMore || loadingDepth} aria-busy={loadingMore} onClick={loadMore}>{loadingMore ? 'Loading paths…' : 'Show more paths'}</button>
        <small>More paths may match your filters. Showing connections up to {data.max_hops} edges.</small></div>}
    </>}
    {pane === 'claims' && <>
      <div className="pathway-section-heading"><div><h3>Claims in this network</h3><p>Includes standalone claims. Select a claim to inspect its exact passage and evidence balance.</p></div>
        {provisional && <label>Sort claims<select aria-label="Sort trust claims" value={sortBy} onChange={e => setSortBy(e.target.value)}>
          <option value="score">Evidence balance</option><option value="contribution">Graph contribution</option></select></label>}</div>
      <p className="pathway-results-count" role="status">{visibleClaims.length} of {graph.edges.length} claims</p>
      <div className="pathway-claims-table"><table><thead><tr><th>Original claim</th><th>Evidence state</th>{provisional && <><th>Balance</th><th>Connections</th></>}<th>Source / external</th></tr></thead>
        <tbody>{visibleClaims.map(edge => <tr key={edge.id}><td><button type="button" className="pathway-claim-link" onClick={() => inspect({ ...edge, kind: 'edge' })}>
          {edge.subject || entityLabel(edge, 'source')} <span>{readable(edge.relation)}</span> {edge.object || entityLabel(edge, 'target')}</button><small>{edge.paper}</small></td>
          <td><EvidenceBadge edge={edge} /></td>{provisional && <><td className="pathway-numeric">{edge.trust?.score > 0 ? '+' : ''}{edge.trust?.score ?? 0}</td>
          <td className="pathway-numeric">{edge.trust?.contribution?.two_edge_connections ?? 0}</td></>}
          <td><small>Source: {readable(edge.source_status) || 'Not recorded'}</small><small>External: {readable(edge.external_status) || 'Not assessed'}</small></td></tr>)}</tbody></table>
        {!visibleClaims.length && <PathwayEmpty title={graph.edges.length ? 'No matching claims' : 'No network claims yet'}>{readOnly ? 'Try different filters. Only published findings are available.' : 'Try different filters or resume the saved pipeline.'}</PathwayEmpty>}</div>
    </>}
    {pane === 'connections' && <div className="pathway-connections"><div className="pathway-section-heading"><div><h3>Cross-paper connection compatibility assessments</h3>
      <p>Conditions and entity identity are compared for adjacent edges. Missing connections remain unassessed; compatibility does not establish a causal chain.</p></div></div>
      {links.length ? links.map((row, n) => <article key={`${row.document_key}-${row.upstream_claim_id}-${row.downstream_claim_id}-${n}`}>
        <span className="pathway-badge">{readable(row.compatibility)}</span><p>{row.reason}</p><small>{row.document_key}</small>
        <details><summary>Claim and paper identifiers</summary><p>{row.upstream_claim_id} → {row.downstream_claim_id}</p>
          <p>Paper IDs: {(row.evidence_document_ids || []).join(', ') || 'No decisive compatibility evidence'}</p></details></article>)
        : <PathwayEmpty title="No connection assessments yet">Completed cross-paper synthesis will show assessed adjacent connections here.</PathwayEmpty>}</div>}
  </section>
}
