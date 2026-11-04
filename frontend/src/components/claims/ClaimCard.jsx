import { memo, useState } from 'react'
import { ArrowRight, ChevronDown, CircleAlert, ExternalLink } from 'lucide-react'
import { claimStatus } from '../../lib/claimPresentation.js'
import StatusPill from './StatusPill.jsx'
import TrustEvidence from './TrustEvidence.jsx'
import { CitationTracking, JointAssessment, ClaimActions, ClaimContext, ValidationTrace, ExternalDecisionDetails } from './ClaimDetails.jsx'

function ClaimCard({ record, index, busy, onClaimAction, onInspectPathway, defaultExpanded = false, readOnly = false }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const { candidate, validation } = record
  const decisions = validation?.attempts?.flatMap(attempt => attempt.decisions || []) || []
  const articles = validation?.attempts?.flatMap(attempt => attempt.evidence_documents || []) || []
  const semanticDecision = candidate.semantic_validation?.decision
  const sourceDecision = record.source_review?.fidelity_decision || candidate.source_validation?.decision
  const resultStatus = claimStatus(record)
  const scopeDecision = record.source_review?.scope_decision || candidate.source_validation?.project_scope?.decision
  const preview = candidate.schema_error || validation?.error
    || record.source_review?.active_reviews?.source_fidelity?.note
    || record.source_review?.active_reviews?.project_scope?.note
    || (['unsupported', 'needs_review'].includes(sourceDecision) ? candidate.source_validation.rationale : '')
    || (['out_of_scope', 'needs_review'].includes(scopeDecision) ? candidate.source_validation?.project_scope?.rationale : '')
    || candidate.claim_text || candidate.source_evidence_span || ''
  return <article className={`claim-card ${expanded ? 'expanded' : ''}`}>
    <button type="button" className="claim-main" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
      <span className="claim-number">{String(index + 1).padStart(2, '0')}</span>
      <span className="claim-content"><span className="claim-line"><strong>{candidate.subject}</strong><span className="relation-chip">{candidate.relation.replaceAll('_', ' ')}</span><strong>{candidate.object}</strong></span>
        <span className="claim-meta">Page {candidate.source_page ?? '—'} · {candidate.subject_type} → {candidate.object_type}</span>
        {preview && <span className="claim-preview">{preview}</span>}</span>
      <StatusPill value={resultStatus} /><ChevronDown size={17} className={`chevron ${expanded ? 'open' : ''}`} />
    </button>
    {expanded && <div className="claim-expanded">
      <ClaimActions readOnly={readOnly} candidate={candidate} sourceReview={record.source_review} pathwayOnly={record.external_validation_target === 'whole_pathway'} repairEligibility={record.repair_eligibility} revisions={record.revisions}
        busy={busy} onAction={(action, note) => onClaimAction?.(candidate.candidate_id, action, note, record.source_review?.revision)} />
      {record.external_validation_target !== 'whole_pathway' && <TrustEvidence trust={record.trust || validation?.trust} />}
      {record.external_validation_target === 'whole_pathway' && <div className="quote-box"><strong>External assessment target: complete pathways</strong><p>This claim has no independent external-validation verdict. Its source fidelity is assessed separately.</p>{record.pathway_participation?.length ? <><p>{record.pathway_participation.length} assessed pathway{record.pathway_participation.length === 1 ? '' : 's'}</p>{record.pathway_participation.map(row => <div key={row.pathway_id} className="claim-pathway-row"><span>{row.pathway_id}: {row.stale ? 'Stale; reassessment needed' : (row.verdict === 'pathway_supported' ? 'Pathway supported' : row.verdict.replaceAll('_',' '))}</span>{onInspectPathway && <button type="button" className="text-button" onClick={() => onInspectPathway(row.pathway_id)}>Inspect pathway <ArrowRight size={13} /></button>}</div>)}</> : <p>Not included in an assessed pathway.</p>}</div>}
      <ClaimContext candidate={candidate} sourceReview={record.source_review} />
      <ValidationTrace candidate={candidate} validation={validation} sourceReview={record.source_review} reviewHistory={record.source_review_history} externalTarget={record.external_validation_target} />
      <JointAssessment validation={validation} />
      <CitationTracking candidate={candidate} validation={validation} />
      {(candidate.source_evidence_span || candidate.source_evidence_quote) && <div className="quote-box"><span className="eyebrow">{candidate.source_context_packet ? `DIRECT EVIDENCE ${candidate.source_evidence_is_contiguous === false ? 'FRAGMENTS' : 'SPAN'} · SENTENCES ${(candidate.source_evidence_sentence_numbers || []).join(', ')}` : 'SOURCE CONTEXT'}</span>{candidate.source_evidence_is_contiguous === false ? <>
        <p>These source sentences are separated in the PDF. Their connection requires source validation.</p>
        {candidate.source_evidence_fragments?.map(fragment => <div key={fragment.sentence_number}><small>Sentence {fragment.sentence_number} · PDF page {fragment.page}</small><blockquote>{fragment.text}</blockquote></div>)}
        <details><summary>Intervening source text · retained for review</summary>{candidate.source_intervening_context?.map(fragment => <p key={fragment.sentence_number}>[{fragment.sentence_number}] Page {fragment.page}: {fragment.text}</p>)}</details>
      </> : <p>“{candidate.source_evidence_span || candidate.source_evidence_quote}”</p>}</div>}
      {candidate.source_context_packet && <details className="context-packet"><summary>Section context · {candidate.source_section_heading || 'Unlabelled paper text'} · pages {candidate.source_page_start}-{candidate.source_page_end}{candidate.source_context_estimated_tokens ? ` · ~${candidate.source_context_estimated_tokens} tokens` : ''}</summary><p>{candidate.source_context_packet}</p></details>}
      {candidate.schema_error && <p className="inline-error"><CircleAlert size={16} /> Vocabulary check: {candidate.schema_error}</p>}
      {candidate.scope_error && <p className="inline-error"><CircleAlert size={16} /> {candidate.scope_error}</p>}
      {candidate.semantic_validation?.type_disagreement && <p className="decision-note">Type disagreement recorded: extractor chose {candidate.semantic_validation.type_disagreement.extracted?.subject_type} → {candidate.semantic_validation.type_disagreement.extracted?.object_type}; classifier suggested {candidate.semantic_validation.type_disagreement.classified?.subject_type} → {candidate.semantic_validation.type_disagreement.classified?.object_type}. Source evidence still decides whether the claim is faithful.</p>}
      {candidate.semantic_validation?.normalization_applied && <p className="decision-note">Entity types normalized after semantic review; original types are retained in the candidate record.</p>}
      {candidate.source_context && <p className="soft-note"><strong>Source setting:</strong> {candidate.source_context} (provenance, not an external-study requirement)</p>}
      {!!candidate.qualifier_adjustments?.length && <details className="context-packet"><summary>Qualifier adjustments · {candidate.qualifier_adjustments.length}</summary><div>{candidate.qualifier_adjustments.map((adjustment, adjustmentIndex) => <p key={adjustmentIndex}>{adjustment.field}: {adjustment.reason || adjustment.action || 'Adjusted during extraction'}</p>)}</div></details>}
      {candidate.certainty && <p className="soft-note">Claim wording: {candidate.certainty}</p>}
      {!!candidate.source_references?.length && <details className="context-packet"><summary>Cited evidence candidates · {candidate.source_references.length} reference(s)</summary><div>{candidate.source_references.map(reference => <p key={reference.reference_number}><strong>[{reference.reference_number}]</strong> {reference.citation_text}</p>)}</div></details>}
      {validation?.error && <p className="inline-error"><CircleAlert size={16} /> {validation.error}</p>}
      {articles.length > 0 && <div className="evidence-list"><span className="eyebrow">EXTERNAL ARTICLES · {articles.length}</span>
        {articles.map((article, articleIndex) => {
          const decision = decisions.find(item => item.evidence_document_id === article.evidence_document_id)
          return <div className="evidence-item" key={`${article.evidence_document_id}-${articleIndex}`}>
            <div className="evidence-item-head"><span>{article.title?.replace(/<[^>]+>/g, '') || 'Untitled article'}</span><StatusPill value={decision?.decision || 'insufficient'}>{decision?.decision || 'pending'}</StatusPill></div>
            <span className="evidence-detail">{article.publication_year} · {article.doi ? <a href={`https://doi.org/${article.doi}`} target="_blank" rel="noreferrer">DOI {article.doi} <ExternalLink size={12} /></a> : 'No DOI'} · {article.evidence_level === 'source_reference' ? `source reference ${article.source_reference_number || ''}` : 'independent search'} · {article.evidence_access === 'abstract' ? 'abstract' : 'full text'}{article.open_access_license ? ` · ${article.open_access_license}` : ''}</span>
            {decision?.evidence?.[0]?.quoted_span && <blockquote>“{decision.evidence[0].quoted_span}”</blockquote>}
            {decision?.rationale && <p>{decision.rationale}</p>}
            <ExternalDecisionDetails decision={decision} />
            {article.passage_selection && <details className="context-packet"><summary>Retrieved passages / {article.passage_selection.retained_chunks}/{article.passage_selection.candidate_chunks}</summary>
              <p>Ranking claim: {article.passage_selection.query}</p><p>Neutral exposure/outcome query: {article.passage_selection.neutral_query}</p>
              {article.evidence_chunks?.map(chunk => <div key={chunk.chunk_id}><strong>{chunk.section_heading || chunk.chunk_id}</strong><p>{chunk.text}</p><small>Dense: {chunk.dense_score} / Cross-encoder: {chunk.cross_encoder_score} / Neutral: {chunk.neutral_cross_encoder_score}</small></div>)}
            </details>}
          </div>
        })}</div>}
      {record.external_validation_target !== 'whole_pathway' && !validation && !candidate.schema_error && !candidate.scope_error && semanticDecision !== 'rejected' && semanticDecision !== 'needs_review' && sourceDecision !== 'unsupported' && sourceDecision !== 'needs_review' && <p className="soft-note">External evaluation has not run for this claim yet.</p>}
    </div>}
  </article>
}

export default memo(ClaimCard)
