export default function DefinitionContext({ contexts }) {
  if (!contexts || !Object.values(contexts).some(rows => rows?.length)) return null
  return <details><summary>Document definitions used for grounding</summary>
    <p className="muted">Verbatim source descriptions clarify the endpoint. They do not change the extracted finding.</p>
    {Object.entries(contexts).map(([role, rows]) => <div key={role}><strong>{role}</strong>{rows.map((r, i) =>
      <blockquote key={i}><span className="muted">Page {r.page ?? 'NA'} · sentence {r.sentence_number} · packet {r.packet_number}</span><br />{r.text}</blockquote>)}</div>)}
  </details>
}
