import { useEffect, useState, useRef } from 'react'
import { request } from '../lib/api.js'
import { defaultThinking, resumeWithThinking } from '../lib/thinking.js'
import { watchJob } from '../lib/jobPolling.js'

export function usePipelineRun({ workspace, selectedId, setView, setMessage }) {
  const { pdfs, selected, health, readOnly, canExecute, refreshPdfs, refreshDetails, setLogs, clearExtractionResults } = workspace
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [options, setOptions] = useState({ neighbor_max_pairs: 100, max_pages: 0, articles_per_pathway: 8, claims_per_packet: 0,
    extraction_mode: 'target_context', extraction_target_words: 200, extraction_generation_budget: 50000, thinking: defaultThinking() })
  const [claimReviewBusy, setClaimReviewBusy] = useState(false)
  const [recoveryEvent, setRecoveryEvent] = useState(null)
  const [recoveryInfo, setRecoveryInfo] = useState(null)
  const [recoveryRefresh, setRecoveryRefresh] = useState(0)
  const lastDetailsRefreshRef = useRef(0)
  const [confirmingRestart, setConfirmingRestart] = useState(false)
  const [starting, setStarting] = useState(false)
  const [stopPending, setStopPending] = useState(false)
  const isRunning = canExecute && ['running', 'queued', 'stopping'].includes(job?.status)
  const needsRecovery = canExecute && !!selected && !isRunning && (['failed', 'stopped'].includes(selected.status) || recoveryEvent?.pdfId === selectedId)
  const recoveryData = recoveryInfo?.pdfId === selectedId ? recoveryInfo : null
  const runDisabled = !canExecute || starting || isRunning || pdfs.some(item => ['running', 'queued', 'stopping'].includes(item.status))

  useEffect(() => { setConfirmingRestart(false) }, [selectedId])

  useEffect(() => {
    if (!health) return
    const pipeline = health.pipeline || {}
    setOptions(previous => ({ ...previous,
      articles_per_pathway: pipeline.external_top_k ?? previous.articles_per_pathway,
      neighbor_max_pairs: pipeline.neighbor_max_pairs ?? previous.neighbor_max_pairs,
      extraction_mode: pipeline.extraction_mode ?? previous.extraction_mode,
      extraction_target_words: pipeline.extraction_target_words ?? previous.extraction_target_words,
      extraction_generation_budget: pipeline.extraction_generation_budget ?? previous.extraction_generation_budget,
      claims_per_packet: pipeline.extraction_claims_per_packet ?? previous.claims_per_packet,
    }))
  }, [health])

  useEffect(() => {
    if (!canExecute || (!needsRecovery && !selected?.can_resume && !selected?.can_redo_external_validation)) return
    let cancelled = false
    setRecoveryInfo({ pdfId: selectedId, loading: true })
    request(`/pdfs/${selectedId}/recovery`).then(data => {
      if (!cancelled) setRecoveryInfo({ pdfId: selectedId, data, loading: false })
    }).catch(error => {
      if (!cancelled) setRecoveryInfo({ pdfId: selectedId, loading: false, error: error.message })
    })
    return () => { cancelled = true }
  }, [needsRecovery, canExecute, selectedId, selected?.can_resume, selected?.can_redo_external_validation, selected?.latest_job_id, recoveryEvent?.jobId, recoveryRefresh])

  useEffect(() => {
    const paper = pdfs.find(item => item.id === selectedId)
    setJob(null)
    setJobId(paper?.active_job_id || paper?.latest_job_id || null)
  }, [selectedId, selected?.id, selected?.active_job_id, selected?.latest_job_id])

  useEffect(() => {
    if (!jobId || !health) return
    let cancelled = false
    if (readOnly) {
      request(`/jobs/${jobId}`).then(current => { if (!cancelled) setJob(current) })
        .catch(error => { if (!cancelled) setMessage(error.message) })
      return () => { cancelled = true }
    }
    if (!canExecute) return
    const stop = watchJob({
      fetchJob: () => request(`/jobs/${jobId}`),
      onJob: async current => {
        if (cancelled) return
        setJob(current)
        if (current.pdf_id === selectedId) setLogs(current.logs)
        if (['running', 'queued', 'stopping'].includes(current.status)) {
          await refreshPdfs()
          const now = Date.now()
          if (current.pdf_id === selectedId && now - lastDetailsRefreshRef.current >= 15000) {
            lastDetailsRefreshRef.current = now
            refreshDetails(selectedId).catch(() => {})
          }
        } else {
          await refreshPdfs()
          if (current.pdf_id === selectedId) await refreshDetails(selectedId)
          if (!cancelled && ['failed', 'stopped'].includes(current.status) && current.pdf_id === selectedId) {
            setRecoveryEvent({ pdfId: selectedId, jobId: current.id, kind: current.status === 'stopped' ? 'stopped' : current.interrupted ? 'interrupted' : 'failed', error: current.error })
            setMessage(current.status === 'stopped' ? 'Pipeline stopped. Saved progress is available.' : current.error || 'Pipeline failed')
          }
        }
      },
      onMissing: async () => {
        if (cancelled) return
        setJob(null)
        setRecoveryEvent({ pdfId: selectedId, kind: 'missing', jobId })
        try {
          const items = await refreshPdfs()
          if (cancelled) return
          const paper = items.find(item => item.id === selectedId)
          if (paper?.active_job_id && paper.active_job_id !== jobId) {
            setRecoveryEvent(null)
            setJobId(paper.active_job_id)
            setMessage('Reconnected to the current run.')
            return
          }
          await refreshDetails(selectedId)
          if (!cancelled) setMessage(paper?.status === 'completed' || paper?.status === 'completed_with_warnings'
            ? 'Loaded the saved results. The previous live job is no longer available.'
            : 'Live monitoring was interrupted, possibly by a backend restart. Saved findings are preserved. Use Resume failed run when available.')
        } finally {
          if (!cancelled) setJobId(current => current === jobId ? null : current)
        }
      },
      onError: error => { if (!cancelled) setMessage(error.message) },
    })
    return () => { cancelled = true; stop() }
  }, [jobId, selectedId, refreshDetails, refreshPdfs, setLogs, readOnly, canExecute, health, setMessage])

  async function assembleSavedPaths() {
    if (!canExecute || !selected || runDisabled) return
    setStarting(true)
    try {
      const started = await request(`/pdfs/${selected.id}/assemble-pathways`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(options) })
      setJob(started)
      setJobId(started.id)
      setLogs(started.logs || [])
      setMessage(null)
    } catch (error) {
      setMessage(error.message)
    } finally {
      setStarting(false)
    }
  }

  async function stopRun() {
    if (!canExecute || !job?.id || job.pdf_id !== selectedId || !isRunning || stopPending || job.status === 'stopping') return
    setStopPending(true)
    try {
      const updated = await request(`/jobs/${job.id}/stop`, { method: 'POST' })
      setJob(updated)
      setLogs(updated.logs || [])
      setView('activity')
    } catch (error) { setMessage(error.message) }
    finally { setStopPending(false) }
  }

  async function startRun(resume = false, runOptions = options, redoExternal = false) {
    if (!canExecute || !selected || runDisabled) return
    setStarting(true)
    setMessage(null)
    try {
      const started = await request(`/pdfs/${selected.id}/${redoExternal ? 'redo-external-validation' : resume ? 'resume' : 'run'}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(runOptions) })
      setRecoveryEvent(null)
      setRecoveryInfo(null)
      setConfirmingRestart(false)
      if (resume) setOptions(runOptions)
      setJob(started)
      setJobId(started.id)
      setLogs(started.logs || [])
      if (!resume) {
        clearExtractionResults()
      }
      setView('activity')
    } catch (error) {
      setMessage(error.message)
      setLogs(previous => [...previous, { at: new Date().toISOString(), level: 'error', message: error.message }])
      setRecoveryRefresh(value => value + 1)
    } finally {
      setStarting(false)
    }
  }

  function continueSavedRun() {
    startSavedAssessment(false)
  }

  function redoSavedExternalValidation() {
    startSavedAssessment(true)
  }

  function startSavedAssessment(redoExternal) {
    if (recoveryData?.data?.can_continue) {
      const saved = recoveryData.data.options
      startRun(true, { ...resumeWithThinking(saved, options), thinking: { ...options.thinking, extraction: saved.thinking.extraction } }, redoExternal)
    }
  }

  async function runClaimAction(candidateId, action, note = '', reviewRevision) {
    if (!canExecute || !selected) return
    setMessage(null)
    const manual = /^(accept|reject)_(source|scope)$/.exec(action)
    if (manual) {
      setClaimReviewBusy(true)
      try {
        const result = await request(`/pdfs/${selected.id}/claims/${candidateId}/source-review`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dimension: manual[2] === 'source' ? 'source_fidelity' : 'project_scope', decision: manual[1], expected_revision: reviewRevision, note }),
        })
        setMessage(result.message)
        await refreshPdfs()
        await refreshDetails()
      } catch (error) { setMessage(error.message) }
      finally { setClaimReviewBusy(false) }
      return
    }
    if (!['repair','reextract'].includes(action)) return
    try {
      const started = await request(`/pdfs/${selected.id}/claims/${candidateId}/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(options),
      })
      setJob(started)
      setJobId(started.id)
      setLogs(started.logs || [])
    } catch (error) {
      setMessage(error.message)
      setLogs(previous => [...previous, { at: new Date().toISOString(), level: 'error', message: error.message }])
    }
  }

  return { job, setJob, jobId, setJobId, options, setOptions, claimReviewBusy, recoveryEvent, recoveryData,
    confirmingRestart, setConfirmingRestart, starting, stopPending, isRunning, needsRecovery, runDisabled,
    assembleSavedPaths, stopRun, startRun, continueSavedRun, redoSavedExternalValidation, runClaimAction,
    checkRecovery: () => setRecoveryRefresh(value => value + 1) }
}
