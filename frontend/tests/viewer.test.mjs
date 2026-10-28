import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { readFileSync } from 'node:fs'
import { canExecute, isViewer } from '../src/lib/capabilities.js'

test('execution is enabled only by explicit local capability', () => {
  for (const health of [null, {}, {mode: 'viewer'}, {capabilities: {execute: false, read_only: true}}]) assert.equal(canExecute(health), false)
  assert.equal(canExecute({capabilities: {execute: true}}), true)
  assert.equal(isViewer({capabilities: {read_only: true}}), true)
})

const server = await createServer({server: {middlewareMode: true}, appType: 'custom'})
try {
  const { ClaimActions } = await server.ssrLoadModule('/src/components/claims/ClaimDetails.jsx')
  const { default: SavedRunDetails } = await server.ssrLoadModule('/src/components/workspace/SavedRunDetails.jsx')
  const { default: PathwayPanel } = await server.ssrLoadModule('/src/components/graph/PathwayPanel.jsx')
  const { ClaimCard } = await server.ssrLoadModule('/src/App.jsx')
  const render = (component, props) => renderToStaticMarkup(React.createElement(component, props))
  test('viewer hides claim mutations while preserving revision history', () => {
    const props = {candidate: {source_validation: {decision: 'needs_review'}},
      revisions: [{status: 'completed', original_candidate: {subject: 'Crop C', object: 'Compound T'}}]}
    assert.match(render(ClaimActions, props), /Repair and revalidate claim/)
    const html = render(ClaimActions, {...props, readOnly: true})
    assert.doesNotMatch(html, /Repair and revalidate|Reassess external|Retry unfinished/)
    assert.match(html, /Claim revision history/)
    assert.match(html, /Crop C/)
  })
  test('saved details expose historical settings without editable run controls', () => {
    const html = render(SavedRunDetails, {jobs: [{id: 'job-a', status: 'failed'}], jobId: 'job-a',
      job: {id: 'job-a', status: 'failed', options: {thinking: {external: false}}, error: 'Saved error'}, pipeline: {schema_version: 'v-test'}})
    for (const text of ['Historical run', 'job-a', 'Saved error', 'thinking', 'false', 'v-test', 'not a reconstruction']) assert.ok(html.includes(text), text)
    assert.doesNotMatch(html, /type="checkbox"|Run pipeline|Resume pipeline/)
  })
  test('read-only pathways keep browsing, filters and depth but omit assembly', () => {
    const props = {data: {view: 'validated', graph: {nodes: [], edges: []}, paths: [], stale: true}}
    assert.match(render(PathwayPanel, props), /Assemble validated claims/)
    const html = render(PathwayPanel, {...props, readOnly: true})
    assert.doesNotMatch(html, /Assemble validated claims|Assemble again|resume the pipeline/i)
    for (const text of ['Path length in edges', 'Search pathways and claims', 'Show assembled graph', 'stale-data warning']) assert.ok(html.includes(text), text)
  })
  test('all published claims render expanded with evidence and without execution actions', () => {
    const root = new URL('../../deploy/render/snapshot/', import.meta.url)
    const manifest = JSON.parse(readFileSync(new URL('manifest.json', root), 'utf8'))
    for (const entry of Object.values(manifest.documents)) {
      const { claims } = JSON.parse(readFileSync(new URL(entry.claims, root), 'utf8'))
      for (const [index, record] of claims.entries()) {
        const html = render(ClaimCard, {record, index, readOnly: true, defaultExpanded: true})
        assert.match(html, /claim-expanded/)
        assert.doesNotMatch(html, /Repair and revalidate claim|Reassess external evidence|Retry unfinished external checks/)
      }
    }
  })
} finally { await server.close() }
