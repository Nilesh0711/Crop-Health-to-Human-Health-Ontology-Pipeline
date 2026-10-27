// Schedule after each completed poll so slow requests cannot overlap.
export function watchJob({ fetchJob, onJob, onMissing, onError, intervalMs = 2000,
  schedule = setTimeout, unschedule = clearTimeout }) {
  let stopped = false
  let timer
  const again = () => { if (!stopped) timer = schedule(poll, intervalMs) }
  const poll = async () => {
    let current
    try {
      current = await fetchJob()
    } catch (error) {
      if (stopped) return
      if (error.status === 404) {
        stopped = true
        try { await onMissing() } catch (recoveryError) { onError(recoveryError) }
      } else {
        onError(error)
        again()
      }
      return
    }
    if (stopped) return
    try { await onJob(current) } catch (error) { if (!stopped) onError(error) }
    if (['running', 'queued', 'stopping'].includes(current.status)) again()
  }
  poll()
  return () => { stopped = true; unschedule(timer) }
}
