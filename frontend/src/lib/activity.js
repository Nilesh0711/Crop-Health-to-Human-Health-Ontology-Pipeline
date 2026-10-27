export const STAGE_LABELS = {
  system: 'System', pdf_parsing: 'PDF parsing', claim_extraction: 'Claim extraction',
  schema_validation: 'Schema checks', source_validation: 'Source validation', query_planning: 'Query planning',
  literature_search: 'Literature search', article_ranking: 'Article ranking', article_fetch: 'Paper fetching',
  chunk_preparation: 'Chunk preparation', external_validation: 'External judgement',
  pathway_assembly: 'Pathway assembly', graph_construction: 'Graph construction',
}
export const ACTIVITY_STAGES = Object.keys(STAGE_LABELS)
const aliases = { extraction: 'claim_extraction', source: 'source_validation', query: 'query_planning', external: 'external_validation' }
export function activityStage(entry) {
  const message = String(entry.message || '').toLowerCase()
  const legacy = [
    ['chunk_preparation', /ranked \d+ passages|passage preparation|chunk preparation/],
    ['literature_search', /europe pmc queries|literature search/],
    ['pathway_assembly', /pathway assembly|assembling path/],
    ['external_validation', /joint external|external validation|external model|claim status:|claim \d/],
    ['query_planning', /query planning|query translation|search planning|llm query/],
    ['source_validation', /source evidence|source judgement|source fidelity|source validation|source batch/],
    ['claim_extraction', /extracting |extraction |extraction:|llm extraction/],
    ['pdf_parsing', /pdf text|parsed pdf|document scope|text quality/],
    ['graph_construction', /building knowledge graph/],
  ].find(([, pattern]) => pattern.test(message))?.[0]
  const stage = entry.stage || entry.progress?.stage || legacy || 'system' 
  return aliases[stage] || stage
}
export const activityStageLabel = stage => STAGE_LABELS[stage] || stage.replaceAll('_', ' ')
export function filterActivity(logs, stage = 'all', severity = 'all') {
  return logs.filter(entry => (stage === 'all' || activityStage(entry) === stage)
    && (severity === 'all' || ['error', 'warning'].includes(entry.level)))
}
