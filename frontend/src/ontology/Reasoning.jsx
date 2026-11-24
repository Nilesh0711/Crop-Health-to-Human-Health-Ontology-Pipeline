import { useMemo, useState } from 'react'
import { useData, encoded, Notice, Pagination, label } from './useData.jsx'

function Proof({ id }) {
  const data = useData(`/ontology/proof?id=${encoded(id)}`)
  return <><Notice {...data} /><ol className="ontology-proof">{data.data?.proof.map(f =>
    <li key={f.id}><span>{f.subject_label || label(f.subject)} → {f.predicate_label || label(f.predicate)} → {f.object_label || label(f.object)}</span>
      <p>{f.rule || 'Recorded premise'} · {f.scientific_status.replaceAll('_', ' ')}</p>
      {f.source && <details><summary>Premise provenance</summary><pre>{JSON.stringify(f.source, null, 2)}</pre></details>}</li>)}</ol></>
}

const NODE_W = 190, NODE_H = 56, GAP_X = 124, ROW = 122, PAD = 12
const FLOW = '#000', LINK = '#8fbcd4'

function domainLabel(domain) {
  if (domain === 'human_animal') return 'Human & diet'
  if (domain === 'plant') return 'Crop & cooking'
  if (domain === 'crop_to_human') return 'Crop → Human'
  return 'Mixed'
}

function wrapLines(text, max, maxLines) {
  const words = String(text || '').split(/\s+/).filter(Boolean)
  const lines = []
  let line = ''
  for (const word of words) {
    if ((line + ' ' + word).trim().length > max && line) { lines.push(line); line = word } else line = (line + ' ' + word).trim()
  }
  if (line) lines.push(line)
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines)
    kept[maxLines - 1] = kept[maxLines - 1].slice(0, max - 1) + '…'
    return kept
  }
  return lines
}

function clip(text, max) {
  const s = String(text || '')
  return s.length > max ? s.slice(0, max - 1) + '…' : s
}

function Node({ x, y, endpoint }) {
  const lines = [endpoint.id, ...wrapLines(endpoint.label || endpoint.mention, 26, 2)]
  const first = y + NODE_H / 2 - (lines.length - 1) * 7 + 4
  return <g>
    <title>{`${endpoint.id} ${endpoint.label} (${endpoint.mapping})${endpoint.mention ? ` — "${endpoint.mention}"` : ''}`}</title>
    <rect x={x} y={y} width={NODE_W} height={NODE_H} fill="#fff" stroke="#000" strokeWidth="1"
      strokeDasharray={endpoint.mapping === 'broader' ? '4 3' : undefined} />
    {lines.map((line, i) => <text key={i} x={x + NODE_W / 2} y={first + i * 14} textAnchor="middle">{line}</text>)}
  </g>
}

function alignmentText(type) {
  if (type === 'subclass') return 'subclass of'
  if (type === 'superclass') return 'superclass of'
  return 'same class'
}

function ChainGraph({ chain }) {
  const n = chain.hops.length
  const colX = col => PAD + col * (NODE_W + GAP_X)
  const width = PAD * 2 + (n > 1 ? NODE_W * 2 + GAP_X + 165 : NODE_W + 250)
  const height = PAD * 2 + n * ROW + NODE_H
  const flowMarker = `flow-${chain.id}`, linkMarker = `link-${chain.id}`
  return <div className="merge-graph-scroll">
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img"
      aria-label={`Chain ${chain.paper_trail.join(' to ')}`} fontFamily="'Times New Roman', Times, serif" fontSize="12">
      <defs>
        <marker id={flowMarker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill={FLOW} />
        </marker>
        <marker id={linkMarker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill={LINK} />
        </marker>
      </defs>
      {chain.hops.map((hop, i) => {
        const col = i % 2
        const x = colX(col)
        const top = PAD + i * ROW
        const bottom = top + ROW
        const cx = x + NODE_W / 2
        const connector = i > 0 ? chain.connectors[i - 1] : null
        const prevCol = (i - 1) % 2
        const xFrom = prevCol < col ? colX(prevCol) + NODE_W : colX(prevCol)
        const xTo = prevCol < col ? x : x + NODE_W
        const lineY = top + NODE_H / 2
        const midX = (xFrom + xTo) / 2
        return <g key={`${hop.candidate_id}-${i}`}>
          <Node x={x} y={top} endpoint={hop.subject} />
          <Node x={x} y={bottom} endpoint={hop.object} />
          <line x1={cx} y1={top + NODE_H} x2={cx} y2={bottom} stroke={FLOW} strokeWidth="1.3" markerEnd={`url(#${flowMarker})`} />
          <text x={cx + 8} y={(top + NODE_H + bottom) / 2 + 4}>{`${hop.predicate_alias} ${clip(hop.relation_label, 38)}`}</text>
          {connector && <g>
            <line x1={xFrom} y1={lineY} x2={xTo} y2={lineY} stroke={LINK} strokeWidth="2"
              markerStart={connector.alignment_type !== 'subclass' ? `url(#${linkMarker})` : undefined}
              markerEnd={connector.alignment_type !== 'superclass' ? `url(#${linkMarker})` : undefined} />
            <text x={midX} y={lineY - 7} textAnchor="middle">{alignmentText(connector.alignment_type)}</text>
            {connector.context_tier === 'context_shift' && <text x={midX} y={lineY + 16} textAnchor="middle">context differs</text>}
          </g>}
        </g>
      })}
    </svg>
  </div>
}

function formatFieldLabel(key) {
  const spaced = String(key || '').replaceAll('_', ' ')
  return spaced ? spaced[0].toUpperCase() + spaced.slice(1) : ''
}

function QualifierList({ qualifiers }) {
  const entries = Object.entries(qualifiers || {}).filter(([, v]) => Boolean(v))
  if (!entries.length) return null
  return <dl className="merge-qualifiers-dl">
    {entries.map(([k, v]) => <div key={k} className="merge-qualifier-row">
      <dt>{formatFieldLabel(k)}</dt>
      <dd>{v}</dd>
    </div>)}
  </dl>
}

function ChainDetails({ chain }) {
  return <details className="merge-details">
    <summary>Details</summary>
    <ol className="merge-hop-list">
      {chain.hops.map((hop, i) => {
        const connector = i < chain.connectors.length ? chain.connectors[i] : null
        const nextPaper = chain.hops[i + 1]?.paper_short || 'Next step'
        return <li key={`${hop.candidate_id}-${i}`} className="merge-hop">
          <div className="merge-hop-meta">Step {i + 1} · {hop.paper_short}</div>
          <div className="merge-hop-claim">
            <span>{hop.subject.label}</span>
            <span className="merge-hop-arrow" aria-hidden="true"> → </span>
            <strong>{hop.predicate_alias} {hop.relation_label}</strong>
            <span className="merge-hop-arrow" aria-hidden="true"> → </span>
            <span>{hop.object.label}</span>
          </div>
          <QualifierList qualifiers={hop.qualifiers} />
          {hop.quote && <details className="merge-source-details">
            <summary>Source text (p. {hop.source_page ?? '?'})</summary>
            <blockquote className="merge-quote">{hop.quote}</blockquote>
          </details>}
          {connector && connector.context_tier === 'context_shift' && <div className="merge-context-shift">
            <div className="merge-context-shift-title">↔ Context differs between steps {i + 1} &amp; {i + 2}</div>
            <div className="merge-diff-table" role="table" aria-label="Context differences">
              <div className="merge-diff-header" role="row">
                <span role="columnheader">Qualifier</span>
                <span role="columnheader">{hop.paper_short}</span>
                <span role="columnheader" aria-hidden="true" />
                <span role="columnheader">{nextPaper}</span>
              </div>
              {connector.differing_qualifiers.map((d, j) => <div key={j} className="merge-diff-row" role="row">
                <span className="merge-diff-field" role="cell">{formatFieldLabel(d.field)}</span>
                <span className="merge-diff-val" role="cell">{d.left}</span>
                <span className="merge-diff-sep" role="cell"> ↔ </span>
                <span className="merge-diff-val" role="cell">{d.right}</span>
              </div>)}
            </div>
          </div>}
        </li>
      })}
    </ol>
  </details>
}

function MergedPathwaysSection({ merged }) {
  const [domain, setDomain] = useState('all')
  const [includeBroader, setIncludeBroader] = useState(true)
  const [includeHierarchy, setIncludeHierarchy] = useState(true)
  const [includeContextShift, setIncludeContextShift] = useState(true)
  const [search, setSearch] = useState('')
  const [showLimit, setShowLimit] = useState(10)

  const chains = merged?.chains || []
  const hubs = merged?.convergent_hubs || []
  const gaps = merged?.evidence_gaps || []

  const filteredChains = useMemo(() => {
    const q = search.trim().toLowerCase()
    return chains.filter(ch => {
      if (domain !== 'all' && ch.domain !== domain) return false
      if (!includeBroader && ch.uses_broader) return false
      if (!includeHierarchy && ch.has_hierarchy) return false
      if (!includeContextShift && ch.has_context_shift) return false
      return !q || JSON.stringify(ch).toLowerCase().includes(q)
    })
  }, [chains, domain, includeBroader, includeHierarchy, includeContextShift, search])

  const filteredHubs = useMemo(() => {
    const q = search.trim().toLowerCase()
    return hubs.filter(h => {
      if (domain !== 'all' && h.domain !== domain) return false
      if (!includeBroader && h.uses_broader) return false
      return !q || JSON.stringify(h).toLowerCase().includes(q)
    })
  }, [hubs, domain, includeBroader, search])

  return <div className="merge-workspace" aria-label="Cross-paper chains">
    <div className="ontology-toolbar">
      <select aria-label="Pathway domain" value={domain} onChange={e => { setDomain(e.target.value); setShowLimit(10) }}>
        <option value="all">All domains</option>
        <option value="crop_to_human">Crop → Human</option>
        <option value="human_animal">Human &amp; diet</option>
        <option value="plant">Crop &amp; cooking</option>
      </select>
      <input type="search" aria-label="Filter merged chains" placeholder="Search entity, ID, predicate or paper"
        value={search} onChange={e => { setSearch(e.target.value); setShowLimit(10) }} />
      <label className="inline-check"><input type="checkbox" checked={includeBroader} onChange={e => setIncludeBroader(e.target.checked)} /> Broader</label>
      <label className="inline-check"><input type="checkbox" checked={includeHierarchy} onChange={e => setIncludeHierarchy(e.target.checked)} /> Subclass / superclass</label>
      <label className="inline-check"><input type="checkbox" checked={includeContextShift} onChange={e => setIncludeContextShift(e.target.checked)} /> Context differs</label>
    </div>

    <p className="muted merge-legend" aria-label="Arrow legend">
      <span>↓ flow (black)</span>
      <span>↔ class link (blue)</span>
      <span>dashed = broader</span>
      {gaps.map(g => <span key={g.id}>· Gap: {g.from_node} → {g.to_node}</span>)}
    </p>

    <h3>Chains ({filteredChains.length})</h3>
    {!filteredChains.length && <p className="muted">No matching chains.</p>}
    {filteredChains.slice(0, showLimit).map(chain => <section key={chain.id} className="merge-chain">
      <h4>{chain.paper_trail.join(' → ')}</h4>
      <ChainGraph chain={chain} />
      <ChainDetails chain={chain} />
    </section>)}
    {filteredChains.length > showLimit && <p><button type="button" onClick={() => setShowLimit(s => s + 10)}>Show more ({filteredChains.length - showLimit} remaining)</button></p>}

    {!!filteredHubs.length && <details className="merge-details">
      <summary>Shared hubs ({filteredHubs.length})</summary>
      <ul className="merge-hub-list">
        {filteredHubs.map(hub => <li key={hub.id} className="merge-hub-item">
          <div><strong>{hub.curie}</strong> {hub.label} <span className="muted">— {hub.papers.join(', ')}</span></div>
          <ul className="merge-hub-incoming">
            {hub.incoming.map((inc, i) => <li key={`${inc.candidate_id}-${i}`}>
              <div><span className="muted">[{inc.paper_short}]</span> {inc.subject_label} <span className="muted">({inc.subject_id})</span> ↓ <strong>{inc.predicate_alias} {inc.relation_label}</strong></div>
              <QualifierList qualifiers={inc.qualifiers} />
            </li>)}
          </ul>
        </li>)}
      </ul>
    </details>}
  </div>
}

export default function Reasoning({ revision }) {
  const [offset, setOffset] = useState(0)
  const [proof, setProof] = useState(null)
  const data = useData(`/ontology/reasoning?offset=${offset}&limit=25`, revision)
  const merged = data.data?.merged_chains
  if (data.data?.status === 'paused' && !merged) return <section className="ontology-panel"><h2>Reasoning paused</h2></section>

  const showFormal = data.data && data.data.status !== 'paused'
  return <section className="ontology-panel">
    <h2>Reasoning</h2>
    <Notice {...data} />
    {data.data?.stale && <p className="ontology-warning">The underlying claims/reference changed. Rebuild grounding and reasoning.</p>}

    {merged && <MergedPathwaysSection merged={merged} />}

    {showFormal && <>
      <details><summary>Supported rules and reasoning coverage</summary>
        <p>{data.data?.coverage?.supported_fragment?.join(', ').replaceAll('_', ' ') || 'No reasoning run yet.'}</p>
        <p>Full OWL consistency is not claimed. Anonymous restrictions and unsupported axioms are retained in the reference but not silently treated as evaluated.</p>
        <p>Biological rules enabled: {data.data?.coverage?.biological_rules_enabled ?? 0}. Adding a rule requires an attributed review and evidence references.</p>
        {!!data.data?.coverage?.unsupported_axiom_counts && <pre>{JSON.stringify(data.data.coverage.unsupported_axiom_counts, null, 2)}</pre>}
      </details>
      {!!data.data?.issues?.length && <details open><summary>Consistency issues ({data.data.issues.length})</summary><pre>{JSON.stringify(data.data.issues, null, 2)}</pre></details>}
      <div className="ontology-table-scroll"><table><thead><tr><th>Subject</th><th>Relationship</th><th>Conclusion</th><th>Rule</th><th /></tr></thead>
        <tbody>{data.data?.facts.map(f => <tr key={f.id}><td title={f.subject}>{f.subject_label || label(f.subject)}</td><td>{f.predicate_label || label(f.predicate)}</td><td title={f.object}>{f.object_label || label(f.object)}</td>
          <td>{f.rule?.replaceAll('_', ' ')}</td><td><button onClick={() => setProof(proof === f.id ? null : f.id)}>Show premises</button></td></tr>)}</tbody></table></div>
      {proof && <details open><summary>Inference trace</summary><Proof id={proof} /></details>}
      {data.data && !data.data.total && <p>No derived conclusions yet. Run grounding on saved claims to execute the reasoning stage.</p>}
      {data.data && <Pagination {...data.data} onChange={setOffset} />}
    </>}
  </section>
}
