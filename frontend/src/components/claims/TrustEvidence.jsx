export default function TrustEvidence({ trust }) {
  if (!trust) return null
  return <section className="quote-box" aria-label="Claim trust evidence">
    <span className="eyebrow">EVIDENCE BALANCE</span>
    <p><strong>{trust.score > 0 ? '+' : ''}{trust.score}</strong> · {trust.status.replaceAll('_', ' ')}
      {!trust.assessment_complete && ' · assessment unfinished'}</p>
    <p>{trust.support_count} supporting study groups · {trust.contradiction_count} contradicting study groups
      {trust.disputed_study_groups?.length ? ` · ${trust.disputed_study_groups.length} internally disputed groups` : ''}
      {' · '}{trust.unresolved_count} unresolved paper findings</p>
    <small>Score = distinct original supporting study groups minus contradicting groups. It is not a probability.
      Repeated pathways, chunks and reviews do not add votes. Source fidelity and scope remain separate.</small>
    {!!trust.findings?.length && <details className="context-packet"><summary>Study contributions and exact evidence</summary>
      {trust.findings.map(row => <div key={row.evidence_document_id}><p><strong>{row.title || row.evidence_document_id}</strong> · {row.decision} · {row.evidence_origin}</p>
        <p>{row.rationale}</p>{row.evidence.map((item, i) => <div key={i}>
          {item.source_kind === 'table_row' && <small>Source table {item.table_id}, row {item.row_number} · cells serialized with headers and footnotes</small>}
          <blockquote>{item.quoted_span}</blockquote></div>)}
        {!!row.guardrail_reasons?.length && <p>{row.guardrail_reasons.join('; ')}</p>}</div>)}
    </details>}
  </section>
}
