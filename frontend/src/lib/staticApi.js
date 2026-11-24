import propertyUsage from '../../../backend/resources/ontologies/property_usage.json' with { type: 'json' }

function directPropertyAllowed(iri) {
  return !propertyUsage.restrictions[iri] || propertyUsage.restrictions[iri].allowed_mappings.includes('exact')
}

function httpError(status, detail) {
  const error = new Error(detail || `Request failed (${status})`)
  error.status = status
  return error
}

function cmp(a, b) {
  return a < b ? -1 : a > b ? 1 : 0
}

function intParam(params, name, fallback) {
  const raw = params.get(name)
  if (raw === null || raw === '') return fallback
  const n = Number.parseInt(raw, 10)
  return Number.isNaN(n) ? fallback : n
}

function ancestorsOf(terms, iri) {
  const precomputed = terms[iri]?.ancestors
  if (Array.isArray(precomputed)) return precomputed
  const seen = new Set()
  const pending = [...(terms[iri]?.parents || [])]
  while (pending.length) {
    const parent = pending.pop()
    if (seen.has(parent) || !terms[parent]) continue
    seen.add(parent)
    for (const p of terms[parent].parents || []) pending.push(p)
  }
  return [...seen]
}

function lineagePathsOf(terms, iri, visited = new Set()) {
  const term = terms[iri]
  if (!term) return []
  if (Array.isArray(term.lineage_paths)) return term.lineage_paths
  if (visited.has(iri)) return []
  const nextVisited = new Set(visited)
  nextVisited.add(iri)
  const parents = (term.parents || []).filter(p => terms[p] && terms[p].kind === term.kind)
  const node = { iri: term.iri, label: term.label, id: term.id }
  if (!parents.length) return [[node]]
  const paths = []
  for (const parent of parents) {
    const subPaths = lineagePathsOf(terms, parent, nextVisited)
    if (!subPaths.length) {
      const pTerm = terms[parent]
      paths.push([{ iri: pTerm.iri, label: pTerm.label, id: pTerm.id }, node])
    } else {
      for (const sp of subPaths) paths.push([...sp, node])
    }
  }
  return paths
}

function normalizeTerm(term, sourcesList) {
  const synonyms = term.synonyms || [
    ...new Set([
      ...(term.exact_synonyms || []),
      ...(term.related_synonyms || []),
      ...(term.broad_synonyms || []),
      ...(term.narrow_synonyms || []),
    ]),
  ]
  const sources = term.sources || (term.source_ids || []).map(i => sourcesList[i]).filter(Boolean)
  const children = term.children || []
  return {
    definition: '',
    parents: [],
    domain: [],
    range: [],
    exact_synonyms: [],
    related_synonyms: [],
    broad_synonyms: [],
    narrow_synonyms: [],
    ...term,
    synonyms,
    sources,
    children,
    child_count: term.child_count ?? children.length,
    has_children: term.has_children ?? children.length > 0,
  }
}

function formatClaimSummary(claim) {
  const s = claim.subject || ''
  const sc = claim.subject_context || ''
  const r = claim.relation || ''
  const o = claim.object || ''
  const oc = claim.object_context || ''
  const sLbl = sc ? `${s} (${sc})` : s
  const oLbl = oc ? `${o} (${oc})` : o
  const formatted = claim.claim_text || (s && r && o ? `${sLbl} → ${r} → ${oLbl}` : '')
  return { s, sc, r, o, oc, formatted }
}

export function extractionReviewStatus(row) {
  const review = row.extraction_review || {}
  const validReview = row.claim_signature && review.claim_signature === row.claim_signature
  return validReview && ['accepted', 'rejected'].includes(review.status)
    ? review.status
    : row.extraction_warnings?.length ? 'held' : 'not_flagged'
}

function observationOrPartial(row) {
  return Boolean(row.original_claim?.measured_comparison || Object.values(row.bindings || {})
    .some(binding => ['held', 'partial'].includes(binding.components?.grounding_completeness)))
}

export function assertionEligible(row) {
  if (observationOrPartial(row)) return false
  const bindings = row.bindings || {}
  const status = extractionReviewStatus(row)
  const ptStatus = row.property_type_check?.status
  return Boolean(row.scope === 'in_scope' && !['held', 'rejected'].includes(status)
    && !row.mapping_consistency_warnings?.length
    && !row.mapping_review_warnings?.length
    && !['incompatible', 'missing_required_endpoint', 'insufficient_type_info'].includes(ptStatus)
    && bindings.relation?.mapping === 'exact' && bindings.relation?.iri
    && directPropertyAllowed(bindings.relation.iri)
    && ['subject', 'object'].every(role => ['exact', 'broader'].includes(bindings[role]?.mapping) && bindings[role]?.iri))
}

const INVENTORY_SIGNATURE = 'scoped-index-hybrid-rrf-v10:ef09c613b18a829fcf63cb1798ed86a1739396acb4cb7a41a73221f3a03a4951:a7cec87b7018aaf710d20045a99b0a8f1b9f729453ca1c207827d6fd84ed0139'
export const CLASSIFICATION_POLICY = 'ontology-classification-coverage-v6'

function bindingClassified(binding = {}, role, terms) {
  const retrieved = role !== 'relation' && binding.retrieved_term?.iri === binding.iri && binding.retrieved_term?.kind === 'class'
    && binding.retrieved_term?.inventory_signature === INVENTORY_SIGNATURE
  const term = retrieved ? { ...binding.retrieved_term, selectable: true } : terms[binding.iri]
  return Boolean(['exact', 'broader'].includes(binding.mapping) && term?.selectable
    && (role !== 'relation' || binding.mapping !== 'exact' || directPropertyAllowed(binding.iri))
    && term.kind === (role === 'relation' ? 'object_property' : 'class'))
}

function warnedEndpointRoles(row = {}) {
  return new Set((row.mapping_review_warnings || []).map(w => w.role).filter(r => r === 'subject' || r === 'object'))
}

function endpointAccepted(row, role, terms) {
  return Boolean(bindingClassified(row.bindings?.[role], role, terms) && !warnedEndpointRoles(row).has(role))
}

function relationAccepted(row, terms) {
  return Boolean(!row.original_claim?.measured_comparison && !row.mapping_review_warnings?.some(w => w.role === 'relation')
    && bindingClassified(row.bindings?.relation, 'relation', terms)
    && !['held', 'rejected'].includes(extractionReviewStatus(row))
    && !['incompatible', 'missing_required_endpoint'].includes(row.property_type_check?.status))
}

export function classificationComplete(row, terms) {
  if (observationOrPartial(row)) return false
  return Boolean(row.scope === 'in_scope' && relationAccepted(row, terms)
    && !row.mapping_consistency_warnings?.length
    && !row.mapping_review_warnings?.length
    && ['subject', 'object'].every(role => endpointAccepted(row, role, terms)))
}

function comparisonComplete(row) {
  return Boolean(row.scope === 'in_scope' && row.comparison_assessment?.complete
    && !['held', 'rejected'].includes(extractionReviewStatus(row))
    && !row.mapping_consistency_warnings?.length && !row.mapping_review_warnings?.length)
}

export function classificationCounts(rows, terms) {
  const included = rows.filter(row => row.scope === 'in_scope')
  const countRelations = mapping => included.filter(row => row.bindings?.relation?.mapping === mapping
    && relationAccepted(row, terms)).length
  const exact = countRelations('exact'), broader = countRelations('broader')
  const proposedUnderReview = included.filter(row => bindingClassified(row.bindings?.relation, 'relation', terms)
    && !relationAccepted(row, terms)).length
  const typeConflicts = included.filter(row => row.property_type_check?.status === 'incompatible').length
  const missingEndpoint = included.filter(row => row.property_type_check?.status === 'missing_required_endpoint').length
  const typeReviews = included.filter(row => row.property_type_check?.status === 'insufficient_type_info').length
  const endpointWarns = included.reduce((n, row) => n + warnedEndpointRoles(row).size, 0)
  const mappingWarns = included.filter(row => Boolean(row.mapping_review_warnings?.length)).length
  return {
    measured_comparisons: included.filter(row => row.comparison_assessment).length,
    complete_measured_comparisons: included.filter(comparisonComplete).length,
    classified_endpoint_mentions: included.reduce((n, row) => n + ['subject', 'object']
      .filter(role => endpointAccepted(row, role, terms)).length, 0),
    endpoint_review_warning_mentions: endpointWarns,
    mapping_review_warning_claims: mappingWarns,
    classified_relation_mentions: exact + broader, exact_relation_mentions: exact, broader_relation_mentions: broader,
    proposed_relation_under_review_mentions: proposedUnderReview,
    property_type_conflict_claims: typeConflicts,
    property_type_missing_endpoint_claims: missingEndpoint,
    property_type_review_claims: typeReviews,
    classified_claims: rows.filter(row => classificationComplete(row, terms)).length,
    classification_review_claims: rows.filter(row => row.scope !== 'out_of_scope'
      && extractionReviewStatus(row) !== 'rejected' && !classificationComplete(row, terms) && !comparisonComplete(row)).length,
  }
}

export function createStaticHandler(fetchJson) {
  const cache = new Map()
  const catalogIndexCache = new WeakMap()
  const assertionsIndexCache = new WeakMap()
  const entitiesIndexCache = new WeakMap()
  const relationsIndexCache = new WeakMap()

  function load(file) {
    if (!cache.has(file)) {
      const pending = Promise.resolve().then(() => fetchJson(file))
      cache.set(file, pending)
    }
    return cache.get(file)
  }

  function getCatalogIndex(catalogData) {
    let idx = catalogIndexCache.get(catalogData)
    if (idx) return idx
    const sources = catalogData.sources || []
    const rawTerms = catalogData.terms || {}
    const terms = {}
    const list = []
    for (const [iri, raw] of Object.entries(rawTerms)) {
      const norm = normalizeTerm(raw, sources)
      terms[iri] = norm
      list.push(norm)
    }
    const visibleByKind = new Map()
    const visibleFor = kind => {
      if (!visibleByKind.has(kind)) {
        const vis = new Set()
        for (const t of list) {
          if (t.selectable && t.kind === kind) {
            vis.add(t.iri)
            for (const anc of ancestorsOf(terms, t.iri)) {
              if (terms[anc]) vis.add(anc)
            }
          }
        }
        visibleByKind.set(kind, vis)
      }
      return visibleByKind.get(kind)
    }
    const searchHaystack = new WeakMap()
    for (const t of list) {
      searchHaystack.set(t, [t.label, t.id, t.iri, ...t.synonyms].join(' ').toLowerCase())
    }
    idx = { terms, list, visibleFor, searchHaystack, signature: catalogData.catalog_signature }
    catalogIndexCache.set(catalogData, idx)
    return idx
  }

  function getAssertionsIndex(assertionsData, catalogData) {
    let idx = assertionsIndexCache.get(assertionsData)
    if (idx && idx.catalogData === catalogData) return idx
    const { terms } = getCatalogIndex(catalogData)
    const rows = (assertionsData.assertions || []).map(row => ({ ...row,
      classification_complete: classificationComplete(row, terms),
      extraction_review_status: extractionReviewStatus(row), assembly_eligible: assertionEligible(row) }))
    const byKey = new Map()
    const searchText = new WeakMap()
    for (const row of rows) {
      byKey.set(`${row.document_key}\0${row.candidate_id}`, row)
      searchText.set(row, JSON.stringify(row.original_claim || {}).toLowerCase())
    }
    const policyStale = rows.some(row => !['established-only-context-grounding-v17', 'closed-predicate-entity-grounding-v20'].includes(row.policy)
      || ((row.inventory_signature || ['scoped-index-hybrid-rrf-v10', 'scoped-index-hybrid-rrf-v9', 'scoped-index-hybrid-rrf-v8', 'scoped-index-hybrid-rrf-v7', 'scoped-index-lexical-typed-shortlists-v6', 'scoped-index-lexical-typed-shortlists-v5', 'scoped-index-lexical-typed-shortlists-v4', 'scoped-index-core-and-relation-shortlists-v3', 'scoped-index-core-and-relation-shortlists-v2', 'scoped-formula-exact-bm25-v1'].includes(row.class_selection_policy)) && row.inventory_signature !== INVENTORY_SIGNATURE)
      || row.classification_policy !== CLASSIFICATION_POLICY
      || row.eligibility_policy !== 'grounded-predicate-eligibility-v7'
      || row.consistency_policy !== 'document-context-interpretation-v3')
    idx = { rows, byKey, searchText, catalogData, stale: Boolean(assertionsData.stale || policyStale) }
    assertionsIndexCache.set(assertionsData, idx)
    return idx
  }

  function getEntitiesIndex(entitiesData, assertionMap = null) {
    const cache = assertionMap ? null : entitiesIndexCache.get(entitiesData)
    if (cache) return cache
    const normalized = []
    for (const e of (entitiesData.entities || [])) {
      const occs = e.occurrences || []
      if (!assertionMap || !occs.length) {
        normalized.push(e)
        continue
      }
      const cleanOccs = []
      for (const occ of occs) {
        const row = assertionMap.get(`${occ.document_key}\0${occ.candidate_id}`)
        if (row && warnedEndpointRoles(row).has(occ.role)) {
          const claim = row.original_claim || {}
          normalized.push({
            ...e,
            id: occ.id || e.id,
            label: claim[occ.role] || occ.source_mention || e.label,
            status: 'alignment_review',
            alignment_category: 'review',
            shared_concept: false,
            occurrences: [occ],
          })
        } else {
          cleanOccs.push(occ)
        }
      }
      if (cleanOccs.length) {
        normalized.push({ ...e, occurrences: cleanOccs })
      }
    }
    const groupedMap = new Map()
    for (const e of normalized) {
      const key = `${(e.label || '').trim().toLowerCase()}\0${e.iri || ''}\0${e.mapping || ''}\0${e.status || ''}\0${e.alignment_category || ''}`
      if (!groupedMap.has(key)) {
        groupedMap.set(key, { ...e, occurrences: [...(e.occurrences || [])] })
      } else {
        groupedMap.get(key).occurrences.push(...(e.occurrences || []))
      }
    }
    const uniqueList = [...groupedMap.values()]
    const idx = { raw: normalized, uniqueList }
    if (!assertionMap) {
      entitiesIndexCache.set(entitiesData, idx)
    }
    return idx
  }

  function getRelationsRegistry(assertionsData, catalogData, scope, doc) {
    let byScopeDoc = relationsIndexCache.get(assertionsData)
    if (!byScopeDoc) {
      byScopeDoc = new Map()
      relationsIndexCache.set(assertionsData, byScopeDoc)
    }
    const cacheKey = `${scope}\0${doc}`
    if (byScopeDoc.has(cacheKey)) return byScopeDoc.get(cacheKey)

    const { rows } = getAssertionsIndex(assertionsData, catalogData)
    const { terms, list } = getCatalogIndex(catalogData)
    const groups = new Map()
    const variantCounts = new Map()
    const usedCanonical = new Set()
    let totalClaims = 0
    let groundedMentions = 0
    let broaderMentions = 0
    let unresolvedMentions = 0

    for (const row of rows) {
      if (row.original_claim?.measured_comparison) continue
      if (scope !== 'all' && row.scope !== scope) continue
      if (doc && row.document_key !== doc) continue
      totalClaims += 1
      const claim = row.original_claim || {}
      const binding = row.bindings?.relation || {}
      const rawRel = (claim.relation || '').trim()
      const approved = terms[binding.iri]?.selectable && terms[binding.iri]?.kind === 'object_property'
      const grounded = binding.mapping === 'exact' && approved
      const broader = binding.mapping === 'broader' && approved

      let key
      let propIri = null
      let termId = ''
      let definition = ''
      let label
      let status
      let mapping

      if (grounded || broader) {
        groundedMentions += Number(Boolean(grounded))
        broaderMentions += Number(Boolean(broader))
        const rawIri = binding.iri
        const canon = binding.canonical_iri || terms[rawIri]?.canonical_iri || rawIri
        const term = terms[canon] || terms[rawIri] || {}
        propIri = term.iri || canon || rawIri
        usedCanonical.add(canon)
        usedCanonical.add(propIri)
        status = grounded ? 'grounded' : 'classification'
        key = `${status}\0${canon}`
        label = term.label || binding.label || propIri
        termId = term.id || ''
        definition = term.definition || ''
        mapping = grounded ? 'exact' : 'broader'
      } else {
        unresolvedMentions += 1
        const normRel = rawRel.toLowerCase()
        key = `unresolved\0${normRel}`
        label = rawRel || '(empty relation)'
        status = 'unresolved'
        mapping = 'unresolved'
      }

      if (!groups.has(key)) {
        groups.set(key, {
          id: `rel:${key}`,
          status,
          mapping,
          iri: propIri,
          term_id: termId,
          label,
          definition,
          occurrences: [],
        })
        variantCounts.set(key, new Map())
      }

      const phrase = rawRel || '(empty relation)'
      const vMap = variantCounts.get(key)
      vMap.set(phrase, (vMap.get(phrase) || 0) + 1)
      const { formatted } = formatClaimSummary(claim)
      groups.get(key).occurrences.push({
        id: `rel-occ:${row.document_key}:${row.candidate_id}`,
        document_key: row.document_key,
        candidate_id: row.candidate_id,
        scope: row.scope,
        source_page: claim.source_page,
        relation_phrase: rawRel,
        reason: binding.reason || '',
        evidence: claim.source_evidence_span,
        claim_text: formatted,
        claim_assembly_eligible: assertionEligible(row),
      })
    }

    const searchText = new WeakMap()
    const extracted = []
    for (const [key, entry] of groups.entries()) {
      const variants = [...variantCounts.get(key).entries()]
        .sort((a, b) => b[1] - a[1] || cmp(a[0].toLowerCase(), b[0].toLowerCase()))
        .map(([phrase, count]) => ({ label: phrase, count }))
      entry.raw_variants = variants
      if (entry.status === 'unresolved' && variants.length) entry.label = variants[0].label
      entry.occurrence_count = entry.occurrences.length
      entry.sample_reason = entry.occurrences.find(o => o.reason)?.reason || ''
      const hay = [
        entry.label,
        entry.iri,
        entry.term_id,
        entry.definition,
        ...variants.map(v => v.label),
        ...entry.occurrences.flatMap(o => [o.relation_phrase, o.reason, o.candidate_id, o.claim_text]),
      ].filter(Boolean).join(' ').toLowerCase()
      searchText.set(entry, hay)
      extracted.push(entry)
    }
    extracted.sort((a, b) => (b.occurrence_count - a.occurrence_count) || cmp(a.label.toLowerCase(), b.label.toLowerCase()) || cmp(a.id, b.id))

    const selectableProps = list.filter(t => t.kind === 'object_property' && t.selectable)
    const unused = []
    for (const term of selectableProps) {
      const canon = term.canonical_iri || term.iri
      if (usedCanonical.has(term.iri) || usedCanonical.has(canon)) continue
      const item = {
        id: `rel-unused:${term.iri}`,
        status: 'unused',
        mapping: 'unused',
        iri: term.iri,
        term_id: term.id || '',
        label: term.label || term.iri,
        definition: term.definition || '',
        raw_variants: [],
        occurrence_count: 0,
        sample_reason: '',
        occurrences: [],
      }
      searchText.set(item, [item.label, item.term_id, item.iri, item.definition].filter(Boolean).join(' ').toLowerCase())
      unused.push(item)
    }
    unused.sort((a, b) => cmp(a.label.toLowerCase(), b.label.toLowerCase()) || cmp(a.iri, b.iri))

    const summary = {
      total_claims: totalClaims,
      grounded_mentions: groundedMentions,
      broader_mentions: broaderMentions,
      classified_mentions: groundedMentions + broaderMentions,
      classified_properties: new Set(extracted.filter(item => item.status !== 'unresolved').map(item => item.iri)).size,
      unresolved_mentions: unresolvedMentions,
      used_properties: extracted.filter(item => item.status === 'grounded').length,
      catalog_properties: selectableProps.length,
      unused_properties: unused.length,
      unresolved_phrases: extracted.filter(item => item.status === 'unresolved').length,
    }
    const built = { extracted, unused, summary, searchText }
    byScopeDoc.set(cacheKey, built)
    return built
  }

  async function handle(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase()
    if (method !== 'GET' && method !== 'HEAD') {
      throw httpError(403, 'This published static viewer is read-only.')
    }
    if (options.signal?.aborted) {
      throw new DOMException('Aborted', 'AbortError')
    }

    const url = new URL(path, 'https://static.local')
    const pathname = url.pathname
    const params = url.searchParams

    let result
    if (pathname === '/health') {
      result = await load('health.json')
    } else if (pathname === '/pdfs') {
      result = await load('pdfs.json')
    } else if (pathname === '/ontology/status') {
      const [status, assertions, catalogData] = await Promise.all([load('ontology-status.json'), load('ontology-assertions.json'), load('ontology-catalog.json')])
      const index = getAssertionsIndex(assertions, catalogData)
      result = { ...status, stale: Boolean(status.stale || index.stale),
        counts: { ...status.counts, ...classificationCounts(index.rows, getCatalogIndex(catalogData).terms),
          mapped_claims: index.rows.filter(row => row.assembly_eligible).length } }
    } else if (pathname === '/ontology/hierarchy') {
      const { terms, visibleFor } = getCatalogIndex(await load('ontology-catalog.json'))
      const kind = params.get('kind') || 'class'
      const parent = params.get('parent') || null
      const limit = Math.max(1, Math.min(100, intParam(params, 'limit', 50)))
      const offset = Math.max(0, intParam(params, 'offset', 0))

      const visible = visibleFor(kind)
      const parentChildren = parent ? new Set(terms[parent]?.children || []) : null
      const rows = []
      for (const iri of visible) {
        const term = terms[iri]
        if (!term || term.kind !== kind) continue
        const matches = parentChildren
          ? parentChildren.has(iri)
          : !term.parents.some(p => visible.has(p))
        if (matches) rows.push(term)
      }
      rows.sort((a, b) => cmp(a.label.toLowerCase(), b.label.toLowerCase()) || cmp(a.iri, b.iri))
      const paged = rows.slice(offset, offset + limit).map(t => {
        const visChildren = t.children.filter(ch => visible.has(ch)).length
        return { ...t, child_count: visChildren, has_children: visChildren > 0 }
      })
      result = { terms: paged, total: rows.length, offset, limit }
    } else if (pathname === '/ontology/terms') {
      const { list, searchHaystack } = getCatalogIndex(await load('ontology-catalog.json'))
      const kind = params.get('kind') || 'class'
      const needle = (params.get('q') || '').trim().toLowerCase()
      const selectableOnly = params.get('selectable') === 'true' || params.get('selectable') === '1'
      const limit = Math.max(1, Math.min(100, intParam(params, 'limit', 50)))
      const offset = Math.max(0, intParam(params, 'offset', 0))

      const rows = list.filter(t => {
        if (kind && t.kind !== kind) return false
        if (selectableOnly && !t.selectable) return false
        return !needle || searchHaystack.get(t).includes(needle)
      })
      rows.sort((a, b) => {
        const aExact = needle && (a.label.toLowerCase() === needle || a.id.toLowerCase() === needle) ? 0 : 1
        const bExact = needle && (b.label.toLowerCase() === needle || b.id.toLowerCase() === needle) ? 0 : 1
        return aExact - bExact || cmp(a.label.toLowerCase(), b.label.toLowerCase()) || cmp(a.iri, b.iri)
      })
      result = { terms: rows.slice(offset, offset + limit), total: rows.length, offset, limit }
    } else if (pathname === '/ontology/term') {
      const iri = params.get('iri') || ''
      const { terms, signature } = getCatalogIndex(await load('ontology-catalog.json'))
      const term = terms[iri]
      if (!term) throw httpError(404, 'Term not present in the frozen ontology reference')
      result = {
        ...term,
        ancestors: ancestorsOf(terms, iri),
        lineage_paths: lineagePathsOf(terms, iri),
        parents: term.parents.map(p => terms[p] || { iri: p, label: p }),
        children: term.children.map(ch => terms[ch]).filter(Boolean),
        catalog_signature: signature,
      }
    } else if (pathname === '/ontology/assertions') {
      const [assertionsRaw, catalogRaw] = await Promise.all([
        load('ontology-assertions.json'),
        load('ontology-catalog.json'),
      ])
      const { rows: allAssertions, searchText, stale } = getAssertionsIndex(assertionsRaw, catalogRaw)
      const { terms } = getCatalogIndex(catalogRaw)
      const iri = params.get('iri') || null
      const canonical = iri && terms[iri] ? terms[iri].canonical_iri : null
      const doc = params.get('document') || ''
      const scope = params.get('scope') || 'all'
      const needle = (params.get('q') || '').trim().toLowerCase()
      const limit = Math.max(1, Math.min(100, intParam(params, 'limit', 25)))
      const offset = Math.max(0, intParam(params, 'offset', 0))

      const rows = allAssertions.filter(row => {
        if (iri) {
          const bound = Object.values(row.bindings || {}).some(
            b => b.iri === iri || (canonical && b.canonical_iri === canonical),
          )
          if (!bound) return false
        }
        if (doc && row.document_key !== doc) return false
        if (scope !== 'all' && row.scope !== scope) return false
        if (needle && !searchText.get(row).includes(needle)) return false
        return true
      })
      result = {
        assertions: rows.slice(offset, offset + limit),
        counts: classificationCounts(rows, terms),
        total: rows.length,
        offset,
        limit,
        stale,
        unprocessed_documents: [],
      }
    } else if (pathname === '/ontology/entities') {
      const [entitiesRaw, assertionsRaw, catalogRaw] = await Promise.all([
        load('ontology-entities.json'),
        load('ontology-assertions.json'),
        load('ontology-catalog.json'),
      ])
      const needle = (params.get('q') || '').trim().toLowerCase()
      const doc = params.get('document') || ''
      const status = params.get('status') || 'all'
      const unique = params.get('unique') === 'true' || params.get('unique') === '1'
      const limit = Math.max(1, Math.min(100, intParam(params, 'limit', 25)))
      const offset = Math.max(0, intParam(params, 'offset', 0))

      const { byKey: assertionMap } = getAssertionsIndex(assertionsRaw, catalogRaw)
      const { raw, uniqueList } = getEntitiesIndex(entitiesRaw, assertionMap)
      const entityList = unique ? uniqueList : raw

      const statusCounts = { grounded: 0, classified: 0, vocabulary_gap: 0, alignment_review: 0, classification: 0, review: 0, all: 0 }
      const found = []
      for (const entity of entityList) {
        const occurrences = doc
          ? (entity.occurrences || []).filter(o => o.document_key === doc)
          : (entity.occurrences || [])
        if (!occurrences.length) continue
        if (needle && !JSON.stringify([entity.label, entity.iri, occurrences]).toLowerCase().includes(needle)) continue

        statusCounts[entity.status] = (statusCounts[entity.status] || 0) + 1
        const reviewFlagged = entity.alignment_category === 'review'
          || (entity.status === 'alignment_review' && entity.mapping !== 'broader')
        const category = reviewFlagged
          ? 'review'
          : entity.mapping === 'broader'
            ? 'classification'
            : (entity.mapping === 'related' || entity.mapping === 'narrower')
              ? 'review'
              : entity.status
        if (category === 'classification' || category === 'review') {
          statusCounts[category] += 1
        }
        statusCounts.all += 1
        const catalogTerms = getCatalogIndex(catalogRaw).terms
        const classified = !reviewFlagged && (
          bindingClassified(entity, 'subject', catalogTerms)
          || occurrences.some(o => {
            const row = assertionMap.get(`${o.document_key}\0${o.candidate_id}`)
            return row && bindingClassified(row.bindings?.[o.role], 'subject', catalogTerms)
          })
        )
        statusCounts.classified += Number(classified)

        const selectedBucket = (status === 'classification' || status === 'review') ? category : entity.status
        if (status === 'classified' && !classified) continue
        if (!['all', 'classified'].includes(status) && selectedBucket !== status) continue
        found.push({ ...entity, occurrences, occurrence_count: occurrences.length })
      }

      const page = found.slice(offset, offset + limit).map(entity => {
        const occurrences = []
        for (const occ of entity.occurrences) {
          const row = assertionMap.get(`${occ.document_key}\0${occ.candidate_id}`)
          if (!row) continue
          const claim = row.original_claim || {}
          const { s, sc, r, o, oc, formatted } = formatClaimSummary(claim)
          occurrences.push({
            ...occ,
            claim_assembly_eligible: assertionEligible(row),
            evidence: claim.source_evidence_span,
            display_mention: claim[occ.role] ?? occ.source_mention,
            endpoint_context: claim[`${occ.role}_context`] || '',
            claim_text: formatted,
            claim: { candidate_id: occ.candidate_id, subject: s, subject_context: sc, relation: r, object: o, object_context: oc },
            qualifiers: claim.qualifiers || {},
            reason: row.bindings?.[occ.role]?.reason || '',
          })
        }
        let label = entity.label
        if (entity.status !== 'grounded' && occurrences.length) {
          label = occurrences[0].display_mention || label
        }
        return { ...entity, label, occurrences }
      })

      result = {
        entities: page,
        total: found.length,
        offset,
        limit,
        stale: Boolean(entitiesRaw.stale || getAssertionsIndex(assertionsRaw, catalogRaw).stale),
        counts: { ...entitiesRaw.counts, ...classificationCounts(getAssertionsIndex(assertionsRaw, catalogRaw).rows, getCatalogIndex(catalogRaw).terms) },
        status_counts: statusCounts,
      }
    } else if (pathname === '/ontology/relations') {
      const [assertionsRaw, catalogRaw] = await Promise.all([
        load('ontology-assertions.json'),
        load('ontology-catalog.json'),
      ])
      const needle = (params.get('q') || '').trim().toLowerCase()
      const doc = params.get('document') || ''
      const scope = params.get('scope') === 'all' ? 'all' : 'in_scope'
      const status = params.get('status') || 'grounded'
      const limit = Math.max(1, Math.min(100, intParam(params, 'limit', 20)))
      const offset = Math.max(0, intParam(params, 'offset', 0))

      if (params.get('group_by') === 'property') {
        if (status === 'unused') throw httpError(422, 'Unused reference properties are available in the Ontology browser')
        const { rows, stale } = getAssertionsIndex(assertionsRaw, catalogRaw)
        const { terms } = getCatalogIndex(catalogRaw)
        const scopeRows = rows.filter(row => !row.original_claim?.measured_comparison && (!doc || row.document_key === doc))
        const filtered = scopeRows.filter(row => {
          if (scope !== 'all' && row.scope !== scope) return false
          const binding = row.bindings?.relation || {}, term = terms[binding.iri] || {}
          return !needle || JSON.stringify([row.original_claim, row.candidate_id, binding.reason,
            term.label, term.id, term.iri, term.definition]).toLowerCase().includes(needle)
        })
        const built = getRelationsRegistry({ ...assertionsRaw, assertions: filtered }, catalogRaw, 'all', '')
        const mentionCounts = { all: filtered.length, grounded: built.summary.grounded_mentions,
          classification: built.summary.broader_mentions, classified: built.summary.classified_mentions,
          unresolved: built.summary.unresolved_mentions }
        const groups = new Map()
        for (const item of built.extracted) {
          if (status !== 'all' && (status === 'classified'
            ? !['grounded', 'classification'].includes(item.status) : item.status !== status)) continue
          const key = item.iri || `unresolved:${item.label.toLowerCase().trim().replace(/\s+/g, ' ')}`
          if (!groups.has(key)) groups.set(key, { ...item, id: `property:${key}`, occurrences: [],
            exact_count: 0, broader_count: 0, raw_variants: [] })
          const group = groups.get(key)
          group.exact_count += item.mapping === 'exact' ? item.occurrence_count : 0
          group.broader_count += item.mapping === 'broader' ? item.occurrence_count : 0
          group.occurrences.push(...item.occurrences.map(o => ({ ...o, mapping: item.mapping })))
          group.raw_variants.push(...item.raw_variants)
        }
        for (const group of groups.values()) {
          const variants = new Map()
          for (const v of group.raw_variants) variants.set(v.label, (variants.get(v.label) || 0) + v.count)
          group.raw_variants = [...variants].map(([label, count]) => ({ label, count }))
          group.occurrence_count = group.occurrences.length
        }
        const ordered = [...groups.values()].sort((a, b) => b.occurrence_count - a.occurrence_count || cmp(a.label.toLowerCase(), b.label.toLowerCase()))
        const inventory = assertionsRaw.extraction_inventory?.filter(r => !doc || r.document_key === doc)
        const identity = row => `${row.document_key}\0${row.candidate_id}`
        const ids = new Set(scopeRows.map(identity))
        const inventoryIds = inventory ? new Set(inventory.map(identity)) : null
        result = { relations: ordered.slice(offset, offset + limit), total: ordered.length, offset, limit, stale,
          group_by: 'property', summary: built.summary, mention_counts: mentionCounts,
          represented_claims: ordered.reduce((n, g) => n + g.occurrence_count, 0),
          extracted_claim_relations: inventoryIds ? inventoryIds.size : null,
          awaiting_grounding_claims: inventoryIds ? [...inventoryIds].filter(id => !ids.has(id)).length : null,
          scope_counts: Object.fromEntries(['in_scope', 'out_of_scope', 'needs_review'].map(k => [k, scopeRows.filter(r => r.scope === k).length])) }
        if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
        return result
      }
      const built = getRelationsRegistry(assertionsRaw, catalogRaw, scope, doc)
      const extracted = needle ? built.extracted.filter(item => built.searchText.get(item).includes(needle)) : built.extracted
      const unused = needle ? built.unused.filter(item => built.searchText.get(item).includes(needle)) : built.unused
      const statusCounts = {
        grounded: extracted.filter(item => item.status === 'grounded').length,
        classification: extracted.filter(item => item.status === 'classification').length,
        classified: extracted.filter(item => ['grounded', 'classification'].includes(item.status)).length,
        unresolved: extracted.filter(item => item.status === 'unresolved').length,
        unused: unused.length,
        all: extracted.length,
      }
      const pool = status === 'unused'
        ? unused
        : status === 'all'
          ? extracted
          : extracted.filter(item => status === 'classified'
            ? ['grounded', 'classification'].includes(item.status) : item.status === status)

      result = {
        relations: pool.slice(offset, offset + limit),
        total: pool.length,
        offset,
        limit,
        stale: getAssertionsIndex(assertionsRaw, catalogRaw).stale,
        summary: built.summary,
        status_counts: statusCounts,
      }
    } else if (pathname === '/ontology/reasoning') {
      const data = await load('ontology-reasoning.json')
      if (data.status === 'paused') {
        result = data
      } else {
        const iri = params.get('iri') || null
        const kind = params.get('kind') || 'derived'
        const limit = Math.max(1, Math.min(100, intParam(params, 'limit', 25)))
        const offset = Math.max(0, intParam(params, 'offset', 0))
        const facts = (data.facts || []).filter(
          f => (kind === 'all' || f.kind === kind) && (!iri || [f.subject, f.predicate, f.object].includes(iri)),
        )
        result = { ...data, facts: facts.slice(offset, offset + limit), total: facts.length, offset, limit }
      }
    } else if (pathname === '/ontology/proof') {
      const data = await load('ontology-reasoning.json')
      if (data.status === 'paused') {
        throw httpError(409, data.message || 'Reasoning is paused.')
      }
      const id = params.get('id') || ''
      const proof = data.proofs?.[id]
      if (!proof) throw httpError(404, 'Inference not found')
      result = proof
    } else {
      const pdfClaimsMatch = pathname.match(/^\/pdfs\/([^/]+)\/claims$/)
      const pdfLogsMatch = pathname.match(/^\/pdfs\/([^/]+)\/logs$/)
      const pdfRecoveryMatch = pathname.match(/^\/pdfs\/([^/]+)\/recovery$/)
      if (pdfClaimsMatch) {
        const id = decodeURIComponent(pdfClaimsMatch[1])
        const data = await load(`pdfs/${id}/claims.json`)
        const limit = Math.max(0, intParam(params, 'limit', 0))
        const offset = Math.max(0, intParam(params, 'offset', 0))
        const allClaims = data.claims || []
        const claims = limit > 0 ? allClaims.slice(offset, offset + limit) : allClaims.slice(offset)
        result = { claims, total: data.total ?? allClaims.length, limit, offset }
      } else if (pdfLogsMatch) {
        const id = decodeURIComponent(pdfLogsMatch[1])
        result = await load(`pdfs/${id}/logs.json`)
      } else if (pdfRecoveryMatch) {
        result = { can_continue: false, reason: 'Published results are read-only.' }
      } else {
        throw httpError(404, 'Static viewer endpoint not found')
      }
    }

    if (options.signal?.aborted) {
      throw new DOMException('Aborted', 'AbortError')
    }
    return result
  }

  return handle
}
