import assert from 'node:assert/strict'
import { createServer } from 'vite'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const server = await createServer({ server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true, entries: [] }, appType: 'custom' })
try {
  const { ActivityLog } = await server.ssrLoadModule('/src/components/workspace/WorkbenchPanels.jsx')
  const { filterActivity } = await server.ssrLoadModule('/src/lib/activity.js')
  const logs = [{level:'warning', stage:'article_fetch', message:'fetch failed'}, {level:'info', stage:'external_validation', message:'judge ready'}]
  assert.equal(filterActivity(logs, 'article_fetch', 'attention').length, 1)
  assert.equal(filterActivity(logs, 'external_validation', 'attention').length, 0)
  assert.equal(filterActivity(logs, 'all', 'all').length, 2)
  assert.equal(filterActivity([{level:'info', message:'Ranked 4 passages: historical paper'}], 'chunk_preparation').length, 1)
  const activity = renderToStaticMarkup(React.createElement(ActivityLog, {logs, running:false}))
  assert.match(activity, /Filter activity by stage/)
  assert.match(activity, /All stages/)
  assert.match(activity, /Paper fetching/)
  assert.match(activity, /External judgement/)
  const liveActivity = renderToStaticMarkup(React.createElement(ActivityLog, { logs: [], running: true,
    progress: { task: 'paper external: claim / paper-a', model: 'opencode-go/deepseek-v4.1-flash',
      status: 'waiting_for_provider', started_at: new Date().toISOString(), paper_count: 1, timeout_seconds: 1200 } }))
  assert.match(liveActivity, /paper external: claim/)
  assert.match(liveActivity, /deepseek-v4.1-flash/)
  const { StopRunButton } = await server.ssrLoadModule('/src/components/workspace/WorkbenchPanels.jsx')
  const stopButton = props => renderToStaticMarkup(React.createElement(StopRunButton, props))
  assert.equal(stopButton({active:false}), '')
  assert.match(stopButton({active:true, stopping:false}), /Stop pipeline/)
  assert.match(stopButton({active:true, stopping:true}), /disabled/)
  assert.match(stopButton({active:true, stopping:true}), /Stopping/)
  const { RunSetup } = await server.ssrLoadModule('/src/components/workspace/WorkbenchPanels.jsx')
  const options = { max_pages: 0, max_claims: 0, articles_per_claim: 8, claims_per_packet: 4,
    extraction_mode: 'target_context', extraction_target_words: 800, extraction_generation_budget: 24576 }
  const taskModels = { extraction: 'opencode-go/deepseek-v4.1-flash', source: 'opencode-go/deepseek-v4.1-flash',
    query: 'opencode-go/mimo-v2.6-flash', external: 'opencode-go/mimo-v2.6-pro', pathway_assembly: 'opencode-go/mimo-v2.6-flash' }
  const render = current => renderToStaticMarkup(React.createElement(RunSetup, {
    health: { pipeline: { task_models: taskModels, external_phase_models: {paper: taskModels.source, synthesis: taskModels.external} } },
    options: current, onOptions: () => {}, open: true, onToggle: () => {},
  }))
  const { defaultThinking, resumeWithThinking } = await server.ssrLoadModule('/src/lib/thinking.js')
  assert.ok(Object.values(defaultThinking()).every(value => value === false))
  const resumed = resumeWithThinking({max_pages:2, extraction_mode:'whole_packet', thinking:defaultThinking()}, {thinking:{external:false}})
  assert.equal(resumed.max_pages, 2)
  assert.equal(resumed.thinking.external, false)
  assert.equal(resumed.thinking.extraction, false)
  const target = render(options)
  assert.equal((target.match(/role="switch"/g) || []).length, 5)
  assert.equal((target.match(/aria-checked="false"/g) || []).length, 5)
  assert.match(target, /Thinking defaults to Off for every stage/)
  assert.match(target, /Per-paper judgement model/)
  assert.match(target, /Cross-paper synthesis model/)
  assert.match(target, /DeepSeek Flash judges each paper/)
  assert.doesNotMatch(target, /<h3>Model thinking<\/h3>/)
  assert.equal((target.match(/<h3>Assigned models<\/h3>/g) || []).length, 1)
  for (const [key, label] of (await server.ssrLoadModule('/src/lib/thinking.js')).THINKING_STAGES) {
    const group = target.match(new RegExp(`<div class="assigned-model" role="group" aria-label="${label} model settings">(.*?)</div>`))?.[1]
    assert.ok(group, `Missing combined model/thinking group for ${label}`)
    assert.ok(group.includes(taskModels[key]))
    assert.ok(group.includes(`aria-label="${label} thinking" aria-checked="false"`))
  }
  const onMarkup = render({...options, thinking:{external:true}})
  assert.match(onMarkup, /aria-label="External judgement thinking" aria-checked="true"/)
  assert.equal((onMarkup.match(/aria-checked="false"/g) || []).length, 4)

  assert.match(target, /<option(?=[^>]*value="target_context")(?=[^>]*selected)[^>]*>/)
  assert.match(target, /Target size \(words\)/)
  assert.match(target, /value="800"/)
  assert.match(target, /Generation budget \(tokens\)/)
  assert.match(target, /value="24576"/)
  assert.match(target, /Claims per extraction call/)
  assert.match(target, /More calls may be needed/)
  const whole = render({ ...options, extraction_mode: 'whole_packet' })
  assert.match(whole, /<option(?=[^>]*value="whole_packet")(?=[^>]*selected)[^>]*>/)
  assert.doesNotMatch(whole, /Target size \(words\)/)
  assert.match(whole, /Select claims from anywhere/)
  assert.match(whole, /Generation budget/)
  assert.match(whole, /PDF pages/)
  assert.match(whole, /Claims to validate/)
  assert.match(whole, /Search papers per claim/)
  const uncapped = render({ ...options, claims_per_packet: 0 })
  assert.match(uncapped, /0 = all eligible claims/)
  const { default: PathwayPanel } = await server.ssrLoadModule('/src/components/graph/PathwayPanel.jsx')
  const renderPaths = props => renderToStaticMarkup(React.createElement(PathwayPanel, props))
  const edge = { id: 'edge-1', source_label: 'Disease D', target_label: 'wheat grains', subject: 'Disease D',
    relation: 'affects', object: 'wheat grains', paper: 'Paper A', quote: 'Disease D affects wheat grains.',
    source_status: 'entailed', external_status: 'external_evidence_not_found', qualifiers: { condition: 'infection' } }
  const edge2 = { ...edge, id: 'edge-2', source_label: 'wheat grains', target_label: 'mycotoxins',
    subject: 'These infected grains', relation: 'has_contaminant', object: 'mycotoxins', paper: 'Paper B',
    quote: 'These infected grains may contain mycotoxins.', certainty: 'conditional' }
  const pathMarkup = renderPaths({ data: { counts: { claims: 2, entities: 3, papers: 2 },
    paths: [[edge, edge2]], stale: true, max_hops: 6, has_more: true }, busy: false })
  assert.match(pathMarkup, /Global pathway assembly/)
  assert.match(pathMarkup, /Original claims and passages/)
  assert.match(pathMarkup, /These infected grains may contain mycotoxins/)
  assert.match(pathMarkup, /external evidence not found/)
  assert.match(pathMarkup, /infection/)
  assert.match(pathMarkup, /Saved claims have changed/)
  assert.match(pathMarkup, /Show more paths/)
  assert.doesNotMatch(pathMarkup, /validated causal chain/)
  assert.match(render({ max_pages: 0 }), /<option(?=[^>]*value="target_context")(?=[^>]*selected)[^>]*>/)
} finally {
  await server.close()
}
