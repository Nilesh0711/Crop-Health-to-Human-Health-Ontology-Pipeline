// Display/search helpers use saved assessments; they never decide scientific support.
export const readable = value => String(value ?? '').replaceAll('_', ' ')
export const entityLabel = (edge, side) => {
  const raw = String(edge[side] || '')
  return edge[`${side}_label`] || edge[side === 'source' ? 'subject' : 'object']
    || (raw.includes(':') ? raw.slice(raw.indexOf(':') + 1) : raw) || 'Unnamed entity'
}

export function evidenceState(edge) {
  if (edge.trust?.assessment_complete === false) return 'unfinished'
  return edge.trust?.status || edge.external_status || edge.status || 'not_assessed'
}

export function matchesEdge(edge, query = '', state = 'all') {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  const text = [entityLabel(edge, 'source'), entityLabel(edge, 'target'), edge.subject, edge.object,
    readable(edge.relation), edge.claim_text, edge.paper, edge.candidate_id,
    ...Object.values(edge.qualifiers || {})].join(' ').toLocaleLowerCase()
  return (state === 'all' || evidenceState(edge) === state) && words.every(word => text.includes(word))
}

export function matchesPath(path, query = '', state = 'all') {
  // State means at least one edge matches, never a judgement about the whole chain.
  return (state === 'all' || path.some(edge => evidenceState(edge) === state))
    && query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
      .every(word => path.some(edge => matchesEdge(edge, word)))
}

// Whole-pathway outcome vocabulary (docs/protocols/whole_pathway_validation.md).
// `group` separates scientific outcomes from execution states, which are never
// negative scientific verdicts. Tone is presentation only.
export const PATHWAY_OUTCOMES = {
  connection_supported: { label: 'Locally supported connection', tone: 'support', group: 'scientific',
    description: 'Both qualified claims have applicable external support and their local connection is compatible. Whole-chain correctness is not assessed.' },
  pathway_supported: { label: 'Pathway supported', tone: 'support', group: 'scientific',
    description: 'Explicit route evidence, supported components, compatible connections and affirmed project scope.' },
  components_supported: { label: 'Components supported; assessment partial', tone: 'partial', group: 'scientific',
    description: 'Some claims have full or partial support. Inspect each claim and the local compatibility finding.' },
  unresolved: { label: 'Insufficient pathway evidence', tone: 'neutral', group: 'scientific',
    description: 'Insufficient usable evidence. This is not a scientific rejection.' },
  contradicted: { label: 'Opposing evidence', tone: 'conflict', group: 'scientific',
    description: 'Paper-bound proof identifies opposing findings for the route or a component.' },
  incompatible: { label: 'Incompatible connection', tone: 'conflict', group: 'scientific',
    description: 'Paper-bound proof identifies incompatible conditions at a connection.' },
  out_of_scope: { label: 'Outside project scope', tone: 'neutral', group: 'scientific',
    description: 'The evidence assessment places the route outside the project domain.' },
  assessment_incomplete: { label: 'Assessment incomplete', tone: 'execution', group: 'execution',
    description: 'Some assessment work remains pending. A provisional scientific result may be available separately.' },
  pipeline_error: { label: 'Technical failure', tone: 'error', group: 'execution',
    description: 'A technical failure prevented assessment. Not a scientific outcome.' },
  queued: { label: 'Awaiting assessment', tone: 'execution', group: 'execution',
    description: 'Selected for assessment; no verdict yet.' },
  in_progress: { label: 'Assessment in progress', tone: 'execution', group: 'execution',
    description: 'Retrieval or judgement is still running.' },
  stale: { label: 'Inputs changed; reassessment required', tone: 'stale', group: 'execution',
    description: 'Source claims, scope, alignment or the assessment contract changed; refresh the assessment.' },
}
export const pathwayOutcome = record => record?.stale ? 'stale' : record?.verdict
export const outcomeLabel = key => PATHWAY_OUTCOMES[key]?.label || readable(key)
export const outcomeTone = key => PATHWAY_OUTCOMES[key]?.tone || 'neutral'

export function assessmentState(record) {
  if (!record) return { scientific: null, execution: 'not_assessed' }
  const scientific = record.scientific_verdict || record.assessment?.verdict
    || (PATHWAY_OUTCOMES[record.verdict]?.group === 'scientific' ? record.verdict : null)
  const execution = record.stale ? 'needs_refresh' : record.execution_status
    || (record.verdict === 'pipeline_error' ? 'failed'
      : ['queued','in_progress','assessment_incomplete'].includes(record.verdict) ? 'pending' : 'complete')
  return { scientific, execution }
}

export function localSupportBreakdown(record) {
  if (record.proof_audit?.passed === false) return { key:'audit_hold', label:'Proposed support requires external quotation review; no support admitted' }
  // Summarize model-declared findings only; never grant a verdict or graph admission.
  const expected = record.target?.edges || []
  const links = record.assessment?.edge_assessments || []
  const connections = record.assessment?.connection_assessments || []
  const supported = links.filter(row => ['supported', 'partially_supported'].includes(row.stance)).length
  const complete = expected.length === 2 && links.length === 2
    && new Set(links.map(row => row.edge_id)).size === 2
    && expected.every(edge => links.some(row => row.edge_id === edge.id))
    && connections.length === 1 && record.target?.connections?.length === 1
    && connections[0].connection_id === record.target.connections[0].connection_id
  if (!complete) return { key: 'not_assessed', label: 'Local breakdown not established' }
  if (connections[0].connection_kind === 'exclusion') return {
    key: 'exclusion', label: 'Exclusion contrast; no positive pathway propagation' }
  if (links.some(row => row.stance === 'contradicted')) return {
    key: 'opposed', label: 'Opposing evidence for a component' }
  if (supported === 2 && connections[0].compatibility === 'compatible') return {
    key: links.some(row => row.stance === 'partially_supported') ? 'qualified_components' : 'both_supported',
    label: links.some(row => row.stance === 'partially_supported')
      ? 'Both relationships have support; junction compatible; original details unresolved'
      : 'Both claims supported; junction compatible' }
  if (supported === 2) return { key: 'junction_unresolved', label: connections[0].compatibility === 'incompatible'
    ? 'Both relationships have support; junction incompatible' : 'Both relationships have support; junction unresolved' }
  return { key: 'component_only', label: `${supported} of 2 relationships have support; remaining claim evidence unresolved` }
}

export function findingPresentation(item) {
  const contextual = item.conditions_match !== true || item.claim_match === 'context_only'
  if (['supports','contradicts'].includes(item.stance) && contextual) return {
    label: item.stance === 'supports' ? 'Contextual support' : 'Contextual opposing finding', tone: 'neutral' }
  if (item.claim_match === 'partial') return {
    label: item.stance === 'supports' ? 'Partial wording support' : 'Related opposing finding', tone: 'partial' }
  return { label: readable(item.stance), tone: STANCE_TONE[item.stance] || 'neutral' }
}

// Per-link and per-connection findings returned by the synthesis contract.
export const STANCE_TONE = { supported: 'support', supports: 'support', contradicted: 'conflict', contradicts: 'conflict',
  partially_supported: 'partial', compatible: 'support', incompatible: 'conflict' }
export const EVIDENCE_BASIS_TONE = { local_connection_support: 'support', demonstrated_route: 'support', multi_paper_mechanistic_support: 'support',
  component_evidence_only: 'partial', conflicting: 'conflict' }

export function countOutcomes(records = []) {
  return records.reduce((counts, record) => {
    const key = pathwayOutcome(record)
    counts[key] = (counts[key] || 0) + 1
    return counts
  }, {})
}

export function orderClaims(edges, sortBy) {
  return [...edges].sort((a, b) => sortBy === 'contribution'
    ? (b.trust?.contribution?.two_edge_connections || 0) - (a.trust?.contribution?.two_edge_connections || 0)
    : (b.trust?.score || 0) - (a.trust?.score || 0))
}
