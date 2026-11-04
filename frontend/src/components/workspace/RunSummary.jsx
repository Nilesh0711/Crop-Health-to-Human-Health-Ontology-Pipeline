import { ArrowRight, CircleAlert, Filter, Check, ShieldCheck } from 'lucide-react'
import { PATHWAY_OUTCOMES } from '../../lib/pathways.js'
import { formatDuration } from '../../lib/claimPresentation.js'

// Saved whole-pathway outcome counts for the selected paper. Counts are copied from
// the backend summary; execution states are shown apart from scientific outcomes.
export function PathwayOutcomeOverview({ summary, onOpen }) {
  if (summary?.knowledge_building_mode !== 'pathway_only') return null
  const outcomes = summary.pathway_validation?.outcomes || {}
  const coverage = summary.pathway_validation?.coverage || {}
  const keys = [...Object.keys(PATHWAY_OUTCOMES), ...Object.keys(outcomes).filter(key => !PATHWAY_OUTCOMES[key])]
    .filter(key => outcomes[key] > 0)
  const total = keys.reduce((sum, key) => sum + outcomes[key], 0)
  const label = key => PATHWAY_OUTCOMES[key]?.label || key.replaceAll('_', ' ')
  const tone = key => PATHWAY_OUTCOMES[key]?.tone || 'neutral'
  const outside = coverage.claims_outside_selected_pathways?.length
  return <section className="pathway-outcome-overview" aria-label="Whole-pathway outcomes for this paper">
    <div className="pathway-outcome-head"><span className="metric-icon"><ShieldCheck size={19} /></span>
      <div><span className="eyebrow">WHOLE-PATHWAY OUTCOMES</span>
        <strong>{total ? `${total} assessed ${total === 1 ? 'route involves' : 'routes involve'} this paper` : 'No pathway assessments saved yet'}</strong></div>
      {onOpen && <button type="button" className="text-button" onClick={onOpen}>Open pathway assessments <ArrowRight size={15} /></button>}</div>
    {total > 0 && <>
      <div className="pathway-outcome-bar" role="img" aria-label={keys.map(key => `${outcomes[key]} ${label(key)}`).join(', ')}>
        {keys.map(key => <span key={key} className={`tone-${tone(key)}`} style={{ flexGrow: outcomes[key] }} title={`${label(key)}: ${outcomes[key]}`} />)}</div>
      <ul className="pathway-outcome-legend">{keys.map(key => <li key={key} className={`tone-${tone(key)}`} title={PATHWAY_OUTCOMES[key]?.description}>
        <i /><span>{label(key)}</span><b>{outcomes[key]}</b></li>)}</ul></>}
    <p>{total ? 'Verdicts concern complete ordered routes. Unresolved evidence, queued work and technical failures are not scientific rejections.'
      : 'PDF-supported, in-scope claims are assembled into provisional chains before whole-route external assessment.'}
      {(coverage.has_more || coverage.depth_limited) ? ' Additional or deeper chains remain unassessed because of the recorded run limits.' : ''}
      {outside ? ` ${outside} source claims are outside the selected chains and remain externally unvalidated.` : ''}</p>
  </section>
}

export function RunBreakdown({ summary }) {
  if (!summary || summary.extracted == null) return null
  const stages = [
    ['Extracted', summary.extracted],
    ['Vocabulary', summary.schema_valid],
    ['Source evidence', summary.source_entailed],
    ...(summary.pathway_eligible_claims == null ? [] : [['In-scope source claims', summary.pathway_eligible_claims]]),
    [summary.external_validation_target === 'neighboring_claims' ? 'Supported local connections' : summary.knowledge_building_mode === 'pathway_only' ? 'Supported pathways' : 'External support', summary.knowledge_building_mode === 'pathway_only' ? summary.pathway_validation?.outcomes?.connection_supported ?? summary.pathway_validation?.outcomes?.pathway_supported ?? 0 : summary.graph?.supported],
  ]
  const max = Math.max(1, summary.extracted)
  const calls = summary.llm_calls
  const requestSeconds = Object.keys(summary.task_models || { extraction: null, validation: null, helper: null })
    .reduce((seconds, task) => seconds + (calls?.[task]?.elapsed_seconds ?? 0), 0)
  const coverageUnit = summary.extraction_coverage?.unit || 'packets'
  return <section className="run-breakdown" aria-label="Latest run breakdown">
    {summary.extraction_coverage?.complete === false && <div className="extraction-coverage-warning" role="alert"><strong>Partial extraction: {summary.extraction_coverage.completed_packets}/{summary.extraction_coverage.total_packets} {coverageUnit} completed</strong><p>Claims in missing regions were not extracted or evaluated. The counts below do not describe the whole paper.</p><details><summary>Inspect incomplete regions</summary>{summary.extraction_coverage.failed_packets?.map(item => <p key={item.target_id || item.packet_number}>{item.target_id ? `Target ${item.target_number}` : `Packet ${item.packet_number}`}{item.page_start != null ? ` · pages ${item.page_start}–${item.page_end}` : ''} · {item.status}: {item.error || 'Extraction did not complete'}</p>)}</details></div>}
    <div className="run-breakdown-heading"><div><span className="eyebrow">LATEST RUN</span><h2>Where claims stand</h2></div><span className="run-version">{summary.schema_version || 'Legacy run'}</span></div>
    <div className="run-stage-grid">{stages.map(([label, count], index) => <div className="run-stage" key={label}>
      <span className="run-stage-label">{label}</span><strong>{count ?? '—'}</strong>
      <span className="run-stage-track"><span style={{ width: `${Math.max(0, Math.min(100, ((count || 0) / max) * 100))}%` }} /></span>
      {index < stages.length - 1 && <ArrowRight size={14} className="run-stage-arrow" />}
    </div>)}</div>
    {summary.source_project_scope && <p className="soft-note">Project scope: {summary.source_project_scope.in_scope ?? 0} in scope · {summary.source_project_scope.out_of_scope ?? 0} outside scope · {summary.source_project_scope.needs_review ?? 0} need review · {summary.source_project_scope.not_assessed ?? 0} not assessed. Scope exclusions do not mean the source assertion is false.</p>}
    <div className="run-breakdown-footer"><span>{summary.knowledge_building_mode === 'pathway_only' ? 'Whole-pathway external assessment' : summary.semantic_stage === 'removed' ? 'Joint evidence assessment' : (summary.semantic_needs_review ?? 0) + ' semantic review'} · {summary.source_needs_review ?? 0} source review · {summary.audit_graph?.edges ?? 0} audit edges</span>
      {calls && <span title="Sum of individual model-request durations, not wall-clock runtime">OpenCode calls: {calls.total_requests ?? '—'} · extraction {calls.extraction?.requests ?? '—'}{calls.source?.requests ? ` / source ${calls.source.requests}` : ''}{calls.pathway_assembly?.requests ? ` / assembly ${calls.pathway_assembly.requests}` : ''} / validation {calls.validation?.requests ?? '—'}{calls.helper?.requests ? ` / helper ${calls.helper.requests}` : ''} · request time {formatDuration(requestSeconds)}</span>}
    </div>
    {summary.extraction_mode && <p className="run-breakdown-note">Extraction: {summary.extraction_mode === 'target_context' ? 'Target / context' : 'Whole packet'}
      {' · '}{summary.claims_per_packet ? `${summary.claims_per_packet} claims maximum per call` : 'All eligible claims per call'}
      {summary.extraction_generation_budget ? ` · ${summary.extraction_generation_budget.toLocaleString()} generation tokens per initial call` : ''}.
      Completed regions measure processing coverage, not exhaustive claim recall.</p>}
    <p className="run-breakdown-note">External evidence not found means the search did not establish support; it does not prove the claim false. {summary.knowledge_building_mode === 'pathway_only' ? 'The network contains members of supported pathways; its union does not establish additional routes or independent edge verdicts.' : 'Only independently supported claims enter the external graph.'}</p>
    {summary.stage_timings_seconds && <details className="context-packet"><summary>Runtime breakdown · {formatDuration(summary.wall_elapsed_seconds)} total</summary><div>{Object.entries(summary.stage_timings_seconds).map(([stage, seconds]) => <p key={stage}>{stage.replaceAll('_', ' ')}: {formatDuration(seconds)}</p>)}<p>{summary.canonical_duplicates_removed ?? 0} duplicate claims merged · {summary.retrieval_cache?.hits ?? 0} retrieval cache hits{summary.validation_batch_size != null ? ` · batches of ${summary.validation_batch_size}` : ''}</p></div></details>}
  </section>
}

const documentScopeCopy = {
  not_screened: { label: 'Text quality checked', detail: 'Parsed text passed the readability check. Original passages remain available alongside each claim.' },
  in_scope: {
    label: 'Pathway-relevant',
    detail: 'This paper can contribute evidence to the soil-to-human-health pathway.',
  },
  upstream_only: {
    label: 'Upstream evidence',
    detail: 'Retained for agriculture, soil, crop, or disease bridge evidence; it is not treated as a health-outcome paper.',
  },
  out_of_scope: {
    label: 'Outside project scope',
    detail: 'Extraction was skipped because no usable biological or chemical pathway evidence was found.',
  },
  uncertain: {
    label: 'Scope uncertain',
    detail: 'The paper was retained because the scope check could not decide safely.',
  },
}

const textQualityCopy = {
  usable: 'Text usable',
  degraded: 'Text quality degraded',
  unusable: 'Text unusable',
}

export function DocumentScopeNotice({ documentScope }) {
  if (!documentScope) return null
  const scope = documentScope.scope || 'uncertain'
  const quality = documentScope.text_quality || 'usable'
  const copy = documentScopeCopy[scope] || documentScopeCopy.uncertain
  const qualityLabel = textQualityCopy[quality] || 'Text quality unknown'
  const isWarning = scope === 'out_of_scope' || quality === 'unusable'
  const Icon = isWarning || quality === 'degraded' ? CircleAlert : scope === 'uncertain' ? Filter : Check
  return <section className={`document-scope-notice scope-${scope} quality-${quality}`} aria-label="Document scope and text quality">
    <span className="document-scope-icon"><Icon size={18} /></span>
    <div className="document-scope-copy">
      <div className="document-scope-labels"><strong>{copy.label}</strong><span className="text-quality-badge">{qualityLabel}</span></div>
      <p>{scope === 'not_screened' ? quality === 'usable' ? copy.detail : quality === 'unusable' ? 'Too little readable text was extracted for claim validation.' : 'Parsed text contains quality warnings. Review the original PDF alongside each passage.' : documentScope.rationale || copy.detail}</p>
      {quality === 'degraded' && <small>The extracted PDF text contains broken words or characters; review source evidence carefully.</small>}
      {quality === 'unusable' && <small>A cleaner PDF or OCR is required before faithful extraction.</small>}
    </div>
  </section>
}

