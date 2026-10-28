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

export function orderClaims(edges, sortBy) {
  return [...edges].sort((a, b) => sortBy === 'contribution'
    ? (b.trust?.contribution?.two_edge_connections || 0) - (a.trust?.contribution?.two_edge_connections || 0)
    : (b.trust?.score || 0) - (a.trust?.score || 0))
}
