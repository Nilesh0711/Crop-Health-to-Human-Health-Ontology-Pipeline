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
    claims: claimData.claims || [],
    discards: claimData.extraction_rejections || [],
    modelRequests: claimData.model_requests || [],
    logs: logs || [],
    paths: paths || [],
  }
}

export async function loadPaperDetails(request, pdfId, scope, mode) {
  const [graph, claimData, logs, paths, pathwayData, trustData, pathAssessments] = await Promise.all([
    mode === 'assembled' ? Promise.resolve({ nodes: [], edges: [], counts: { nodes: 0, edges: 0 } }) : request(graphEndpoint(pdfId, scope, mode)),
    request(`/pdfs/${pdfId}/claims`), request(`/pdfs/${pdfId}/logs`), request(`/pdfs/${pdfId}/paths`),
    request('/pathways').catch(error => ({ status: 'failed', error: error.message })),
    request('/pathways?view=provisional').catch(error => ({ status: 'failed', error: error.message })),
    request('/pathway-assessments').catch(error => ({ records: [], error: error.message })),
  ])
  return { graph, claims: claimData.claims, discards: claimData.extraction_rejections || [],
    modelRequests: claimData.model_requests || [], logs, paths, pathwayData, trustData, pathAssessments }
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
