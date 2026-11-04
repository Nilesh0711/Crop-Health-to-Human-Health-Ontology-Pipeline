import assert from 'node:assert/strict'
import test from 'node:test'
import cytoscape from 'cytoscape'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { highlightNetwork, rememberPositions, scopeAssemblyGraph, syncNetwork, visibleNetwork } from '../src/lib/graphNetwork.js'
import { combineRoutes, edgeKey, filterRoutes, graphForRoutes, includeRouteContext, routeGroup, routeHighlights, routeLabel, splitLocalAssessments } from '../src/lib/researchNetwork.js'
import { createLatestDetailsLoader, loadPaperDetails } from '../src/lib/workspaceData.js'
import { assessmentState, findingPresentation } from '../src/lib/pathways.js'

const graph = {
  nodes: ['A', 'B', 'C'].map(id => ({ id, label: `Entity ${id}`, type: 'Unresolved' })),
  edges: [{ id: 'ab', source: 'A', target: 'B', status: 'extracted', document_key: 'Paper 1', relation: 'affects', quote: 'Original source.' },
    { id: 'bc', source: 'B', target: 'C', status: 'source_supported', document_key: 'Paper 2', relation: 'increases' }],
}

test('scientific support with pending work remains outside the eligible supported network', () => {
  const record = { verdict: 'assessment_incomplete', scientific_verdict: 'connection_supported', execution_status: 'pending', eligible: false }
  assert.deepEqual(assessmentState(record), { scientific: 'connection_supported', execution: 'pending' })
  assert.equal(routeGroup({ record }), 'review')
  assert.equal(routeLabel({ record }), 'Support found; work pending')
  assert.equal(assessmentState({ ...record, stale: true }).execution, 'needs_refresh')
})

test('partial and contextual findings are displayed without claiming an exact contradiction', () => {
  assert.equal(findingPresentation({ stance: 'supports', claim_match: 'partial', conditions_match: true }).label, 'Partial wording support')
  assert.equal(findingPresentation({ stance: 'contradicts', claim_match: 'context_only', conditions_match: true }).label, 'Contextual opposing finding')
})

function engine(t) {
  const cy = cytoscape({ headless: true, elements: [], layout: { name: 'preset' } })
  t.after(() => cy.destroy())
  const positions = new Map()
  syncNetwork(cy, visibleNetwork(graph), positions)
  cy.getElementById('A').position({ x: 123, y: 456 })
  cy.getElementById('B').position({ x: 300, y: 400 })
  cy.zoom(1.8); cy.pan({ x: 81, y: -27 })
  rememberPositions(cy, positions)
  return { cy, positions }
}

test('equivalent refreshes preserve the graph instance, manual positions, selection and viewport', t => {
  const { cy, positions } = engine(t)
  const node = cy.getElementById('A')[0], edge = cy.getElementById('ab')[0]
  edge.select()
  let layouts = 0
  cy.on('layoutstart', () => { layouts += 1 })
  const before = { position: { ...node.position() }, zoom: cy.zoom(), pan: { ...cy.pan() } }
  for (let n = 0; n < 5; n += 1) {
    const refreshed = structuredClone(graph)
    refreshed.edges[0].status = 'source_supported'
    syncNetwork(cy, visibleNetwork(refreshed), positions)
  }
  assert.equal(cy.getElementById('A')[0], node)
  assert.equal(cy.getElementById('ab')[0], edge)
  assert.equal(edge.selected(), true)
  assert.deepEqual({ position: node.position(), zoom: cy.zoom(), pan: cy.pan() }, before)
  assert.equal(edge.data('status'), 'source_supported')
  assert.equal(layouts, 0)
  assert.equal(graph.edges[0].status, 'extracted')
})

test('filtering and restoring entities retains their saved placement without automatic fitting', t => {
  const { cy, positions } = engine(t)
  const old = { ...cy.getElementById('A').position() }
  syncNetwork(cy, visibleNetwork(graph, 'source_supported'), positions)
  assert.equal(cy.getElementById('A').empty(), true)
  syncNetwork(cy, visibleNetwork(graph), positions)
  assert.deepEqual(cy.getElementById('A').position(), old)
  assert.equal(cy.zoom(), 1.8)
  assert.deepEqual(cy.pan(), { x: 81, y: -27 })
})

test('new branches are added without moving existing nodes or changing the camera', t => {
  const { cy, positions } = engine(t)
  const before = { ...cy.getElementById('B').position() }
  const updated = structuredClone(graph)
  updated.nodes.push({ id: 'D', label: 'Entity D' })
  updated.edges.push({ id: 'bd', source: 'B', target: 'D', relation: 'reduces' })
  const result = syncNetwork(cy, visibleNetwork(updated), positions)
  assert.equal(result.addedNodes, 1)
  assert.deepEqual(cy.getElementById('B').position(), before)
  assert.ok(Number.isFinite(cy.getElementById('D').position().x))
  assert.equal(cy.getElementById('bd').source().id(), 'B')
  assert.equal(cy.zoom(), 1.8)
})

test('identity corrections rewire edges and remove obsolete evidence instead of showing stale metadata', t => {
  const { cy, positions } = engine(t)
  const updated = structuredClone(graph)
  updated.edges[0].target = 'C'
  delete updated.edges[0].quote
  updated.edges[1].quote = 'Updated finding.'
  syncNetwork(cy, visibleNetwork(updated), positions)
  assert.equal(cy.getElementById('ab').target().id(), 'C')
  assert.equal(cy.getElementById('ab').data('quote'), undefined)
  assert.equal(cy.getElementById('bc').data('quote'), 'Updated finding.')
  const removed = structuredClone(updated)
  delete removed.edges[1].quote
  syncNetwork(cy, visibleNetwork(removed), positions)
  assert.equal(cy.getElementById('bc').data('quote'), undefined)
  assert.equal(graph.edges[0].target, 'B')
  assert.equal(graph.edges[0].quote, 'Original source.')
})

test('paper-scoped assembly keeps aligned entity IDs and includes only that document edges', () => {
  const one = scopeAssemblyGraph(graph, 'Paper 1')
  assert.deepEqual(one.edges.map(row => row.id), ['ab'])
  assert.deepEqual(one.nodes.map(row => row.id), ['A', 'B'])
  assert.equal(one.provisional, true)
  assert.deepEqual(scopeAssemblyGraph(graph).counts, { nodes: 3, edges: 2 })
  assert.equal(scopeAssemblyGraph(graph, 'Unknown paper').edges.length, 0)
})

test('assembled view reuses the loaded provisional graph without fetching a supported-only graph', async () => {
  const calls = []
  const request = async path => {
    calls.push(path)
    if (path.endsWith('/claims')) return { claims: [] }
    if (path === '/pathways?view=provisional') return { graph, view: 'provisional' }
    return {}
  }
  const details = await loadPaperDetails(request, 'paper-id', 'all', 'assembled')
  assert.equal(details.trustData.graph, graph)
  assert.ok(!calls.includes('/graph') && !calls.includes('/pdfs/paper-id/graph'))
  assert.equal(calls.filter(path => path === '/pathways?view=provisional').length, 1)
})

const route = graph.edges.map((edge, n) => ({ ...edge, candidate_id: `claim-${n}` }))
const fullGraph = { ...graph, edges: route }
const supported = { pathway_id: 'route-1', verdict: 'pathway_supported', eligible: true,
  target: { edges: route }, paper_assessments: [{ evidence_document_id: 'external-1' }] }

test('retired local results remain in history while current stale pairs still request reassessment', () => {
  const current = { ...supported, target: { ...supported.target, assessment_contract: 'current' } }
  const old = { ...current, pathway_id: 'previous', stale: true, target: { ...current.target, assessment_contract: 'earlier' } }
  const changed = { ...current, pathway_id: 'changed-source', stale: true }
  const removed = { ...changed, pathway_id: 'retired-connection', target: { ...changed.target,
    edges: [{ ...route[0], target: 'old-node' }, route[1]] } }
  const records = [current, old, changed, removed]
  const original = structuredClone(records)
  const split = splitLocalAssessments({ policy: 'neighbor-pair-v1', assessment_contract: 'current', records }, { graph: fullGraph, stale: false })
  assert.deepEqual(split.current.map(row => row.pathway_id), ['route-1', 'changed-source'])
  assert.deepEqual(split.historical.map(row => row.pathway_id), ['previous', 'retired-connection'])
  assert.deepEqual(records, original)
})

test('a slow prior findings refresh cannot replace newer completed findings or surface its obsolete error', async () => {
  const load = createLatestDetailsLoader()
  let finishOld
  const oldClaims = new Promise(resolve => { finishOld = resolve })
  const old = load(async path => path.endsWith('/claims') ? oldClaims : {}, 'same-paper', 'all', 'assembled')
  const latest = await load(async path => path.endsWith('/claims') ? { claims: [{ candidate: { candidate_id: 'new' } }] } : {}, 'same-paper', 'all', 'assembled')
  finishOld({ claims: [{ candidate: { candidate_id: 'old' } }] })
  assert.equal(await old, null)
  assert.equal(latest.claims[0].candidate.candidate_id, 'new')
  let failOld
  const failure = new Promise((resolve, reject) => { failOld = reject })
  const obsolete = load(async path => path.endsWith('/claims') ? failure : {}, 'same-paper', 'all', 'assembled')
  await load(async path => path.endsWith('/claims') ? { claims: [] } : {}, 'same-paper', 'all', 'assembled')
  failOld(new Error('Obsolete snapshot failed'))
  assert.equal(await obsolete, null)
})

test('candidate routes and assessments form one list without duplicating routes or losing unassessed branches', () => {
  const extra = [{ ...route[0], id: 'new-1' }, { ...route[1], id: 'new-2' }]
  const input = structuredClone({ paths: [route, extra], records: [supported] })
  const before = structuredClone(input)
  const rows = combineRoutes(input.paths, input.records)
  assert.equal(rows.length, 2)
  assert.equal(rows.filter(row => row.record).length, 1)
  assert.equal(routeLabel(rows.find(row => !row.record)), 'Not assessed')
  assert.deepEqual(input, before)
})

test('support filters require current eligible assessments and never promote stale results or component-only support', () => {
  const rows = [supported, { ...supported, eligible: false }, { ...supported, stale: true },
    { ...supported, verdict: 'components_supported', eligible: false }].map(record => ({ edges: route, key: record.verdict, record }))
  assert.deepEqual(rows.map(routeGroup), ['supported', 'review', 'review', 'partial'])
  assert.equal(filterRoutes(rows, { group: 'supported' }).length, 1)
  assert.equal(routeLabel(rows[2]), 'Needs refresh')
  assert.equal(routeLabel(rows[1]), 'Support not currently eligible')
  assert.equal(filterRoutes(rows, { outcome: 'stale' }).length, 1)
})

test('paper and text filters retain cross-paper routes without presenting arbitrary union paths as assessed', () => {
  const rows = combineRoutes([route], [supported])
  assert.equal(filterRoutes(rows, { paperKey: 'Paper 1', query: 'affects' }).length, 1)
  assert.equal(filterRoutes(rows, { paperKey: 'Paper 2' }).length, 1)
  assert.equal(filterRoutes(rows, { paperKey: 'Unrelated' }).length, 0)
  assert.equal(filterRoutes(rows, { query: 'missing outcome' }).length, 0)
  const unrelated = { id: 'ca', candidate_id: 'other', source: 'C', target: 'A', document_key: 'Paper 1' }
  const source = { ...fullGraph, edges: [...route, unrelated] }
  assert.deepEqual(graphForRoutes(source, rows).edges.map(edge => edge.id), ['ab', 'bc'])
  assert.equal(source.edges.length, 3)
})

test('selecting a cross-paper route includes exact current edges while stale evidence cannot be projected onto a new graph', () => {
  const scoped = scopeAssemblyGraph(fullGraph, 'Paper 1')
  const current = combineRoutes([route], [supported])[0]
  assert.deepEqual(includeRouteContext(scoped, fullGraph, current).edges.map(edge => edge.id), ['ab', 'bc'])
  assert.deepEqual(routeHighlights(fullGraph, current), ['ab', 'bc'])
  const stale = { ...current, record: { ...supported, stale: true } }
  assert.equal(includeRouteContext(scoped, fullGraph, stale), scoped)
  assert.deepEqual(routeHighlights(fullGraph, stale), [])
  const missing = { ...current, edges: [{ ...route[0], candidate_id: 'replaced' }] }
  assert.deepEqual(routeHighlights(fullGraph, missing), [])
  assert.notEqual(edgeKey(route[0]), edgeKey({ ...route[0], document_key: 'Other PDF' }))
})

test('pathway highlighting changes visual focus without relayout, viewport movement or scientific verdict changes', t => {
  const cy = cytoscape({ headless: true, styleEnabled: true, layout: { name: 'preset' }, elements: [] })
  t.after(() => cy.destroy())
  syncNetwork(cy, visibleNetwork(fullGraph), new Map())
  cy.getElementById('A').position({ x: 135, y: 260 })
  cy.zoom(1.6); cy.pan({ x: 34, y: 51 })
  const before = { position: { ...cy.getElementById('A').position() }, zoom: cy.zoom(), pan: { ...cy.pan() } }
  highlightNetwork(cy, ['ab'], false)
  assert.equal(cy.getElementById('ab').hasClass('path-active'), true)
  assert.equal(cy.getElementById('bc').hasClass('path-dimmed'), true)
  assert.equal(cy.getElementById('ab').style('label'), 'affects')
  assert.equal(cy.getElementById('bc').style('label'), '')
  assert.equal(cy.getElementById('ab').data('status'), 'extracted')
  assert.deepEqual({ position: cy.getElementById('A').position(), zoom: cy.zoom(), pan: cy.pan() }, before)
  highlightNetwork(cy, [], true)
  assert.equal(cy.$('.path-active, .path-dimmed').length, 0)
  assert.equal(cy.getElementById('bc').style('label'), 'increases')
})

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { default: Workspace } = await server.ssrLoadModule('/src/components/graph/GraphWorkspace.jsx')
  const { default: PathwayPanel } = await server.ssrLoadModule('/src/components/graph/PathwayPanel.jsx')
  const { default: ResearchNetwork } = await server.ssrLoadModule('/src/components/graph/ResearchNetwork.jsx')
  const { PathwayAssessmentCard } = await server.ssrLoadModule('/src/components/graph/PathwayAssessments.jsx')
  const render = (component, props) => renderToStaticMarkup(React.createElement(component, props))
  const props = { graph, mode: 'assembled', scope: 'all', filter: 'all', pathwayOnly: true,
    selected: { id: 'paper', status: 'completed' }, assembly: { graph } }
  test('latest local network separates obsolete assessments into accessible history', () => {
    const current = { ...supported, verdict: 'connection_supported', assessment_scope: 'neighboring_claims',
      target: { ...supported.target, policy: 'neighbor-pair-v1', assessment_contract: 'current', document_keys: ['Paper 1'] } }
    const older = { ...current, pathway_id: 'previous-pair', stale: true, eligible: false,
      target: { ...current.target, assessment_contract: 'earlier' } }
    const html = render(ResearchNetwork, { assembly: { graph: fullGraph, paths: [route], stale: false },
      assessments: { policy: 'neighbor-pair-v1', assessment_contract: 'current', records: [older, current] },
      scope: 'all', selected: { id: 'paper' }, pathwayOnly: true })
    assert.match(html, /Earlier local assessments \(1\)/)
    assert.match(html, /Review previous evidence/)
    assert.doesNotMatch(html.split('Earlier local assessments')[0], /Needs refresh/)
    assert.equal((html.match(/class="network-route-select"/g) || []).length, 1)
  })
  test('assembled and supported-only networks remain distinct, with an explicit empty-support explanation', () => {
    const html = render(Workspace, props)
    for (const text of ['Assembled network', 'Supported pathway network', 'Source-supported', 'Audit view',
      '2 original claims', 'not a validated complete mechanism', 'Rearrange', 'Lock nodes']) assert.ok(html.includes(text), text)
    const empty = render(Workspace, { ...props, mode: 'main', scope: 'paper', graph: { nodes: [], edges: [] } })
    assert.match(empty, /No fully supported pathways yet/)
    assert.match(empty, /View assembled network/)
    assert.match(empty, /Download GraphML/)
    assert.doesNotMatch(empty, /2 original claims/)
    const selected = render(Workspace, { ...props,
      element: { kind: 'edge', ...graph.edges[0], external_status: 'not_individually_evaluated', support_count: 0 } })
    assert.match(selected, /not individually externally validated/)
    assert.doesNotMatch(selected, /0 supporting article/)
  })
  test('provisional pathway graph is visible initially without losing collapse or read-only controls', () => {
    const html = render(PathwayPanel, { data: { graph, paths: [], view: 'provisional' }, pathwayOnly: true, readOnly: true })
    assert.match(html, /Hide assembled graph/)
    assert.match(html, /aria-expanded="true"/)
    assert.doesNotMatch(html, /Assemble source-grounded chains/)
  })
  test('unified workspace shows one compact pathway list and defers detailed evidence and advanced controls', () => {
    const html = render(ResearchNetwork, { assembly: { graph: fullGraph, paths: [route] }, assessments: { records: [supported] },
      scope: 'all', selected: { id: 'paper', status: 'completed' }, readOnly: true, pathwayOnly: true })
    assert.equal((html.match(/class="network-route-select"/g) || []).length, 1)
    for (const text of ['Research network and pathways', 'Pathways in this network', 'Fully supported', 'View evidence',
      'Browse all network claims', 'Download saved GraphML', 'Show relation labels']) assert.ok(html.includes(text), text)
    assert.doesNotMatch(html, /aria-label="Whole-pathway assessment"|Rebuild connections|Historical research views/)
    assert.match(html, /aria-label="Research network scope selector"/)
    assert.match(html, /<details class="graph-display-menu compact"><summary/)
    assert.match(html, /<dd>1<\/dd><dt>Currently fully supported/)
  })
  test('stale findings remain visible but do not inflate support and execution features stay behind options', () => {
    const html = render(ResearchNetwork, { assembly: { graph: fullGraph, paths: [route] },
      assessments: { records: [{ ...supported, stale: true }] }, scope: 'all', selected: { id: 'paper' }, pathwayOnly: true })
    assert.match(html, /Needs refresh/)
    assert.match(html, /<dd>0<\/dd><dt>Currently fully supported/)
    assert.doesNotMatch(html, /Rebuild connections/)
  })
  test('legacy views and empty or failed data remain inspectable in the unified workspace', () => {
    const html = render(ResearchNetwork, { assembly: { graph: { nodes: [], edges: [] }, paths: [], error: 'Saved network unavailable' },
      assessments: { records: [] }, scope: 'all', selected: { id: 'old-paper' }, readOnly: true, pathwayOnly: false })
    assert.match(html, /Historical research views/)
    assert.match(html, /Saved network unavailable/)
    assert.match(html, /No continuous pathways are loaded/)
    assert.doesNotMatch(html, /Rebuild connections/)
    assert.match(render(ResearchNetwork, {}), /Loading saved network and pathway findings/)
  })
  test('local pair support is labelled separately from complete-chain and historical assessments', () => {
    const local = { ...supported, verdict: 'connection_supported', assessment_scope: 'neighboring_claims',
      target: { ...supported.target, policy: 'neighbor-pair-v1', document_keys: ['Paper 1', 'Paper 2'] },
      assessment: { in_scope: true, synthesis: 'The neighboring claims have applicable evidence.', limitations: [], edge_assessments: [], connection_assessments: [], entity_roles: [] } }
    const html = render(ResearchNetwork, { assembly: { graph: fullGraph, paths: [route] },
      assessments: { policy: 'neighbor-pair-v1', records: [local], historical_records: [{ ...supported, historical: true, eligible: false }],
        coverage: { selected_pairs: 1, reused_pair_ids: ['saved-pair'], has_more: true } },
      scope: 'all', selected: { id: 'paper', status: 'completed' }, readOnly: true, pathwayOnly: true })
    for (const text of ['Locally supported', 'Neighbor pair', 'Historical whole-route assessments', 'Locally supported pairs']) assert.ok(html.includes(text), text)
    assert.match(html, /<dd>1<\/dd><dt>Locally supported pairs/)
    assert.match(html, /aria-label="Network overview"/)
    assert.match(html, /1 neighboring pair selected for assessment/)
    assert.match(html, /1 compatible completed pair assessment reused/)
    assert.match(html, /Resume the saved run to continue/)
    const detail = render(PathwayAssessmentCard, { record: local })
    assert.match(detail, /Neighboring-claim assessment/)
    assert.match(detail, /Whole-chain assessment/)
    assert.match(detail, /Not assessed/)
    assert.doesNotMatch(detail, /experimentally demonstrated/)
    const held = render(PathwayAssessmentCard, { record: { ...local, verdict: 'assessment_incomplete', eligible: false,
      paper_assessment_errors: [{ evidence_document_id: 'paper-review', title: 'Study awaiting review',
        error: 'A proof label selected conflicting evidence.', review_response: { proofs: [] } }] } })
    for (const text of ['Assessment incomplete', 'Study awaiting review', 'conflicting evidence', 'Unadmitted model proposal', 'Resume retries the pending work']) assert.ok(held.includes(text), text)
    assert.match(held, /Provisional scientific result/)
    assert.match(held, /Execution/)
    assert.match(held, /not admitted to the supported graph/)
    const corrected = render(PathwayAssessmentCard, { record: { ...local, verdict: 'components_supported', eligible: false,
      assessment: { ...local.assessment, corrections: [{ edge_id: route[0].id,
        claim_text: 'A precise externally evidenced description.', reason: 'A descriptor needs revision.', proof_refs: [] }],
        edge_assessments: [{ edge_id: route[0].id, stance: 'partially_supported', reason: 'Only the broader wording is supported.', proof_refs: [] }] } } })
    for (const text of ['Proposed corrections for review', 'Original:', 'Proposed:', 'precise externally evidenced', 'partial wording support']) assert.ok(corrected.includes(text), text)
  })
  test('long candidate chains report local coverage without inheriting a whole-chain verdict', () => {
    const last = { id: 'cd', candidate_id: 'claim-3', document_key: 'Paper 2', source: 'C', target: 'D' }
    const pair1 = { ...supported, verdict: 'connection_supported' }
    const pair2 = { ...pair1, pathway_id: 'pair-2', target: { edges: [route[1], last] } }
    const rows = combineRoutes([[...route, last]], [pair1, pair2])
    const chain = rows.find(row => row.edges.length === 3)
    assert.deepEqual(chain.localCoverage, { supported: 2, total: 2 })
    assert.equal(chain.record, null)
    assert.equal(routeGroup(chain), 'review')
    assert.equal(filterRoutes(rows, { group: 'supported' }).length, 2)
  })
  test('network workspace surfaces prominent scope, data layer and graph download options', () => {
    const html = render(ResearchNetwork, { assembly: { graph: fullGraph, paths: [route] }, assessments: { records: [supported] },
      scope: 'paper', selected: { id: 'paper-1', title: 'Paper 1', status: 'completed' }, api: 'http://127.0.0.1:8000/api' })
    for (const text of [
      'aria-label="Research network scope selector"',
      'This paper',
      'All papers',
      'Data layer:',
      'aria-label="Network data layer quick select"',
      'Download displayed graph as JSON',
      'Download saved GraphML',
      'Save PNG',
      'Save JSON'
    ]) assert.ok(html.includes(text), `Expected ${text} in ResearchNetwork render`)
  })
} finally { await server.close() }
