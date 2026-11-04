import { useEffect, useState } from 'react'
import { ArrowRight, BookOpen, CircleAlert, ExternalLink, FileSearch, Link2, ListChecks, Search } from 'lucide-react'
import { EVIDENCE_BASIS_TONE, PATHWAY_OUTCOMES, STANCE_TONE, countOutcomes, entityLabel, outcomeLabel,
  assessmentState, localSupportBreakdown, findingPresentation, outcomeTone, pathwayOutcome, readable } from '../../lib/pathways.js'

// Presentation only: every verdict, stance and proof shown here comes from the saved record.
const ROUTE_TONE = { supports_route: 'support', contradicts_route: 'conflict', components_only: 'partial' }
const OUTCOME_ORDER = Object.keys(PATHWAY_OUTCOMES)
const anchor = (path, paper, proof) => `proof-${encodeURIComponent(`${path}|${paper}|${proof}`)}`
const cardAnchor = path => `pathway-card-${encodeURIComponent(path)}`

// Open every enclosing <details> so a proof link never lands on hidden content.
function revealProof(event, id) {
  const element = typeof document === 'undefined' ? null : document.getElementById(id)
  if (!element) return
  event.preventDefault()
  for (let node = element.closest('details'); node; node = node.parentElement?.closest('details')) node.open = true
  element.scrollIntoView({ behavior: 'smooth', block: 'center' })
  element.classList.remove('proof-highlight')
  void element.offsetWidth
  element.classList.add('proof-highlight')
}

function References({ refs = [], path, label = 'Proof' }) {
  if (!refs?.length) return null
  return <span className="pathway-proof-references"><span className="pathway-proof-references-label">{label}</span>{refs.map((ref, n) => {
    const id = anchor(path, ref.evidence_document_id, ref.proof_id)
    return <a key={n} href={`#${id}`} onClick={event => revealProof(event, id)}>{ref.evidence_document_id} / {ref.proof_id}</a>
  })}</span>
}

function Badge({ tone = 'neutral', children, title }) {
  return <span className={`pathway-badge ${tone}`} title={title}>{children}</span>
}

function edgeText(edge) {
  return edge ? `${entityLabel(edge, 'source')} → ${readable(edge.relation)} → ${entityLabel(edge, 'target')}` : ''
}

function AssessmentChain({ edges, connections, edgeFindings, connectionFindings }) {
  if (!edges.length) return null
  const connectionAfter = Object.fromEntries((connections || []).map(row => [row.upstream_edge_id, row]))
  return <ol className="pathway-chain assessment-chain" aria-label="Assessment target chain">
    <li><span className="pathway-entity">{entityLabel(edges[0], 'source')}</span></li>
    {edges.map((edge, n) => {
      const finding = edgeFindings[edge.id]
      const connection = connectionAfter[edge.id]
      const compatibility = connection && connectionFindings[connection.connection_id]?.compatibility
      return <li key={edge.id || n}>
        <span className={`pathway-link stance-${STANCE_TONE[finding?.stance] || 'none'}`}
          title={`Link ${n + 1}: ${finding ? readable(finding.stance) : 'no link finding recorded'}`}>
          <ArrowRight size={15} aria-hidden="true" />{readable(edge.relation)}</span>
        <span className={`pathway-entity${connection ? ` connection-${STANCE_TONE[compatibility] || 'none'}` : ''}`}
          title={connection ? `Connection: ${compatibility ? readable(compatibility) : 'not assessed'}` : undefined}>
          {entityLabel(edge, 'target')}</span>
      </li>
    })}
  </ol>
}

function ChainLegend() {
  return <div className="assessment-chain-legend" aria-hidden="true">
    <span><i className="legend-line support" />Link supported</span>
    <span><i className="legend-line partial" />Wording partly supported</span>
    <span><i className="legend-line conflict" />Link contradicted</span>
    <span><i className="legend-line none" />Unresolved or not assessed</span>
    <span><i className="legend-node support" />Compatible connection</span>
    <span><i className="legend-node conflict" />Incompatible connection</span>
  </div>
}

export function PathwayAssessmentCard({ record, number, focused = false, paperKey, onInspectClaim }) {
  const local = record.assessment_scope === 'neighboring_claims' || record.target?.policy === 'neighbor-pair-v1'
  const target = record.target || {}
  const edges = target.edges || []
  const assessment = record.assessment
  const status = pathwayOutcome(record)
  const outcome = PATHWAY_OUTCOMES[status]
  const papers = record.paper_assessments || []
  const edgeById = Object.fromEntries(edges.map(edge => [edge.id, edge]))
  const edgeIndex = Object.fromEntries(edges.map((edge, n) => [edge.id, n + 1]))
  const connectionById = Object.fromEntries((target.connections || []).map(row => [row.connection_id, row]))
  const nodeLabelById = {}
  edges.forEach(edge => {
    if (edge.source) nodeLabelById[edge.source] = entityLabel(edge, 'source')
    if (edge.target) nodeLabelById[edge.target] = entityLabel(edge, 'target')
  })
  const edgeFindings = Object.fromEntries((assessment?.edge_assessments || []).map(row => [row.edge_id, row]))
  const connectionFindings = Object.fromEntries((assessment?.connection_assessments || []).map(row => [row.connection_id, row]))
  const supportedLinks = (assessment?.edge_assessments || []).filter(row => row.stance === 'supported').length
  const partialLinks = (assessment?.edge_assessments || []).filter(row => row.stance === 'partially_supported').length
  const state = assessmentState(record)
  const breakdown = local && localSupportBreakdown(record)
  const docCount = (target.document_keys || []).length
  return <article id={cardAnchor(record.pathway_id)} className={`research-path-card pathway-assessment tone-${outcomeTone(status)}${focused ? ' is-focused' : ''}`} aria-label={local ? "Neighboring-claim assessment" : "Whole-pathway assessment"}>
    <header><div className="pathway-card-title">
      {number != null && <span className="pathway-number">{String(number).padStart(2, '0')}</span>}
      <div><span className="eyebrow">{local ? 'NEIGHBORING-CLAIM ASSESSMENT' : 'HISTORICAL WHOLE-PATHWAY ASSESSMENT'}{docCount > 1 ? ' · CROSS-PAPER CHAIN' : ''}</span><h3>{outcomeLabel(status)}</h3>
        <small>{edges.length} connected relations · {docCount} source document(s) · {papers.length} external paper(s) assessed</small></div></div>
      <Badge tone={EVIDENCE_BASIS_TONE[record.evidence_basis] || (status === 'pipeline_error' ? 'conflict' : 'neutral')}
        title="Evidence basis recorded by the pathway synthesis">{readable(record.evidence_basis || 'not established')}</Badge></header>
    {outcome?.description && <p className="assessment-outcome-note">{outcome.description}</p>}
    <AssessmentChain edges={edges} connections={target.connections} edgeFindings={edgeFindings} connectionFindings={connectionFindings} />
    <div className="assessment-body">
      {local && <dl className="assessment-status-grid" aria-label="Scientific result and execution status">
        <div><dt>{state.execution === 'needs_refresh' ? 'Previous scientific result' : state.execution === 'complete' ? 'Scientific result' : 'Provisional scientific result'}</dt><dd>{state.scientific ? outcomeLabel(state.scientific) : 'Not assessed'}</dd></div>
        <div><dt>Execution</dt><dd>{readable(state.execution)}{state.execution === 'pending' ? ' — not admitted to the supported graph' : ''}</dd></div>
      </dl>}
      {breakdown && assessment && <p className="assessment-outcome-note" aria-label="Local support breakdown">
        <strong>{state.execution === 'needs_refresh' ? 'Previous breakdown: ' : state.execution === 'complete' ? '' : 'Provisional breakdown: '}</strong>
        {breakdown.label}{breakdown.key === 'qualified_components' ? '. This does not fully validate the original wording or admit this pair to the supported graph.' : ''}
      </p>}
      {record.proof_audit && <section className="pathway-proof-item" aria-label="Independent external proof audit">
        <strong>{record.proof_audit.passed ? 'External proof audit passed' : 'External proof audit requires review'}</strong>
        <p>Checks only admitted external quotations against the fixed claims. Arithmetic does not establish matching scientific definitions. {record.proof_audit.passed ? 'Whole-chain causation is still not assessed.' : 'The original synthesis is preserved; this pair is not admitted to the supported graph.'}</p>
        {[...(record.proof_audit.edge_checks || []),...(record.proof_audit.connection_checks || [])].map((item,n) => <div key={item.edge_id || item.connection_id || n}>
          <strong>{readable(item.decision)}</strong><p>{item.reason}</p><References path={record.pathway_id} refs={item.proof_refs} />
        </div>)}
      </section>}
      {assessment && <>
        {assessment.edge_assessments?.length > 0 && <p className="assessment-link-tally">{supportedLinks} of {edges.length} links supported
          {partialLinks > 0 ? ` · ${partialLinks} with partial wording support` : ''}
          {assessment.connection_assessments?.length ? ` · ${assessment.connection_assessments.filter(row => row.compatibility === 'compatible').length} of ${assessment.connection_assessments.length} connections compatible` : ''}</p>}
        <p className="assessment-synthesis">{assessment.synthesis}</p>
        <dl className="assessment-facts">
          {local && assessment.connection_assessments?.[0]?.connection_kind && <div><dt>Connection meaning</dt><dd>{readable(assessment.connection_assessments[0].connection_kind)}; no direct first-to-last effect is inferred</dd></div>}
          <div><dt>Project scope</dt><dd>{assessment.in_scope ? 'In scope' : 'Outside scope'}{assessment.scope_reason ? `. ${assessment.scope_reason}` : ''}</dd></div>
          <div><dt>{local ? 'Whole-chain assessment' : 'Route demonstration'}</dt><dd>{local ? 'Not assessed. This verdict applies only to the two neighboring claims and their connection.' : record.experimentally_demonstrated ? 'A supplied paper reports evidence for this complete route.' : 'This result does not establish an experimentally demonstrated end-to-end causal pathway.'}</dd></div>
        </dl>
        {!!assessment.limitations?.length && <div className="assessment-limitations"><strong>Limitations</strong>
          <ul>{assessment.limitations.map((text, n) => <li key={n}>{text}</li>)}</ul></div>}
        <References path={record.pathway_id} refs={assessment.route_proof_refs} label="Route proof" />
      </>}
      {(record.error || record.reason) && <p className="pathway-notice" role="status"><CircleAlert size={16} />{record.error || record.reason}</p>}
      {!!record.paper_assessment_errors?.length && <div className="pathway-notice" role="status"><CircleAlert size={16} />
        {record.paper_assessment_errors.length} paper assessment(s) await evidence review. Their invalid proof is excluded; completed findings are retained. Resume retries the pending work.</div>}
    </div>
    {!!assessment?.corrections?.length && <section className="assessment-corrections" aria-label="Proposed corrections for review">
      <h4>Proposed corrections for review</h4><p>The original claims are preserved. These proposals require researcher review and do not validate the original wording.</p>
      {assessment.corrections.map(row => <div key={row.edge_id} className="pathway-proof-item">
        <small>Link {edgeIndex[row.edge_id] ?? '?'}</small><p><strong>Original:</strong> {target.source_claims?.find(item => item.edge_id === row.edge_id)?.claim?.claim_text || edgeText(edgeById[row.edge_id])}</p>
        <p><strong>Proposed:</strong> {row.claim_text}</p><p>{row.reason}</p><References path={record.pathway_id} refs={row.proof_refs} label="Correction evidence" />
        {onInspectClaim && (!paperKey || edgeById[row.edge_id]?.document_key === paperKey) && <button type="button" className="text-button" onClick={() => onInspectClaim(edgeById[row.edge_id].candidate_id)}>Inspect original claim <ArrowRight size={13} /></button>}
      </div>)}
    </section>}
    {assessment && <details className="pathway-proof"><summary><ListChecks size={15} aria-hidden="true" />Connection and component findings
      <span>{(assessment.edge_assessments?.length || 0) + (assessment.connection_assessments?.length || 0) + (assessment.entity_roles?.length || 0)}</span></summary>
      <div className="pathway-proof-list">
        {assessment.edge_assessments?.map(row => <section key={row.edge_id} className="pathway-proof-item">
          <div className="pathway-proof-title"><span>Link {edgeIndex[row.edge_id] ?? '?'}</span><Badge tone={STANCE_TONE[row.stance]}>{readable(row.stance)}</Badge></div>
          {edgeById[row.edge_id] && <h4>{edgeText(edgeById[row.edge_id])}</h4>}
          <p>{row.reason}</p><small className="pathway-proof-id">{row.edge_id}</small>
          <References path={record.pathway_id} refs={row.proof_refs} /></section>)}
        {assessment.connection_assessments?.map(row => <section key={row.connection_id} className="pathway-proof-item">
          <div className="pathway-proof-title"><span><Link2 size={12} aria-hidden="true" /> Connection{connectionById[row.connection_id]?.label ? ` at ${connectionById[row.connection_id].label}` : ''}</span>
            <Badge tone={STANCE_TONE[row.compatibility]}>Connection: {readable(row.compatibility)}</Badge></div>
          <p>{row.reason}</p><References path={record.pathway_id} refs={row.proof_refs} /></section>)}
        {assessment.entity_roles?.map((row, n) => <section key={row.node_id || n} className="pathway-proof-item">
          <div className="pathway-proof-title"><span>Entity role{row.node_id ? ` · ${nodeLabelById[row.node_id] || row.node_id}` : ''}</span>
            <Badge tone={row.resolved ? 'support' : 'neutral'}>{row.resolved ? 'Evidence-grounded role' : 'Unresolved role'}</Badge></div>
          <h4>{row.contextual_role}: {row.proposed_type}</h4>
          <p>{row.reason}</p><References path={record.pathway_id} refs={row.proof_refs} /></section>)}
      </div>
    </details>}
    <details className="pathway-proof"><summary><BookOpen size={15} aria-hidden="true" />Original claims, conditions and source passages<span>{(target.source_claims || []).length}</span></summary>
      <div className="pathway-proof-list">{(target.source_claims || []).map((row, n) => <section key={row.edge_id} className="pathway-proof-item">
        <div className="pathway-proof-title"><span>Link {edgeIndex[row.edge_id] ?? n + 1} · {row.document_key}</span><span>page {row.claim.source_page ?? 'not recorded'}</span></div>
        <h4>{row.claim.subject} <span>{readable(row.claim.relation)}</span> {row.claim.object}</h4>
        <dl className="pathway-facts">
          <div><dt>Certainty</dt><dd>{row.claim.certainty || 'not recorded'}</dd></div>
          {Object.entries(row.claim.qualifiers || {}).filter(([, value]) => value).map(([key, value]) =>
            <div key={key}><dt>{readable(key)}</dt><dd>{String(value)}</dd></div>)}
        </dl>
        <blockquote>{row.claim.source_evidence_span}</blockquote>
        <div className="assessment-claim-footer"><small className="pathway-proof-id">{row.claim.candidate_id} · {row.edge_id}</small>
          {onInspectClaim && (!paperKey || row.document_key === paperKey) && <button type="button" className="text-button" onClick={() => onInspectClaim(row.claim.candidate_id)}>Inspect in Claims <ArrowRight size={13} /></button>}</div></section>)}</div>
    </details>
    <details className="pathway-proof"><summary><FileSearch size={15} aria-hidden="true" />External paper findings and exact evidence · {papers.length} papers</summary>
      <div className="pathway-proof-list">{papers.map(paper => {
        const paperRefs = ids => (ids || []).map(proof_id => ({ evidence_document_id: paper.evidence_document_id, proof_id }))
        return <section key={paper.evidence_document_id} className="pathway-proof-item">
          <div className="pathway-proof-title"><span>{paper.paper?.evidence_level === 'source_reference' ? 'Cited source reference' : readable(paper.paper?.evidence_level || 'independent search')}{paper.paper?.publication_year ? ` · ${paper.paper.publication_year}` : ''}</span>
            <Badge tone={ROUTE_TONE[paper.route_assessment]}>{local ? 'Neighboring claim findings' : readable(paper.route_assessment)}</Badge></div>
          <h4>{paper.paper?.title || paper.evidence_document_id}</h4>
          {paper.paper?.doi && <a className="assessment-paper-link" href={`https://doi.org/${paper.paper.doi}`} target="_blank" rel="noreferrer">Open paper <ExternalLink size={11} aria-hidden="true" /></a>}
          <p>{local ? 'Local evidence' : readable(paper.route_assessment)} · {paper.finding}</p>
          <div className="assessment-paper-meta">{!local && <><span>{readable(paper.study_design || 'design not recorded')}</span><span>{readable(paper.route_demonstration || 'demonstration not recorded')}</span></>}
            {paper.paper?.evidence_access && <span>{readable(paper.paper.evidence_access)}</span>}{paper.paper?.evidence_level && <span>{readable(paper.paper.evidence_level)}</span>}</div>
          <References path={record.pathway_id} refs={paperRefs(paper.route_proof_ids)} label="Route proof" />
          {(paper.edge_findings?.length > 0 || paper.connection_findings?.length > 0) && <details className="assessment-raw">
            <summary>Per-link &amp; connection findings in this paper ({(paper.edge_findings?.length || 0) + (paper.connection_findings?.length || 0)})</summary>
            <div className="paper-subfindings">
              {paper.edge_findings?.map(item => <div key={item.edge_id} className="paper-subfinding">
                <div className="pathway-proof-title"><span>Link {edgeIndex[item.edge_id] ?? '?'}{edgeById[item.edge_id] ? `: ${edgeText(edgeById[item.edge_id])}` : ''}</span>
                  <span>{item.conditions_match === true && <Badge tone="support">Conditions matched</Badge>}
                    {item.conditions_match === false && <Badge tone="partial">Conditions differ</Badge>}
                    {item.conditions_match == null && <Badge>Conditions unconfirmed</Badge>}
                    <Badge tone={findingPresentation(item).tone}>
                      {findingPresentation(item).label}</Badge></span></div>
                {item.claim_match && <small className="paper-claim-match">Original proposition: {readable(item.claim_match)} · Applicable conditions: {item.conditions_match === true ? 'yes' : item.conditions_match === false ? 'no' : 'unresolved'}</small>}
                <p>{item.reason}</p><References path={record.pathway_id} refs={paperRefs(item.proof_ids)} /></div>)}
              {paper.connection_findings?.map(item => <div key={item.connection_id} className="paper-subfinding">
                <div className="pathway-proof-title"><span>Connection{connectionById[item.connection_id]?.label ? ` at ${connectionById[item.connection_id].label}` : ''}</span>
                  <Badge tone={STANCE_TONE[item.compatibility]}>{readable(item.compatibility)}</Badge></div>
                <p>{item.reason}</p><References path={record.pathway_id} refs={paperRefs(item.proof_ids)} /></div>)}
            </div>
          </details>}
          {paper.proofs?.map(proof => <div key={proof.proof_id} className="assessment-proof" id={anchor(record.pathway_id, paper.evidence_document_id, proof.proof_id)}>
            {proof.source_kind === 'table_row' && <small className="pathway-proof-id">Table {proof.table_id}, row {proof.row_number} · serialized with headers</small>}
            <blockquote>{proof.quoted_span}</blockquote>
            <small className="pathway-proof-id">{proof.proof_id} · {proof.chunk_id} · exact sentence IDs: {(proof.sentence_numbers || []).join(', ')}{proof.is_contiguous === false ? ' · non-adjacent sentences selected individually, not as a range' : ''}</small></div>)}
          {!!paper.calculation_results?.length && <details className="assessment-raw"><summary>Arithmetic assistance</summary>
            <p>Calculated from literal numbers in this paper. Units and scientific comparability remain model judgements.</p>
            {paper.calculation_results.map((item, n) => <div key={n} className="paper-subfinding">
              <p>{readable(item.operation)}: {item.operands.join(', ')} → {item.value_percent}%</p>
              <References path={record.pathway_id} refs={paperRefs([item.proof_id])} /></div>)}
          </details>}
          {!!record.evidence_documents?.find(item => item.evidence_document_id === paper.evidence_document_id)?.access_diagnostics?.length &&
            <details className="assessment-raw"><summary>Full-text access attempts</summary><ul>{record.evidence_documents.find(item => item.evidence_document_id === paper.evidence_document_id).access_diagnostics.map((item, n) =>
              <li key={n}>{item.source || item.url || 'Public full text'}: {readable(item.status || item.error || 'unavailable')}</li>)}</ul></details>}
          {paper.nli_diagnostic && <details className="assessment-raw"><summary>NLI diagnostic — not scientific proof</summary>
            {paper.nli_diagnostic.scores && <p className="nli-summary">Entailment {(100 * paper.nli_diagnostic.scores.entailment).toFixed(1)}% · Neutral {(100 * paper.nli_diagnostic.scores.neutral).toFixed(1)}% · Contradiction {(100 * paper.nli_diagnostic.scores.contradiction).toFixed(1)}% — language-consistency diagnostic only; never promotes a pathway.</p>}
            {paper.nli_diagnostic.unavailable && <p className="nli-summary">NLI diagnostic unavailable: {paper.nli_diagnostic.unavailable}</p>}
            <pre>{JSON.stringify(paper.nli_diagnostic, null, 2)}</pre></details>}
        </section>
      })}
      {!papers.length && <p className="soft-note">No external paper findings are recorded for this pathway.</p>}</div>
    </details>
    {!!record.paper_assessment_errors?.length && <details className="pathway-proof"><summary>Paper assessments awaiting evidence review ({record.paper_assessment_errors.length})</summary>
      <div className="pathway-proof-list">{record.paper_assessment_errors.map(row => <section key={row.evidence_document_id} className="pathway-proof-item">
        <h4>{row.title || row.evidence_document_id}</h4><p>{row.error}</p><small className="pathway-proof-id">{row.evidence_document_id}</small>
        {row.review_response && <details className="assessment-raw"><summary>Unadmitted model proposal</summary><pre>{JSON.stringify(row.review_response, null, 2)}</pre></details>}
      </section>)}</div></details>}
    {record.review_response && <details className="pathway-proof"><summary>Unadmitted assessment proposal</summary><pre className="assessment-raw">{JSON.stringify(record.review_response, null, 2)}</pre></details>}
    <details className="pathway-proof"><summary>Retrieval coverage, query plan and assessment history</summary>
      <div className="pathway-proof-list">
        {record.retrieval_rounds?.map((round, n) => {
          const queries = round.query_translation?.queries || []
          const missingFacets = round.pathway_coverage?.targets_without_retained_passages || []
          const citations = round.citation_ledger || []
          const pending = round.pending_sources || []
          return <section key={n} className="pathway-proof-item">
            <div className="pathway-proof-title"><span>Search round {n + 1}{n > 0 ? ' · targeted follow-up' : ''}</span>
              <Badge tone={round.retrieval_pending ? 'partial' : 'neutral'}>{round.retrieval_pending ? 'Missing searches pending' : 'Selected searches completed'}</Badge></div>
            <p>{round.evidence_documents_count} papers retained · {round.retrieval_pending ? 'Missing searches pending' : 'Selected searches completed'}
              {citations.length > 0 && ` · ${citations.filter(c => c.included_in_judgement).length}/${citations.length} source citations in judgement`}</p>
            {citations.filter(c => c.resolution_diagnostics?.reason).map(c => <p className="soft-note" key={c.reference_id}>Citation lookup: {readable(c.resolution_diagnostics.reason)}. This does not establish that the claim is false.</p>)}
            {missingFacets.length > 0 && <p className="soft-note">Retrieval facets with no retained passages: {missingFacets.join(', ')}. Passage ranking reflects lexical/semantic similarity, not scientific proof.</p>}
            {pending.length > 0 && <p className="pathway-notice"><CircleAlert size={15} />Pending sources: {pending.map(item => `${item.provider || item.kind} (${item.reason})`).join('; ')}</p>}
            {queries.length > 0 && <details className="assessment-raw"><summary>Planned literature queries · {queries.length} facet(s)</summary>
              <div className="query-plan-list">{queries.map((item, qIndex) => <div key={`${item.target_id}-${qIndex}`} className="query-plan-item">
                <strong>{item.target_id === 'route' ? 'Complete route' : edgeIndex[item.target_id] ? `Link ${edgeIndex[item.target_id]}` : connectionById[item.target_id]?.label ? `Connection at ${connectionById[item.target_id].label}` : item.target_id}</strong>
                <small><b>Europe PMC:</b> {item.query}</small>
                <small><b>OpenAlex:</b> {item.openalex_query}</small></div>)}</div></details>}
            <details className="assessment-raw"><summary>Query plan, coverage and citation ledger (saved JSON)</summary>
              <pre>{JSON.stringify({ queries: round.query_translation, coverage: round.pathway_coverage, citations: round.citation_ledger, pending: round.pending_sources }, null, 2)}</pre></details>
          </section>
        })}
        {record.assessment_history?.length > 1 && <section className="pathway-proof-item">
          <div className="pathway-proof-title"><span>Synthesis progression across follow-up retrieval</span><span>{record.assessment_history.length} readings</span></div>
          {record.assessment_history.map((reading, rIndex) => <p key={rIndex}><strong>Reading {rIndex + 1} ({outcomeLabel(reading.verdict)}):</strong> {reading.synthesis}</p>)}
          <details className="assessment-raw"><summary>Prior synthesis readings (saved JSON)</summary>
            <pre>{JSON.stringify(record.assessment_history, null, 2)}</pre></details>
        </section>}
        <p className="pathway-proof-id">{record.pathway_id}</p><small className="pathway-proof-id">Completed: {record.completed_at || 'not completed'} · {record.elapsed_seconds ?? '—'} seconds</small>
      </div>
    </details>
  </article>
}

function sortRecords(rows, sortBy) {
  const rank = row => { const index = OUTCOME_ORDER.indexOf(pathwayOutcome(row)); return index < 0 ? OUTCOME_ORDER.length : index }
  const compare = {
    outcome: (a, b) => rank(a) - rank(b),
    edges: (a, b) => (b.target?.edges?.length || 0) - (a.target?.edges?.length || 0),
    papers: (a, b) => (b.paper_assessments?.length || 0) - (a.paper_assessments?.length || 0),
    recent: (a, b) => String(b.completed_at || '').localeCompare(String(a.completed_at || '')),
  }[sortBy]
  return compare ? [...rows].sort(compare) : rows
}

function OutcomeSummary({ counts, filter, onFilter }) {
  const groups = [['scientific', 'Scientific outcomes'], ['execution', 'Execution states · not negative verdicts']]
  return <div className="outcome-summary" aria-label="Pathway outcome counts">
    {groups.map(([group, title]) => {
      const keys = OUTCOME_ORDER.filter(key => PATHWAY_OUTCOMES[key].group === group && counts[key])
      if (!keys.length) return null
      return <div className="outcome-group" key={group}><span className="outcome-group-title">{title}</span>
        <div className="outcome-chips">{keys.map(key => <button type="button" key={key}
          className={`outcome-chip tone-${PATHWAY_OUTCOMES[key].tone} ${filter === key ? 'active' : ''}`}
          aria-pressed={filter === key} title={PATHWAY_OUTCOMES[key].description}
          onClick={() => onFilter(filter === key ? 'all' : key)}><i />{PATHWAY_OUTCOMES[key].label}<b>{counts[key]}</b></button>)}</div></div>
    })}
  </div>
}

export default function PathwayAssessments({ data, supportedOnly = false, paperKey, focusedPathwayId, onClearFocus, onInspectClaim }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [sortBy, setSortBy] = useState('outcome')
  const [scope, setScope] = useState('all')
  const records = data?.records || []
  const eligibleFiltered = supportedOnly ? records.filter(row => row.eligible) : records
  const paperMatches = paperKey ? eligibleFiltered.filter(row => (row.target?.document_keys || []).includes(paperKey)) : eligibleFiltered
  const hasOtherPapers = Boolean(paperKey && paperMatches.length !== eligibleFiltered.length)
  const scoped = scope === 'paper' && paperKey ? paperMatches : eligibleFiltered
  const counts = countOutcomes(scoped)
  const visible = sortRecords(scoped.filter(row => (filter === 'all' || pathwayOutcome(row) === filter)
    && (!query || `${row.target?.description || ''} ${row.assessment?.synthesis || ''} ${(row.target?.document_keys || []).join(' ')} ${row.pathway_id || ''}`.toLowerCase().includes(query.toLowerCase()))), sortBy)

  useEffect(() => {
    if (!focusedPathwayId) return
    setFilter('all')
    setScope('all')
    setQuery('')
    const timer = setTimeout(() => {
      const el = typeof document === 'undefined' ? null : document.getElementById(cardAnchor(focusedPathwayId))
      el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 60)
    return () => clearTimeout(timer)
  }, [focusedPathwayId])

  if (!data) return <section className="pathway-workspace" aria-label="Whole-pathway validation results" role="status">
    <p className="soft-note">Loading saved pathway assessments…</p></section>
  return <section className="pathway-workspace" aria-label="Whole-pathway validation results">
    <div className="pathway-section-heading assessment-heading"><div>
      <h3>{supportedOnly ? 'Evidence-supported pathways' : 'Whole-pathway validation'}</h3>
      <p>Each verdict concerns a specific ordered chain and its conditions. Individually supported components alone do not establish the complete mechanism.</p></div>
      <div className="assessment-heading-controls">
        {hasOtherPapers && <div className="segmented" role="group" aria-label="Filter assessments by paper scope">
          <button type="button" className={scope === 'all' ? 'active' : ''} aria-pressed={scope === 'all'} onClick={() => setScope('all')}>All workspace ({eligibleFiltered.length})</button>
          <button type="button" className={scope === 'paper' ? 'active' : ''} aria-pressed={scope === 'paper'} onClick={() => setScope('paper')}>This paper ({paperMatches.length})</button>
        </div>}
        <span className="subtle-count">{scoped.length} {supportedOnly ? 'eligible' : 'saved'} assessments</span>
      </div></div>
    {data.error && <p className="pathway-notice error" role="alert"><CircleAlert size={16} />Pathway assessments could not be loaded: {data.error}</p>}
    {data.assembly_stale && <p className="pathway-notice" role="status"><CircleAlert size={16} />Saved claims have changed since the provisional assembly was built. Affected routes need reassessment.</p>}
    {scoped.length > 0 && <OutcomeSummary counts={counts} filter={filter} onFilter={setFilter} />}
    <div className="pathway-searchbar"><label className="pathway-search"><Search size={16} /><input type="search" aria-label="Search pathway assessments" value={query} onChange={e => setQuery(e.target.value)} placeholder="Search a pathway, entity, ID or source paper" /></label>
      <label className="pathway-state-filter">Outcome<select aria-label="Filter pathway outcomes" value={filter} onChange={e => setFilter(e.target.value)}><option value="all">All outcomes</option>
        {OUTCOME_ORDER.map(key => <option value={key} key={key}>{PATHWAY_OUTCOMES[key].label}{counts[key] ? ` (${counts[key]})` : ''}</option>)}</select></label>
      <label className="pathway-state-filter">Sort<select aria-label="Sort pathway assessments" value={sortBy} onChange={e => setSortBy(e.target.value)}>
        <option value="outcome">Outcome</option><option value="edges">Most relations</option><option value="papers">Most papers assessed</option><option value="recent">Recently completed</option></select></label>
      {(query || filter !== 'all' || focusedPathwayId) && <button type="button" className="text-button" onClick={() => { setQuery(''); setFilter('all'); onClearFocus?.() }}>Clear filters</button>}</div>
    {data.coverage && Object.keys(data.coverage).length > 0 && <div className="pathway-guidance assessment-coverage"><p>Latest selection{data.coverage.document_key ? ` (${data.coverage.document_key})` : ''}: {data.coverage.selected_pathways ?? records.length} pathways · up to {data.coverage.max_hops ?? '—'} edges per chain. Saved assessments can include earlier selections across the workspace.</p>
      {(data.coverage.has_more || data.coverage.depth_limited) && <p className="pathway-notice"><CircleAlert size={16} />Additional or deeper chains remain unassessed because of the recorded run limits.</p>}
      {!!data.coverage.claims_outside_selected_pathways?.length && <p>{data.coverage.claims_outside_selected_pathways.length} source claims are outside the selected chains and remain externally unvalidated.</p>}
    </div>}
    <div className="assessment-results-bar"><p className="pathway-results-count" role="status">{visible.length} matching pathways · {scoped.filter(r => r.stale).length} stale assessments</p>
      {visible.length > 0 && <ChainLegend />}</div>
    <div className="research-paths-list">{visible.map((row, n) => <PathwayAssessmentCard key={row.pathway_id} record={row} number={n + 1} focused={focusedPathwayId === row.pathway_id} paperKey={paperKey} onInspectClaim={onInspectClaim} />)}</div>
    {!visible.length && <p className="soft-note">{records.length ? 'No pathway assessments match these filters.' : 'No whole-pathway assessments are saved yet. Earlier claim-level results remain historical; they do not establish validated pathways.'}</p>}
  </section>
}
