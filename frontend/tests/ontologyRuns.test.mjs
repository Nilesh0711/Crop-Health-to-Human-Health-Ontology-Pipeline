import assert from 'node:assert/strict'
import test from 'node:test'
import React, { act, useState } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const papers = [
  { id: 'a', title: 'Potato nutrition paper', status: 'completed', latest_job_id: 'job-a' },
  { id: 'b', title: 'Soil nutrients paper', status: 'not_started' },
]
const health = { capabilities: { execute: true }, model_ready: true }
const response = data => ({ ok: true, json: async () => data })
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

test('extraction-only policy hides grounding controls and explains the stopping point', async () => {
  const ui = await setup(async () => response([]))
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: [papers[1]],
      health: { ...health, pipeline: { stage_status: { ontology_grounding: 'paused' }, task_models: { extraction: 'selected-model' } } }, onRefresh() {} }))
    assert.match(ui.text(), /Extraction-only runs/)
    assert.match(ui.text(), /Run pipeline/)
    assert.doesNotMatch(ui.text(), /Ground saved claims/)
    assert.equal(ui.dom.window.document.querySelectorAll('input[role="switch"]').length, 1)
  } finally { await ui.close() }
})

test('manual grounding button uses saved extraction and its dedicated endpoint', async () => {
  const posts = []
  const ui = await setup(async (path, options) => {
    if (options?.method === 'POST') {
      posts.push({ path, body: JSON.parse(options.body) })
      return response({ id: 'ground-job', status: 'queued', logs: [] })
    }
    if (path === '/api/jobs/ground-job') return response({ id: 'ground-job', status: 'completed', logs: [{ message: 'Saved claims grounded' }] })
    return response([])
  })
  const manualHealth = { ...health, pipeline: { stage_status: { ontology_grounding: 'manual' },
    manual_grounding: { enabled: true, ready: true, model: 'ground-model' } } }
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: [{ ...papers[1], status: 'completed',
      summary: { extracted: 12, extraction_coverage: { complete: true } } }], health: manualHealth, onRefresh() {} }))
    assert.match(ui.text(), /Run pipeline extracts claims only/)
    assert.match(ui.text(), /Ontology grounding \(manual\)/)
    assert.match(ui.text(), /ground-model/)
    await ui.click('Run grounded claims')
    assert.equal(posts.length, 1)
    assert.equal(posts[0].path, '/api/pdfs/b/ground-claims')
    assert.equal(posts[0].body.thinking.pathway_assembly, true)
    assert.match(ui.text(), /Saved claims grounded/)
  } finally { await ui.close() }
})

test('appendix switches between illustrative extraction and grounding without API calls', async () => {
  const calls = []
  const ui = await setup(async path => { calls.push(path); return response({}) }, '/src/ontology/Appendix.jsx')
  try {
    await ui.render(React.createElement(ui.Runs))
    const stageButtons = [...ui.dom.window.document.querySelectorAll('.appendix-stage-picker button')].map(b => b.textContent.trim())
    assert.deepEqual(stageButtons, ['1. Claim extraction', '2. Ontology grounding'])
    const output = () => JSON.parse(ui.dom.window.document.querySelector('[aria-label="LLM output JSON"]').textContent)
    assert.equal(output().claims.length, 1)
    assert.deepEqual(output().claims.map(c => c.object), ['root system'])
    assert.equal(output().claims[0].subject, 'root')
    assert.equal(output().claims[0].subject_anchor, 'root')
    assert.equal(output().claims[0].object_anchor, 'root system')
    await ui.click('Ontology grounding')
    assert.doesNotMatch(ui.text(), /T77 \(Bacillus subtilis\), T183/)
    assert.match(ui.text(), /211 classes.*19 object properties/)
    assert.match(ui.text(), /PO:0009005 root/)
    assert.equal(output().results[0].claim_id, 'C1')
    assert.match(output().results[0].subject.term, /^T\d+$/)
    assert.match(output().results[0].object.term, /^T\d+$/)
    await ui.click('Claim extraction')
    assert.equal(output().claims.length, 1)
    assert.deepEqual(calls, [])
  } finally { await ui.close() }
})

test('paused reasoning panel omits post-grounding pause instructions', async () => {
  const ui = await setup(async () => response({ status: 'paused', facts: [], total: 0 }), '/src/ontology/Reasoning.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0 }))
    assert.match(ui.text(), /Reasoning paused/)
    assert.doesNotMatch(ui.text(), /The pipeline currently stops after ontology grounding/)
    assert.doesNotMatch(ui.text(), /Previous reasoning results are preserved/)
  } finally { await ui.close() }
})

test('mapping review lists all assertions without the review-only checkbox filter', async () => {
  const urls = []
  const ui = await setup(async path => { urls.push(path); return response({ assertions: [], total: 0, offset: 0, limit: 15 }) }, '/src/ontology/MappingReview.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0, onRefresh() {}, pdfs: [] }))
    assert.equal(ui.dom.window.document.querySelector('input[type="checkbox"]'), null)
    assert.doesNotMatch(ui.text(), /Needs mapping or scope review/)
    assert.ok(urls.some(u => u.startsWith('/api/ontology/assertions?')))
    assert.ok(urls.every(u => !u.includes('review_only=')))
  } finally { await ui.close() }
})

test('extraction holds show evidence and hide mutation controls in read-only mode', async () => {
  const row = { candidate_id: 'c', document_key: 'Study A', revision: 'a'.repeat(64), scope: 'in_scope', scope_reason: 'Relevant domain.',
    original_claim: { subject: 'Treatment T', relation: 'increases', object: 'Trait Q', source_evidence_span: 'Treatment T did not increase Trait Q.' },
    extraction_review_status: 'held', extraction_warnings: [{ message: 'Check missing negation.', source_text: 'Treatment T did not increase Trait Q.' }],
    bindings: Object.fromEntries(['subject', 'relation', 'object'].map(role => [role, { mapping: 'unresolved', reason: 'Review needed.' }])) }
  const ui = await setup(async () => response({ assertions: [row], total: 1, offset: 0, limit: 15 }), '/src/ontology/MappingReview.jsx')
  try {
    const props = { revision: 0, pdfs: [{ id: 'p', title: 'Study A', status: 'completed' }], onRefresh() {} }
    await ui.render(React.createElement(ui.Runs, { ...props, canReview: false }))
    assert.match(ui.text(), /Check missing negation/)
    assert.doesNotMatch(ui.text(), /Accept extraction|Reject extraction/)
    await ui.render(React.createElement(ui.Runs, { ...props, canReview: true }))
    const accept = [...ui.dom.window.document.querySelectorAll('button')].find(b => b.textContent === 'Accept extraction')
    assert.ok(accept && accept.disabled, 'A note is required before acceptance')
    assert.match(ui.text(), /does not approve mappings or scientific truth/)
  } finally { await ui.close() }
})

async function setup(fetcher, modulePath = '/src/ontology/Runs.jsx') {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' })
  const originals = new Map()
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true, fetch: fetcher })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key))
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  const server = await createServer({ server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [] }, appType: 'custom' })
  const { createRoot } = await import('react-dom/client')
  const root = createRoot(dom.window.document.getElementById('root'))
  const { default: Runs } = await server.ssrLoadModule(modulePath)
  const { useData } = await server.ssrLoadModule('/src/ontology/useData.jsx')
  const flush = () => act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return { Runs, useData, root, dom, flush,
    async render(element) { await act(async () => { root.render(element) }); await flush() },
    async click(text) {
      const button = [...dom.window.document.querySelectorAll('button, a')].find(b => b.textContent.includes(text))
      assert.ok(button, `Missing button or link: ${text}`)
      if (button.disabled !== undefined) assert.equal(button.disabled, false, `${text} should be enabled`)
      await act(async () => { button.click() }); await flush()
    },
    text() { return dom.window.document.body.textContent },
    async close() {
      await act(async () => root.unmount())
      await server.close(); dom.window.close()
      for (const [key, descriptor] of originals) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor)
        else delete globalThis[key]
      }
    },
  }
}

test('entity registry exposes partial mappings and displays unique entity occurrences', async () => {
  const paths = []
  const ui = await setup(async path => {
    paths.push(path)
    return response({ stale: false, counts: { grounded_mentions: 2, shared_concepts: 1 }, total: 1, limit: 20, offset: 0,
      entities: [{ id: 'concept-q', label: 'Trait Q', iri: 'urn:test:trait-q', concept_label: 'Trait Q',
        shared_concept: true, kind: 'concept', mapping: 'exact', occurrence_count: 2, occurrences: [
          { id: 'one', document_key: 'Study A', source_page: 2, source_mention: 'Trait Q', role: 'object', reason: 'Definition agrees.',
            evidence: 'Exact source A.', qualifiers: { condition: 'Condition A' }, certainty: 'conditional', claim_assembly_eligible: false },
          { id: 'two', document_key: 'Study B', source_page: 3, source_mention: 'Q trait', role: 'subject', reason: 'Same whole concept.',
            evidence: 'Exact source B.', qualifiers: { condition: 'Condition B' }, certainty: 'associative', claim_assembly_eligible: false },
        ] }] })
  }, '/src/ontology/Entities.jsx')
  let selected
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0, onTerm: iri => { selected = iri } }))
    assert.match(ui.text(), /Trait Q/)
    assert.match(ui.text(), /2 mentions/)
    assert.match(ui.text(), /exact/)
    assert.ok(paths.every(p => p.startsWith('/api/ontology/entities?')))
    await ui.click('Trait Q')
    assert.equal(selected, 'urn:test:trait-q')
    assert.equal(ui.dom.window.document.querySelector('canvas'), null)
  } finally { await ui.close() }
})

test('entity filter shows all category totals and keeps the selected total while loading', async () => {
  const pending = deferred()
  const counts = { grounded: 7, vocabulary_gap: 580, alignment_review: 67, classification: 50, review: 17, all: 654 }
  const ui = await setup(async path => {
    if (path.includes('status=vocabulary_gap')) return pending.promise
    return response({ entities: [], counts: {}, status_counts: counts, total: 0, limit: 20, offset: 0 })
  }, '/src/ontology/Entities.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0, onTerm() {} }))
    const select = ui.dom.window.document.querySelector('select[aria-label="Entity mapping status"]')
    assert.deepEqual([...select.options].map(o => o.textContent), [
      'Exact concept matches (7)', 'Broader classifications (50)', 'Related / narrower — review (17)', 'Unresolved (580)', 'All entities (654)',
    ])
    assert.equal(select.selectedOptions[0].textContent, 'Exact concept matches (7)')
    await act(async () => {
      select.value = 'vocabulary_gap'
      select.dispatchEvent(new ui.dom.window.Event('change', { bubbles: true }))
    })
    await ui.flush()
    assert.equal(select.selectedOptions[0].textContent, 'Unresolved (580)')
    pending.resolve(response({ entities: [], counts: {}, status_counts: counts, total: 580, limit: 20, offset: 0 }))
    await ui.flush()
    assert.equal(select.selectedOptions[0].textContent, 'Unresolved (580)')
  } finally {
    pending.resolve(response({ entities: [], counts: {}, status_counts: counts, total: 0, limit: 20, offset: 0 }))
    await ui.close()
  }
})

test('completed job refresh preserves all papers and does not restart its job monitor', async () => {
  let paperRequests = 0, jobRequests = 0
  const refreshResponse = deferred()
  const ui = await setup(async path => {
    if (path === '/api/pdfs') {
      paperRequests += 1
      return jobRequests === 0 ? response(papers) : refreshResponse.promise
    }
    if (path === '/api/jobs/job-a') {
      jobRequests += 1
      return response({ id: 'job-a', status: 'completed', logs: [{ message: 'Grounding completed', stage: 'ontology_grounding', level: 'success' }] })
    }
    if (path.endsWith('/logs')) return response([])
    throw new Error(`Unexpected request: ${path}`)
  })
  function Harness() {
    const [revision, setRevision] = useState(0)
    const data = ui.useData('/pdfs', revision)
    return React.createElement(ui.Runs, { pdfs: data.data || [], health, loading: data.loading,
      onRefresh: () => setRevision(n => n + 1) })
  }
  try {
    await ui.render(React.createElement(React.StrictMode, null, React.createElement(Harness)))
    await ui.flush()
    assert.match(ui.text(), /Potato nutrition paper/)
    assert.match(ui.text(), /Soil nutrients paper/)
    assert.match(ui.text(), /Grounding completed/)
    // The terminal refresh is deliberately still in flight: no empty-paper flash.
    assert.match(ui.text(), /All papers \(2\)/)
    const requestsWhileRefreshing = paperRequests
    assert.ok(requestsWhileRefreshing >= 2)
    const completedMonitorRequests = jobRequests
    refreshResponse.resolve(response(papers.map(p => ({ ...p }))))
    await ui.flush(); await ui.flush()
    assert.equal(paperRequests, requestsWhileRefreshing)
    assert.equal(jobRequests, completedMonitorRequests)
    assert.match(ui.text(), /Grounding completed/)
  } finally { refreshResponse.resolve(response(papers)); await ui.close() }
})

test('switching papers loads saved logs without a job ID and ignores a late previous-job response', async () => {
  const oldJob = deferred()
  const ui = await setup(async path => {
    if (path === '/api/jobs/job-a') return oldJob.promise
    if (path === '/api/pdfs/a/logs') return response([{ message: 'Paper A saved activity' }])
    if (path === '/api/pdfs/b/logs') return response([{ message: 'Paper B saved activity', stage: 'ontology_grounding' }])
    throw new Error(`Unexpected request: ${path}`)
  })
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: papers, health, onRefresh() {} }))
    assert.match(ui.text(), /Paper A saved activity/)
    await ui.click('Soil nutrients paper')
    assert.match(ui.text(), /Paper B saved activity/)
    assert.doesNotMatch(ui.text(), /Paper A saved activity/)
    oldJob.resolve(response({ id: 'job-a', status: 'failed', logs: [{ message: 'Late paper A result' }] }))
    await ui.flush()
    assert.doesNotMatch(ui.text(), /Late paper A result/)
    assert.match(ui.text(), /Run pipeline/)
  } finally { oldJob.resolve(response({ id: 'job-a', status: 'completed', logs: [] })); await ui.close() }
})

test('run button targets the selected paper and displays the new job activity', async () => {
  const posts = []
  let refreshes = 0
  const ui = await setup(async (path, options) => {
    if (options?.method === 'POST') {
      posts.push({ path, body: JSON.parse(options.body) })
      return response({ id: 'new-job', status: 'queued', logs: [] })
    }
    if (path === '/api/jobs/new-job') return response({ id: 'new-job', status: 'completed', logs: [{ message: 'New run finished', level: 'success' }] })
    if (path.endsWith('/logs')) return response([])
    throw new Error(`Unexpected request: ${path}`)
  })
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: [papers[1]], health, onRefresh() { refreshes += 1 } }))
    await ui.click('Run pipeline')
    assert.equal(posts.length, 1)
    assert.equal(posts[0].path, '/api/pdfs/b/run')
    assert.equal(posts[0].body.thinking.extraction, false)
    assert.equal(posts[0].body.thinking.pathway_assembly, true)
    assert.equal(posts[0].body.extraction_generation_budget, 50000)
    assert.match(ui.text(), /New run finished/)
    assert.equal(refreshes, 2) // Once on submission; once on terminal completion.
    await ui.flush()
    assert.equal(refreshes, 2)
  } finally { await ui.close() }
})

test('missing live job retains saved activity and leaves recovery controls accessible', async () => {
  const ui = await setup(async path => path.includes('/jobs/')
    ? { ok: false, status: 404, json: async () => ({ detail: 'Job not found' }) }
    : response([{ message: 'Saved checkpoint activity' }]))
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: [papers[0]], health, onRefresh() {} }))
    assert.match(ui.text(), /The live job is unavailable/)
    assert.match(ui.text(), /Saved checkpoint activity/)
    assert.match(ui.text(), /Resume saved run/)
  } finally { await ui.close() }
})

test('switching papers preserves selected thinking settings and active runs keep stop available', async () => {
  const ui = await setup(async path => path === '/api/jobs/job-a'
    ? response({ id: 'job-a', status: 'running', logs: [{ message: 'Grounding in progress' }] })
    : response([]))
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: [{ ...papers[0], status: 'running' }, papers[1]], health, onRefresh() {} }))
    assert.match(ui.text(), /Stop pipeline/)
    const switches = ui.dom.window.document.querySelectorAll('input[role="switch"]')
    assert.equal(switches[0].checked, false)
    assert.equal(switches[1].checked, true)
    await act(async () => switches[1].click())
    assert.equal(switches[1].checked, false)
    await ui.click('Soil nutrients paper')
    assert.equal(ui.dom.window.document.querySelectorAll('input[role="switch"]')[1].checked, false)
    assert.equal([...ui.dom.window.document.querySelectorAll('button')].find(b => b.textContent === 'Run pipeline').disabled, true)
    await ui.click('Potato nutrition paper')
    assert.match(ui.text(), /Stop pipeline/)
    assert.match(ui.text(), /Grounding in progress/)
  } finally { await ui.close() }
})
