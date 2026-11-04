import { assessmentState, matchesPath, pathwayOutcome } from './pathways.js'

export const routeKey = edges => (edges || []).map(edge => edge.id).join('|')
export const edgeKey = edge => edge.document_key && edge.candidate_id ? `${edge.document_key}:${edge.candidate_id}` : edge.id

export function splitLocalAssessments(data, assembly) {
  const records = data?.records || []
  if (data?.policy !== 'neighbor-pair-v1') return { current: records, historical: [] }
  const edges = new Map((assembly?.graph?.edges || []).map(edge => [edge.id, edge]))
  const graphKnown = Boolean(assembly?.graph && !assembly.stale)
  const current = [], historical = []
  for (const record of records) {
    const contract = record.assessment_contract || record.target?.assessment_contract
    const retiredContract = Boolean(data.assessment_contract && contract !== data.assessment_contract)
    const retiredConnection = graphKnown && (record.target?.edges || []).some(edge => {
      const actual = edges.get(edge.id)
      return !actual || actual.source !== edge.source || actual.target !== edge.target
    })
    // Keep stale results for still-current pairs visible for reassessment. Retired
    // contracts/topologies belong in history, never in the latest result counts.
    const destination = record.stale && (retiredContract || retiredConnection) ? historical : current
    destination.push(record)
  }
  return { current, historical }
}

export function routeGroup(row) {
  const state = routeState(row)
  if (['pathway_supported','connection_supported'].includes(state) && row.record?.eligible === true) return 'supported'
  if (state === 'components_supported') return 'partial'
  if (['contradicted', 'incompatible', 'out_of_scope'].includes(state)) return 'excluded'
  return 'review'
}

export function routeState(row) {
  if (row.record) return pathwayOutcome(row.record)
  if (row.pairs?.length) {
    if (row.pairs.some(p => p.stale)) return 'stale'
    if (row.pairs.some(p => ['contradicted', 'incompatible', 'out_of_scope'].includes(p.verdict))) return 'contradicted'
    if (row.pairs.some(p => p.verdict === 'assessment_incomplete')) return 'assessment_incomplete'
    if (row.pairs.every(p => p.verdict === 'connection_supported' && p.eligible)) return 'connection_supported'
    if (row.pairs.some(p => ['connection_supported', 'components_supported'].includes(p.verdict))) return 'components_supported'
  }
  return 'not_assessed'
}

const shortStates = {
  connection_supported: 'Locally supported',
  pathway_supported: 'Fully supported', components_supported: 'Parts supported',
  stale: 'Needs refresh', assessment_incomplete: 'Incomplete', pipeline_error: 'Technical issue',
  unresolved: 'Insufficient evidence', not_assessed: 'Not assessed', queued: 'Queued',
  in_progress: 'Assessing', contradicted: 'Opposing evidence', incompatible: 'Incompatible', out_of_scope: 'Out of scope',
}
export function routeLabel(row) {
  const state = routeState(row)
  if (!row.record?.stale && row.record?.proof_audit?.passed === false) return 'Proof audit needs review'
  if (state === 'assessment_incomplete') {
    if (row.record && assessmentState(row.record).scientific === 'connection_supported') return 'Support found; work pending'
    return 'Incomplete'
  }
  if (['pathway_supported','connection_supported'].includes(state)) {
    if (row.record && row.record.eligible !== true) return 'Support not currently eligible'
    return shortStates[state] || state
  }
  return shortStates[state] || state
}

export function combineRoutes(paths = [], records = []) {
  const rows = new Map()
  paths.forEach(edges => {
    if (edges.length >= 2) rows.set(routeKey(edges), { key: routeKey(edges), edges, record: null, candidate: true })
  })
  records.forEach(record => {
    const edges = record.target?.edges || []
    if (edges.length < 2) return
    const key = routeKey(edges), current = rows.get(key)
    rows.set(key, { ...current, key, edges: current?.edges || edges, record, candidate: Boolean(current) })
  })
  const result = [...rows.values()]
  for (const row of result) {
    if (row.edges.length <= 2) continue
    const pairs = row.edges.slice(1).map((edge,i) => rows.get(routeKey([row.edges[i],edge]))?.record)
    const validPairs = pairs.filter(Boolean)
    const paperIds = new Set(validPairs.flatMap(p => (p.paper_assessments || []).map(pa => pa.evidence_document_id || pa.title)))
    row.pairs = validPairs
    row.paperCount = paperIds.size
    row.localCoverage = { supported:pairs.filter(r => r?.verdict === 'connection_supported' && r.eligible && !r.stale).length, total:pairs.length }
  }
  return result
}

export function filterRoutes(rows, { paperKey = null, group = 'all', outcome = 'all', query = '', sort = 'outcome' } = {}) {
  const result = rows.filter(row => (!paperKey || row.edges.some(edge => edge.document_key === paperKey || (!edge.document_key && edge.paper === paperKey)))
    && (group === 'all' || routeGroup(row) === group) && (outcome === 'all' || routeState(row) === outcome)
    && (!query.trim() || matchesPath(row.edges, query) || `${row.record?.pathway_id || ''} ${row.record?.assessment?.synthesis || ''}`.toLowerCase().includes(query.toLowerCase())))
  const order = { supported: 0, partial: 1, review: 2, excluded: 3 }
  return [...result].sort((a, b) => {
    if (sort === 'edges') return b.edges.length - a.edges.length || a.key.localeCompare(b.key)
    if (sort === 'papers') return (b.record?.paper_assessments?.length || 0) - (a.record?.paper_assessments?.length || 0) || a.key.localeCompare(b.key)
    if (sort === 'recent') return String(b.record?.completed_at || '').localeCompare(String(a.record?.completed_at || '')) || a.key.localeCompare(b.key)
    return order[routeGroup(a)] - order[routeGroup(b)] || a.key.localeCompare(b.key)
  })
}

export function graphForRoutes(graph, routes) {
  const wanted = new Set(routes.flatMap(row => row.edges.map(edgeKey)))
  const edges = (graph?.edges || []).filter(edge => wanted.has(edgeKey(edge)))
  const ids = new Set(edges.flatMap(edge => [edge.source, edge.target]))
  const nodes = (graph?.nodes || []).filter(node => ids.has(node.id))
  return { ...graph, edges, nodes, counts: { edges: edges.length, nodes: nodes.length } }
}

export function routeHighlights(graph, row) {
  // A stale result is never projected as if it assessed the current network.
  if (!row || row.record?.stale) return []
  const wanted = new Set(row.edges.map(edgeKey))
  return (graph?.edges || []).filter(edge => wanted.has(edgeKey(edge))).map(edge => edge.id)
}

export function includeRouteContext(scoped, complete, row) {
  if (!row || row.record?.stale) return scoped
  const context = graphForRoutes(complete, [row])
  const edges = [...new Map([...scoped.edges, ...context.edges].map(edge => [edge.id, edge])).values()]
  const ids = new Set(edges.flatMap(edge => [edge.source, edge.target]))
  const nodes = (complete?.nodes || []).filter(node => ids.has(node.id))
  return { ...scoped, edges, nodes, counts: { nodes: nodes.length, edges: edges.length } }
}
