import { ArrowRight, BookOpen, GitBranch } from 'lucide-react'
import { entityLabel, evidenceState, readable } from '../../lib/pathways.js'

export function EvidenceBadge({ edge }) {
  const state = evidenceState(edge)
  const tone = ['supported', 'corroborated', 'single_source'].includes(state) ? 'support'
    : ['contested', 'contradicted'].includes(state) ? 'conflict' : 'neutral'
  return <span className={`pathway-badge ${tone}`}>{readable(state)}</span>
}

export function PathwayCard({ path, number, provisional, onSelect, health = false }) {
  const papers = new Set(path.map(edge => edge.paper || edge.document_key).filter(Boolean))
  const ledger = path.reduce((states, edge) => {
    const state = evidenceState(edge)
    states[state] = (states[state] || 0) + 1
    return states
  }, {})
  return <article className="research-path-card" aria-label={`Pathway ${number}`}>
    <header><div className="pathway-card-title"><span className="pathway-number">{String(number).padStart(2, '0')}</span>
      <div><h3>{health ? 'Health pathway' : 'Assembled path'}</h3><span>{path.length} relations
        {papers.size > 0 && ` · ${papers.size} source ${papers.size === 1 ? 'paper' : 'papers'}`}</span></div></div>
      <span className="pathway-badge">{health ? 'Corroborated edges' : provisional ? 'Provisional' : 'Validated edges'}</span>
    </header>
    <ol className="pathway-chain" aria-label="Connected entities and relations">
      {path.length > 0 && <li className="pathway-start"><span className="pathway-entity">{entityLabel(path[0], 'source')}</span></li>}
      {path.map((edge, n) => <li key={`${edge.id}-${n}`}><span className="pathway-link"><ArrowRight size={15} aria-hidden="true" />
        {onSelect ? <button type="button" onClick={() => onSelect({ ...edge, kind: 'edge' })}
          title="Inspect this claim and its evidence">{readable(edge.relation)}</button> : <span>{readable(edge.relation)}</span>}</span>
        <span className="pathway-entity">{entityLabel(edge, 'target')}</span></li>)}
    </ol>
    {provisional && <div className="pathway-ledger-summary" aria-label="Saved evidence ledger states">
      <span>Evidence ledger</span>{Object.entries(ledger).map(([state, count]) =>
        <span key={state} className="pathway-badge">{count} {readable(state)}</span>)}</div>}
    <details className="pathway-proof"><summary><BookOpen size={15} aria-hidden="true" />Original claims and passages<span>{path.length}</span></summary>
      <div className="pathway-proof-list">{path.map((edge, n) => <section key={`${edge.id}-${n}`} className="pathway-proof-item">
        <div className="pathway-proof-title"><span>Claim {n + 1}</span><EvidenceBadge edge={edge} /></div>
        <h4>{edge.subject || entityLabel(edge, 'source')} <span>{readable(edge.relation)}</span> {edge.object || entityLabel(edge, 'target')}</h4>
        {edge.claim_text && <p>{edge.claim_text}</p>}
        <p className="pathway-source"><BookOpen size={13} aria-hidden="true" />{edge.paper || 'Source paper not recorded'}
          {edge.source_page ? ` · page ${edge.source_page}` : ''}</p>
        <dl className="pathway-facts">
          <div><dt>Source fidelity</dt><dd>{readable(edge.source_status) || 'Not recorded'}</dd></div>
          <div><dt>External assessment</dt><dd>{readable(edge.external_status || edge.status) || 'Not recorded'}</dd></div>
          <div><dt>Certainty</dt><dd>{readable(edge.certainty) || 'Not recorded'}</dd></div>
          {Object.entries(edge.qualifiers || {}).filter(([, value]) => value).map(([key, value]) =>
            <div key={key}><dt>{readable(key)}</dt><dd>{value}</dd></div>)}
        </dl>
        {edge.quote ? <blockquote>{edge.quote}</blockquote> : <p className="soft-note">No source passage attached to this saved path edge.</p>}
        {edge.candidate_id && <small className="pathway-proof-id">Claim ID: {edge.candidate_id}</small>}
        {onSelect && <button type="button" className="text-button" onClick={() => onSelect({ ...edge, kind: 'edge' })}>
          Inspect evidence balance <ArrowRight size={14} /></button>}
      </section>)}</div>
    </details>
  </article>
}

export function PathwayEmpty({ title, children, action }) {
  return <div className="pathway-empty"><span><GitBranch size={26} /></span><h3>{title}</h3><p>{children}</p>{action}</div>
}

export function CorroboratedPaths({ paths = [] }) {
  return <div className="pathway-workspace">
    <div className="pathway-section-heading"><div><h3>Connections to human health</h3>
      <p>Corroborated relations in this paper. Connected edges do not establish whole-chain causation.</p></div>
      <span className="subtle-count">{paths.length} paths</span></div>
    <div className="research-paths-list">{paths.length ? paths.map((path, n) =>
      <PathwayCard key={path.map(e => e.id).join('|') || n} path={path} number={n + 1} health />)
      : <PathwayEmpty title="No supported reasoning paths yet">Paths appear when corroborated relations connect an agricultural factor to a human-health outcome.</PathwayEmpty>}</div>
  </div>
}
