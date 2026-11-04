// Graph identity and display updates. These helpers never infer scientific support.
export function scopeAssemblyGraph(graph, documentKey = null) {
  const edges = (graph?.edges || []).filter(edge => !documentKey || (edge.document_key ? edge.document_key === documentKey : edge.paper === documentKey))
  const ids = new Set(edges.flatMap(edge => [edge.source, edge.target]))
  const nodes = (graph?.nodes || []).filter(node => ids.has(node.id))
  return { ...graph, nodes, edges, counts: { nodes: nodes.length, edges: edges.length }, provisional: true }
}

export function visibleNetwork(graph, filter = 'all') {
  const edges = (graph?.edges || []).filter(edge => filter === 'all' || edge.status === filter)
  const ids = new Set(edges.flatMap(edge => [edge.source, edge.target]))
  const byId = (a, b) => String(a.id).localeCompare(String(b.id))
  return { nodes: (graph?.nodes || []).filter(node => ids.has(node.id)).sort(byId), edges: [...edges].sort(byId) }
}

export function rememberPositions(cy, positions) {
  cy.nodes().forEach(node => positions.set(node.id(), { ...node.position() }))
}

// Selection is a visual focus only; it never changes saved edge verdicts or layout.
export function highlightNetwork(cy, edgeIds = [], showRelations = true) {
  const ids = new Set(edgeIds)
  cy.batch(() => {
    cy.elements().removeClass('path-active path-dimmed')
    const highlighted = cy.edges().filter(edge => ids.has(edge.id()))
    if (highlighted.nonempty()) {
      cy.elements().addClass('path-dimmed')
      highlighted.union(highlighted.connectedNodes()).removeClass('path-dimmed').addClass('path-active')
    }
    cy.edges().forEach(edge => edge.style('label', showRelations || ids.has(edge.id()) ? edge.data('relation') || '' : ''))
  })
}

function updateData(element, row) {
  const protectedFields = new Set(['id', 'source', 'target', 'parent'])
  const obsolete = Object.keys(element.data()).filter(key => !(key in row) && !protectedFields.has(key))
  if (obsolete.length) element.removeData(obsolete.join(' '))
  element.data({ ...row })
}

export function syncNetwork(cy, network, positions) {
  const firstLayout = positions.size === 0 && cy.nodes().empty()
  rememberPositions(cy, positions)
  const nodeIds = new Set(network.nodes.map(node => node.id))
  const edges = new Map(network.edges.map(edge => [edge.id, edge]))
  const neighbours = new Map(network.nodes.map(node => [node.id, []]))
  for (const edge of network.edges) {
    neighbours.get(edge.source)?.push(edge.target)
    neighbours.get(edge.target)?.push(edge.source)
  }
  const columns = Math.max(1, Math.ceil(Math.sqrt(network.nodes.length)))
  let added = 0
  cy.batch(() => {
    cy.edges().forEach(edge => {
      const row = edges.get(edge.id())
      if (!row || row.source !== edge.source().id() || row.target !== edge.target().id()) edge.remove()
    })
    cy.nodes().filter(node => !nodeIds.has(node.id())).remove()
    network.nodes.forEach((row, index) => {
      const current = cy.getElementById(row.id)
      if (current.nonempty()) { updateData(current, row); return }
      let position = positions.get(row.id)
      if (!position) {
        const anchors = firstLayout ? [] : (neighbours.get(row.id) || []).map(id => positions.get(id)).filter(Boolean)
        const angle = added * 2.3999632297
        position = anchors.length ? {
          x: anchors.reduce((total, value) => total + value.x, 0) / anchors.length + 90 * Math.cos(angle),
          y: anchors.reduce((total, value) => total + value.y, 0) / anchors.length + 90 * Math.sin(angle),
        } : { x: (index % columns) * 160, y: Math.floor(index / columns) * 130 }
      }
      positions.set(row.id, { ...position })
      cy.add({ group: 'nodes', data: { ...row }, position })
      added += 1
    })
    network.edges.forEach(row => {
      const current = cy.getElementById(row.id)
      if (current.nonempty()) updateData(current, row)
      else cy.add({ group: 'edges', data: { ...row } })
    })
  })
  // Deliberately no fit/layout call here. Polls and filters retain manual placement,
  // selection, pan and zoom. Only initial rendering or an explicit action arranges.
  return { firstLayout, addedNodes: added }
}
