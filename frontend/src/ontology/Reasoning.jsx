import { useState } from 'react'
import { useData, encoded, Notice, Pagination, label } from './useData.jsx'

function Proof({ id }) {
  const data = useData(`/ontology/proof?id=${encoded(id)}`)
  return <><Notice {...data} /><ol className="ontology-proof">{data.data?.proof.map(f =>
    <li key={f.id}><span>{f.subject_label || label(f.subject)} → {f.predicate_label || label(f.predicate)} → {f.object_label || label(f.object)}</span>
      <p>{f.rule || 'Recorded premise'} · {f.scientific_status.replaceAll('_', ' ')}</p>
      {f.source && <details><summary>Premise provenance</summary><pre>{JSON.stringify(f.source, null, 2)}</pre></details>}</li>)}</ol></>
}

export default function Reasoning({ revision }) {
  const [offset, setOffset] = useState(0)
  const [proof, setProof] = useState(null)
  const data = useData(`/ontology/reasoning?offset=${offset}&limit=25`, revision)
  if (data.data?.status === 'paused') return <section className="ontology-panel"><h2>Reasoning paused</h2></section>
  return <section className="ontology-panel"><h2>Reasoning</h2>
    <p>Logical consequences of imported axioms and explicitly reviewed rules. These are not independent scientific validation.</p>
    <Notice {...data} />{data.data?.stale && <p className="ontology-warning">The underlying claims/reference changed. Rebuild grounding and reasoning.</p>}
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
  </section>
}
