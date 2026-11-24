import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createStaticHandler, assertionEligible, extractionReviewStatus, classificationComplete, classificationCounts } from '../src/lib/staticApi.js'

test('a complete measured comparison keeps a grounding gap in its predicate without entering reasoning', () => {
  const row = {
    scope: 'in_scope', original_claim: { measured_comparison: { direction: 'higher' } },
    comparison_assessment: { complete: true },
    bindings: { object: { iri: 'trait', mapping: 'exact' }, relation: { mapping: 'unresolved' } },
  }
  const terms = { trait: { kind: 'class', selectable: true } }
  const counts = classificationCounts([row], terms)
  assert.equal(counts.classified_endpoint_mentions, 1)
  assert.equal(counts.complete_measured_comparisons, 1)
  assert.equal(counts.classified_claims, 0)
  assert.equal(counts.classification_review_claims, 0)
  assert.equal(assertionEligible(row), false)
  row.extraction_warnings = [{ code: 'possible_bundled_outcomes' }]
  assert.equal(classificationCounts([row], terms).complete_measured_comparisons, 0)
})

test('earlier grounding and consistency policies remain stale without changing saved mappings', async () => {
  const original = { candidate_id: 'c1', document_key: 'doc', scope: 'in_scope',
    original_claim: { subject: 'root', relation: 'part of', object: 'root system' },
    policy: 'established-only-context-grounding-v17',
    classification_policy: 'ontology-classification-coverage-v6',
    eligibility_policy: 'grounded-predicate-eligibility-v7',
    consistency_policy: 'document-context-interpretation-v2', bindings: {} }
  const files = { 'ontology-assertions.json': { assertions: [original] }, 'ontology-catalog.json': { terms: {} } }
  const old = createStaticHandler(async path => files[path])
  assert.equal((await old('/ontology/assertions')).stale, true)
  const current = createStaticHandler(async path => path === 'ontology-assertions.json'
    ? { assertions: [{ ...original, consistency_policy: 'document-context-interpretation-v3' }] } : files[path])
  assert.equal((await current('/ontology/assertions')).stale, false)
  assert.equal(original.consistency_policy, 'document-context-interpretation-v2')
})

test('property grouping uses the live conformance fixture and claim-level counts', async () => {
  const fixture = JSON.parse(readFileSync(new URL('../../tests/fixtures/relation_registry_v16.json', import.meta.url), 'utf8'))
  const rows = fixture.cases.map(c => ({ candidate_id: c.id, document_key: c.document, scope: c.scope,
    original_claim: { subject: 'Entity Q', relation: c.relation, object: 'Entity R', qualifiers: {} },
    bindings: { relation: { iri: c.iri, mapping: c.mapping, reason: 'Fixture mapping.' } } }))
  const inventory = [...rows.map(r => ({ candidate_id: r.candidate_id, document_key: r.document_key })), fixture.pending]
  const files = { 'ontology-assertions.json': { assertions: rows, extraction_inventory: inventory },
    'ontology-catalog.json': { terms: fixture.terms } }
  const handle = createStaticHandler(async path => files[path])
  const base = '/ontology/relations?group_by=property&status=classified'
  const data = await handle(base)
  assert.deepEqual(data.mention_counts, { all: 5, grounded: 2, classification: 1, classified: 3, unresolved: 2 })
  assert.equal(data.extracted_claim_relations, 8)
  assert.equal(data.awaiting_grounding_claims, 1)
  assert.equal(data.total, 1)
  assert.equal(data.represented_claims, 3)
  assert.equal(data.relations[0].exact_count, 2)
  assert.equal(data.relations[0].broader_count, 1)
  assert.deepEqual(new Set(data.relations[0].occurrences.map(o => o.mapping)), new Set(['exact', 'broader']))
  const unresolved = await handle('/ontology/relations?group_by=property&status=unresolved')
  assert.equal(unresolved.total, 1)
  assert.equal(unresolved.represented_claims, 2)
  const searched = await handle(`${base}&q=directly`)
  assert.equal(searched.mention_counts.classified, 1)
  assert.equal(searched.relations[0].exact_count, 0)
  assert.equal((await handle('/ontology/relations?group_by=property&status=grounded')).represented_claims, 2)
  assert.equal((await handle(`${base}&scope=all`)).mention_counts.all, 7)
  assert.equal((await handle(`${base}&document=Paper%20B`)).mention_counts.all, 2)
  assert.deepEqual((await handle(`${base}&offset=1&limit=1`)).relations, [])
  const older = createStaticHandler(async path => path === 'ontology-assertions.json' ? { assertions: rows } : files[path])
  assert.equal((await older(base)).extracted_claim_relations, null)
})

test('historical exact grouping properties never grant eligibility', () => {
  const row = { scope: 'in_scope', bindings: {
    subject: { mapping: 'exact', iri: 'termA' }, object: { mapping: 'exact', iri: 'termB' },
    relation: { mapping: 'exact', iri: 'http://purl.obolibrary.org/obo/RO_0002410' } } }
  assert.equal(assertionEligible(row), false)
})

test('live and static classification use the shared conformance cases', () => {
  const fixture = JSON.parse(readFileSync(new URL('../../tests/fixtures/classification_policy_v1.json', import.meta.url), 'utf8'))
  const terms = Object.fromEntries([...fixture.allowed_classes.map(iri => [iri, { selectable: true, kind: 'class' }]),
    ...fixture.allowed_object_properties.map(iri => [iri, { selectable: true, kind: 'object_property' }])])
  for (const example of fixture.cases) {
    assert.equal(classificationComplete(example.row, terms), example.classification_complete, example.case_id)
    assert.equal(assertionEligible(example.row), example.assembly_eligible, example.case_id)
  }
  assert.deepEqual(classificationCounts(fixture.cases.map(c => c.row), terms), {
    measured_comparisons: 0, complete_measured_comparisons: 0,
    classified_endpoint_mentions: 20,
    endpoint_review_warning_mentions: 0,
    mapping_review_warning_claims: 0,
    classified_relation_mentions: 7,
    exact_relation_mentions: 6, broader_relation_mentions: 1,
    proposed_relation_under_review_mentions: 2,
    property_type_conflict_claims: 0, property_type_missing_endpoint_claims: 0,
    property_type_review_claims: 0,
    classified_claims: 4, classification_review_claims: 7,
  })
})

test('static broader relations stay classified separately from exact and reasoning eligibility', async () => {
  const files = fixtureData()
  const rows = files['ontology-assertions.json'].assertions
  rows[0].bindings.relation.mapping = 'broader'
  rows[0].original_claim.relation = 'is directly part of'
  rows.push({ ...structuredClone(rows[0]), candidate_id: 'exact-copy',
    bindings: { ...structuredClone(rows[0].bindings), relation: { ...rows[0].bindings.relation, mapping: 'exact' } } })
  const handle = createStaticHandler(async path => files[path])
  const data = await handle('/ontology/relations?status=classified')
  assert.equal(data.total, 2)
  assert.equal(data.summary.classified_mentions, 2)
  assert.equal(data.summary.broader_mentions, 1)
  assert.equal(data.summary.classified_properties, 1)
  assert.deepEqual(new Set(data.relations.map(r => r.mapping)), new Set(['exact', 'broader']))
  const assertions = await handle('/ontology/assertions')
  const broader = assertions.assertions.find(row => row.candidate_id === 'c1')
  assert.equal(broader.classification_complete, true)
  assert.equal(broader.assembly_eligible, false)
  assert.equal(broader.original_claim.relation, 'is directly part of')
})

function fixtureData() {
  const rootIri = 'http://purl.obolibrary.org/obo/PO_0009005'
  const childIri = 'http://purl.obolibrary.org/obo/PO_0025025'
  const relUsedIri = 'http://purl.obolibrary.org/obo/BFO_0000050'
  const relUnusedIri = 'http://purl.obolibrary.org/obo/RO_0002610'
  return {
    'health.json': { status: 'ok', mode: 'static', capabilities: { read_only: true, execute: false, upload: false } },
    'pdfs.json': [{ id: 'doc1', name: 'Study A.pdf', title: 'Study A', status: 'completed', pdf_available: true }],
    'pdfs/doc1/claims.json': {
      total: 2,
      claims: [
        { candidate: { candidate_id: 'c1', subject: 'root', relation: 'is part of', object: 'root system' } },
        { candidate: { candidate_id: 'c2', subject: 'Milva', relation: 'increases', object: 'root growth' } },
      ],
    },
    'pdfs/doc1/logs.json': [{ level: 'info', message: 'Saved static log.' }],
    'ontology-status.json': { counts: { claims: 2 }, stale: false },
    'ontology-catalog.json': {
      catalog_signature: 'sig-1',
      sources: ['https://purl.obolibrary.org/obo/po.owl'],
      terms: {
        [rootIri]: {
          iri: rootIri, id: 'PO:0009005', label: 'root', kind: 'class',
          exact_synonyms: ['radicle'], parents: [], children: [childIri],
          canonical_iri: rootIri, selectable: true, source_ids: [0],
          assertion_count: 1,
        },
        [childIri]: {
          iri: childIri, id: 'PO:0025025', label: 'root system', kind: 'class',
          parents: [rootIri],
          canonical_iri: childIri, selectable: true, source_ids: [0],
          assertion_count: 1,
        },
        [relUsedIri]: {
          iri: relUsedIri, id: 'BFO:0000050', label: 'part of', kind: 'object_property',
          definition: 'A core mereological relation.', canonical_iri: relUsedIri, selectable: true, source_ids: [0],
        },
        [relUnusedIri]: {
          iri: relUnusedIri, id: 'RO:0002610', label: 'correlated with', kind: 'object_property',
          definition: 'Statistical association.', canonical_iri: relUnusedIri, selectable: true, source_ids: [0],
        },
      },
    },
    'ontology-assertions.json': {
      stale: false,
      assertions: [
        {
          candidate_id: 'c1',
          document_key: 'Study A',
          scope: 'in_scope',
          scope_reason: 'In scope.',
          original_claim: {
            subject: 'root', relation: 'is part of', object: 'root system',
            source_evidence_span: 'A root is part of a root system.',
          },
          bindings: {
            subject: { iri: rootIri, canonical_iri: rootIri, label: 'root', mapping: 'exact', reason: 'Exact root.' },
            object: { iri: childIri, canonical_iri: childIri, label: 'root system', mapping: 'exact', reason: 'Exact.' },
            relation: { iri: relUsedIri, canonical_iri: relUsedIri, label: 'part of', mapping: 'exact', reason: 'Exact.' },
          },
        },
        {
          candidate_id: 'c2',
          document_key: 'Study A',
          scope: 'in_scope',
          scope_reason: 'Potato cultivar response.',
          original_claim: {
            subject: 'Milva', relation: 'increases', object: 'root growth',
            source_evidence_span: 'Milva increased root growth under P supply.',
          },
          bindings: {
            subject: { iri: rootIri, canonical_iri: rootIri, label: 'Solanum tuberosum', mapping: 'broader', reason: 'Cultivar broader.' },
            object: { iri: null, canonical_iri: null, label: null, mapping: 'unresolved', reason: 'Missing.' },
            relation: { iri: 'R2', canonical_iri: 'R2', label: 'positively regulates', mapping: 'exact', reason: 'Increase.' },
          },
        },
        {
          candidate_id: 'c3',
          document_key: 'Study A',
          scope: 'in_scope',
          scope_reason: 'Comparison claim.',
          original_claim: {
            subject: 'leaf', relation: 'has higher', object: 'tuber',
            source_evidence_span: 'Leaf has higher value than tuber.',
          },
          bindings: {
            subject: { iri: rootIri, canonical_iri: rootIri, label: 'root', mapping: 'exact', reason: 'Ok.' },
            object: { iri: childIri, canonical_iri: childIri, label: 'root system', mapping: 'exact', reason: 'Ok.' },
            relation: { iri: null, canonical_iri: null, label: null, mapping: 'unresolved', reason: 'Pure comparison.' },
          },
        },
        {
          candidate_id: 'c4',
          document_key: 'Study A',
          scope: 'out_of_scope',
          scope_reason: 'Out of scope.',
          original_claim: {
            subject: 'soil', relation: 'differs from', object: 'air',
            source_evidence_span: 'Soil differs from air.',
          },
          bindings: {
            subject: { iri: null, canonical_iri: null, label: null, mapping: 'unresolved', reason: 'OOS.' },
            object: { iri: null, canonical_iri: null, label: null, mapping: 'unresolved', reason: 'OOS.' },
            relation: { iri: null, canonical_iri: null, label: null, mapping: 'unresolved', reason: 'OOS comparison.' },
          },
        },
      ],
    },
    'ontology-entities.json': {
      stale: false,
      counts: { grounded_mentions: 2 },
      entities: [
        {
          id: 'e1', label: 'root', iri: rootIri, concept_label: 'root',
          kind: 'concept', mapping: 'exact', status: 'grounded',
          occurrences: [{ document_key: 'Study A', candidate_id: 'c1', role: 'subject', source_mention: 'root' }],
        },
        {
          id: 'e2', label: 'Solanum tuberosum', iri: rootIri, concept_label: 'Solanum tuberosum',
          kind: 'particular', mapping: 'broader', status: 'grounded',
          occurrences: [{ document_key: 'Study A', candidate_id: 'c2', role: 'subject', source_mention: 'Milva' }],
        },
        {
          id: 'e3', label: 'root growth', iri: null, concept_label: null,
          kind: 'concept', mapping: 'unresolved', status: 'vocabulary_gap',
          occurrences: [{ document_key: 'Study A', candidate_id: 'c2', role: 'object', source_mention: 'root growth' }],
        },
      ],
    },
    'ontology-reasoning.json': { status: 'paused', message: 'Reasoning paused.', facts: [], total: 0 },
  }
}

test('static handler serves read-only ontology, claims, entity filters and blocks writes', async () => {
  const files = fixtureData()
  let fetches = 0
  const handle = createStaticHandler(async file => {
    fetches += 1
    if (!(file in files)) throw new Error(`Missing fixture ${file}`)
    return files[file]
  })

  await assert.rejects(() => handle('/ontology/pdfs/doc1/claims/c1/review', { method: 'POST' }), err => err.status === 403)

  const health = await handle('/health')
  assert.equal(health.mode, 'static')

  const roots = await handle('/ontology/hierarchy?kind=class&offset=0&limit=40')
  assert.equal(roots.total, 1)
  assert.equal(roots.terms[0].label, 'root')
  assert.equal(roots.terms[0].has_children, true)

  const children = await handle(`/ontology/hierarchy?kind=class&parent=${encodeURIComponent('http://purl.obolibrary.org/obo/PO_0009005')}`)
  assert.equal(children.terms.length, 1)
  assert.equal(children.terms[0].label, 'root system')

  const found = await handle('/ontology/terms?kind=class&q=radicle&selectable=true')
  assert.equal(found.terms[0].label, 'root')

  const term = await handle(`/ontology/term?iri=${encodeURIComponent('http://purl.obolibrary.org/obo/PO_0025025')}`)
  assert.equal(term.parents[0].label, 'root')
  assert.deepEqual(term.sources, ['https://purl.obolibrary.org/obo/po.owl'])
  assert.equal(term.lineage_paths[0].map(s => s.label).join(' -> '), 'root -> root system')
  await assert.rejects(() => handle('/ontology/term?iri=urn:missing'), err => err.status === 404)

  const assertions = await handle('/ontology/assertions?q=Milva&document=Study%20A&offset=0&limit=15')
  assert.equal(assertions.total, 1)
  assert.equal(assertions.assertions[0].candidate_id, 'c2')
  const allClaims = await handle('/ontology/assertions')
  assert.equal(allClaims.assertions.find(row => row.candidate_id === 'c1').assembly_eligible, true)
  assert.equal(allClaims.assertions.find(row => row.candidate_id === 'c3').assembly_eligible, false)
  const status = await handle('/ontology/status')
  assert.equal(status.counts.mapped_claims, 1)
  assert.equal(status.stale, true) // old saved policy remains visibly historical

  const broaderEntities = await handle('/ontology/entities?unique=true&status=classification&offset=0&limit=20')
  assert.equal(broaderEntities.total, 1)
  assert.equal(broaderEntities.status_counts.grounded, 2)
  assert.equal(broaderEntities.status_counts.classification, 1)
  assert.equal(broaderEntities.status_counts.vocabulary_gap, 1)
  assert.equal(broaderEntities.entities[0].occurrences[0].claim_text, 'Milva → increases → root growth')

  const pagedClaims = await handle('/pdfs/doc1/claims?limit=1&offset=1')
  assert.equal(pagedClaims.total, 2)
  assert.equal(pagedClaims.claims.length, 1)
  assert.equal(pagedClaims.claims[0].candidate.candidate_id, 'c2')

  const relGrounded = await handle('/ontology/relations?status=grounded&scope=in_scope')
  assert.equal(relGrounded.total, 1) // Unapproved historical R2 is not classified as a catalogue property.
  assert.deepEqual(relGrounded.summary, {
    total_claims: 3, grounded_mentions: 1, broader_mentions: 0, classified_mentions: 1, classified_properties: 1, unresolved_mentions: 2,
    used_properties: 1, catalog_properties: 2, unused_properties: 1, unresolved_phrases: 2,
  })
  assert.deepEqual(relGrounded.status_counts, { grounded: 1, classification: 0, classified: 1, unresolved: 2, unused: 1, all: 3 })

  const relUnresolvedAll = await handle('/ontology/relations?status=unresolved&scope=all')
  assert.equal(relUnresolvedAll.total, 3)
  assert.equal(relUnresolvedAll.summary.total_claims, 4)
  assert.equal(relUnresolvedAll.relations.find(rel => rel.raw_variants.some(v => v.label === 'has higher'))
    .occurrences[0].claim_assembly_eligible, false)

  const relUnused = await handle('/ontology/relations?status=unused&q=correlated')
  assert.equal(relUnused.total, 1)
  assert.equal(relUnused.relations[0].label, 'correlated with')

  const priorFetches = fetches
  await handle('/ontology/hierarchy?kind=class&offset=0&limit=40')
  assert.equal(fetches, priorFetches)
})


test('eligibility rejects legacy unresolved predicates independently of endpoint grounding', async () => {
  const row = { scope: 'in_scope', assembly_eligible: true, bindings: {
    subject: { mapping: 'exact', iri: 'urn:subject' }, object: { mapping: 'broader', iri: 'urn:object' },
    relation: { mapping: 'unresolved', iri: null },
  } }
  assert.equal(assertionEligible(row), false)
  row.bindings.relation = { mapping: 'exact', iri: 'urn:relation' }
  assert.equal(assertionEligible(row), true)
  row.extraction_warnings = [{ code: 'warning' }]
  assert.equal(assertionEligible(row), false)
  row.claim_signature = 'current'
  row.extraction_review = { status: 'accepted', claim_signature: 'current' }
  assert.equal(assertionEligible(row), true)
  row.extraction_review_status = 'accepted'
  row.claim_signature = 'changed'
  assert.equal(assertionEligible(row), false)
})

test('mapping conflicts never masquerade as extraction holds in static results', async () => {
  const files = fixtureData()
  const row = files['ontology-assertions.json'].assertions[0]
  row.extraction_review_status = 'held' // legacy combined status must not leak
  row.mapping_consistency_warnings = [{ role: 'subject', message: 'Mapping conflict.' }]
  const handle = createStaticHandler(async name => files[name])
  const result = await handle('/ontology/assertions')
  const shown = result.assertions.find(r => r.candidate_id === row.candidate_id)
  assert.equal(shown.extraction_review_status, 'not_flagged')
  assert.equal(shown.assembly_eligible, false)
  assert.equal(extractionReviewStatus(row), 'not_flagged')
  row.extraction_warnings = [{ message: 'Separate source warning.' }]
  assert.equal(extractionReviewStatus(row), 'held')
})

test('static viewer routes warned activity endpoints to review and preserves clean counterpart endpoints', async () => {
  const files = fixtureData()
  const childIri = 'http://purl.obolibrary.org/obo/PO_0025025'
  const row = files['ontology-assertions.json'].assertions[0]
  row.mapping_review_warnings = [{ role: 'object', code: 'possible_function_vs_measurement_conflation', message: 'Measured activity.' }]
  files['ontology-entities.json'].entities.push({
    id: 'concept-obj', label: 'root system', iri: childIri, concept_label: 'root system',
    kind: 'concept', mapping: 'exact', status: 'grounded', shared_concept: true,
    occurrences: [{ id: 'occ-obj', document_key: 'Study A', candidate_id: 'c1', role: 'object', source_mention: 'root system' }],
  })
  const handle = createStaticHandler(async name => files[name])
  const assertions = await handle('/ontology/assertions')
  const shown = assertions.assertions.find(r => r.candidate_id === row.candidate_id)
  assert.equal(shown.classification_complete, false)
  assert.equal(shown.assembly_eligible, false)
  assert.equal(assertions.counts.classified_endpoint_mentions, 4)
  assert.equal(assertions.counts.endpoint_review_warning_mentions, 1)
  assert.equal(assertions.counts.mapping_review_warning_claims, 1)
  const grounded = await handle('/ontology/entities?status=grounded')
  assert.ok(grounded.entities.some(e => e.id === 'e1'))
  assert.ok(!grounded.entities.some(e => e.occurrences.some(o => o.candidate_id === 'c1' && o.role === 'object')))
  const classified = await handle('/ontology/entities?status=classified')
  assert.ok(!classified.entities.some(e => e.occurrences.some(o => o.candidate_id === 'c1' && o.role === 'object')))
  assert.ok(classified.entities.some(e => e.occurrences.some(o => o.candidate_id === 'c1' && o.role === 'subject')))
  const alignmentReview = await handle('/ontology/entities?status=alignment_review')
  const warnedEnt = alignmentReview.entities.find(e => e.occurrences.some(o => o.candidate_id === 'c1' && o.role === 'object'))
  assert.ok(warnedEnt)
  assert.equal(warnedEnt.shared_concept, false)
  assert.equal(warnedEnt.alignment_category, 'review')
  assert.equal(warnedEnt.id, 'occ-obj')
  const review = await handle('/ontology/entities?status=review')
  assert.equal(review.entities.length, 1)
  assert.equal(review.entities[0].id, 'occ-obj')
})
