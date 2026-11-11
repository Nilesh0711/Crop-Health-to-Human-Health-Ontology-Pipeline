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

export function createStaticHandler(fetchJson) {
  const cache = new Map()
  const catalogIndexCache = new WeakMap()
  const assertionsIndexCache = new WeakMap()
  const entitiesIndexCache = new WeakMap()

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

  function getAssertionsIndex(assertionsData) {
    let idx = assertionsIndexCache.get(assertionsData)
    if (idx) return idx
    const rows = assertionsData.assertions || []
    const byKey = new Map()
    const searchText = new WeakMap()
    for (const row of rows) {
      byKey.set(`${row.document_key}\0${row.candidate_id}`, row)
      searchText.set(row, JSON.stringify(row.original_claim || {}).toLowerCase())
    }
    idx = { rows, byKey, searchText, stale: Boolean(assertionsData.stale) }
    assertionsIndexCache.set(assertionsData, idx)
    return idx
  }

  function getEntitiesIndex(entitiesData) {
    let idx = entitiesIndexCache.get(entitiesData)
    if (idx) return idx
    const raw = entitiesData.entities || []
    const groupedMap = new Map()
    for (const e of raw) {
      const key = `${(e.label || '').trim().toLowerCase()}\0${e.iri || ''}\0${e.mapping || ''}`
      if (!groupedMap.has(key)) {
        groupedMap.set(key, { ...e, occurrences: [...(e.occurrences || [])] })
      } else {
        groupedMap.get(key).occurrences.push(...(e.occurrences || []))
      }
    }
    const uniqueList = [...groupedMap.values()]
    idx = { raw, uniqueList }
    entitiesIndexCache.set(entitiesData, idx)
    return idx
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
      result = await load('ontology-status.json')
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
      const { rows: allAssertions, searchText, stale } = getAssertionsIndex(assertionsRaw)
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
        total: rows.length,
        offset,
        limit,
        stale,
        unprocessed_documents: [],
      }
    } else if (pathname === '/ontology/entities') {
      const [entitiesRaw, assertionsRaw] = await Promise.all([
        load('ontology-entities.json'),
        load('ontology-assertions.json'),
      ])
      const needle = (params.get('q') || '').trim().toLowerCase()
      const doc = params.get('document') || ''
      const status = params.get('status') || 'all'
      const unique = params.get('unique') === 'true' || params.get('unique') === '1'
      const limit = Math.max(1, Math.min(100, intParam(params, 'limit', 25)))
      const offset = Math.max(0, intParam(params, 'offset', 0))

      const { byKey: assertionMap } = getAssertionsIndex(assertionsRaw)
      const { raw, uniqueList } = getEntitiesIndex(entitiesRaw)
      const entityList = unique ? uniqueList : raw

      const statusCounts = { grounded: 0, vocabulary_gap: 0, alignment_review: 0, classification: 0, review: 0, all: 0 }
      const found = []
      for (const entity of entityList) {
        const occurrences = doc
          ? (entity.occurrences || []).filter(o => o.document_key === doc)
          : (entity.occurrences || [])
        if (!occurrences.length) continue
        if (needle && !JSON.stringify([entity.label, entity.iri, occurrences]).toLowerCase().includes(needle)) continue

        statusCounts[entity.status] = (statusCounts[entity.status] || 0) + 1
        const category = entity.mapping === 'broader'
          ? 'classification'
          : (entity.mapping === 'related' || entity.mapping === 'narrower')
            ? 'review'
            : entity.status
        if (category === 'classification' || category === 'review') {
          statusCounts[category] += 1
        }
        statusCounts.all += 1

        const selectedBucket = (status === 'classification' || status === 'review') ? category : entity.status
        if (status !== 'all' && selectedBucket !== status) continue
        found.push({ ...entity, occurrences, occurrence_count: occurrences.length })
      }

      const page = found.slice(offset, offset + limit).map(entity => {
        const occurrences = []
        for (const occ of entity.occurrences) {
          const row = assertionMap.get(`${occ.document_key}\0${occ.candidate_id}`)
          if (!row) continue
          const claim = row.original_claim || {}
          const s = claim.subject || ''
          const sc = claim.subject_context || ''
          const r = claim.relation || ''
          const o = claim.object || ''
          const oc = claim.object_context || ''
          const sLbl = sc ? `${s} (${sc})` : s
          const oLbl = oc ? `${o} (${oc})` : o
          const formatted = claim.claim_text || (s && r && o ? `${sLbl} → ${r} → ${oLbl}` : '')
          occurrences.push({
            ...occ,
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
        stale: Boolean(entitiesRaw.stale),
        counts: entitiesRaw.counts || {},
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
