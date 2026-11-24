import { useState } from 'react'
import { useData, encoded, getTermUrl, Notice, Pagination } from './useData.jsx'

function RelationDetails({ relation }) {
  const [shown, setShown] = useState(5)
  return <details><summary>Original wording and evidence</summary>
    {relation.definition && <p className="muted">{relation.definition}</p>}
    <p>{relation.raw_variants.map(v => `${v.label} (${v.count})`).join('; ')}</p>
    {relation.occurrences.slice(0, shown).map(o => <article key={o.id} className="compact-evidence">
      <strong>{o.claim_text || o.relation_phrase}</strong>
      <p className="muted">{o.mapping} alignment | {o.document_key}{o.source_page != null ? ` | page ${o.source_page}` : ''}</p>
      {o.evidence && <blockquote>{o.evidence}</blockquote>}
    </article>)}
    {shown < relation.occurrences.length && <button onClick={() => setShown(n => n + 10)}>Show more claims</button>}
  </details>
}

export default function Relations({ revision, onTerm }) {
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const data = useData(`/ontology/relations?group_by=property&q=${encoded(query)}&scope=in_scope&status=classified&offset=${offset}&limit=20`, revision)
  const result = data.data
  return <section className="ontology-panel">
    <h2>Relation registry</h2>
    <div className="ontology-toolbar">
      <input aria-label="Search relations" placeholder="Original wording, property or claim" value={query} onChange={e => { setQuery(e.target.value); setOffset(0) }} />
    </div>
    <Notice {...data} />
    {result?.stale && <p className="ontology-warning">Saved mappings are out of date. Reground saved claims in Runs to refresh.</p>}
    <div className="ontology-table-scroll"><table>
      <thead><tr><th>Ontology property / unresolved wording</th><th>Exact</th></tr></thead>
      <tbody>{result?.relations.map(rel => {
        const url = getTermUrl(rel.iri)
        return <tr key={rel.id}><td>
          {rel.iri ? url ? <a href={url} target="_blank" rel="noreferrer" className="term-link" onClick={() => onTerm?.(rel.iri)}>{rel.label}</a>
            : <button className="term-link" onClick={() => onTerm?.(rel.iri)}>{rel.label}</button>
            : <strong>{rel.label}</strong>}
          {rel.term_id && <small className="muted"> | {rel.term_id}</small>}
          <RelationDetails key={rel.id} relation={rel} />
        </td><td>{rel.exact_count || 0}</td></tr>
      })}</tbody></table></div>
    {result && !result.total && <p>No matching relations.</p>}
    {result && <Pagination {...result} onChange={setOffset} />}
  </section>
}
