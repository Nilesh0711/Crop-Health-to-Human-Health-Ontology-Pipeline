import { useEffect, useState } from 'react'
import { request } from '../lib/api.js'
import { watchJob } from '../lib/jobPolling.js'
import { encoded, label, Notice, Pagination, useData } from './useData.jsx'

const activeStatuses = ['queued', 'running', 'stopping']
const roles = ['subject', 'relation', 'object']

function ClaimTable({ claims }) {
  return <div className="ontology-table-scroll">
    <table>
      <thead><tr>{roles.map(role => <th key={role} scope="col">{role[0].toUpperCase() + role.slice(1)}</th>)}
        <th scope="col">Context and evidence</th>
      </tr></thead>
      <tbody>{claims.map(claim => <tr key={claim.id}>
        {roles.map(role => {
          const mapping = claim.bindings[role]
          return <td key={role}><strong>{claim[role]}</strong><br />
            <span className="muted" title={mapping.reason}>{mapping.iri
              ? `${mapping.label} · ${label(mapping.iri)} · ${mapping.mapping}` : 'Unresolved'}</span>
          </td>
        })}
        <td>
          {Object.values(claim.qualifiers || {}).filter(Boolean).join(' · ')}
          {(claim.scope || claim.review === 'held' || (claim.assertion_status && claim.assertion_status !== 'affirmed')) && <div className="muted">
            {[
              claim.assertion_status && claim.assertion_status !== 'affirmed' && claim.assertion_status.replaceAll('_', ' '),
              claim.scope?.replaceAll('_', ' '),
              claim.review === 'held' && (claim.hold_reasons?.length
                ? `extraction hold (${claim.hold_reasons.map(r => r.code).join(', ')})`
                : 'extraction hold'),
            ].filter(Boolean).join(' · ')}
          </div>}
          {claim.source_evidence_span && <details className="compact-evidence">
            <summary>Source evidence{claim.source_page ? ` · p. ${claim.source_page}` : ''}</summary>
            <blockquote>{claim.source_evidence_span}</blockquote>
          </details>}
        </td>
      </tr>)}</tbody>
    </table>
  </div>
}

function Sample({ job }) {
  return job.cases?.map((item, index) => <details key={item.id} className="compact-evidence">
    <summary>{item.title} · p. {item.page}</summary>
    <blockquote>{item.source}</blockquote>
    <p>{item.review}</p>
    {item.claims.length ? <ClaimTable claims={item.claims} />
      : <p className="muted">{index < job.completed_targets ? 'No claims returned' : 'Awaiting results'}</p>}
  </details>)
}

function Experiment({ pdfId, canTest }) {
  const path = '/ontology/single-stage'
  const latest = useData(`${path}/latest/${encoded(pdfId)}`)
  const [started, setStarted] = useState(null)
  const [job, setJob] = useState(null)
  const [offset, setOffset] = useState(0)
  const [useVerifier, setUseVerifier] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const run = started || latest.data
  const resultPath = run && `${path}/${encoded(pdfId)}/${run.id}`
  const currentJob = job?.id === run?.id ? job : run

  useEffect(() => {
    if (!resultPath) return
    return watchJob({
      fetchJob: () => request(`${resultPath}?offset=${offset}`),
      onJob: setJob,
      onError: cause => setError(cause.message),
      onMissing: () => setError('Saved run was not found.'),
    })
  }, [resultPath, offset])

  async function act(url) {
    setError('')
    setBusy(true)
    try {
      const next = await request(url, { method: 'POST' })
      if (url === resultPath + '/stop') {
        setJob(previous => ({ ...previous, ...next }))
      } else {
        setOffset(0)
        setStarted(next)
      }
    } catch (cause) {
      setError(cause.message)
    } finally {
      setBusy(false)
    }
  }

  const active = activeStatuses.includes(currentJob?.status)
  const runUrl = `${path}/${encoded(pdfId)}${useVerifier ? '?verifier=true' : ''}`
  const sampleUrl = `${path}/${encoded(pdfId)}?sample=true${useVerifier ? '&verifier=true' : ''}`
  return <>
    <p>
      <button disabled={latest.loading || busy || active} onClick={() => act(runUrl)}>Run full PDF</button>
      {canTest && <> <button disabled={latest.loading || busy || active}
        onClick={() => act(sampleUrl)}>Test 8 passages</button></>}
      {active && <> <button onClick={() => act(resultPath + '/stop')} disabled={busy || currentJob.status === 'stopping'}>Stop</button></>}
      {' '}<label><input type="checkbox" checked={useVerifier} disabled={busy || active}
        onChange={event => setUseVerifier(event.target.checked)} /> Experimental verifier v2</label>
      {' '}<span className="muted">Local ontology grounding</span>
    </p>
    <Notice loading={latest.loading} error={error || latest.error || currentJob?.error} />
    {currentJob && <p role="status" className="muted">
      {currentJob.status}
      {currentJob.verifier && ' · verifier v2'}
      {currentJob.total_targets > 0 && ` · ${currentJob.completed_targets}/${currentJob.total_targets} passages · ${currentJob.claim_count} claims`}
      {currentJob.classified_count !== undefined && ` · ${currentJob.classified_count} fully grounded`}
    </p>}
    {currentJob?.sample && <Sample job={currentJob} />}
    {!!currentJob?.claims?.length && <ClaimTable claims={currentJob.claims} />}
    {!!currentJob?.total && <Pagination offset={offset} limit={currentJob.limit} total={currentJob.total} onChange={setOffset} />}
  </>
}

export default function SingleStage({ pdfs }) {
  const [selected, setSelected] = useState('')
  const paper = pdfs.find(pdf => pdf.id === selected) || pdfs[0]
  const pdfId = paper?.id
  const canTest = paper?.name === 'Kirchgesser_2023_Potato_Root_Architecture_Phosphorus.pdf'
  return <section aria-label="Extract and ground">
    <h2>Extract and ground</h2>
    {pdfId ? <>
      <label>Source PDF{' '}<select value={pdfId} onChange={event => setSelected(event.target.value)}>
        {pdfs.map(pdf => <option key={pdf.id} value={pdf.id}>{pdf.title || pdf.name}</option>)}
      </select></label>
      <Experiment key={pdfId} pdfId={pdfId} canTest={canTest} />
    </> : <p className="muted">Add a PDF in Runs.</p>}
  </section>
}
