import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { request } from '../lib/api.js'
import { isViewer, canExecute as executionAllowed } from '../lib/capabilities.js'
import { createLatestDetailsLoader, graphEndpoint, loadPaperClaims } from '../lib/workspaceData.js'

export function useWorkspaceData({ selectedId, setSelectedId, graphScope, graphMode, onError }) {
  const [pdfs, setPdfs] = useState([])
  const [health, setHealth] = useState(null)
  const [initialLoading, setInitialLoading] = useState(true)
  const [paperLoading, setPaperLoading] = useState(false)
  const [details, setDetails] = useState({ graph: { nodes: [], edges: [], counts: { nodes: 0, edges: 0, corroborated: 0 } },
    claims: [], discards: [], modelRequests: [], paths: [], logs: [], pathwayData: null, trustData: null, pathAssessments: null })
  const [savedJobs, setSavedJobs] = useState([])
  const selection = useRef(null)
  selection.current = { selectedId, graphScope, graphMode }
  const prevSelection = useRef({ selectedId, graphScope, graphMode })
  const readOnly = isViewer(health)
  const canExecute = executionAllowed(health)
  const loadLatestDetails = useMemo(() => createLatestDetailsLoader(), [])

  const refreshPdfs = useCallback(async () => {
    try {
      const items = await request('/pdfs')
      setPdfs(items)
      setSelectedId(current => {
        if (current && items.some(item => item.id === current)) return current
        const active = items.find(item => ['running', 'queued', 'stopping'].includes(item.status))
        return active?.id || items[0]?.id || null
      })
      return items
    } finally {
      setInitialLoading(false)
    }
  }, [setSelectedId])

  const refreshDetails = useCallback(async (pdfId = selectedId, scope = graphScope, mode = graphMode) => {
    if (!pdfId) {
      setPaperLoading(false)
      return
    }
    setPaperLoading(true)
    try {
      loadPaperClaims(request, pdfId).then(claimData => {
        if (selection.current.selectedId === pdfId) {
          setDetails(previous => ({ ...previous, ...claimData }))
        }
      }).catch(() => {})

      const loaded = await loadLatestDetails(request, pdfId, scope, mode)
      const current = selection.current
      if (!loaded || current.selectedId !== pdfId || current.graphScope !== scope || current.graphMode !== mode) return
      setDetails(loaded)
    } finally {
      if (selection.current.selectedId === pdfId) {
        setPaperLoading(false)
      }
    }
  }, [selectedId, graphScope, graphMode, loadLatestDetails])

  const setLogs = useCallback(value => setDetails(previous => ({ ...previous,
    logs: typeof value === 'function' ? value(previous.logs) : value })), [])
  const setPathwayData = useCallback(value => setDetails(previous => ({ ...previous,
    pathwayData: typeof value === 'function' ? value(previous.pathwayData) : value })), [])
  const setTrustData = useCallback(value => setDetails(previous => ({ ...previous,
    trustData: typeof value === 'function' ? value(previous.trustData) : value })), [])
  const clearExtractionResults = useCallback(() => setDetails(previous => ({ ...previous,
    claims: [], discards: [], modelRequests: [] })), [])

  useEffect(() => {
    refreshPdfs().catch(error => onError(error.message))
    request('/health').then(setHealth)
      .catch(() => setHealth({ extraction: { ready: false }, validation: { ready: false } }))
  }, [refreshPdfs, onError])

  useEffect(() => {
    if (!selectedId) return
    const prev = prevSelection.current
    const paperChanged = prev.selectedId !== selectedId
    prevSelection.current = { selectedId, graphScope, graphMode }

    if (paperChanged) {
      setPaperLoading(true)
      refreshDetails(selectedId, graphScope, graphMode)
        .catch(error => onError(error.message))
        .finally(() => setPaperLoading(false))
    } else {
      if (graphMode === 'assembled') {
        setDetails(previous => ({ ...previous, graph: { nodes: [], edges: [], counts: { nodes: 0, edges: 0 } } }))
      } else {
        request(graphEndpoint(selectedId, graphScope, graphMode))
          .then(graph => {
            if (selection.current.selectedId === selectedId && selection.current.graphScope === graphScope && selection.current.graphMode === graphMode) {
              setDetails(previous => ({ ...previous, graph }))
            }
          })
          .catch(error => onError(error.message))
      }
    }
  }, [selectedId, graphScope, graphMode, refreshDetails, onError])

  useEffect(() => {
    if (!readOnly || !selectedId) return
    let cancelled = false
    setSavedJobs([])
    request(`/pdfs/${selectedId}/jobs`).then(rows => { if (!cancelled) setSavedJobs(rows) })
      .catch(error => { if (!cancelled) onError(error.message) })
    return () => { cancelled = true }
  }, [readOnly, selectedId, onError])

  async function upload(event) {
    if (!canExecute) return
    const file = event.target.files?.[0]
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    try {
      const result = await request('/pdfs', { method: 'POST', body: form })
      await refreshPdfs()
      setSelectedId(result.id)
      onError(`${result.name} is ready to run`)
    } catch (error) { onError(error.message) }
    event.target.value = ''
  }

  return { pdfs, selected: pdfs.find(item => item.id === selectedId), health, readOnly, canExecute, savedJobs,
    initialLoading, paperLoading, setPaperLoading,
    ...details, refreshPdfs, refreshDetails, setLogs, setPathwayData, setTrustData, clearExtractionResults, upload }
}
