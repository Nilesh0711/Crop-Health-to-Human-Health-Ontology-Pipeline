import assert from 'node:assert/strict'
import test from 'node:test'
import { watchJob } from '../src/lib/jobPolling.js'
import { graphEndpoint, loadPaperDetails } from '../src/lib/workspaceData.js'
import { request } from '../src/lib/api.js'

const flush = () => new Promise(resolve => setImmediate(resolve))
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

test('workspace graph selection preserves paper/global and historical graph views', () => {
  for (const [mode, suffix] of [['main', 'graph'], ['source', 'source-graph'], ['audit', 'audit-graph']]) {
    assert.equal(graphEndpoint('paper-a', 'paper', mode), `/pdfs/paper-a/${suffix}`)
    assert.equal(graphEndpoint('paper-a', 'all', mode), `/${suffix}`)
  }
})

test('workspace loads independent finding views concurrently and preserves their evidence', async () => {
  const gate = deferred()
  const calls = []
  const claim = { candidate: { candidate_id: 'claim-a', source_evidence_span: 'Exact source text.' },
    source_review_history: [{ decision: 'accept' }], revisions: [{ action: 'repair' }] }
  const responses = {
    '/pdfs/paper-a/source-graph': { nodes: [{ id: 'entity-a' }], edges: [] },
    '/pdfs/paper-a/claims': { claims: [claim], extraction_rejections: [{ reason: 'Invalid ID' }], model_requests: [{ model: 'selected-model' }] },
    '/pdfs/paper-a/logs': [{ level: 'error', message: 'Saved historical error.' }],
    '/pdfs/paper-a/paths': [{ id: 'historical-path' }],
    '/pathways': { paths: [{ id: 'supported-path' }] },
    '/pathways?view=provisional': { paths: [{ id: 'provisional-path' }] },
    '/pathway-assessments': { records: [{ pathway_id: 'route-a', verdict: 'components_supported' }] },
  }
  const loading = loadPaperDetails(async path => { calls.push(path); await gate.promise; return responses[path] }, 'paper-a', 'paper', 'source')
  assert.equal(calls.length, 7, 'No view should wait for another view before its request starts')
  gate.resolve()
  const result = await loading
  assert.equal(result.claims[0], claim)
  assert.equal(result.modelRequests[0].model, 'selected-model')
  assert.equal(result.discards[0].reason, 'Invalid ID')
  assert.equal(result.logs[0].message, 'Saved historical error.')
  assert.equal(result.paths[0].id, 'historical-path')
  assert.equal(result.pathwayData.paths[0].id, 'supported-path')
  assert.equal(result.trustData.paths[0].id, 'provisional-path')
  assert.equal(result.pathAssessments.records[0].verdict, 'components_supported')
})

test('unavailable optional pathway history does not hide saved claims or their logs', async () => {
  const result = await loadPaperDetails(async path => {
    if (path.startsWith('/pathway')) throw new Error('Historical result unavailable')
    if (path.endsWith('/claims')) return { claims: [{ candidate: { candidate_id: 'saved-claim' } }] }
    return []
  }, 'paper-a', 'all', 'audit')
  assert.equal(result.claims[0].candidate.candidate_id, 'saved-claim')
  assert.deepEqual(result.modelRequests, [])
  assert.deepEqual(result.discards, [])
  assert.equal(result.pathwayData.status, 'failed')
  assert.deepEqual(result.pathAssessments.records, [])
  assert.match(result.pathAssessments.error, /Historical result unavailable/)
})

test('missing required claim data fails visibly instead of presenting empty findings', async () => {
  await assert.rejects(loadPaperDetails(async path => {
    if (path.endsWith('/claims')) throw new Error('Claims unavailable')
    return []
  }, 'paper-a', 'paper', 'main'), /Claims unavailable/)
})

test('API errors retain the HTTP status used by job recovery and the backend reason', async () => {
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({ detail: 'Saved job not found' }) })
    await assert.rejects(request('/jobs/missing'), error => error.status === 404 && error.message === 'Saved job not found')
    globalThis.fetch = async () => ({ ok: false, status: 502, json: async () => { throw new Error('Not JSON') } })
    await assert.rejects(request('/jobs/unavailable'), error => error.status === 502 && error.message === 'Request failed (502)')
  } finally { globalThis.fetch = originalFetch }
})

function monitor(fetchJob, extra = {}) {
  const callbacks = [], jobs = [], errors = []
  let missing = 0
  const stop = watchJob({ fetchJob, onJob: job => jobs.push(job), onMissing: () => { missing += 1 },
    onError: error => errors.push(error), schedule: callback => { callbacks.push(callback); return callback },
    unschedule: callback => { const index = callbacks.indexOf(callback); if (index >= 0) callbacks.splice(index, 1) },
    ...extra })
  return { stop, callbacks, jobs, errors, get missing() { return missing } }
}

test('missing jobs stop polling and recover saved findings once', async () => {
  let requests = 0
  const watcher = monitor(async () => { requests += 1; throw Object.assign(new Error('Job not found'), { status: 404 }) })
  await flush()
  assert.equal(requests, 1)
  assert.equal(watcher.missing, 1)
  assert.equal(watcher.callbacks.length, 0)
  assert.equal(watcher.errors.length, 0)
  watcher.stop()
})

test('temporary connectivity failure retries monitoring until the saved job is returned', async () => {
  let requests = 0
  const watcher = monitor(async () => {
    if (++requests === 1) throw new TypeError('Failed to fetch')
    return { status: 'completed' }
  })
  await flush()
  assert.equal(watcher.errors.length, 1)
  await watcher.callbacks.shift()()
  assert.deepEqual(watcher.jobs, [{ status: 'completed' }])
  assert.equal(watcher.callbacks.length, 0)
  assert.equal(watcher.missing, 0)
  watcher.stop()
})

test('slow requests do not overlap and stopping clears the next timer', async () => {
  const pending = deferred()
  const watcher = monitor(() => pending.promise)
  await flush()
  assert.equal(watcher.callbacks.length, 0)
  pending.resolve({ status: 'running' })
  await flush()
  assert.equal(watcher.callbacks.length, 1)
  watcher.stop()
  assert.equal(watcher.callbacks.length, 0)
})

test('late responses after switching papers cannot update the old monitor', async () => {
  const pending = deferred()
  const watcher = monitor(() => pending.promise)
  watcher.stop()
  pending.resolve({ status: 'running' })
  await flush()
  assert.equal(watcher.jobs.length, 0)
  assert.equal(watcher.callbacks.length, 0)
})

test('an interrupted persisted job is displayed as terminal without polling again', async () => {
  const watcher = monitor(async () => ({ status: 'failed', interrupted: true, error: 'Backend stopped' }))
  await flush()
  assert.equal(watcher.jobs[0].interrupted, true)
  assert.equal(watcher.callbacks.length, 0)
  assert.equal(watcher.missing, 0)
  watcher.stop()
})

test('a separate details refresh failing with 404 does not discard a known running job', async () => {
  const watcher = monitor(async () => ({ status: 'running' }), {
    onJob: async () => { throw Object.assign(new Error('Details unavailable'), { status: 404 }) },
  })
  await flush()
  assert.equal(watcher.missing, 0)
  assert.equal(watcher.errors.length, 1)
  assert.equal(watcher.callbacks.length, 1)
  watcher.stop()
})


test('stopping remains polled until stopped is terminal', async () => {
  const replies = [{status:'stopping'}, {status:'stopped'}]
  const watcher = monitor(async () => replies.shift())
  await flush()
  assert.equal(watcher.callbacks.length, 1)
  await watcher.callbacks.shift()()
  assert.deepEqual(watcher.jobs.map(job => job.status), ['stopping','stopped'])
  assert.equal(watcher.callbacks.length, 0)
  watcher.stop()
})
