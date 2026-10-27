import { CircleAlert, LoaderCircle, Play, RotateCcw } from 'lucide-react'

export default function RunRecoveryNotice({ kind = 'failed', jobId, error, recovery, loading,
  checkError, disabled, confirmingRestart, onContinue, onAskRestart, onCancelRestart,
  onRestart, onViewActivity, onCheckAgain }) {
  const title = kind === 'stopped' ? 'Run stopped' : kind === 'missing' ? 'Job ID not found' : kind === 'interrupted' ? 'Run interrupted' : 'Run failed'
  const canContinue = !loading && !checkError && recovery?.can_continue === true
  return <section className="run-recovery" role="alert" aria-label="Run recovery">
    <CircleAlert size={21} aria-hidden="true" />
    <div className="run-recovery-content"><h2>{title}</h2>
      <p>{kind === 'missing' ? 'The previous job ID is no longer available. Existing saved findings are preserved.'
        : 'The pipeline stopped before finishing. Existing saved findings are preserved.'} How would you like to proceed?</p>
      {jobId && <p className="run-recovery-id">Job ID: <code>{jobId}</code></p>}
      {loading ? <p className="run-recovery-check"><LoaderCircle size={14} className="spin" /> Checking saved progress…</p>
        : checkError ? <p>Could not check saved progress: {checkError} <button type="button" className="text-button" onClick={onCheckAgain} disabled={disabled}>Check again</button></p>
        : canContinue ? <p>Continue reuses compatible completed steps and runs the unfinished work.</p>
          : <p>Continue is unavailable: {recovery?.reason || 'No verified checkpoint is available.'}</p>}
      {error && <details><summary>Why the run stopped</summary><p className="run-recovery-error">{error}</p></details>}
      {confirmingRestart ? <div className="run-recovery-confirm" role="group" aria-label="Confirm pipeline restart">
        <strong>Restart this pipeline from the beginning?</strong>
        <p>This replaces the current run’s claims, validation results and activity log. Extraction will run again.</p>
        <div className="run-recovery-actions">
          <button className="button primary" type="button" disabled={disabled} onClick={onRestart}><RotateCcw size={15} /> Confirm restart</button>
          <button className="button secondary" type="button" disabled={disabled} onClick={onCancelRestart}>Cancel</button>
        </div>
      </div> : <div className="run-recovery-actions">
        <button className="button primary" type="button" disabled={disabled || !canContinue} onClick={onContinue}><Play size={15} /> Continue saved run</button>
        <button className="button secondary" type="button" disabled={disabled} onClick={onAskRestart}><RotateCcw size={15} /> Restart from beginning</button>
        <button className="text-button" type="button" onClick={onViewActivity}>View activity log</button>
      </div>}
    </div>
  </section>
}
