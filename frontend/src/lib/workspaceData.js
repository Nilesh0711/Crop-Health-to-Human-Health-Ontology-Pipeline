export function graphEndpoint(pdfId, scope, mode) {
  const graph = mode === 'audit' ? 'audit-graph' : mode === 'source' ? 'source-graph' : 'graph'
  return scope === 'all' ? `/${graph}` : `/pdfs/${pdfId}/${graph}`
}

export async function loadPaperClaims(request, pdfId) {
  const [claimData, logs, paths] = await Promise.all([
    request(`/pdfs/${pdfId}/claims`),
    request(`/pdfs/${pdfId}/logs`),
    request(`/pdfs/${pdfId}/paths`),
  ])
  return {
    claims: claimData?.claims || [],
    discards: claimData?.extraction_rejections || [],
    modelRequests: claimData?.model_requests || [],
    logs: logs || [],
    paths: paths || [],
  }
}

// Caching global pathway promises per request client.
// Ensures browser downloads the 38MB global pathway network only once across paper switches.
const globalPathwayCaches = new WeakMap()

function getGlobalPathwayPromises(request) {
  if (typeof request !== 'function') return null
  let cache = globalPathwayCaches.get(request)
  if (!cache) {
    cache = {
      pathways: request('/pathways').catch(error => {
        cache.pathways = null
        return { status: 'failed', error: error.message }
      }),
      provisional: request('/pathways?view=provisional').catch(error => {
        cache.provisional = null
        return { status: 'failed', error: error.message }
      }),
      assessments: request('/pathway-assessments').catch(error => {
        cache.assessments = null
        return { records: [], error: error.message }
      }),
    }
    globalPathwayCaches.set(request, cache)
  }
  return cache
}

export async function loadPaperDetails(request, pdfId, scope, mode, claimsPromise = null) {
  const graphPromise = mode === 'assembled'
    ? Promise.resolve({ nodes: [], edges: [], counts: { nodes: 0, edges: 0 } })
    : request(graphEndpoint(pdfId, scope, mode))

  // Reuse in-flight claims promise if provided to eliminate duplicate network traffic
  const paperPromise = claimsPromise || loadPaperClaims(request, pdfId)

  // Use memoized global promises
  const globals = getGlobalPathwayPromises(request)
  const pathwaysPromise = globals ? globals.pathways : request('/pathways').catch(error => ({ status: 'failed', error: error.message }))
  const provisionalPromise = globals ? globals.provisional : request('/pathways?view=provisional').catch(error => ({ status: 'failed', error: error.message }))
  const assessmentsPromise = globals ? globals.assessments : request('/pathway-assessments').catch(error => ({ records: [], error: error.message }))

  const [graph, claimPayload, pathwayData, trustData, pathAssessments] = await Promise.all([
    graphPromise,
    paperPromise,
    pathwaysPromise,
    provisionalPromise,
    assessmentsPromise,
  ])

  return {
    graph,
    claims: claimPayload.claims,
    discards: claimPayload.discards || [],
    modelRequests: claimPayload.modelRequests || [],
    logs: claimPayload.logs || [],
    paths: claimPayload.paths || [],
    pathwayData,
    trustData,
    pathAssessments,
  }
}

export function createLatestDetailsLoader() {
  let revision = 0
  return async (...args) => {
    const ticket = ++revision
    try {
      const details = await loadPaperDetails(...args)
      return ticket === revision ? details : null
    } catch (error) {
      if (ticket === revision) throw error
      return null
    }
  }
}
