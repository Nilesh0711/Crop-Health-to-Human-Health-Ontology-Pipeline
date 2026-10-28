import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { entityLabel, evidenceState, matchesEdge, matchesPath, orderClaims } from '../src/lib/pathways.js'

const first = { id: 'edge-1', candidate_id: 'claim-1', subject: 'Disease D', object: 'Crop C grains',
  source_label: 'Disease D', target_label: 'Crop C grains', relation: 'affects', paper: 'Paper A',
  source_page: 7, source_status: 'entailed', external_status: 'external_support_needs_review', certainty: 'conditional',
  quote: 'Disease D may affect Crop C grains under Condition K.', qualifiers: { condition: 'Condition K' },
  trust: { score: 2, status: 'supported', assessment_complete: true, contribution: { two_edge_connections: 1 } } }
const second = { ...first, id: 'edge-2', candidate_id: 'claim-2', subject: 'These affected grains', object: 'Compound T',
  source_label: 'Crop C grains', target_label: 'Compound T', relation: 'may_contain', paper: 'Paper B',
  source_page: 9, quote: 'These affected grains may contain Compound T.', external_status: 'retrieval_pending',
  trust: { score: 0, status: 'not_assessed', assessment_complete: false, contribution: { two_edge_connections: 3 } } }

await test('search crosses connected edges while state filters match individual saved edges', () => {
  const path = [first, second]
  const before = JSON.stringify(path)
  assert.equal(matchesPath(path, 'Disease D Compound T'), true)
  assert.equal(matchesEdge(first, 'Disease D Compound T'), false)
  assert.equal(matchesEdge(first, 'condition k paper a'), true)
  assert.equal(matchesPath(path, '', 'unfinished'), true)
  assert.equal(matchesPath(path, '', 'contested'), false)
  assert.equal(matchesEdge(second, '', 'supported'), false)
  assert.equal(matchesPath(path, 'nonexistent'), false)
  assert.equal(evidenceState(second), 'unfinished')
  assert.equal(first.external_status, 'external_support_needs_review')
  assert.equal(JSON.stringify(path), before)
})

await test('sorting and entity labels preserve original claims and identifiers', () => {
  const edges = [second, first]
  assert.deepEqual(orderClaims(edges, 'score').map(e => e.id), ['edge-1', 'edge-2'])
  assert.deepEqual(orderClaims(edges, 'contribution').map(e => e.id), ['edge-2', 'edge-1'])
  assert.deepEqual(edges.map(e => e.id), ['edge-2', 'edge-1'])
  assert.equal(entityLabel(second, 'source'), 'Crop C grains')
  assert.equal(second.subject, 'These affected grains')
  assert.equal(entityLabel({source: 'Crop:Crop C'}, 'source'), 'Crop C')
  assert.equal(entityLabel({source: 'entity-id'}, 'source'), 'entity-id')
})

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { PathwayCard, CorroboratedPaths } = await server.ssrLoadModule('/src/components/graph/PathwayCards.jsx')
  const { default: Navigation } = await server.ssrLoadModule('/src/components/graph/PathwayNavigation.jsx')
  const { default: Panel } = await server.ssrLoadModule('/src/components/graph/PathwayPanel.jsx')
  const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props))
  await test('path evidence retains original mentions, quotations, conditions, pages and separate decisions', () => {
    const markup = render(PathwayCard, {path: [first, second], number: 1, provisional: true, onSelect: () => {}})
    for (const value of ['These affected grains', second.quote, first.quote, 'Condition K', 'page 7', 'page 9',
      'claim-1', 'claim-2', 'conditional', 'external support needs review', 'retrieval pending', 'unfinished',
      'Provisional', 'Original claims and passages', 'Inspect evidence balance']) assert.ok(markup.includes(value), value)
    assert.equal((markup.match(/class="pathway-entity"/g) || []).length, 3)
    assert.match(markup, /Saved evidence ledger states/)
    assert.match(markup, /1 unfinished/)
    assert.doesNotMatch(markup, /Validated edges|proven causal pathway/)
  })
  await test('all three pathway views and original assembly controls remain available', () => {
    const nav = render(Navigation, {value: 'trust', trustCount: 6, assembledCount: 2, healthCount: 1})
    for (const value of ['Trust network', 'Global pathway assembly', 'Corroborated health paths']) assert.ok(nav.includes(value))
    assert.equal((nav.match(/aria-pressed="true"/g) || []).length, 1)
    const data = {view: 'provisional', graph: {nodes: [], edges: [first, second]}, paths: [[first, second]],
      max_hops: 6, has_more: true, stale: true}
    const network = render(Panel, {data})
    for (const value of ['Network claims', 'Connection assessments', 'Search pathways and claims',
      'Filter pathway evidence state', 'Show more paths', 'Path length in edges', 'Show assembled graph',
      'Resume the pipeline', 'Evidence balance', 'graph contribution']) assert.ok(network.includes(value), value)
    assert.doesNotMatch(network, /Assemble validated claims/)
    assert.match(render(Panel, {data: {...data, view: 'validated'}, busy: true}), /disabled=""/)
    assert.match(render(Panel, {data: {...data, view: 'validated'}, busy: false}), /Assemble validated claims/)
  })
  await test('loading, empty and interrupted states explain the next available action', () => {
    assert.match(render(Panel, {}), /Loading saved pathways/)
    assert.match(render(Panel, {data: {view: 'provisional', paths: []}}), /View network claims/)
    assert.match(render(Panel, {data: {status: 'failed', error: 'Stored interruption', paths: []}}), /Stored interruption/)
    const health = render(CorroboratedPaths, {paths: [[first, second]]})
    assert.match(health, /whole-chain causation/)
    assert.match(health, /Original claims and passages/)
    assert.match(render(CorroboratedPaths, {}), /No supported reasoning paths yet/)
  })
} finally {
  await server.close()
}
