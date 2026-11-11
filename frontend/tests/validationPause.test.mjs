import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { claimGroup, claimStatus } from '../src/lib/claimPresentation.js'
import { splitLocalAssessments } from '../src/lib/researchNetwork.js'

test('paused validation does not promote historical acceptance or hold into a current verdict', () => {
  for (const decision of ['entailed', 'needs_review', 'unsupported']) {
    const record = { validation_stages_paused: true, candidate: { source_validation: { decision } } }
    assert.equal(claimStatus(record), 'validation_paused')
    assert.equal(claimGroup(record), 'unvalidated')
  }
  assert.equal(claimStatus({ validation_stages_paused: true, candidate: { schema_error: 'Missing sentence' } }), 'schema_rejected')
})

test('saved external findings remain historical when the current assembly is unvalidated', () => {
  const records = [{ verdict: 'connection_supported', eligible: true }]
  const result = splitLocalAssessments({ policy: 'neighbor-pair-v1', records }, { validation_stages_paused: true })
  assert.deepEqual(result, { current: [], historical: records })
})

test('paused run setup exposes only extraction and assembly settings', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [] }, appType: 'custom' })
  try {
    const { RunSetup } = await server.ssrLoadModule('/src/components/workspace/WorkbenchPanels.jsx')
    const markup = renderToStaticMarkup(React.createElement(RunSetup, {
      health: { pipeline: { validation_stages_paused: true, task_models: {
        extraction: 'opencode-go/deepseek-v4.1-flash', pathway_assembly: 'opencode-go/mimo-v2.6-flash' } } },
      options: { max_pages: 0, claims_per_packet: 0 }, open: true, onOptions() {}, onToggle() {},
    }))
    assert.match(markup, /Source fidelity, project scope and external validation are paused/)
    assert.equal((markup.match(/role="switch"/g) || []).length, 2)
    assert.doesNotMatch(markup, /Neighbor pairs to assess|Search papers per neighboring pair|Per-paper judgement model|Muse Spark/)
    assert.match(markup, /Unvalidated connections/)
  } finally { await server.close() }
})
