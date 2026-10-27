import assert from 'node:assert/strict'
import { createServer } from 'vite'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const server = await createServer({ server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true, entries: [] }, appType: 'custom' })
try {
  const { CitationTracking, JointAssessment, ClaimActions, ClaimContext, ValidationTrace, ExternalDecisionDetails, ExtractionDiscards } = await server.ssrLoadModule('/src/components/claims/ClaimDetails.jsx')
  const { default: ModelPerformance } = await server.ssrLoadModule('/src/components/diagnostics/ModelPerformance.jsx')
  const render = (component, props) => renderToStaticMarkup(React.createElement(component, props))
  const candidate = { subject: 'Crop R', object: 'toxin T', relation: 'has_contaminant', subject_type: 'Crop',
    object_type: 'Contaminant', claim_text: 'Crop R may contain toxin T.', claim_text_origin: 'extracted', certainty: 'conditional',
    qualifiers: { condition: 'disease Z' }, source_validation: { decision: 'needs_review', claim_text_match: true } }
  const repair = render(ClaimActions, { candidate: { ...candidate,
    source_validation: { decision: 'needs_review', reasons: ['Subject is not grounded'] } }, onAction: () => {} })
  assert.match(repair, /Repair and revalidate claim/)
  assert.match(repair, /Subject is not grounded/)
  assert.match(repair, /One source-grounded correction per click/)
  const busyRepair = render(ClaimActions, { candidate, busy: true, onAction: () => {} })
  assert.match(busyRepair, /disabled/)
  const retry = render(ClaimActions, { candidate: { ...candidate,
    semantic_validation: { decision: 'accepted' }, source_validation: { decision: 'entailed' } },
    validation: { validation_status: 'pipeline_error', error: 'HTTP timeout' }, onAction: () => {} })
  assert.match(retry, /Retry unfinished external checks/)
  assert.match(retry, /HTTP timeout/)
  assert.doesNotMatch(retry, /Repair and revalidate claim/)
  const finished = render(ClaimActions, { candidate: { ...candidate,
    semantic_validation: { decision: 'accepted' }, source_validation: { decision: 'entailed' } },
    validation: { validation_status: 'external_evidence_not_found' }, onAction: () => {} })
  assert.doesNotMatch(finished, /Retry unfinished|Repair and revalidate/)
  assert.match(finished, /Reassess external evidence/)
  const conflict = render(ClaimActions, { candidate: { ...candidate, source_validation: { decision: 'entailed' } },
    validation: { validation_status: 'world_contested_pending_review' }, repairEligibility: { allowed: true,
      external_contradictions: [{ rationale: 'Opposite finding in a compatible experiment.', evidence: [{ quoted_span: 'A real opposite result.' }] }] }, onAction: () => {} })
  assert.match(conflict, /Proof-backed contradiction/)
  assert.match(conflict, /Repair and revalidate claim/)
  assert.match(conflict, /A real opposite result/)
  const unverifiedConflict = render(ClaimActions, { candidate: { ...candidate, source_validation: { decision: 'entailed' } },
    validation: { validation_status: 'world_contested_pending_review', attempts: [{ decisions: [{ decision: 'contradicted', evidence: [{ quoted_span: 'Unverified words.' }] }] }] },
    repairEligibility: { allowed: false, external_contradictions: [] }, onAction: () => {} })
  assert.doesNotMatch(unverifiedConflict, /Repair and revalidate claim/)
  const joint = render(JointAssessment, { validation: { attempts: [{ joint_assessment: { synthesis: 'Context-dependent findings.',
    guarded_counts: { supported: 2, contradicted: 1, needs_review: 1 }, semantic_assessment: { rationale: 'Quoted study context.' } } }] } })
  assert.match(joint, /ONE PRO REQUEST/)
  const staged = render(JointAssessment, { validation: { attempts: [{ joint_assessment: {
    policy: 'per_paper_synthesis-context-v1', paper_judgements: 8, reused_paper_judgements: 3,
    thinking: false, synthesis: 'Compatible findings with preserved conditions.', semantic_assessment: {},
    guarded_counts: { supported: 2, contradicted: 0, needs_review: 1 }
  } }] } })
  assert.match(staged, /PER-PAPER JUDGEMENTS/)
  assert.match(staged, /Thinking: Off/)
  assert.match(staged, /8 paper judgements, 3 reused/)
  assert.match(joint, /After proof checks: 2 supporting/)
  const outside = render(JointAssessment, { validation: { attempts: [{ joint_assessment: {
    semantic_review_required: true, semantic_assessment: { in_scope: false, rationale: 'Unrelated medical intervention.' }
  } }] } })
  assert.match(outside, /Outside scope/)
  assert.match(outside, /excluded from validated graph and pathway assembly/)

  const unverified = render(ClaimContext, { candidate })
  assert.match(unverified, /Unverified restatement/)
  assert.match(unverified, /disease Z/)
  assert.doesNotMatch(unverified, />Source verified</)
  assert.match(render(ClaimContext, { candidate: { ...candidate, source_validation: { decision: 'entailed', claim_text_match: true } } }), /Source verified/)
  const sourceContext = render(ClaimContext, { candidate: { ...candidate,
    source_section_context: '[12, PDF page 3] Fusarium head blight (FHB) affects wheat.' } })
  assert.match(sourceContext, /Same-section source context/)
  assert.match(sourceContext, /\[12, PDF page 3\]/)
  assert.match(sourceContext, /Fusarium head blight \(FHB\) affects wheat/)
  assert.match(render(ValidationTrace, { candidate }), /Not evaluated/)
  const expandedNames = render(ValidationTrace, { candidate: { ...candidate, source_validation: {
    decision: 'entailed', applied_name_corrections: [{ field: 'subject', before: 'FHB',
      after: 'Fusarium head blight', definition_quote: 'Fusarium head blight (FHB)', rationale: 'Defined in the source.' }]
  } } })
  assert.match(expandedNames, /Source-verified full names/)
  assert.match(expandedNames, /Fusarium head blight/)
  const unchangedNames = render(ValidationTrace, { candidate: { ...candidate, source_validation: {
    decision: 'entailed', unchanged_name_annotations: [{ field: 'subject', before: 'Fusarium head blight', after: 'Fusarium head blight' }]
  } } })
  assert.match(unchangedNames, /already spelled out/)
  assert.doesNotMatch(unchangedNames, /Source-verified full names/)
  const undefinedNames = render(ValidationTrace, { candidate: { ...candidate, source_validation: {
    decision: 'needs_review', abbreviation_review: { status: 'unresolved', unresolved: ['XYZ'] }
  } } })
  assert.match(undefinedNames, /Unresolved source abbreviations: XYZ/)
  const hinted = render(ValidationTrace, { candidate: { ...candidate, extraction_review_hints: ['review_comparator_or_metric'] } })
  assert.match(hinted, /Relations are open; entity labels are provisional/)
  assert.match(hinted, /claim retained/)
  assert.match(hinted, /review comparator or metric/)
  assert.match(render(ExternalDecisionDetails, { decision: { guardrail_reasons: ['Missing literal trigger'],
    rejected_model_decision: { evidence: [{ quoted_span: 'A valid sentence.', relation_trigger: 'made-up trigger' }] } } }), /not accepted as proof/)
  const separatedEvidence = render(ExternalDecisionDetails, { decision: { evidence: [{ chunk_id: 'paper-a:0',
    sentence_numbers: [4, 6, 8], is_contiguous: false, fragments: ['Sentence four proof.', 'Sentence six proof.', 'Sentence eight proof.'],
    intervening_context: [{ number: 5, text: 'A limiting condition.' }, { number: 7, text: 'A separate experiment.' }] }] } })
  assert.match(separatedEvidence, /selected sentences: 4, 6, 8/)
  assert.match(separatedEvidence, /individual sentences, not a range/)
  assert.match(separatedEvidence, /context only, not selected proof/)
  assert.match(separatedEvidence, /Sentence 5:/)
  assert.match(separatedEvidence, /A limiting condition/)
  assert.match(separatedEvidence, /A separate experiment/)
  assert.equal(render(ExtractionDiscards, { records: [] }), '')
  const performance = render(ModelPerformance, { requests: [{ task: 'extraction packet 1', model: 'model-X', elapsed_seconds: 10,
    usage_reported: true, reasoning_usage_reported: true, output_tokens: 10, reasoning_tokens: 390,
    effective_output_tps: 1, effective_generated_tps: 40, requested_reasoning_effort: 'low' }] })
  assert.match(performance, /neither is an engine-only decode benchmark/)
  assert.match(performance, /40\.00/)
  assert.match(performance, /390/)
  const routed = render(ModelPerformance, { summary: {
    extraction_model: 'opencode/cat-jam-free#high', validation_model: 'opencode/cat-jam-free#high',
    helper_model: 'opencode/cat-jam-free#medium', llm_calls: { total_requests: 6 },
    llm_reasoning_policy: { extraction: { requested_reasoning_effort: 'high' },
      validation: { requested_reasoning_effort: 'high' }, helper: { requested_reasoning_effort: 'medium' } },
  } })
  assert.match(routed, /cat-jam-free#high/)
  assert.match(routed, /cat-jam-free#medium/)
  assert.match(routed, /Requested effort: medium/)
  assert.doesNotMatch(routed, /Step 5|LongCat/)
  const stageRouting = { scope: 'mimo', extraction: 'longcat', semantic: 'glm',
    source: 'deepseek', query: 'mimo', external: 'nemotron', external_priority: 'mimo-pro' }
  const stageCards = render(ModelPerformance, { requests: [{ task: 'source: example', model: 'glm',
    status: 'ok', elapsed_seconds: 1, primary_model: 'deepseek', stage: 'source' }], routing: stageRouting })
  for (const label of ['Document screening', 'Extraction', 'Type checking', 'Source verification', 'Search planning', 'External evidence', 'Top independent article']) {
    assert.ok(stageCards.includes(label), `Missing task card: ${label}`)
  }
  assert.match(stageCards, /longcat/)
  assert.match(stageCards, /nemotron/)
  assert.match(render(ExternalDecisionDetails, { decision: { judge: {
    tier: 'priority', primary_model: 'opencode-go/mimo-v2.6-pro', actual_model: 'opencode-go/deepseek-v4.1-flash',
  } } }), /Judge: opencode-go\/deepseek-v4.1-flash \(priority\)/)
  const { matchesClaim, initialWorkspaceView, ClaimCard } = await server.ssrLoadModule('/src/App.jsx')
  const { PdfQualityReport, RequestProgress, ResumeRunButton, WorkspaceNavigation, FindingsToolbar, ActivityLog, RunSetup, SidebarToggle, PaperRunStatus } = await server.ssrLoadModule('/src/components/workspace/WorkbenchPanels.jsx')
  const { default: RunRecoveryNotice } = await server.ssrLoadModule('/src/components/workspace/RunRecoveryNotice.jsx')
  const recoverable = render(RunRecoveryNotice, { kind: 'missing', jobId: 'lost-job-123',
    recovery: { can_continue: true }, onContinue: () => {}, onAskRestart: () => {} })
  for (const label of ['Job ID not found', 'lost-job-123', 'How would you like to proceed?',
    'Continue saved run', 'Restart from beginning', 'View activity log', 'reuses compatible completed steps']) assert.ok(recoverable.includes(label))
  assert.doesNotMatch(recoverable, /disabled|Confirm restart/)
  const unrecoverable = render(RunRecoveryNotice, { kind: 'failed', recovery: {
    can_continue: false, reason: 'Extraction has not completed; start a new run' } })
  assert.match(unrecoverable, /Run failed/)
  assert.match(unrecoverable, /Continue is unavailable: Extraction has not completed/)
  assert.match(unrecoverable, /disabled=""[^>]*>.*?Continue saved run/)
  const checking = render(RunRecoveryNotice, { kind: 'interrupted', loading: true })
  assert.match(checking, /Run interrupted/)
  assert.match(checking, /Checking saved progress/)
  const confirmation = render(RunRecoveryNotice, { confirmingRestart: true })
  assert.match(confirmation, /Confirm pipeline restart/)
  assert.match(confirmation, /replaces the current run/)
  assert.match(confirmation, /Confirm restart/)
  assert.match(confirmation, /Cancel/)
  assert.doesNotMatch(confirmation, /Continue saved run/)
  const recoveryUnavailable = render(RunRecoveryNotice, { checkError: 'Backend unavailable' })
  assert.match(recoveryUnavailable, /Could not check saved progress: Backend unavailable/)
  assert.match(recoveryUnavailable, /Check again/)
  assert.equal(render(ResumeRunButton, { canResume: false }), '')
  assert.match(render(ResumeRunButton, { canResume: true, disabled: true }), /disabled/)
  assert.match(render(ResumeRunButton, { canResume: true }), /Resume run/)
  assert.match(render(RequestProgress, { running: true, progress: { model: 'DeepSeek Flash',
    task: 'Source batch: formatting repair 1/1', status: 'repairing_format', started_at: new Date().toISOString(),
    input_tokens: null, input_tokens_reported: false, timeout_seconds: 300, provider_status: 'Waiting' } }), /Repairing response formatting \(1 attempt\)/)
  assert.match(render(RequestProgress, { running: true, progress: { model: 'DeepSeek Flash',
    task: 'Source batch: response regeneration 1/1', status: 'regenerating_response', started_at: new Date().toISOString(),
    input_tokens: null, input_tokens_reported: false, timeout_seconds: 300, provider_status: 'Waiting' } }), /Regenerating response \(1 attempt\)/)
  const recoveryPending = render(RequestProgress, { running: true, progress: { model: 'MiMo Pro',
    task: 'joint external: claim', status: 'incomplete_response', recovery_pending: true,
    recovery_action: 'response_regeneration', output_limit_tokens: 28192,
    started_at: new Date().toISOString(), timeout_seconds: 1200,
    provider_status: 'Response incomplete; regenerating response (1/1) with the same model' } })
  assert.match(recoveryPending, /Regenerating response \(1 attempt\)/)
  assert.match(recoveryPending, /28,192/)
  assert.match(recoveryPending, /generation token limit \(answer \+ reasoning\)/)
  const warningLog = render(ActivityLog, { running: true, logs: [{ level: 'warning',
    message: 'Response truncated; regenerating response (recovery 1/1)' }] })
  assert.match(warningLog, /log-entry warning/)
  assert.match(warningLog, /regenerating response/)
  assert.doesNotMatch(warningLog, /log-entry error/)
  const openSidebar = render(SidebarToggle, { hidden: false, onToggle: () => {} })
  const hiddenSidebar = render(SidebarToggle, { hidden: true, onToggle: () => {} })
  assert.match(openSidebar, /aria-label="Hide paper library"/)
  assert.match(openSidebar, /aria-expanded="true"/)
  assert.match(hiddenSidebar, /aria-label="Show paper library"/)
  assert.match(hiddenSidebar, /aria-expanded="false"/)
  for (const [status, label] of [['not_started', 'Not run yet'], ['running', 'Running'], ['completed', 'Run completed'],
    ['failed', 'Run failed'], ['completed_with_warnings', 'Completed with warnings'], ['blocked_text_quality', 'Blocked: text quality']]) {
    assert.ok(render(PaperRunStatus, { paper: { status } }).includes(label), `Missing readable PDF status: ${status}`)
  }
  const searchRecord = { candidate, validation: { attempts: [{ evidence_documents: [{ title: 'Controlled rice contamination experiment' }] }] } }
  assert.ok(matchesClaim(searchRecord, '  CROP R  '))
  assert.ok(matchesClaim(searchRecord, 'disease Z'))
  assert.ok(matchesClaim(searchRecord, 'rice contamination'))
  assert.ok(!matchesClaim(searchRecord, 'unrelated article'))
  assert.equal(initialWorkspaceView(''), 'claims')
  assert.equal(initialWorkspaceView('#activity'), 'activity')
  assert.equal(initialWorkspaceView('#unrecognized'), 'claims')
  const navigation = render(WorkspaceNavigation, { view: 'claims', claims: 12, paths: 2, errors: 1, onChange: () => {} })
  for (const label of ['Claims', 'Graph', 'Paths', 'Activity', 'Run details']) assert.ok(navigation.includes(label))
  assert.match(navigation, /aria-current="page"/)
  const toolbar = render(FindingsToolbar, { search: 'root growth', filter: 'source', counts: { all: 12, source: 9, supported: 7, review: 3, rejected: 2 }, shown: 2 })
  assert.match(toolbar, /Search claims and evidence/)
  assert.match(toolbar, /Showing 2 of 12 claims/)
  assert.match(toolbar, /Source supported/)
  const settings = render(RunSetup, { health: { pipeline: { task_models: {
    extraction: 'opencode-go/deepseek-v4.1-flash', source: 'opencode-go/deepseek-v4.1-flash',
    query: 'opencode-go/mimo-v2.6-flash', external: 'opencode-go/mimo-v2.6-pro',
  } } }, options: { max_pages: 0, claims_per_packet: 4, max_claims: 0, articles_per_claim: 8 }, open: true })
  for (const label of ['PDF pages', 'Claims per extraction call', 'Claims to validate', 'Search papers per claim', 'MiMo Pro', 'DeepSeek Flash']) assert.ok(settings.includes(label))
  assert.doesNotMatch(settings, /Cat Jam|Semantic gate/)
  const activity = render(ActivityLog, { logs: [{ at: '2026-10-23T09:20:33Z', level: 'error', message: 'Request timed out after 600s' }], running: false })
  assert.match(activity, /Request timed out after 600s/)
  assert.match(activity, /Errors &amp; warnings/)
  assert.match(activity, /Follow live updates/)
  const historical = render(ActivityLog, { logs: [{ level: 'error',
    message: 'Response JSON parsing failed: incomplete results',
    historical_error_notice: 'Historical parser label corrected for display; original log preserved. Recorded model: opencode-go/deepseek-v4.1-flash.',
  }], running: false })
  assert.match(historical, /original log preserved/)
  assert.match(historical, /deepseek-v4.1-flash/)
  assert.match(historical, /Response JSON parsing failed/)
  assert.match(settings, /1200/)
  const progress = render(RequestProgress, { running: true, progress: { model: 'MiMo Pro', task: 'joint external: claim-1',
    started_at: new Date().toISOString(), status: 'waiting_for_provider', paper_count: 9, chunk_count: 24,
    input_tokens: null, input_tokens_reported: false, timeout_seconds: 1200, provider_status: 'Live provider status unavailable during a buffered API request' } })
  for (const label of ['Waiting for model response', 'papers', 'chunks', 'input tokens', 'Pending', 'elapsed / 1200 s', 'provider status unavailable']) assert.ok(progress.includes(label))
  assert.doesNotMatch(progress, /characters|NaN/)
  const completedTokens = render(RequestProgress, { running: false, progress: { model: 'MiMo Pro',
    status: 'ok', input_tokens: 12345, input_tokens_reported: true, elapsed_seconds: 18, timeout_seconds: 1200,
    provider_status: 'Completed' } })
  assert.match(completedTokens, /12,345/)
  assert.match(completedTokens, /input tokens \(provider-reported\)/)
  const oldTokens = render(RequestProgress, { running: false, progress: { status: 'timeout', input_characters: 2000,
    prompt_characters: 2500, timeout_seconds: 300, provider_status: 'Timed out' } })
  assert.match(oldTokens, /Unavailable/)
  assert.doesNotMatch(oldTokens, /characters|NaN|2,500/)
  const tracking = render(CitationTracking, { candidate, validation: { attempts: [{ citation_ledger: [
    { citation_key: 'khan|2010', status: 'included_in_judgement', identified: true, resolved: true, fetched: true, included_in_judgement: true, citation_text: 'Khan reference' },
    { citation_key: 'baloch|2024', status: 'missing_bibliography_link', identified: false, resolved: false, fetched: false, included_in_judgement: false, reason: 'No unambiguous bibliography entry' },
  ] }] } })
  for (const label of ['Citation tracking', 'khan|2010', 'baloch|2024', 'missing bibliography link', 'In judgement', 'No unambiguous bibliography entry']) assert.ok(tracking.includes(label))
  const quality = render(PdfQualityReport, { quality: { pages_with_text: 2, total_pages: 3, selected_pages: 3, pages_with_warnings: 1,
    pages: [{ page: 2, word_count: 0, body_characters: 0, reference_characters: 0, within_page_limit: true,
      reading_order: 'not_verified', flags: ['no_extracted_text_ocr_or_blank_page'] }] } })
  assert.match(quality, /2 \/ 3 pages/)
  assert.match(quality, /not verified/)
  assert.match(quality, /No text: blank page or OCR needed/)
  const evidenceCard = render(ClaimCard, { defaultExpanded: true, index: 0, onClaimAction: () => {}, record: {
    candidate: { ...candidate, candidate_id: 'claim-1', source_evidence_span: 'Source evidence remains available.' },
    validation: { validation_status: 'world_contested_pending_review', attempts: [{ evidence_documents: [{
      evidence_document_id: 'paper-a', title: 'Original external paper', doi: '10.1234/example', evidence_access: 'full_text',
      passage_selection: { query: 'Crop R toxin T', neutral_query: 'Crop R toxin T', retained_chunks: 1, candidate_chunks: 4 },
      evidence_chunks: [{ chunk_id: 'paper-a:0', text: 'Retained passage remains available.' }],
    }], decisions: [{ evidence_document_id: 'paper-a', decision: 'supported', evidence: [{ quoted_span: 'Exact external quote remains available.' }] }] }] },
  } })
  for (const preserved of ['Repair and revalidate claim', 'Source evidence remains available', 'Exact external quote remains available',
    'Original external paper', 'Retained passage remains available', 'Source fidelity', 'Ranking claim', 'doi.org/10.1234/example']) {
    assert.ok(evidenceCard.includes(preserved), `Missing evidence feature: ${preserved}`)
  }
  console.log('Claim context, verification labels, decision trace and throughput rendering checks passed.')
} finally {
  await server.close()
}
