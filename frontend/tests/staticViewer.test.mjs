import assert from 'node:assert/strict'
import test from 'node:test'
import { createStaticHandler } from '../src/lib/staticApi.js'

function fixtureData() {
  const rootIri = 'http://purl.obolibrary.org/obo/PO_0009005'
  const childIri = 'http://purl.obolibrary.org/obo/PO_0025025'
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
            relation: { iri: 'R1', canonical_iri: 'R1', label: 'part of', mapping: 'exact', reason: 'Exact.' },
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

  const priorFetches = fetches
  await handle('/ontology/hierarchy?kind=class&offset=0&limit=40')
  assert.equal(fetches, priorFetches)
})
