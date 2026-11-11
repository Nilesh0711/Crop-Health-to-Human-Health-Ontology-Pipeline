import { useEffect, useState } from 'react'
import { useData, encoded, getPdfUrl, getTermUrl, Notice, Pagination } from './useData.jsx'
import { request } from '../lib/api.js'

function ExtractionReview({ row, pdf, disabled, canReview, onRefresh }) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const warnings = [...(row.extraction_warnings || []), ...(row.mapping_consistency_warnings || [])]
  if (!warnings.length && !row.extraction_review) return null
  async function decide(status) {
    setBusy(true); setError('')
    try {
      await request(`/ontology/pdfs/${encoded(pdf.id)}/claims/${encoded(row.candidate_id)}/review`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role: 'extraction', extraction_status: status, note, expected_revision: row.revision }),
      })
      setNote(''); onRefresh?.()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  return <details className="ontology-warning">
    <summary>Extraction review: {row.extraction_review_status || 'held'}</summary>
    <ul>{warnings.map((w, i) => <li key={i}>{w.message}{w.source_text && <blockquote>{w.source_text}</blockquote>}</li>)}</ul>
    <p>Entity mappings remain available. A hold prevents automatic sharing and downstream eligibility.</p>
    {!!row.mapping_consistency_warnings?.length && <p>Conflicting mappings require separate mapping review; accepting extraction does not resolve them.</p>}
    {canReview && <><label>Review note<textarea value={note} onChange={e => setNote(e.target.value)} maxLength={2000} /></label>
    <div className="ontology-toolbar">
      <button disabled={disabled || !pdf || busy || note.trim().length < 3} onClick={() => decide('accepted')}>Accept extraction</button>
      <button disabled={disabled || !pdf || busy || note.trim().length < 3} onClick={() => decide('rejected')}>Reject extraction</button>
    </div></>}
    <p className="muted">Acceptance confirms the extraction only; it does not approve mappings or scientific truth.</p>
    {error && <p role="alert">{error}</p>}
  </details>
}

export default function MappingReview({ revision, onRefresh, pdfs, onTerm, canReview = false }) {
  const [offset, setOffset] = useState(0)
  const [query, setQuery] = useState('')
  const [document, setDocument] = useState('')
  const completedPdfs = pdfs.filter(p => p.status === 'completed')
  const data = useData(`/ontology/assertions?q=${encoded(query)}&document=${encoded(document)}&offset=${offset}&limit=15`, revision)
  useEffect(() => { setOffset(0) }, [document, query])
  useEffect(() => {
    if (document && !pdfs.some(p => p.title === document && p.status === 'completed')) setDocument('')
  }, [document, pdfs])
  return <section className="ontology-panel"><h2>Mapping review</h2>
    <p className="muted">Review concept identity and scope. Original claims and evidence remain separate.</p>
    <div className="ontology-toolbar"><input aria-label="Search assertions" placeholder="Search claims" value={query} onChange={e => setQuery(e.target.value)} />
      <select aria-label="Source document" value={document} onChange={e => setDocument(e.target.value)}><option value="">All documents</option>{completedPdfs.map(p => <option key={p.id} value={p.title}>{p.title}</option>)}</select></div>
    <Notice {...data} />
    {data.data?.stale && <p className="ontology-warning">The reference or claims changed. Reground saved claims before editing these results.</p>}
    {!!data.data?.unprocessed_documents?.length && <details><summary>Saved documents awaiting ontology grounding ({data.data.unprocessed_documents.length})</summary>
      <ul>{data.data.unprocessed_documents.map(d => <li key={d}>{d}</li>)}</ul><p>Use Runs → Ground saved claims. Existing extraction can be reused.</p></details>}
    {data.data?.assertions.map(row => {
      const pdf = pdfs.find(p => p.title === row.document_key)
      const key = row.document_key + row.candidate_id
      return <article className="ontology-assertion" key={key}>
        <h3>{row.original_claim.subject} → {row.original_claim.relation} → {row.original_claim.object}</h3>
        <p><strong>Scope:</strong> {row.scope.replaceAll('_', ' ')}. {row.scope_reason}</p>
        <ExtractionReview row={row} pdf={pdf} disabled={data.data?.stale} canReview={canReview} onRefresh={onRefresh} />
        <div className="ontology-table-scroll"><table><thead><tr><th>Role</th><th>Source expression</th><th>Ontology mapping</th><th>Decision</th></tr></thead>
          <tbody>{['subject', 'relation', 'object'].map(role => <tr key={role}><td>{role}</td><td>{row.original_claim[role]}{row.original_claim[role + '_context'] && <p className="muted">Context: {row.original_claim[role + '_context']}</p>}</td>
            <td>{row.bindings[role].iri ? (() => {
              const url = getTermUrl(row.bindings[role].iri)
              return url ? (
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  className="term-link"
                  style={{ color: '#000000', fontWeight: 600, textDecoration: 'underline' }}
                  onClick={() => onTerm?.(row.bindings[role].iri)}
                >
                  {row.bindings[role].label}
                </a>
              ) : (
                <button type="button" className="term-link" onClick={() => onTerm?.(row.bindings[role].iri)}>
                  {row.bindings[role].label}
                </button>
              )
            })() : 'Unresolved'}</td>
            <td>{row.bindings[role].mapping}{role !== 'relation' && <> · {row.bindings[role].denotation}</>}</td>
          </tr>)}</tbody></table></div>
        <details style={{ marginTop: 10, borderTop: '1px solid #e3e7ea', paddingTop: 8 }}>
          <summary style={{ cursor: 'pointer', fontWeight: 600, fontSize: 13 }}>Source evidence &amp; mapping details</summary>
          <div style={{ marginTop: 8 }}>
            <p className="muted" style={{ margin: '3px 0', fontSize: 12 }}><strong>Document:</strong> {row.document_key} {(() => {
              const pdfUrl = getPdfUrl(pdf, row.original_claim.source_page || 1)
              return pdfUrl ? <>· <a href={pdfUrl} target="_blank" rel="noreferrer">Open source PDF</a></> : null
            })()} · page {row.original_claim.source_page ?? 'not recorded'}</p>
            <p style={{ margin: '4px 0', fontSize: 13 }}><strong>Source claim:</strong> {row.original_claim.claim_text || `${row.original_claim.subject} → ${row.original_claim.relation} → ${row.original_claim.object}`}</p>
            <p style={{ margin: '4px 0', fontSize: 13, fontStyle: 'italic' }}>&ldquo;{row.original_claim.source_evidence_span}&rdquo;</p>
            {!!row.original_claim.source_references?.length && <div style={{ margin: '4px 0', fontSize: 12 }}><strong>Attached citations:</strong> <ul>{row.original_claim.source_references.map((ref, i) => <li key={i}>{ref.raw || ref.raw_reference || ref.title || JSON.stringify(ref)}</li>)}</ul></div>}
            {Object.keys(row.original_claim.qualifiers || {}).length > 0 && (
              <dl style={{ margin: '4px 0' }}>{Object.entries(row.original_claim.qualifiers).map(([k, v]) => <div key={k}><dt>{k.replaceAll('_', ' ')}</dt><dd>{String(v)}</dd></div>)}</dl>
            )}
            <div style={{ margin: '6px 0', fontSize: 12 }}>
              {['subject', 'relation', 'object'].map(role => (
                <p key={role} style={{ margin: '2px 0' }}>
                  <strong>{role} mapping:</strong> {row.bindings[role].reason}
                </p>
              ))}
            </div>
            {!!row.review_history?.length && <div style={{ margin: '4px 0', fontSize: 12 }}><strong>Review history:</strong> {row.review_history.map((h, i) => <p key={i} className="muted" style={{ margin: '2px 0' }}>{h.at} · {h.role} · {h.note}</p>)}</div>}
          </div>
        </details>
      </article>
    })}
    {data.data && !data.data.total && <p>No matching grounded assertions. Saved PDF extractions can be grounded from Runs.</p>}
    {data.data && <Pagination {...data.data} onChange={setOffset} />}
  </section>
}
