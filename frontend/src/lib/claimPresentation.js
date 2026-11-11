export const statusLabels = {
  not_started: 'Ready to run', running: 'In progress', completed: 'Completed',
  completed_with_warnings: 'Completed with warnings', failed: 'Run failed', stopping: 'Stopping', stopped: 'Stopped',
  external_in_progress: 'External check in progress',
  retrieval_pending: 'Retrieval incomplete - resume missing sources',
  external_support_needs_review: 'Support found - review needed',
  external_assessment_needs_review: 'Evidence assessment needs review',
  external_needs_review: 'Review external evidence', out_of_scope: 'Outside project scope',
  scope_needs_review: 'Review project scope', scope_not_assessed: 'Scope not assessed',
  blocked_text_quality: 'Blocked: text quality',
  validation_paused: 'Unvalidated ? validation paused',
  extracted: 'Extracted', single_source: 'Independent article', corroborated: 'Independently corroborated',
  source_reference_supported: 'Source reference support',
  contested: 'Contested', contradicted: 'Contradicted', insufficient: 'Evidence not found', not_found: 'Evidence not found', error: 'Needs attention',
  rejected: 'Rejected', pipeline_error: 'Pipeline error', schema_rejected: 'Vocabulary check failed',
  world_supported_pending_corroboration: 'One article',
  world_corroborated_pending_review: 'Corroborated', world_contested_pending_review: 'Contested',
  world_independent_supported_pending_corroboration: 'Independent article',
  world_independently_corroborated_pending_review: 'Independently corroborated',
  source_reference_supported_pending_independent_corroboration: 'Source reference support',
  world_insufficient: 'Evidence not found', world_insufficient_after_retry: 'Evidence not found', external_evidence_not_found: 'Evidence not found', retrieval_failed_or_no_open_access_evidence: 'No evidence found',
  world_contradicted_or_contested: 'Contradicted',
  semantic_accepted: 'Semantically accepted', semantic_rejected: 'Semantic rejected', semantic_needs_review: 'Review semantics',
  source_entailed: 'Source supported', source_supported: 'Source supported', source_unsupported: 'Source unsupported', source_needs_review: 'Review source',
  local_connection_supported: 'Locally supported connection member', local_connection_membership: 'Local connection member',
  pathway_supported: 'Supported pathway member', pathway_membership_only: 'Supported pathway member',
}

export const colorClass = value => ({
  completed: 'positive', corroborated: 'positive', single_source: 'warning', source_reference_supported: 'warning',
  retrieval_pending: 'warning',
  external_support_needs_review: 'warning', external_assessment_needs_review: 'warning', external_needs_review: 'warning',
  completed_with_warnings: 'warning', failed: 'negative', stopping: 'running', stopped: 'muted',
  world_supported_pending_corroboration: 'warning', running: 'running',
  world_corroborated_pending_review: 'positive', world_contested_pending_review: 'negative',
  world_independent_supported_pending_corroboration: 'warning', world_independently_corroborated_pending_review: 'positive',
  source_reference_supported_pending_independent_corroboration: 'warning',
  contested: 'negative', contradicted: 'negative', world_contradicted_or_contested: 'negative',
  error: 'negative', pipeline_error: 'negative', schema_rejected: 'muted', rejected: 'muted',
  blocked_text_quality: 'negative',
  insufficient: 'muted', not_found: 'muted', world_insufficient: 'muted', world_insufficient_after_retry: 'muted', external_evidence_not_found: 'muted',
  semantic_accepted: 'warning', semantic_rejected: 'negative', semantic_needs_review: 'muted',
  source_entailed: 'warning', source_supported: 'warning', source_unsupported: 'negative', source_needs_review: 'muted',
  local_connection_supported: 'positive', local_connection_membership: 'positive',
  pathway_supported: 'positive', pathway_membership_only: 'positive',
}[value] || 'neutral')

export const formatSize = size => `${(size / 1024 / 1024).toFixed(1)} MB`
export const formatDuration = value => value == null ? 'Not recorded' : value >= 60 ? `${(value / 60).toFixed(1)} min` : `${Math.round(value)} sec`

export function claimStatus(record) {
  const { candidate, validation } = record
  if (record.validation_stages_paused && !candidate.schema_error) return 'validation_paused'
  const semanticDecision = candidate.semantic_validation?.decision
  const sourceDecision = record.source_review?.fidelity_decision || candidate.source_validation?.decision
  const scopeDecision = record.source_review?.scope_decision || candidate.source_validation?.project_scope?.decision
  return candidate.schema_error ? 'schema_rejected' : candidate.scope_error ? 'rejected'
    : semanticDecision === 'rejected' ? 'semantic_rejected' : semanticDecision === 'needs_review' ? 'semantic_needs_review'
    : sourceDecision === 'unsupported' ? 'source_unsupported' : sourceDecision === 'needs_review' ? 'source_needs_review'
    : scopeDecision === 'out_of_scope' ? 'out_of_scope' : scopeDecision === 'needs_review' ? 'scope_needs_review'
    : ['whole_pathway', 'neighboring_claims'].includes(record.external_validation_target) && sourceDecision === 'entailed' && scopeDecision !== 'in_scope' ? 'scope_not_assessed'
    : validation?.validation_status || (sourceDecision === 'entailed' ? 'source_entailed' : semanticDecision === 'accepted' ? 'semantic_accepted' : 'extracted')
}

const supportedStatuses = new Set(['single_source', 'corroborated', 'world_supported_pending_corroboration',
  'world_corroborated_pending_review', 'world_independent_supported_pending_corroboration',
  'world_independently_corroborated_pending_review'])
const rejectedStatuses = new Set(['schema_rejected', 'rejected', 'semantic_rejected', 'source_unsupported',
  'contradicted', 'world_contradicted_or_contested', 'out_of_scope'])
export const claimGroup = record => {
  const status = claimStatus(record)
  if (status === 'validation_paused') return 'unvalidated'
  if (['whole_pathway', 'neighboring_claims'].includes(record.external_validation_target) && status === 'source_entailed') return 'unvalidated'
  if (supportedStatuses.has(status)) return 'supported'
  if (rejectedStatuses.has(status)) return 'rejected'
  return 'review'
}

export function matchesClaim(record, query) {
  const needle = query.trim().toLocaleLowerCase()
  if (!needle) return true
  const candidate = record.candidate
  const paperTitles = record.validation?.attempts?.flatMap(attempt => attempt.evidence_documents?.map(paper => paper.title) || []) || []
  return [candidate.subject, candidate.object, candidate.relation?.replaceAll('_', ' '), candidate.claim_text,
    candidate.source_evidence_span, candidate.candidate_id, ...Object.values(candidate.qualifiers || {}), ...paperTitles].filter(Boolean).join(' ').toLocaleLowerCase().includes(needle)
}

