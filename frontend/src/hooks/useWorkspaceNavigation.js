import { useCallback, useEffect, useState } from 'react'

export function initialWorkspaceView(hash) {
  const view = String(hash || '').replace(/^#/, '')
  if (['graph', 'paths'].includes(view)) return 'network'
  return ['claims', 'network', 'activity', 'details'].includes(view) ? view : 'claims'
}

export function useWorkspaceNavigation() {
  const [selectedId, setSelectedId] = useState(() => typeof window === 'undefined' ? null : new URLSearchParams(window.location.search).get('paper'))
  const [view, setViewState] = useState(() => initialWorkspaceView(typeof window === 'undefined' ? '' : window.location.hash))
  const setView = useCallback(value => setViewState(current => initialWorkspaceView(typeof value === 'function' ? value(current) : value)), [])
  const [sidebarHidden, setSidebarHidden] = useState(() => {
    try { return typeof window !== 'undefined' && window.localStorage.getItem('soil-health.sidebar-hidden') === 'true' }
    catch { return false }
  })

  useEffect(() => {
    if (!selectedId) return
    const location = new URL(window.location.href)
    location.searchParams.set('paper', selectedId)
    location.hash = view
    window.history.replaceState(null, '', location)
  }, [selectedId, view])

  useEffect(() => {
    try { window.localStorage.setItem('soil-health.sidebar-hidden', String(sidebarHidden)) }
    catch { /* The toggle still works when browser storage is unavailable. */ }
  }, [sidebarHidden])

  return { selectedId, setSelectedId, view, setView, sidebarHidden, setSidebarHidden }
}
