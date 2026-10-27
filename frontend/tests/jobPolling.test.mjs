import assert from 'node:assert/strict'
import test from 'node:test'
import { watchJob } from '../src/lib/jobPolling.js'

const flush = () => new Promise(resolve => setImmediate(resolve))
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

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
