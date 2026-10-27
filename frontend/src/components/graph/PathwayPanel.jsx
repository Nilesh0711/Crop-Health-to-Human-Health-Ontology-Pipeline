import { useState } from 'react'
import GraphView from './GraphView.jsx'

export default function PathwayPanel({ data, busy, onAssemble, onLoadMore, onDepthChange }) {
  const [showGraph, setShowGraph] = useState(false)
  const [selection, setSelection] = useState(null)
  const graph = data?.graph || { nodes: [], edges: [] }
  return <section aria-label="Global pathway assembly">
    <div className="graph-controls"><div>
      <strong>Pathway Assembly · all papers</strong>
      <p>{data?.counts?.claims ?? 0} validated claims · {data?.counts?.entities ?? 0} aligned entities · {data?.counts?.papers ?? 0} papers</p>
    </div><button type="button" className="button secondary" disabled={busy} onClick={onAssemble}>
      {busy ? 'Pipeline running' : 'Assemble validated claims'}</button>
      <button type="button" className="button secondary" onClick={() => setShowGraph(!showGraph)}>{showGraph ? 'Hide assembled graph' : 'Show assembled graph'}</button>
      <label>Path length (edges)<select value={data?.max_hops ?? 6} onChange={event => onDepthChange?.(Number(event.target.value))}>
        {Array.from({ length: 11 }, (_, n) => n + 2).map(n => <option key={n} value={n}>{n}</option>)}
      </select></label>
    </div>
    <p>Only source-entailed, in-scope claims with accepted independent external support enter assembly. MiMo Flash aligns their entities; connections retain conditions and do not establish causal-chain validity.</p>
    {data?.stale && <p role="status">Saved claims have changed. Assemble again to update these connections.</p>}
    {data?.status === 'failed' && <p role="alert">Assembly stopped: {data.error}. Saved claims remain available.</p>}
    {data?.status === 'running' && <p role="status">Global assembly is in progress.</p>}
    {showGraph && <GraphView graph={graph} activeFilter="all" onSelect={setSelection} />}
    {selection && <div className="graph-selection"><button type="button" className="button secondary" onClick={() => setSelection(null)}>Close details</button>
      <strong>{selection.kind === 'edge' ? `${selection.source_label} → ${selection.relation} → ${selection.target_label}` : selection.label}</strong>
      {selection.quote && <blockquote>{selection.quote}</blockquote>}
      {selection.paper && <p>{selection.paper} · {selection.external_status}</p>}
      {selection.aliases && <p>Aligned names: {selection.aliases.join(', ')}</p>}
    </div>}
    <div className="paths-list">{data?.paths?.length ? data.paths.map((path, index) => <article className="path-card" key={path.map(e => e.id).join('|')}>
      <span className="eyebrow">ASSEMBLED PATH {index + 1}</span>
      <div>{path.map((edge, n) => <span key={edge.id}>
        {n === 0 && <strong>{edge.source_label}</strong>}
        <em>{edge.relation.replaceAll('_', ' ')}</em><strong>{edge.target_label}</strong>
      </span>)}</div>
      <details><summary>Original claims and passages</summary>{path.map(edge => <div key={edge.id}>
        <p><strong>{edge.subject} → {edge.relation} → {edge.object}</strong></p>
        <p>{edge.paper}{edge.source_page ? ` · page ${edge.source_page}` : ''} · {edge.candidate_id}</p>
        <p>Source: {edge.source_status} · External: {edge.external_status} · {edge.certainty}</p>
        {Object.entries(edge.qualifiers || {}).filter(([, value]) => value).map(([key, value]) => <p key={key}>{key.replaceAll('_', ' ')}: {value}</p>)}
        <blockquote>{edge.quote}</blockquote>
      </div>)}</details>
    </article>) : <p>No connected multi-edge pathways of validated claims assembled yet. You can assemble eligible saved claims without repeating extraction or external validation.</p>}</div>
    {!!data?.paths?.length && <p>Showing paths up to {data.max_hops} edges. The assembled graph contains only eligible validated claims.</p>}
    {data?.has_more && <button type="button" className="button secondary" onClick={onLoadMore}>Show more paths</button>}
  </section>
}
