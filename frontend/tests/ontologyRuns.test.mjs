import assert from 'node:assert/strict'
import test from 'node:test'
import React, { act, useState } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'
import { createStaticHandler } from '../src/lib/staticApi.js'

const papers = [
  { id: 'a', title: 'Potato nutrition paper', status: 'completed', latest_job_id: 'job-a' },
  { id: 'b', title: 'Soil nutrients paper', status: 'not_started' },
]
const health = { capabilities: { execute: true }, model_ready: true, pipeline: {
  stage_status: { ontology_grounding: 'manual' },
  manual_grounding: { enabled: true, ready: true, model: 'ground-model', extraction_contract_version: 22 },
} }
const response = data => ({ ok: true, json: async () => data })
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

test('extraction-only policy hides grounding controls and explains the stopping point', async () => {
  const ui = await setup(async () => response([]))
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: [papers[1]],
      health: { ...health, pipeline: { stage_status: { ontology_grounding: 'paused' }, task_models: { extraction: 'selected-model' } } }, onRefresh() {} }))
    assert.match(ui.text(), /Run pipeline stops after extraction/)
    assert.match(ui.text(), /Run pipeline/)
    assert.doesNotMatch(ui.text(), /Ground saved claims/)
    assert.equal(ui.dom.window.document.querySelectorAll('input[role="switch"]').length, 1)
  } finally { await ui.close() }
})

test('manual grounding uses the saved extraction without claim checkboxes or IDs', async () => {
  const posts = []
  const claim = { candidate_id: 'one', subject: 'Treatment T', relation: 'increased', object: 'Trait Q',
    proposed_relation_mapping: 'exact', source_evidence_span: 'Treatment T increased Trait Q.' }
  const ui = await setup(async (path, options) => {
    if (options?.method === 'POST') {
      posts.push({ path, body: JSON.parse(options.body) })
      return response({ id: 'ground-job', status: 'queued', logs: [] })
    }
    if (path === '/api/pdfs/b/claims') return response({ claims: [{ candidate: claim }, {
      candidate: { ...claim, candidate_id: 'held', extraction_review_status: 'held' },
    }] })
    if (path === '/api/jobs/ground-job') return response({ id: 'ground-job', status: 'completed', logs: [{ message: 'Saved claims grounded' }] })
    return response([])
  })
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: [{ ...papers[1], status: 'completed',
      summary: { extracted: 2, ready_to_inspect: 1, extraction_contract_version: 22,
        extraction_coverage: { complete: true } } }], health, onRefresh() {} }))
    assert.match(ui.text(), /Ontology grounding \(manual\)/)
    assert.match(ui.text(), /ground-model/)
    assert.equal(ui.dom.window.document.querySelector('.run-claims').open, false)
    await ui.click('Ground saved claims')
    assert.equal(posts.length, 1)
    assert.equal(posts[0].path, '/api/pdfs/b/ground-claims')
    assert.equal('candidate_ids' in posts[0].body, false)
    assert.equal(posts[0].body.thinking.pathway_assembly, true)
    assert.match(ui.text(), /Saved claims grounded/)
  } finally { await ui.close() }
})

test('extract and ground starts only on click and shows all three mappings', async () => {
  const posts = []
  const base = '/api/ontology/single-stage'
  const run = { id: 'one', status: 'completed', claim_count: 1, completed_targets: 1, total_targets: 1 }
  const ui = await setup(async (path, options) => {
    if (options?.method === 'POST') {
      posts.push(path)
      return response({ ...run, status: 'queued' })
    }
    if (path === base + '/a/one?offset=0') {
      return response({ ...run, claims: [{ id: 'claim', subject: 'root', relation: 'is part of', object: 'root system',
        bindings: {
          subject: { label: 'root', iri: 'http://purl.obolibrary.org/obo/PO_0009005', mapping: 'exact' },
          relation: { label: 'part of', iri: 'http://purl.obolibrary.org/obo/BFO_0000050', mapping: 'exact' },
          object: { label: 'root system', iri: 'http://purl.obolibrary.org/obo/PO_0025025', mapping: 'exact' },
        } }], total: 1, limit: 20 })
    }
    return response(null)
  }, '/src/ontology/SingleStage.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: papers }))
    assert.deepEqual(posts, [])
    assert.match(ui.text(), /Local ontology grounding/)
    await ui.click('Run full PDF')
    assert.deepEqual(posts, [base + '/a'])
    assert.match(ui.text(), /PO:0009005/)
    assert.match(ui.text(), /BFO:0000050/)
    assert.match(ui.text(), /PO:0025025/)
  } finally { await ui.close() }
})

test('extract and ground optional verifier v2 toggle stays off by default and appends verifier=true when checked', async () => {
  const posts = []
  const base = '/api/ontology/single-stage'
  const run = { id: 'ver-1', status: 'completed', verifier: true, claim_count: 0, completed_targets: 1, total_targets: 1 }
  const ui = await setup(async (path, options) => {
    if (options?.method === 'POST') {
      posts.push(path)
      return response({ ...run, status: 'queued' })
    }
    if (path === base + '/a/ver-1?offset=0') return response({ ...run, claims: [], total: 0, limit: 20 })
    return response(null)
  }, '/src/ontology/SingleStage.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: [
      { ...papers[0], name: 'Kirchgesser_2023_Potato_Root_Architecture_Phosphorus.pdf' },
    ] }))
    const checkbox = ui.dom.window.document.querySelector('input[type="checkbox"]')
    assert.ok(checkbox)
    assert.equal(checkbox.checked, false)
    await act(async () => { checkbox.click() })
    await ui.flush()
    assert.equal(checkbox.checked, true)
    await ui.click('Test 8 passages')
    assert.deepEqual(posts, [base + '/a?sample=true&verifier=true'])
    assert.match(ui.text(), /verifier v2/)
  } finally { await ui.close() }
})

test('Kirchgesser sample runs the extraction and local grounding workflow once', async () => {
  const posts = []
  const base = '/api/ontology/single-stage'
  const run = { id: 'sample', status: 'completed', sample: true, completed_targets: 8,
    total_targets: 8, claim_count: 0 }
  const ui = await setup(async (path, options) => {
    if (options?.method === 'POST') {
      posts.push(path)
      return response({ ...run, status: 'queued' })
    }
    if (path === base + '/a/sample?offset=0') return response({ ...run, cases: [
      { id: 'frozen-a', title: 'Uptake comparisons', page: 10, source: 'A source passage.',
        review: 'Check the comparators.', claims: [] },
    ] })
    return response(null)
  }, '/src/ontology/SingleStage.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { pdfs: [
      { ...papers[0], name: 'Kirchgesser_2023_Potato_Root_Architecture_Phosphorus.pdf' },
    ] }))
    assert.deepEqual(posts, [])
    await ui.click('Test 8 passages')
    assert.deepEqual(posts, [base + '/a?sample=true'])
    assert.match(ui.text(), /8\/8 passages/)
    assert.match(ui.text(), /Uptake comparisons/)
    assert.match(ui.text(), /Check the comparators/)
    assert.equal(ui.dom.window.document.querySelectorAll('details h3').length, 0)
  } finally { await ui.close() }
})

test('appendix switches between current extraction and entity grounding without API calls or version labels', async () => {
  const calls = []
  const ui = await setup(async path => { calls.push(path); return response({}) }, '/src/ontology/Appendix.jsx')
  try {
    await ui.render(React.createElement(ui.Runs))
    const stageButtons = [...ui.dom.window.document.querySelectorAll('.appendix-stage-picker button')].map(b => b.textContent.trim())
    assert.deepEqual(stageButtons, [
      '1. Claim extraction',
      '2. Entity grounding',
      '3. Cross-paper merging',
    ])
    const output = () => JSON.parse(ui.dom.window.document.querySelector('[aria-label="LLM output JSON"]').textContent)
    assert.equal(output().claims[0].predicate_alias, 'P7')
    assert.equal(output().claims[0].relation, undefined)
    assert.equal(output().claims[0].relation_anchor, 'consists of')
    assert.equal(ui.dom.window.document.querySelector('[aria-label="Approved relations reference"]'), null)
    assert.match(ui.text(), /Organ O Trait Q/)
    assert.match(ui.text(), /Comparison without an approved property/)
    assert.match(ui.text(), /"claims": \[\]/)
    const promptDetails = ui.dom.window.document.querySelector('.appendix-section-box')
    assert.ok(promptDetails && promptDetails.open === false)
    assert.ok(ui.dom.window.document.querySelector('[aria-label="How this stage works"]'))
    assert.equal(ui.dom.window.document.querySelector('[aria-label="Entity candidate retrieval pipeline"]'), null)
    assert.equal(ui.dom.window.document.querySelector('[aria-label="Entity retrieval worked example"]'), null)
    assert.equal(ui.dom.window.document.querySelector('.appendix-stage-picker [aria-pressed="true"]').textContent.trim(), '1. Claim extraction')
    assert.doesNotMatch(ui.text(), /\b(?:v\d+|Contract v\d+|Catalogue profile v\d+|closed-ro-bfo-\d+-v\d+|closed-predicate-entity-grounding-v\d+)\b|Historical extraction|Historical grounding/i)
    await ui.click('2. Entity grounding')
    assert.equal(ui.dom.window.document.querySelector('.appendix-stage-picker [aria-pressed="true"]').textContent.trim(), '2. Entity grounding')
    const retrievalBox = ui.dom.window.document.querySelector('[aria-label="Entity candidate retrieval pipeline"]')
    assert.ok(retrievalBox)
    assert.match(retrievalBox.textContent, /Candidate Class Retrieval.*Reciprocal Rank Fusion.*14 candidates/s)
    const workedExample = ui.dom.window.document.querySelector('[aria-label="Entity retrieval worked example"]')
    assert.ok(workedExample)
    assert.match(workedExample.textContent, /0 LLM calls.*PECO:0001046.*0\.0432.*TO:0000656/s)
    assert.equal(output().results[0].claim_id, 'C1')
    assert.equal(output().results[0].relation, undefined)
    assert.match(ui.text(), /PO:0025025 root system/)
    await ui.click('3. Cross-paper merging')
    assert.equal(ui.dom.window.document.querySelector('.appendix-stage-picker [aria-pressed="true"]').textContent.trim(), '3. Cross-paper merging')
    const mergeRules = ui.dom.window.document.querySelector('[aria-label="Cross-paper merging methodology"]')
    assert.ok(mergeRules)
    assert.match(mergeRules.textContent, /CDNO:0000018.*P8⁻¹.*MONDO:0005300.*blocked_cross_domain/s)
    assert.doesNotMatch(ui.text(), /\b(?:v\d+|Contract v\d+|Catalogue profile v\d+)\b/i)
    assert.deepEqual(calls, [])
    assert.doesNotMatch(ui.text(), /\b(?:v\d+|Contract v\d+|Catalogue profile v\d+|closed-ro-bfo-\d+-v\d+|closed-predicate-entity-grounding-v\d+)\b|Historical extraction|Historical grounding/i)
    await ui.click('1. Claim extraction')
    assert.equal(output().claims[0].predicate_alias, 'P7')
    assert.deepEqual(calls, [])
  } finally { await ui.close() }
})

test('static Runs shows paginated claims and evidence only after expanding the disclosures', async () => {
  const claims = Array.from({ length: 67 }, (_, index) => {
    const hold = (index >= 3 && index <= 5) || index >= 42
    const correlation = index === 1
    return { candidate: {
      candidate_id: `claim-${index}`,
      subject: `Source finding ${index}`,
      relation: correlation ? 'correlated with' : 'led to an elevation in',
      relation_trigger: correlation ? 'correlated with' : 'led to an elevation in',
      object: 'Trait Q',
      predicate_alias: correlation ? 'P5' : 'P1',
      proposed_relation_id: correlation ? 'RO:0002610' : 'RO:0019001',
      proposed_relation_display_label: correlation ? 'correlated with' : 'positively regulates characteristic',
      qualifiers: { condition: 'under Condition C' },
      source_page: 1,
      source_evidence_sentence_numbers: [13],
      source_evidence_span: 'Verbatim source evidence.',
      extraction_review_status: hold ? 'held' : 'not_flagged',
      extraction_warnings: hold ? [{ code: 'possible_non_process_subject', message: 'Verify the subject in context.' }] : [],
    } }
  })
  const paper = {
    id: 'static-paper',
    title: 'Published paper',
    status: 'completed',
    summary: { extracted: claims.length, ready_to_inspect: 39, held_for_review: 28 },
  }
  const calls = []
  const handle = createStaticHandler(async path => {
    if (path === 'pdfs/static-paper/logs.json') return []
    if (path === 'pdfs/static-paper/claims.json') return { claims, total: claims.length }
    throw new Error(`Unexpected static data path: ${path}`)
  })
  const ui = await setup(async path => {
    calls.push(path)
    return response(await handle(path.slice('/api'.length)))
  })
  try {
    await ui.render(React.createElement(ui.Runs, {
      pdfs: [paper],
      health: {
        capabilities: { read_only: true, execute: false },
        pipeline: {
          stage_status: { ontology_grounding: 'paused' },
          manual_grounding: { enabled: false },
          task_models: { extraction: 'selected-model' },
        },
      },
      onRefresh() {},
    }))
    await ui.flush()
    const list = ui.dom.window.document.querySelector('.run-claims')
    const claimRows = () => [...list.querySelectorAll('.run-claim')]
    async function toggle(disclosure) {
      await act(async () => { disclosure.querySelector('summary').click() })
      await ui.flush()
    }
    assert.equal(list.querySelector('summary').textContent, 'Claims')
    assert.equal(list.open, false)
    assert.equal(claimRows().length, 0)
    assert.deepEqual(calls.filter(path => path.includes('/claims')), [])
    await toggle(list)
    assert.match(ui.text(), /1–25 of 67/)
    assert.equal(claimRows().length, 25)
    assert.ok(claimRows().every(claim => !claim.open))
    assert.match(claimRows()[1].querySelector('summary').textContent, /↔ correlated with ↔/)
    const first = claimRows()[0]
    await toggle(first)
    assert.equal(first.open, true)
    assert.equal(list.open, true)
    assert.match(first.querySelector('summary').textContent, /Source finding 0 → led to an elevation in → Trait Q/)
    assert.match(first.querySelector('.claim-qualifiers').textContent, /under Condition C/)
    assert.match(first.textContent, /Page 1 · sentence 13/)
    assert.equal(first.querySelector('blockquote').textContent, 'Verbatim source evidence.')
    await toggle(claimRows()[3])
    assert.match(claimRows()[3].querySelector('[role="note"]').textContent, /Held for review.*Verify the subject in context/)
    assert.doesNotMatch(ui.text(), /Extraction summary|Saved extracted claims and source passages|Ready to inspect/)
    assert.equal(ui.dom.window.document.querySelector('select[aria-label="Claim review filter"]'), null)
    await ui.click('Next')
    assert.match(ui.text(), /26–50 of 67/)
    assert.ok(claimRows().every(claim => !claim.open))
    await toggle(list)
    assert.equal(claimRows().length, 0)
    assert.deepEqual(calls.filter(path => path.includes('/claims')), ['/api/pdfs/static-paper/claims'])
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

test('reasoning panel renders cross-paper merged chains with downward flow and side alignment arrows', async () => {
  const merged = {
    policy: 'qualified-two-arrow-merge-v1',
    summary: {
      eligible_claims: 3,
      cross_paper_chains: 1,
      compatible_chains: 0,
      context_shift_chains: 1,
      exact_only_chains: 0,
      broader_included_chains: 1,
      hierarchy_chains: 1,
      convergent_hubs: 1,
      blocked_cross_domain_pairs: 1,
      evidence_gaps: 1,
    },
    chains: [{
      id: 'chain-1',
      domain: 'human_animal',
      hop_count: 2,
      paper_count: 2,
      paper_trail: ['Calvo 2023', 'Lampen 2026'],
      uses_broader: true,
      has_hierarchy: true,
      has_context_shift: true,
      context_shift_count: 1,
      hops: [
        {
          candidate_id: 'c1',
          document_key: 'Calvo_2023',
          paper_short: 'Calvo 2023',
          domain: 'human_animal',
          predicate_alias: 'P7',
          relation_label: 'has part',
          subject: { mention: 'processed foods', id: 'FOODON:03311737', label: 'processed food', mapping: 'broader' },
          object: { mention: 'phosphate additives', id: 'FOODON:03412972', label: 'food additive', mapping: 'broader' },
          qualifiers: { population: 'US adults' },
          source_page: 2,
          quote: 'Processed foods often contain phosphate additives.',
        },
        {
          candidate_id: 'c2',
          document_key: 'Lampen_2026',
          paper_short: 'Lampen 2026',
          domain: 'human_animal',
          predicate_alias: 'P3',
          relation_label: 'causally influences',
          subject: { mention: 'chemical food components', id: 'FOODON:03411041', label: 'chemical food component', mapping: 'broader' },
          object: { mention: 'serum phosphate', id: 'CMO:0000542', label: 'serum phosphate level', mapping: 'exact' },
          qualifiers: { population: '43 CKD children' },
          source_page: 4,
          quote: 'Added phosphate raises serum phosphate.',
        },
      ],
      connectors: [{
        kind: 'alignment',
        arrow: '↔',
        alignment_type: 'subclass',
        explanation: 'FOODON:03412972 (food additive) is a subclass of FOODON:03411041 (chemical food component)',
        mapping_pair: 'broader ↔ broader',
        uses_broader: true,
        context_tier: 'context_shift',
        differing_qualifiers: [{ field: 'population', left: 'US adults', right: '43 CKD children' }],
      }],
    }],
    convergent_hubs: [{
      id: 'hub-1',
      domain: 'human_animal',
      curie: 'CMO:0000542',
      label: 'serum phosphate level',
      paper_count: 2,
      papers: ['Calvo 2023', 'Lampen 2026'],
      uses_broader: true,
      incoming: [{
        candidate_id: 'c2',
        paper_short: 'Lampen 2026',
        predicate_alias: 'P3',
        relation_label: 'causally influences',
        subject_id: 'FOODON:03411041',
        subject_label: 'chemical food component',
        subject_mapping: 'broader',
        object_mapping: 'exact',
        qualifiers: { population: '43 CKD children' },
      }],
    }],
    blocked_cross_domain: [{
      left_paper: 'Chea 2024',
      left_domain: 'plant',
      left_entity: 'GO:0006793 (phosphorus metabolic process)',
      right_paper: 'Lampen 2026',
      right_domain: 'human_animal',
      right_entity: 'GO:0006793 (phosphorus metabolic process)',
      reason: 'Blocked cross-domain join (plant ↔ human_animal)',
    }],
    evidence_gaps: [{
      id: 'gap-potato-to-human-intake',
      from_node: 'FOODON:03315354 (potato)',
      to_node: 'MAXO:0001018 (nutrition intake)',
      summary: 'No eligible claim directly connects potato tubers to human dietary intake.',
    }],
  }
  const ui = await setup(async () => response({ status: 'paused', facts: [], total: 0, merged_chains: merged }), '/src/ontology/Reasoning.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0 }))
    const txt = ui.text()
    const doc = ui.dom.window.document
    assert.match(txt, /↓ flow \(black\)/)
    assert.match(txt, /↔ class link \(blue\)/)
    assert.match(txt, /Calvo 2023 → Lampen 2026/)
    assert.match(txt, /context differs/)
    assert.match(txt, /US adults ↔ 43 CKD children/)
    const sourceToggle = doc.querySelector('.merge-source-details')
    assert.ok(sourceToggle && sourceToggle.open === false)
    assert.match(sourceToggle.querySelector('summary').textContent, /Source text \(p\. 2\)/)
    assert.match(txt, /Shared hubs \(1\)/)
    assert.match(txt, /Gap: FOODON:03315354 \(potato\) → MAXO:0001018 \(nutrition intake\)/)
    const svg = doc.querySelector('svg[aria-label^="Chain"]')
    assert.ok(svg)
    assert.equal(svg.querySelectorAll('rect').length, 4)
    assert.ok(svg.querySelector('line[marker-end]'))
    assert.match(svg.textContent, /FOODON:03412972/)
    assert.match(svg.textContent, /subclass of/)
    const broaderCheck = doc.querySelectorAll('input[type="checkbox"]')[0]
    assert.equal(broaderCheck.checked, true)
    await act(async () => { broaderCheck.click() })
    await ui.flush()
    assert.match(ui.text(), /Chains \(0\)/)
  } finally { await ui.close() }
})

test('mapping review defaults to in-scope assertions without the review-only checkbox filter', async () => {
  const urls = []
  const ui = await setup(async path => { urls.push(path); return response({ assertions: [], total: 0, offset: 0, limit: 15 }) }, '/src/ontology/MappingReview.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0, onRefresh() {}, pdfs: [] }))
    assert.equal(ui.dom.window.document.querySelector('input[type="checkbox"]'), null)
    assert.doesNotMatch(ui.text(), /Needs mapping or scope review/)
    assert.ok(urls.some(u => u.startsWith('/api/ontology/assertions?') && u.includes('scope=in_scope')))
    assert.ok(urls.every(u => !u.includes('review_only=')))
    assert.equal(ui.dom.window.document.querySelector('select[aria-label="Assertion scope"]'), null)
  } finally { await ui.close() }
})

test('mapping review shows an extracted property separately from entity mappings', async () => {
  const row = {
    candidate_id: 'bound', document_key: 'Study A', scope: 'in_scope', scope_reason: 'Relevant.',
    original_claim: { subject: 'Organism A', relation: 'produces', object: 'Compound B',
      relation_trigger: 'synthesized', source_evidence_span: 'Organism A synthesized Compound B.' },
    bindings: {
      subject: { mapping: 'exact', denotation: 'concept', label: 'Organism A' },
      relation: { mapping: 'exact', label: 'produces', iri: 'http://purl.obolibrary.org/obo/RO_0003000',
        fixed_from_extraction: true, admission_reason: 'Endpoint type review.' },
      object: { mapping: 'unresolved', denotation: 'concept' },
    },
  }
  const ui = await setup(async () => response({ assertions: [row], total: 1, offset: 0, limit: 15 }), '/src/ontology/MappingReview.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0, canReview: true, onRefresh() {}, pdfs: [] }))
    assert.match(ui.text(), /Property selected during extraction.*produces/)
    assert.match(ui.text(), /Endpoint type review/)
    assert.match(ui.text(), /Source wording synthesized/)
    assert.doesNotMatch(ui.text(), /Edit ontology alignment|Save mapping review/)
    assert.deepEqual([...ui.dom.window.document.querySelectorAll('tbody tr td:first-child')].map(cell => cell.textContent),
      ['subject', 'object'])
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
    assert.doesNotMatch(ui.text(), /Edit ontology alignment|Save mapping review/)
  } finally { await ui.close() }
})

test('mapping-only conflicts show mapping details without extraction approval controls', async () => {
  const row = { candidate_id: 'c', document_key: 'Study A', revision: 'a'.repeat(64), scope: 'in_scope',
    original_claim: { subject: 'Entity Q', relation: 'affects', object: 'Trait R' },
    extraction_review_status: 'not_flagged', extraction_warnings: [],
    mapping_consistency_warnings: [{ role: 'subject', message: 'Conflicting contextual mappings.',
      decisions: [{ candidate_id: 'c', iri: 'urn:reference', mapping: 'broader', denotation: 'concept' }] }],
    bindings: Object.fromEntries(['subject', 'relation', 'object'].map(role => [role, { mapping: 'unresolved', reason: 'Review mapping.' }])) }
  const ui = await setup(async () => response({ assertions: [row], total: 1, offset: 0, limit: 15 }), '/src/ontology/MappingReview.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0, canReview: true, pdfs: [], onRefresh() {} }))
    assert.match(ui.text(), /Grounding consistency: needs mapping review/)
    assert.match(ui.text(), /broader.*concept/)
    assert.doesNotMatch(ui.text(), /Extraction review:|Accept extraction|Reject extraction/)
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
    assert.doesNotMatch(ui.text(), /Unique entities extracted from source papers/)
    assert.ok(paths.every(p => p.startsWith('/api/ontology/entities?')))
    await ui.click('Trait Q')
    assert.equal(selected, 'urn:test:trait-q')
    assert.equal(ui.dom.window.document.querySelector('canvas'), null)
  } finally { await ui.close() }
})

test('entity filter shows all category totals and keeps the selected total while loading', async () => {
  const pending = deferred()
  const counts = { grounded: 7, classified: 57, vocabulary_gap: 580, alignment_review: 67, classification: 50, review: 17, all: 654 }
  const ui = await setup(async path => {
    if (path.includes('status=vocabulary_gap')) return pending.promise
    return response({ entities: [], counts: {}, status_counts: counts, total: 0, limit: 20, offset: 0 })
  }, '/src/ontology/Entities.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0, onTerm() {} }))
    const select = ui.dom.window.document.querySelector('select[aria-label="Entity mapping status"]')
    assert.deepEqual([...select.options].map(o => o.textContent), [
      'All classified — exact or broader (57)',
      'Exact concept matches (7)', 'Broader classifications (50)', 'Related / narrower — review (17)', 'Unresolved (580)', 'All entities (654)',
    ])
    assert.equal(select.selectedOptions[0].textContent, 'All classified — exact or broader (57)')
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

test('relation registry shows grounded summary, status counts and scope filter', async () => {
  const paths = []
  const counts = { grounded: 1, classification: 1, classified: 2, unresolved: 1, unused: 9, all: 3 }
  const summary = {
    total_claims: 10, grounded_mentions: 4, broader_mentions: 2, classified_mentions: 6, classified_properties: 1, unresolved_mentions: 4,
    used_properties: 1, catalog_properties: 10, unused_properties: 9, unresolved_phrases: 2,
  }
  const ui = await setup(async path => {
    paths.push(path)
    return response({
      stale: false,
      total: 1,
      limit: 20,
      offset: 0,
      summary,
      status_counts: counts,
      mention_counts: { all: 10, grounded: 4, classification: 2, classified: 6, unresolved: 4 },
      extracted_claim_relations: 10, scope_counts: { in_scope: 10 }, represented_claims: 6,
      relations: [
        {
          id: 'rel-1',
          status: 'grounded',
          mapping: 'exact',
          iri: 'http://purl.obolibrary.org/obo/RO_0002304',
          term_id: 'RO:0002304',
          label: 'causally upstream of, positive effect',
          definition: 'Positive causal relation.',
          raw_variants: [{ label: 'increases', count: 3 }, { label: 'enhances', count: 1 }],
          occurrence_count: 6, exact_count: 4, broader_count: 2,
          occurrences: [
            { id: 'occ-1', document_key: 'Study A', source_page: 2, claim_text: 'P → increases → yield', reason: 'Positive effect.' },
          ],
        },
      ],
    })
  }, '/src/ontology/Relations.jsx')
  let selected
  try {
    await ui.render(React.createElement(ui.Runs, { revision: 0, onTerm: iri => { selected = iri } }))
    assert.doesNotMatch(ui.text(), /extracted claim relations|await grounding|Scope breakdown|ontology properties representing|Positive effect\./)
    assert.match(ui.text(), /increases \(3\); enhances \(1\)/)
    assert.ok(paths.every(p => p.startsWith('/api/ontology/relations?')))
    assert.equal(ui.dom.window.document.querySelector('select[aria-label="Relation mapping status"]'), null)
    const headers = [...ui.dom.window.document.querySelectorAll('thead th')].map(th => th.textContent.trim())
    assert.deepEqual(headers, ['Ontology property / unresolved wording', 'Exact'])
    await ui.click('causally upstream of, positive effect')
    assert.equal(selected, 'http://purl.obolibrary.org/obo/RO_0002304')
    assert.equal(ui.dom.window.document.querySelector('select[aria-label="Relation claim scope"]'), null)
    assert.ok(paths.every(p => p.includes('scope=in_scope')))
  } finally { await ui.close() }
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
    assert.equal(posts[0].body.thinking.extraction, true)
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
    assert.equal(switches[0].checked, true)
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

test('retired Subset bookmarks open Ontology and keep Runs available', async () => {
  const ui = await setup(async path => {
    if (path === '/api/health') return response({ capabilities: { execute: true } })
    if (path.startsWith('/api/ontology/hierarchy?')) return response({ terms: [], total: 0 })
    return response(path === '/api/pdfs' ? [] : {})
  }, '/src/ontology/OntologyApp.jsx')
  try {
    ui.dom.window.history.replaceState(null, '', '/?view=subset')
    await ui.render(React.createElement(ui.Runs))
    await ui.flush()
    const nav = ui.dom.window.document.querySelector('nav[aria-label="Application views"]')
    assert.equal(nav.querySelector('[aria-current="page"]').textContent, 'Ontology')
    assert.match(nav.textContent, /Runs/)
    assert.doesNotMatch(nav.textContent, /Subset/)
    assert.equal(new URL(ui.dom.window.location.href).searchParams.get('view'), null)
  } finally { await ui.close() }
})

test('source definition details preserve verbatim text and provenance without model requests', async () => {
  const calls = []
  const ui = await setup(async path => { calls.push(path); return response({}) }, '/src/ontology/DefinitionContext.jsx')
  try {
    await ui.render(React.createElement(ui.Runs, { contexts: { object: [
      { page: 3, packet_number: 1, sentence_number: 59, text: 'Material Q levels were 1 mg/L as H2QO4−.' },
    ] } }))
    assert.match(ui.text(), /Document definitions used for grounding/)
    assert.match(ui.text(), /Page 3.*sentence 59.*packet 1/)
    assert.match(ui.text(), /1 mg\/L as H2QO4−/)
    assert.deepEqual(calls, [])
  } finally { await ui.close() }
})
