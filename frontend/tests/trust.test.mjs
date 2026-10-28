import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { default: TrustEvidence } = await server.ssrLoadModule('/src/components/claims/TrustEvidence.jsx')
  const trust = { score: 0, status: 'contested', assessment_complete: true, support_count: 1,
    contradiction_count: 1, unresolved_count: 0, disputed_study_groups: [], findings: [] }
  const render = value => renderToStaticMarkup(React.createElement(TrustEvidence, { trust: value }))
  assert.match(render(trust), /contested/)
  assert.match(render(trust), /not a probability/)
  assert.match(render({ ...trust, status: 'not_assessed', assessment_complete: false }), /not assessed/)
  assert.match(render({ ...trust, status: 'not_assessed', assessment_complete: false }), /assessment unfinished/)
  const { RunSetup, RequestProgress } = await server.ssrLoadModule('/src/components/workspace/WorkbenchPanels.jsx')
  const setup = renderToStaticMarkup(React.createElement(RunSetup, { health: {}, options: {}, open: true }))
  assert.match(setup, /Knowledge building mode/)
  assert.match(setup, /value="trust_graph" selected/)
  assert.match(setup, /Previous: validate claims, then assemble/)
  assert.match(setup, /Claims per shared assessment/)
  assert.match(setup, /independent paper requests run in parallel/)
  assert.match(setup, /Paper assessments appear as papers become ready/)
  const { ValidationTrace } = await server.ssrLoadModule('/src/components/claims/ClaimDetails.jsx')
  const pending = renderToStaticMarkup(React.createElement(ValidationTrace, { candidate: {}, validation: {
    validation_status: 'retrieval_pending', attempts: [{pending_sources: [{provider: 'openalex', kind: 'search', reason: 'deadline'}]}],
  } }))
  assert.match(pending, /Retrieval is incomplete/)
  assert.match(pending, /openalex/)
  assert.match(pending, /has not entered the validated graph/)
  const progress = renderToStaticMarkup(React.createElement(RequestProgress, { running: true, progress: {
    model: 'primary', status: 'waiting_for_provider', started_at: new Date().toISOString(), active_request_count: 3,
  } }))
  assert.match(progress, /3<\/b> model requests running in parallel/)
  const { default: Panel } = await server.ssrLoadModule('/src/components/graph/PathwayPanel.jsx')
  const panel = renderToStaticMarkup(React.createElement(Panel, {data: {view: 'provisional', paths: [],
    graph: {nodes: [], edges: [{id:'e', subject:'Factor A', object:'Process B', relation:'affects', trust}]}}}))
  assert.match(panel, /Evidence balance/)
  assert.match(panel, /graph contribution/i)
  assert.match(panel, /contested/)
  assert.match(panel, /standalone claims remain/)
  assert.doesNotMatch(panel, /Assemble validated claims/)
} finally {
  await server.close()
}
