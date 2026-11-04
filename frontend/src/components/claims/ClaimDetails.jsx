import { useState } from 'react'

const readable = value => String(value || '').replaceAll('_', ' ')

export function ClaimContext({ candidate, sourceReview }) {
  const manuallyAccepted = sourceReview?.active_reviews?.source_fidelity?.decision === 'accept'
  const verified = (sourceReview?.fidelity_decision || candidate.source_validation?.decision) === 'entailed'
    && candidate.source_validation?.claim_text_match === true
  const qualifiers = Object.entries(candidate.qualifiers || {}).filter(([, value]) => value)
  return <section className="claim-context-details" aria-label="Contextual claim and restrictions">
    <div className="detail-title"><span className="eyebrow">CONTEXTUAL CLAIM</span><span className={`detail-badge ${verified || manuallyAccepted ? 'verified' : ''}`}>{manuallyAccepted ? 'Source accepted manually' : verified ? 'Source verified' : candidate.claim_text_origin === 'extracted' ? 'Unverified restatement' : 'Structured claim'}</span></div>
    <p className="contextual-claim">{candidate.claim_text || `${candidate.subject} → ${readable(candidate.relation)} → ${candidate.object}`}</p>
    <dl className="claim-facts">
      <div><dt>Subject type</dt><dd>{candidate.subject_type}</dd></div>
      <div><dt>Object type</dt><dd>{candidate.object_type}</dd></div>
      <div><dt>Source modality</dt><dd>{candidate.certainty || 'Not recorded'}</dd></div>
      <div><dt>Local section</dt><dd>{candidate.source_evidence_section_heading || candidate.source_section_heading || 'Not recorded'}</dd></div>
      {qualifiers.map(([field, value]) => <div key={field}><dt>{readable(field)}</dt><dd>{value}</dd></div>)}
    </dl>
    {!qualifiers.length && <small>No explicit restrictions recorded. This does not establish universal prevalence or causality.</small>}
    <details className="context-packet"><summary>Exact source anchors</summary><dl className="claim-facts">{['subject', 'object'].map(role => <div key={role}><dt>{role}</dt><dd>{candidate[`${role}_source_mention`] || 'Not recorded'}</dd></div>)}<div><dt>Relation trigger</dt><dd>{candidate.relation_trigger || 'Not recorded'}</dd></div></dl></details>
    {!!candidate.anchor_adjustments?.length && <details className="context-packet"><summary>Literal anchor compaction · audit</summary>{candidate.anchor_adjustments.map((item, index) => <p key={index}>{item.role}: “{item.original_mention}” → “{item.mention}”. The shorter canonical phrase already occurred inside the original source anchor; no synonym or scientific inference was added.</p>)}</details>}
    {candidate.source_local_context && <details className="context-packet"><summary>Bounded local context · reference resolution only</summary><p className="preserve-lines">{candidate.source_local_context}</p></details>}
    {candidate.source_section_context && <details className="context-packet"><summary>Same-section source context · definitions and reference binding</summary><p className="preserve-lines">{candidate.source_section_context}</p></details>}
    <small>The description helps interpretation and future chaining. It is not independent evidence and does not establish transitivity.</small>
  </section>
}

export function ValidationTrace({ candidate, validation, sourceReview, reviewHistory = [], externalTarget }) {
  const semantic = candidate.semantic_validation
  const source = candidate.source_validation
  const scope = source?.project_scope
  const stages = [
    { name: 'Structure', decision: candidate.schema_error ? 'Failed' : 'Passed', rationale: candidate.schema_error || 'Format and evidence anchors checked. Relations are open; entity labels are provisional. Scientific support and project scope require evidence assessment.' },
    ...(semantic?.decision === 'not_run' ? [] : [{ name: 'Scope + types', decision: semantic?.decision, rationale: semantic?.rationale, reasons: semantic?.reasons }]),
    { name: 'Source fidelity', decision: source?.decision, rationale: source?.rationale, reasons: source?.reasons },
    { name: 'Project scope', decision: scope?.decision || 'Not assessed', rationale: scope?.rationale || 'No claim-level source-scope assessment is recorded. Resume the saved run to assess scope; extraction can be reused.' },
    ...Object.entries(sourceReview?.active_reviews || {}).map(([dimension, review]) => ({
      name: dimension === 'source_fidelity' ? 'Manual source fidelity' : 'Manual project scope',
      decision: review.decision === 'accept' ? 'Accepted' : 'Rejected',
      rationale: `${review.note || 'Researcher decision; original model assessment retained.'} Recorded ${review.at}.`,
    })),
    { name: 'External literature', decision: validation?.validation_status || (['whole_pathway','neighboring_claims'].includes(externalTarget) ? 'Whole-pathway target' : undefined), rationale: validation?.error || (validation ? `${validation.support_summary?.independent_groups ?? 0} independent supporting result group(s); ${validation.support_summary?.independent_document_groups == null ? '' : `${validation.support_summary.independent_document_groups} supporting document group(s); `}${validation.support_summary?.source_reference ?? 0} supporting source reference(s).` : ['whole_pathway','neighboring_claims'].includes(externalTarget) ? 'Not evaluated as a standalone claim. Active runs assess complete continuous pathways.' : 'Not evaluated. An earlier stage may have stopped the claim, or this run is still in progress.') },
  ]
  return <section className="validation-trace" aria-label="Validation decision trace"><span className="eyebrow">DECISION TRACE</span>
    {(sourceReview?.scope_decision || scope?.decision) === 'out_of_scope' && <p className="decision-note">Excluded from pathway assembly because this relationship is outside the project domain. This does not mean the PDF assertion is false or unsupported. The claim and source evidence remain available for audit.</p>}
    {(sourceReview?.scope_decision || scope?.decision) === 'needs_review' && <p className="decision-note">Project relevance remains unresolved. This claim is held out of pathway assembly until scope is assessed as in scope.</p>}
    {!!sourceReview?.stale_dimensions?.length && <p className="inline-error">The claim, evidence or original assessment changed. Previous manual decisions no longer apply; review this version again.</p>}
    {!!reviewHistory.length && <details className="context-packet"><summary>Manual source review history · {reviewHistory.length} decision(s)</summary>{reviewHistory.map((review, index) => <p key={review.event_id || index}>{readable(review.dimension)}: {review.decision === 'accept' ? 'Accepted' : 'Rejected'} · {review.at}. {review.note}</p>)}</details>}
    {source?.previous_assessment && <details className="context-packet"><summary>Previous source policy assessment</summary><p>{source.previous_assessment.decision}. {source.previous_assessment.rationale}</p><p>Project scope: {readable(source.previous_assessment.project_scope?.decision) || 'Not assessed'}. {source.previous_assessment.project_scope?.rationale}</p></details>}
    {validation?.validation_status === 'retrieval_pending' && <div className="quote-box"><strong>Retrieval is incomplete</strong><p>Available paper findings are shown below. Missing searches or citations remain pending; resume retries them. This claim has not entered the validated graph.</p>
      {(validation.attempts?.at(-1)?.pending_sources || []).map((item, index) => <p key={index}>{item.provider || item.kind}: {item.reason}. {item.query || item.reference_id || ''}</p>)}</div>}
    {!!source?.applied_name_corrections?.length && <details className="context-packet"><summary>Source-verified full names</summary>{source.applied_name_corrections.map((edit, index) => <div key={index}><p>{readable(edit.field)}: {edit.before} → {edit.after}</p><blockquote>{edit.definition_quote}</blockquote><p>{edit.rationale}</p></div>)}</details>}
    {source?.source_review_audit && <details className="context-packet"><summary>Historical automated source reassessment · saved readings</summary><p>Automatic source reassessment is now disabled. These readings belong to an earlier run.</p>{['original', 'reassessment'].map(reading => <div key={reading}><p>{reading}: {source.source_review_audit[reading].decision}. {source.source_review_audit[reading].rationale}</p><p>Project scope: {readable(source.source_review_audit[reading].project_scope?.decision) || 'Not assessed'}. {source.source_review_audit[reading].project_scope?.rationale}</p></div>)}</details>}
    {!!source?.abbreviation_review?.unresolved?.length && <p className="inline-error">Unresolved source abbreviations: {source.abbreviation_review.unresolved.join(', ')}. Full names require an unambiguous definition in the supplied PDF context.</p>}
    {stages.map(stage => <div className="trace-row" key={stage.name}><span className="trace-stage">{stage.name}</span><div><strong>{readable(stage.decision) || 'Not run'}</strong><p>{stage.rationale || 'No decision recorded.'}</p>{stage.reasons?.map(reason => <p className="trace-reason" key={reason}>{reason}</p>)}</div></div>)}
    {!!candidate.extraction_review_hints?.length && <details className="context-packet"><summary>Extraction review notes · claim retained</summary><p>These heuristic notes do not reject or correct the claim. Evidence assessment determines their significance.</p>{candidate.extraction_review_hints.map(hint => <p key={hint}>{readable(hint)}</p>)}</details>}
    {!!source?.review_hints?.length && <details className="context-packet"><summary>Source review notes · model judgement retained</summary>{source.review_hints.map(hint => <p key={hint}>{readable(hint)}</p>)}</details>}
    {!!source?.alignment_adjustments?.length && <details className="context-packet"><summary>Audited source-reference adjustments</summary>{source.alignment_adjustments.map((item, index) => <p key={index}>{item.role}: “{item.original_evidence}” → “{item.evidence}” ({readable(item.basis)})</p>)}</details>}
    {!!source?.unchanged_name_annotations?.length && <p>Canonical names were already spelled out; no redundant name edits were applied.</p>}
    {source?.entity_alignment && <details className="context-packet"><summary>How source entities were aligned</summary>{Object.entries(source.entity_alignment).map(([role, item]) => <p key={role}><strong>{role}:</strong> “{item.mention}” → {item.canonical} ({readable(item.kind)}). {item.explanation}<br />Binding evidence: “{item.evidence}”</p>)}</details>}
  </section>
}

export function ExternalDecisionDetails({ decision }) {
  if (!decision) return null
  const nli = decision.nli
  const rejected = decision.rejected_model_decision
  const selections = decision.evidence?.length ? decision.evidence : rejected?.evidence || []
  return <div className="external-decision-details">
    {decision.finding && <div className="decision-note"><strong>Paper finding ({readable(decision.finding.evidence_origin)})</strong><p>{decision.finding.summary}</p><p>Study design: {decision.finding.study_design}. Outcome: {decision.finding.measured_outcome}.</p><p>Conditions: {decision.finding.population_and_conditions}</p><p>Limitations: {decision.finding.limitations}</p></div>}
    {decision.synthesis_disposition && <p>Cross-paper assessment: {readable(decision.synthesis_disposition.disposition)}. {decision.synthesis_disposition.reason}</p>}
    {!!decision.binding_context?.length && <details className="context-packet"><summary>Paper-local definitions and conditions</summary>{decision.binding_context.map((item, index) => <div key={index}><p>{item.chunk_id}; sentences {item.sentence_numbers.join(', ')}</p><blockquote>{item.quoted_span}</blockquote></div>)}</details>}
    {selections.some(item => item.is_contiguous === false) && <details className="context-packet"><summary>Selected evidence sentences and intervening context</summary>
      {!decision.evidence?.length && rejected && <p>Model-proposed selection; not accepted as proof.</p>}
      {selections.filter(item => item.is_contiguous === false).map((item, index) => <div key={index}>
        <p>{item.chunk_id}; selected sentences: {item.sentence_numbers.join(', ')}. These IDs select individual sentences, not a range.</p>
        {item.fragments?.map((fragment, fragmentIndex) => <blockquote key={fragmentIndex}>{fragment}</blockquote>)}
        <p>Intervening sentences are context only, not selected proof:</p>
        {item.intervening_context?.map(sentence => <p key={sentence.number}><strong>Sentence {sentence.number}:</strong> {sentence.text}</p>)}
      </div>)}
    </details>}
    {decision.judge && <p>Judge: {decision.judge.actual_model || 'Not recorded'} ({decision.judge.tier}). Primary: {decision.judge.primary_model || 'Not recorded'}.</p>}
    {!!decision.matches && <div className="match-checks">{Object.entries(decision.matches).map(([key, value]) => <span key={key} className={value ? 'matched' : 'mismatched'}>{readable(key)}: {value ? 'matched' : 'not matched'}</span>)}</div>}
    {!!decision.guardrail_reasons?.length && <div className="decision-note"><strong>Why the proposed decision did not pass</strong>{decision.guardrail_reasons.map(reason => <p key={reason}>{reason}</p>)}</div>}
    {!!decision.review_hints?.length && <details className="context-packet"><summary>Evidence review notes · model judgement retained</summary>{decision.review_hints.map(hint => <p key={hint}>{readable(hint)}</p>)}</details>}
    {decision.evidence?.some(item => item.selection_adjustment) && <details className="context-packet"><summary>Sentence selection adjustment · audit</summary>{decision.evidence.filter(item => item.selection_adjustment).map((item, index) => <p key={index}>Selected IDs {item.selection_adjustment.original_sentence_numbers.join(', ')} were stored as the complete consecutive interval {item.sentence_numbers.join(', ')}. Intervening text is retained and all evidence checks still apply.</p>)}</details>}
    {rejected && <details className="context-packet"><summary>Original model proposal · not accepted as proof</summary><p>{rejected.rationale}</p>{rejected.evidence?.map((item, index) => <div key={index}><blockquote>{item.quoted_span || 'No admissible quote recorded'}</blockquote><p>Proposed trigger: “{item.relation_trigger}”</p><p>Subject: “{item.subject_mention}”; object: “{item.object_mention}”</p></div>)}</details>}
    {decision.entity_alignment && <details className="context-packet"><summary>External entity mappings</summary>{Object.entries(decision.entity_alignment).map(([role, item]) => <p key={role}>{role}: “{item.mention}” → {item.canonical}. {item.explanation}</p>)}</details>}
    {nli && <details className="context-packet"><summary>Independent NLI check{nli.scores ? ` · contradiction ${(100 * nli.scores.contradiction).toFixed(1)}%` : ''}</summary>{nli.scores ? <p>Entailment {(100 * nli.scores.entailment).toFixed(1)}% · neutral {(100 * nli.scores.neutral).toFixed(1)}% · contradiction {(100 * nli.scores.contradiction).toFixed(1)}%. These are model scores, not measured claim-correctness probabilities.</p> : <p>{nli.skipped || nli.error || 'Unavailable'}</p>}</details>}
  </div>
}

export function ExtractionDiscards({ records }) {
  const [open, setOpen] = useState(false)
  if (!records.length) return null
  return <section className="extraction-discards"><button className="discard-toggle" type="button" onClick={() => setOpen(!open)} aria-expanded={open}>Extraction-stage discards ({records.length}) <span>{open ? 'Hide' : 'Inspect reasons and anchors'}</span></button>
    {open && records.map((record, index) => <article className="discard-record" key={index}><strong>{record.proposed_claim?.subject || 'Malformed claim'} → {readable(record.proposed_claim?.relation)} → {record.proposed_claim?.object || '?'}</strong><p className="trace-reason">{readable(record.reason)}</p>{record.proposed_claim_text && <p>{record.proposed_claim_text}</p>}<small>Packet {record.packet_number} · {record.section_heading}</small>{record.source_evidence_span && <blockquote>{record.source_evidence_span}</blockquote>}<p>Subject anchor: {record.proposed_source_anchors?.subject || 'Not saved in this run'}<br />Object anchor: {record.proposed_source_anchors?.object || 'Not saved in this run'}<br />Relation anchor: {record.proposed_source_anchors?.relation || 'Not saved in this run'}</p></article>)}
  </section>
}
export function ManualSourceReview({ candidate, review, busy = false, onAction, readOnly = false }) {
  const [note, setNote] = useState('')
  if (readOnly || !candidate.source_validation || !review) return null
  return <section className="claim-repair-action" aria-label="Manual source review">
    <strong>Review source evidence and project relevance</strong>
    <p>Automatic source reassessment is off. Accept or reject PDF fidelity and project scope separately after reading the source. Both must pass for pathway assembly. These buttons make no LLM calls and do not establish external scientific support.</p>
    {[['source_fidelity', 'PDF fidelity', 'source', review?.fidelity_decision], ['project_scope', 'Project scope', 'scope', review?.scope_decision]].map(([dimension, label, action, effective]) => <div className="manual-source-review-row" key={dimension}>
      <span><strong>{label}</strong><small>Current: {readable(effective) || 'Not assessed'}{review?.active_reviews?.[dimension] ? ' · manual decision' : ' · model assessment'}</small></span>
      <button type="button" className="button secondary" aria-label={`Accept ${label.toLowerCase()}`} aria-pressed={review?.active_reviews?.[dimension]?.decision === 'accept'} disabled={busy || !onAction || !review?.can_accept || !review?.revision} onClick={() => onAction(`accept_${action}`, note)}>Accept</button>
      <button type="button" className="button secondary" aria-label={`Reject ${label.toLowerCase()}`} aria-pressed={review?.active_reviews?.[dimension]?.decision === 'reject'} disabled={busy || !onAction || !review?.revision} onClick={() => onAction(`reject_${action}`, note)}>Reject</button>
    </div>)}
    <label className="manual-source-review-note">Review note (optional)<textarea rows={2} maxLength={2000} value={note} onChange={event => setNote(event.target.value)} disabled={busy} /></label>
    {review && !review.can_accept && <small>Acceptance requires valid claim structure and recorded source evidence. Repair missing or invalid evidence first.</small>}
    <small>Manual decisions are saved with the original model assessment. Resume the saved pipeline after reviewing claims to rebuild affected pathways.</small>
  </section>
}

export function ClaimActions({ candidate, sourceReview, repairEligibility, revisions = [], busy = false, onAction, readOnly = false, pathwayOnly = false }) {
  const source = { ...(candidate.source_validation || {}), ...(sourceReview ? { decision: sourceReview.fidelity_decision } : {}) }
  const semantic = candidate.semantic_validation || {}
  const conflict = repairEligibility?.external_contradictions?.[0]
  const proofContradiction = !!conflict
  const isRepresentationHold = !proofContradiction && source.decision === 'needs_review'
    && source.model_matches?.relation_match === true
    && source.model_matches?.direction_match === true
    && (source.claim_text_match === false || source.model_matches?.certainty_match === false)
    const canRepair = !candidate.superseded_by?.length && (repairEligibility?.allowed ?? (['unsupported', 'needs_review'].includes(source.decision)
    || (!!candidate.repair_revision && source.decision !== 'entailed')))
  const reason = proofContradiction
    ? `${conflict.rationale || 'External evidence contains a verified quoted contradiction.'} A repair must still match the original PDF.`
    : [...(source.reasons || semantic.reasons || []), source.rationale || semantic.rationale].filter(Boolean).join(' / ')
  return <>
    <ManualSourceReview candidate={candidate} review={sourceReview} busy={busy} onAction={onAction} readOnly={readOnly} />
      {!!candidate.superseded_by?.length && <p className="soft-note">Preserved original; superseded by {candidate.superseded_by.length} atomic source claims. Inspect its revision history for the children and their evidence.</p>}
      {candidate.parent_candidate_id && <p className="soft-note">Atomic child of {candidate.parent_candidate_id}; original source provenance is retained.</p>}
    {!readOnly && canRepair && <div className="claim-repair-action">
      {isRepresentationHold && <div className="soft-note">
        <strong className="detail-badge">Source relation grounded · Needs atomic split or certainty adjustment</strong>
        <p>Core relation matches the PDF, but claim text bundles secondary propositions or certainty needs adjustment. Use <strong>Re-extract atomic claims</strong> to split it into independent atomic edges.</p>
      </div>}
      <p><strong>{proofContradiction ? 'Proof-backed contradiction: ' : isRepresentationHold ? 'Atomic split / certainty adjustment needed: ' : 'Source correction needed: '}</strong>{reason}</p>
      {proofContradiction && <blockquote>{conflict.evidence?.[0]?.quoted_span}</blockquote>}
      <button type="button" className="button secondary" disabled={busy || !onAction}
        onClick={() => onAction('repair')}>
        Repair and revalidate claim
      </button>
        <button type="button" className={`button ${isRepresentationHold ? 'primary' : 'secondary'}`} disabled={busy || !onAction}
          onClick={() => onAction('reextract')}>Re-extract atomic claims</button>
        <small>Re-extract only this claim's selected source sentences, source-check each child and reassess affected connections. The original remains in audit history.</small>
      <small>{pathwayOnly ? 'Source-grounded repair or atomic re-extraction, then reassembly and affected neighboring-pair checks. Original versions are retained; changed inputs invalidate stale assessments.' : 'One source-grounded correction per click, followed by source and pathway checks. Original versions are retained.'}</small>
    </div>}
    {!!revisions.length && <details className="context-packet"><summary>Claim revision history · {revisions.length} attempt(s)</summary>
      {revisions.map((revision, index) => <div key={revision.operation_id || index}>
          <p><strong>{revision.action === 'repair' ? 'Source repair' : revision.action === 'reextract' ? 'Atomic re-extraction' : 'External retry'} · {revision.status}</strong></p>
        <p>Before: {revision.original_candidate?.subject} → {revision.original_candidate?.relation} → {revision.original_candidate?.object}</p>
        <p>Previous source verdict: {revision.original_candidate?.source_validation?.decision || 'not evaluated'}</p>
        {revision.original_candidate?.source_validation?.reasons?.map((reason, i) => <p key={i}>{reason}</p>)}
        {revision.revised_candidate && <p>After: {revision.revised_candidate.subject} → {revision.revised_candidate.relation} → {revision.revised_candidate.object}</p>}
          {revision.proposed_candidates?.map(child => <p key={child.candidate_id}>Child: {child.subject} → {child.relation} → {child.object} · Source: {child.source_validation?.decision || 'not assessed'} · {child.candidate_id}</p>)}
        {revision.error && <p className="inline-error">{revision.error}</p>}
        {revision.status === 'no_grounded_correction' && <p>The model produced no admissible source-grounded correction. The original claim was retained.</p>}
      </div>)}
    </details>}
  </>
}


export function JointAssessment({ validation }) {
  const joint = validation?.attempts?.at(-1)?.joint_assessment
  if (!joint) return null
  const semantic = joint.semantic_assessment || {}
  return <div className="quote-box">
    <span className="eyebrow">{joint.policy?.startsWith('per_paper_synthesis') ? 'PER-PAPER JUDGEMENTS / CROSS-PAPER SYNTHESIS' : 'JOINT EXTERNAL ASSESSMENT / ONE PRO REQUEST'}</span>
    {joint.policy?.startsWith('per_paper_synthesis') && <p>{joint.paper_judgements} paper judgements, {joint.reused_paper_judgements ?? 0} reused, followed by one synthesis call. Thinking: {joint.thinking === false ? 'Off' : 'On'}.</p>}
    {joint.phase_models && <p>Per-paper model: {joint.phase_models.paper}. Cross-paper synthesis model: {joint.phase_models.synthesis}.</p>}
    <p><strong>Model synthesis:</strong> {joint.synthesis}</p>
    <p>After proof checks: {joint.guarded_counts?.supported ?? 0} supporting · {joint.guarded_counts?.contradicted ?? 0} contradicting · {joint.guarded_counts?.needs_review ?? 0} requiring review.</p>
    <p>Evidence-grounded types: {semantic.subject_type} / {semantic.object_type}. {semantic.rationale}</p>
    <p>Project scope: {semantic.in_scope === true ? 'In scope' : semantic.in_scope === false ? 'Outside scope — excluded from validated graph and pathway assembly' : 'Not assessed'}.</p>
    {semantic.role_evidence?.map((item, index) => <div key={index}><p><strong>{item.role}: {item.assessed_type}</strong> · {item.evidence_document_id} / {item.chunk_id}</p>
      <blockquote>{item.quoted_span}</blockquote><p>Contextual role phrase: “{item.role_trigger}”. {item.rationale}</p></div>)}
    {semantic.role_grounding_errors?.map((error, index) => <p key={index} className="inline-error">{error}</p>)}
    {joint.semantic_review_required && <p className="inline-error">Type, role or pathway scope requires review. Paper findings remain visible; this claim is excluded from validated assembly until resolved.</p>}
    <small>Each decisive finding must retain an exact quote from its own paper. Retrieval scores indicate relevance, not scientific certainty.</small>
  </div>
}

export function CitationTracking({ candidate, validation }) {
  const attempt = validation?.attempts?.at(-1)
  const rows = attempt?.citation_ledger || candidate.source_citation_links || []
  const coverage = attempt?.retrieval_coverage
  if (!rows.length && !coverage) return null
  return <details className="context-packet"><summary>Citation tracking and retrieval coverage · {rows.length} citation links</summary>
    {!!rows.length && <div className="diagnostic-table"><table><thead><tr><th>Citation / chapter</th><th>Status</th><th>Identified</th><th>Resolved</th><th>Text fetched</th><th>In judgement</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={row.reference_id || index}><td>{row.citation_key}<br /><small>{row.chapter_id}</small>
        <details><summary>Reference and availability</summary><p>{row.citation_text || row.reference?.citation_text || 'No linked bibliography entry.'}</p><p>{row.reason}</p>{row.evidence_access && <p>Evidence access: {row.evidence_access.replaceAll('_', ' ')}</p>}<p>{row.evidence_document_ids?.join(', ')}</p></details></td>
        <td>{row.status?.replaceAll('_', ' ')}</td>{['identified', 'resolved', 'fetched', 'included_in_judgement'].map(key => <td key={key}>{row[key] === true ? 'Yes' : row[key] === false ? 'No' : 'Not yet checked'}</td>)}</tr>)}</tbody></table></div>}
    {coverage && <p>{coverage.unique_metadata_records} unique metadata records; {coverage.accessible_candidates} candidates with an abstract or accessible full text; {coverage.fetch_attempts} fetch attempts; {coverage.papers_retained} retained papers. Search-paper budget: {coverage.independent_paper_budget}. {coverage.eligible_not_fetched} eligible records were not fetched. This search does not guarantee complete literature coverage.</p>}
    {!!attempt?.paper_contributions?.length && <div className="diagnostic-table"><table><thead><tr><th>Paper</th><th>Guarded decision</th><th>Admissible quotes</th><th>Additional quote texts</th></tr></thead>
      <tbody>{attempt.paper_contributions.map(row => <tr key={row.evidence_document_id}><td>{attempt.evidence_documents?.find(d => d.evidence_document_id === row.evidence_document_id)?.title || row.evidence_document_id}</td><td>{row.decision}</td><td>{row.admissible_quotes}</td><td>{row.new_quote_texts}</td></tr>)}</tbody></table></div>}
  </details>
}
