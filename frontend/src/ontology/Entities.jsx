import { useEffect, useState } from 'react'
import { useData, encoded, getTermUrl, Notice, Pagination } from './useData.jsx'

export default function Entities({ revision, onTerm }) {
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('grounded')
  const [offset, setOffset] = useState(0)
  const [lastCounts, setLastCounts] = useState(null)
  const data = useData(`/ontology/entities?unique=true&q=${encoded(query)}&status=${status}&offset=${offset}&limit=20`, revision)

  useEffect(() => {
    if (data.data?.status_counts) setLastCounts({ query, counts: data.data.status_counts })
  }, [data.data, query])

  const counts = data.data?.status_counts || (lastCounts?.query === query ? lastCounts.counts : null)
  const count = key => counts?.[key]?.toLocaleString() ?? '…'

  return (
    <section className="ontology-panel">
      <h2>Entity registry</h2>
      <p>
        Unique entities extracted across source documents with their ontology alignments. Mappings are retained even when parts of a claim are unresolved.
      </p>
      <div className="ontology-toolbar">
        <input
          aria-label="Search entity mentions"
          placeholder="Name, mention or identifier"
          value={query}
          onChange={e => { setQuery(e.target.value); setOffset(0) }}
        />
        <select
          aria-label="Entity mapping status"
          value={status}
          onChange={e => { setStatus(e.target.value); setOffset(0) }}
        >
          <option value="grounded">Exact concept matches ({count('grounded')})</option>
          <option value="classification">Broader classifications ({count('classification')})</option>
          <option value="review">Related / narrower — review ({count('review')})</option>
          <option value="vocabulary_gap">Unresolved ({count('vocabulary_gap')})</option>
          <option value="all">All entities ({count('all')})</option>
        </select>
      </div>

      <Notice {...data} />
      {data.data?.stale && (
        <p className="ontology-warning">
          Saved mappings use an older scope or reference. Ground saved claims to refresh them.
        </p>
      )}

      {data.data && (
        <p className="muted">
          {data.data.total || 0} unique entities in view · {data.data.counts?.grounded_mentions || 0} total grounded mentions
        </p>
      )}

      <div className="ontology-table-scroll">
        <table>
          <thead>
            <tr>
              <th style={{ width: '45%' }}>Entity</th>
              <th style={{ width: '40%' }}>Ontology alignment</th>
              <th style={{ width: '15%' }}>Occurrences</th>
            </tr>
          </thead>
          <tbody>
            {data.data?.entities.map(entity => {
              const url = getTermUrl(entity.iri)
              const mentionCount = entity.occurrence_count || entity.occurrences?.length || 1
              return (
                <tr key={entity.id}>
                  <td>
                    <strong>{entity.label}</strong>
                    {entity.kind && entity.kind !== 'concept' && (
                      <span className="muted" style={{ display: 'block', fontSize: 11, marginTop: 2 }}>
                        {entity.kind}
                      </span>
                    )}
                    {entity.occurrences?.[0]?.endpoint_context && (
                      <span className="muted" style={{ display: 'block', fontSize: 11, marginTop: 2 }}>
                        Context: {entity.occurrences[0].endpoint_context}
                      </span>
                    )}
                  </td>
                  <td>
                    {entity.iri ? (
                      <>
                        {url ? (
                          <a
                            href={url}
                            target="_blank"
                            rel="noreferrer"
                            className="term-link"
                            style={{ color: '#000000', fontWeight: 600, textDecoration: 'underline' }}
                            onClick={() => onTerm?.(entity.iri)}
                          >
                            {entity.concept_label || entity.label}
                          </a>
                        ) : (
                          <button
                            type="button"
                            className="term-link"
                            style={{ color: '#000000', fontWeight: 600 }}
                            onClick={() => onTerm?.(entity.iri)}
                          >
                            {entity.concept_label || entity.label}
                          </button>
                        )}{' '}
                        <small className="muted">· {entity.mapping}</small>
                      </>
                    ) : (
                      <span className="muted">No defensible term yet</span>
                    )}
                  </td>
                  <td>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {mentionCount} {mentionCount === 1 ? 'mention' : 'mentions'}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {data.data && !data.data.total && (
        <p>No matching entities. New results appear after grounding under the current profile.</p>
      )}
      {data.data && <Pagination {...data.data} onChange={setOffset} />}
    </section>
  )
}
