import { useEffect, useState } from 'react'
import { useData, encoded, getPdfUrl, getTermUrl, Notice, Pagination } from './useData.jsx'
import { request } from '../lib/api.js'
import DefinitionContext from './DefinitionContext.jsx'

function ExtractionReview({ row, pdf, disabled, canReview, onRefresh }) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const warnings = row.extraction_warnings || []
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
    {canReview && <><label>Review note<textarea value={note} onChange={e => setNote(e.target.value)} maxLength={2000} /></label>
    <div className="ontology-toolbar">
      <button disabled={disabled || !pdf || busy || note.trim().length < 3} onClick={() => decide('accepted')}>Accept extraction</button>
      <button disabled={disabled || !pdf || busy || note.trim().length < 3} onClick={() => decide('rejected')}>Reject extraction</button>
    </div></>}
    <p className="muted">Acceptance confirms the extraction only; it does not approve mappings or scientific truth.</p>
    {error && <p role="alert">{error}</p>}
  </details>
}

function MappingConsistency({ row }) {
  if (!row.mapping_consistency_warnings?.length) return null
  return <details className="ontology-warning">
    <summary>Grounding consistency: needs mapping review</summary>
    <p>Conflicting mappings prevent sharing and downstream eligibility. Review the affected mappings below; accepting extraction does not resolve this conflict.</p>
    {row.mapping_consistency_warnings.map((warning, index) => <div key={index}>
      <strong>{warning.role}</strong>: {warning.message}
      <ul>{(warning.decisions || []).map(decision => <li key={decision.candidate_id}>
        {decision.candidate_id}: {decision.iri || 'Unresolved'} · {decision.mapping}
        {decision.denotation ? ` · ${decision.denotation}` : ''}
      </li>)}</ul>
    </div>)}
  </details>
}

export default function MappingReview({ revision, onRefresh, pdfs, onTerm, canReview = false }) {
  const [offset, setOffset] = useState(0)
  const [query, setQuery] = useState('')
  const [document, setDocument] = useState('')
  const completedPdfs = pdfs.filter(p => p.status === 'completed')
  const data = useData(`/ontology/assertions?scope=in_scope&q=${encoded(query)}&document=${encoded(document)}&offset=${offset}&limit=15`, revision)
  useEffect(() => { setOffset(0) }, [document, query])
  useEffect(() => {
    if (document && !pdfs.some(p => p.title === document && p.status === 'completed')) setDocument('')
  }, [document, pdfs])
  return <section className="ontology-panel"><h2>Mapping review</h2>
    <div className="ontology-toolbar"><input aria-label="Search assertions" placeholder="Search claims" value={query} onChange={e => setQuery(e.target.value)} />
      <select aria-label="Source document" value={document} onChange={e => setDocument(e.target.value)}><option value="">All documents</option>{completedPdfs.map(p => <option key={p.id} value={p.title}>{p.title}</option>)}</select></div>
    <Notice {...data} />
    {data.data?.stale && <p className="ontology-warning">Saved claims or ontology reference changed. Reground saved claims in Runs to refresh.</p>}
    {!!data.data?.unprocessed_documents?.length && <details><summary>Saved documents awaiting ontology grounding ({data.data.unprocessed_documents.length})</summary>
      <ul>{data.data.unprocessed_documents.map(d => <li key={d}>{d}</li>)}</ul><p>Use Ground saved claims in Runs.</p></details>}
    {data.data?.assertions.map(row => {
      const pdf = pdfs.find(p => p.title === row.document_key)
      const key = row.document_key + row.candidate_id
      const observation = row.original_claim.measured_comparison
      const predicate = row.bindings.relation
      const fixed = predicate.fixed_from_extraction === true
      const roles = observation || fixed ? ['subject', 'object'] : ['subject', 'relation', 'object']
      return <article className="ontology-assertion" key={key}>
        <h3>{observation
          ? `${row.original_claim.subject} · ${row.original_claim.relation} ${row.original_claim.object}`
          : <>{row.original_claim.subject} → {row.original_claim.relation} → {row.original_claim.object}</>}</h3>
        {row.comparison_assessment && <p><strong>Measured comparison</strong> · {row.comparison_assessment.direction?.replaceAll('_', ' ')}
          {' · '}{row.comparison_assessment.statistical_significance?.replaceAll('_', ' ')}
          {' · '}{row.comparison_assessment.complete ? 'Complete observation' : 'Requires review'}</p>}
        <ExtractionReview row={row} pdf={pdf} disabled={data.data?.stale} canReview={canReview} onRefresh={onRefresh} />
        <MappingConsistency row={row} />
        {fixed && !observation && <p>
          <strong>Property selected during extraction</strong>{' '}
          <a href={getTermUrl(predicate.iri) || undefined} onClick={() => onTerm?.(predicate.iri)}>
            {predicate.label || row.original_claim.proposed_relation_display_label || row.original_claim.relation}
          </a>
          {predicate.admission_reason && <span className="muted"> · {predicate.admission_reason}</span>}
        </p>}
        <div className="ontology-table-scroll"><table><thead><tr><th>Role</th><th>Source expression</th><th>Ontology mapping</th><th>Decision</th></tr></thead>
          <tbody>{roles.map(role => <tr key={role}><td>{role}</td><td>{row.original_claim[role]}{row.original_claim[role + '_context'] && <p className="muted">Context: {row.original_claim[role + '_context']}</p>}</td>
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
            <td>{row.bindings[role].mapping}{row.bindings[role].admission_reason && <p className="muted">{row.bindings[role].admission_reason}</p>}{role !== 'relation' && <> · {row.bindings[role].denotation}</>}</td>
          </tr>)}</tbody></table></div>
        <details style={{ marginTop: 12, borderTop: '1px solid #e3e7ea', paddingTop: 10 }}>
          <summary style={{ cursor: 'pointer', fontWeight: 600, fontSize: 13 }}>Source evidence &amp; mapping details</summary>
          <div style={{ marginTop: 10 }}>
            <p className="muted" style={{ margin: '0 0 8px', fontSize: 12 }}><strong>Document:</strong> {row.document_key} {(() => {
              const pdfUrl = getPdfUrl(pdf, row.original_claim.source_page || 1)
              return pdfUrl ? <>· <a href={pdfUrl} target="_blank" rel="noreferrer">Open source PDF</a></> : null
            })()} · page {row.original_claim.source_page ?? 'not recorded'}</p>
            <p style={{ margin: '0 0 8px', fontSize: 13 }}><strong>Source claim:</strong> {row.original_claim.claim_text || `${row.original_claim.subject} → ${row.original_claim.relation} → ${row.original_claim.object}`}</p>
            {row.original_claim.relation_trigger && <p style={{ margin: '0 0 8px' }}><strong>Source wording</strong>{' '}<q>{row.original_claim.relation_trigger}</q></p>}
            {row.original_claim.source_evidence_span && <blockquote>{row.original_claim.source_evidence_span}</blockquote>}
            {!!row.original_claim.source_references?.length && <div style={{ margin: '8px 0', fontSize: 12 }}><strong>Attached citations:</strong> <ul>{row.original_claim.source_references.map((ref, i) => <li key={i}>{ref.raw || ref.raw_reference || ref.title || JSON.stringify(ref)}</li>)}</ul></div>}
            {Object.keys(row.original_claim.qualifiers || {}).length > 0 && (
              <dl className="claim-qualifiers">{Object.entries(row.original_claim.qualifiers).map(([k, v]) => <div key={k}><dt>{k.replaceAll('_', ' ')}</dt><dd>{String(v)}</dd></div>)}</dl>
            )}
            <div style={{ margin: '10px 0 6px', fontSize: 12.5 }}>
              {roles.map(role => (
                <p key={role} style={{ margin: '4px 0' }}>
                  <strong>{role} mapping:</strong> {row.bindings[role].reason}
                  {row.bindings[role].consistency_adjustment && <span className="muted">
                    {' '}Original model decision ({row.bindings[role].consistency_adjustment.original_mapping}).
                    {' '}{row.bindings[role].consistency_adjustment.original_reason}
                  </span>}
                </p>
              ))}
            </div>
            <DefinitionContext contexts={row.source_definition_context} />
            {!!row.review_history?.length && <div style={{ margin: '8px 0 0', fontSize: 12 }}><strong>Review history:</strong> {row.review_history.map((h, i) => <p key={i} className="muted" style={{ margin: '3px 0' }}>{h.at} · {h.role} · {h.note}</p>)}</div>}
          </div>
        </details>
      </article>
    })}
    {data.data && !data.data.total && <p>No matching grounded assertions. Saved PDF extractions can be grounded from Runs.</p>}
    {data.data && <Pagination {...data.data} onChange={setOffset} />}
  </section>
}
