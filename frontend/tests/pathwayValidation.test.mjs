import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const server=await createServer({server:{middlewareMode:true},appType:'custom'})
try {
  const {default:Panel,PathwayAssessmentCard}=await server.ssrLoadModule('/src/components/graph/PathwayAssessments.jsx')
  const {ClaimActions,ManualSourceReview}=await server.ssrLoadModule('/src/components/claims/ClaimDetails.jsx')
  const {default:Navigation}=await server.ssrLoadModule('/src/components/graph/PathwayNavigation.jsx')
  const {ClaimCard,claimStatus,claimGroup}=await server.ssrLoadModule('/src/App.jsx')
  const {ValidationTrace}=await server.ssrLoadModule('/src/components/claims/ClaimDetails.jsx')
  const render=(component,props)=>renderToStaticMarkup(React.createElement(component,props))
  const claims=[{candidate_id:'one',subject:'Crop C',object:'Intermediate B',relation:'increases',qualifiers:{condition:'Condition K'},source_evidence_span:'Crop C increases Intermediate B under Condition K.',source_validation:{decision:'entailed'}},
    {candidate_id:'two',subject:'Intermediate B',object:'Outcome E',relation:'increases',source_validation:{decision:'entailed'}}]
  const edges=claims.map((c,n)=>({...c,id:'edge-'+n,source_label:c.subject,target_label:c.object}))
  const record={pathway_id:'path-p',verdict:'components_supported',evidence_basis:'component_evidence_only',stale:false,eligible:false,
    target:{description:'Crop C through Intermediate B to Outcome E',edges,document_keys:['Paper A','Paper B'],source_claims:claims.map((claim,n)=>({claim,document_key:'Paper '+n,edge_id:'edge-'+n}))},
    assessment:{synthesis:'The components have findings; the connecting mechanism is unresolved.',in_scope:true,scope_reason:'Crop pathway context.',limitations:['No complete-route experiment.'],edge_assessments:[],connection_assessments:[],route_proof_refs:[]},
    paper_assessments:[{evidence_document_id:'paper-external',route_assessment:'components_only',finding:'Component finding.',paper:{title:'Independent study',doi:'10.example/p',evidence_access:'full_text'},
      proofs:[{proof_id:'proof-1',chunk_id:'chunk-2',sentence_numbers:[4,6],quoted_span:'Exact selected finding. [NON-ADJACENT CONTEXT] Exact context.'}]}]}
  test('whole-route results retain exact proof, conditions and unresolved mechanism without trust scores',()=>{
    const html=render(PathwayAssessmentCard,{record})
    for (const value of ['Components supported; assessment partial','Intermediate B','Condition K','Independent study','chunk-2','4, 6','Exact selected finding.','No complete-route experiment.','Source passages']) {
      assert.ok(html.toLowerCase().includes(value.toLowerCase()),value)
    }
    assert.doesNotMatch(html,/Trust score|Evidence balance|probability/)
    assert.match(render(PathwayAssessmentCard,{record:{...record,stale:true}}),/Inputs changed; reassessment required/)
  })
  test('supported-path view excludes components-only and stale assessments',()=>{
    const html=render(Panel,{data:{records:[record]},supportedOnly:true})
    assert.match(html,/0 matching pathways/)
    assert.doesNotMatch(html,/aria-label="Whole-pathway assessment"/)
  })
  test('paper findings with mismatched or unknown conditions remain visibly contextual',()=>{
    const contextual=structuredClone(record)
    contextual.paper_assessments[0].edge_findings=[
      {edge_id:'edge-0',stance:'supports',conditions_match:false,proof_ids:['proof-1'],reason:'Different source conditions.'},
      {edge_id:'edge-1',stance:'contradicts',conditions_match:null,proof_ids:['proof-1'],reason:'Applicability unknown.'},
    ]
    const html=render(PathwayAssessmentCard,{record:contextual})
    for (const value of ['Conditions differ','Conditions unconfirmed','Contextual support','Contextual opposing finding']) {
      assert.ok(html.toLowerCase().includes(value.toLowerCase()),value)
    }
    assert.doesNotMatch(html,/>Conditions matched</)
  })
  test('new navigation and claim views expose no individual external retry or trust selector',()=>{
    const nav=render(Navigation,{pathwayOnly:true,value:'assessments'})
    assert.match(nav,/Pathway assessments/);assert.match(nav,/Provisional chains/);assert.match(nav,/Supported pathways/)
    assert.doesNotMatch(nav,/Trust network|Historical health paths/)
    const record={candidate:claims[0],external_validation_target:'whole_pathway',pathway_participation:[],trust:{score:99}}
    const html=render(ClaimCard,{record,index:0,defaultExpanded:true})
    assert.match(html,/no independent external-validation verdict/)
    assert.doesNotMatch(html,/Evidence balance|99|Retry unfinished|Reassess external evidence/)
    assert.equal(render(ClaimActions,{candidate:claims[0],pathwayOnly:true}), '')
  })
  test('project-scope exclusions remain separate from PDF fidelity and are visible before expansion',()=>{
    for (const scope of ['in_scope','out_of_scope','needs_review',null]) {
      const candidate={...claims[0],source_validation:{decision:'entailed',...(scope ? {project_scope:{decision:scope,rationale:'The model assessed project relevance from the source context.'}} : {})}}
      const row={candidate,external_validation_target:'whole_pathway'}
      const expectedGroup=scope==='in_scope' ? 'unvalidated' : scope==='out_of_scope' ? 'rejected' : 'review'
      assert.equal(claimGroup(row),expectedGroup)
      assert.equal(claimStatus(row),scope==='in_scope' ? 'source_entailed' : scope==='out_of_scope' ? 'out_of_scope' : scope==='needs_review' ? 'scope_needs_review' : 'scope_not_assessed')
      const trace=render(ValidationTrace,{candidate})
      assert.match(trace,/Source fidelity/);assert.match(trace,/entailed/);assert.match(trace,/Project scope/)
      if (scope==='out_of_scope') {
        assert.match(trace,/does not mean the PDF assertion is false or unsupported/)
        assert.match(trace,/claim and source evidence remain available for audit/)
        assert.match(render(ClaimCard,{record:row,index:0}),/Outside project scope/)
      }
      if (!scope) assert.match(trace,/No claim-level source-scope assessment is recorded/)
    }
  })
  test('manual source controls expose separate accept/reject actions and stay hidden in the public viewer',()=>{
    const candidate={...claims[0],source_validation:{decision:'needs_review',rationale:'Ambiguous source wording.',project_scope:{decision:'needs_review',rationale:'Uncertain project relevance.'}}}
    const review={revision:'a'.repeat(64),can_accept:true,active_reviews:{source_fidelity:{decision:'accept',note:'Read the PDF.',at:'2026-10-29'}},fidelity_decision:'entailed',scope_decision:'needs_review'}
    const props={candidate,review,onAction:()=>{}}
    const html=render(ManualSourceReview,props)
    for (const text of ['Accept pdf fidelity','Reject pdf fidelity','Accept project scope','Reject project scope','no LLM calls','Review note (optional)','Resume the saved pipeline']) assert.ok(html.includes(text),text)
    assert.match(html,/aria-pressed="true"/)
    assert.doesNotMatch(html,/disabled=""/)
    assert.match(render(ManualSourceReview,{...props,busy:true}),/disabled/)
    assert.equal(render(ManualSourceReview,{...props,readOnly:true}),'')
    assert.match(render(ManualSourceReview,{...props,review:{...review,can_accept:false}}),/Repair missing or invalid evidence first/)
    const row={candidate,source_review:review,external_validation_target:'whole_pathway'}
    assert.equal(claimStatus(row),'scope_needs_review')
    assert.equal(claimGroup(row),'review')
    const trace=render(ValidationTrace,{candidate,sourceReview:review,reviewHistory:[{event_id:'one',dimension:'source_fidelity',decision:'accept',at:'2026-10-29',note:'Read the PDF.'}]})
    assert.match(trace,/Manual source fidelity/);assert.match(trace,/Accepted/);assert.match(trace,/Ambiguous source wording/)
    assert.match(trace,/Manual source review history/)
    assert.match(render(ValidationTrace,{candidate,sourceReview:{...review,stale_dimensions:['source_fidelity']}}),/Previous manual decisions no longer apply/)
  })
  const {PathwayOutcomeOverview,RunBreakdown}=await server.ssrLoadModule('/src/components/workspace/RunSummary.jsx')
  const {GraphSelectionDetails}=await server.ssrLoadModule('/src/components/graph/GraphView.jsx')
  const {FindingsToolbar}=await server.ssrLoadModule('/src/components/workspace/WorkbenchPanels.jsx')
  const {default:ModelPerformance}=await server.ssrLoadModule('/src/components/diagnostics/ModelPerformance.jsx')
  test('pathway assessments separate scientific outcomes from execution states and surface rich grounded details',()=>{
    const detailed={...record,
      target:{...record.target,
        edges:[{...edges[0],source:'node-c',target:'node-b'},{...edges[1],source:'node-b',target:'node-e'}],
        connections:[{connection_id:'conn-b',upstream_edge_id:'edge-0',downstream_edge_id:'edge-1',node_id:'node-b',label:'Intermediate B'}]},
      assessment:{...record.assessment,
        edge_assessments:[{edge_id:'edge-0',stance:'supported',reason:'Link 1 supported.',proof_refs:[{evidence_document_id:'paper-external',proof_id:'proof-1'}]},
          {edge_id:'edge-1',stance:'unresolved',reason:'Link 2 not tested.',proof_refs:[]}],
        connection_assessments:[{connection_id:'conn-b',compatibility:'unresolved',reason:'Different tissue compartment.',proof_refs:[]}],
        entity_roles:[{node_id:'node-b',contextual_role:'mediator',proposed_type:'Nutrient',resolved:true,reason:'Measured intermediate.',proof_refs:[]}]},
      paper_assessments:[{...record.paper_assessments[0],
        route_proof_ids:['proof-1'],
        edge_findings:[{edge_id:'edge-0',stance:'supports',conditions_match:true,proof_ids:['proof-1'],reason:'Matched Condition K.'}],
        connection_findings:[{connection_id:'conn-b',compatibility:'unresolved',proof_ids:[],reason:'Downstream link absent.'}],
        proofs:[{...record.paper_assessments[0].proofs[0],is_contiguous:false}],
        nli_diagnostic:{scores:{entailment:0.72,neutral:0.2,contradiction:0.08},diagnostic_only:true}}],
      retrieval_rounds:[{evidence_documents_count:4,retrieval_pending:false,
        query_translation:{queries:[{target_id:'route',query:'"Crop C" AND "Outcome E"',openalex_query:'Crop C Intermediate B Outcome E'}]},
        pathway_coverage:{targets_without_retained_passages:['edge-1']},
        citation_ledger:[{citation_key:'ref-1',included_in_judgement:true}]}],
      assessment_history:[{verdict:'unresolved',synthesis:'Initial pass lacked connection studies.'},{verdict:'components_supported',synthesis:'Follow-up confirmed Link 1 only.'}]}
    const card=render(PathwayAssessmentCard,{record:detailed,number:1,paperKey:'Paper 0',onInspectClaim:()=>{}})
    for (const expected of ['CROSS-PAPER CHAIN','1 of 2 links supported','Connection at Intermediate B','Entity role · Intermediate B',
      'Conditions matched','Matched Condition K.','non-adjacent sentences selected individually','Entailment 72.0%','Planned literature queries',
      'OpenAlex:','edge-1','1/1 source citations in judgement','Reading 1 (Insufficient pathway evidence)','Inspect in Claims']) {
      assert.ok(card.includes(expected),`Missing assessment detail: ${expected}`)
    }
    const otherPaper={...detailed,pathway_id:'path-q',verdict:'queued',target:{...detailed.target,document_keys:['Paper C']}}
    const workspaceHtml=render(Panel,{data:{records:[detailed,otherPaper]},paperKey:'Paper A'})
    for (const expected of ['Scientific outcomes','Execution states · not negative verdicts','Awaiting assessment','All workspace (2)','This paper (1)']) {
      assert.ok(workspaceHtml.includes(expected),`Missing workspace control: ${expected}`)
    }
  })
  test('paper overview, graph selection and claim participation preserve whole-pathway semantics',()=>{
    assert.equal(render(PathwayOutcomeOverview,{summary:{knowledge_building_mode:'trust_graph'}}),'')
    const overview=render(PathwayOutcomeOverview,{summary:{knowledge_building_mode:'pathway_only',
      pathway_validation:{outcomes:{pathway_supported:1,components_supported:2,queued:1},coverage:{has_more:true,claims_outside_selected_pathways:['c1','c2']}}},onOpen:()=>{}})
    for (const expected of ['4 assessed routes involve this paper','Pathway supported','Awaiting assessment','2 source claims are outside the selected chains']) {
      assert.ok(overview.includes(expected),`Missing overview text: ${expected}`)
    }
    const graphEdge=render(GraphSelectionDetails,{element:{kind:'edge',source:'node-c',target:'node-b',source_label:'Crop C',target_label:'Intermediate B',
      relation:'increases',status:'pathway_supported',external_status:'pathway_membership_only',individually_externally_validated:false,
      supported_pathway_ids:['pathway-1234567890abcdef'],candidate_id:'one',document_key:'Paper A',quote:'Exact PDF quote.'},onClose:()=>{},onInspectPathway:()=>{},onInspectClaim:()=>{}})
    assert.match(graphEdge,/Crop C → increases → Intermediate B/)
    assert.match(graphEdge,/Supported pathway member/)
    assert.match(graphEdge,/edge not individually externally validated/)
    assert.match(graphEdge,/Inspect source claim/)
    assert.doesNotMatch(graphEdge,/undefined|supporting article\(s\)/)
    const historicalEdge=render(GraphSelectionDetails,{element:{kind:'edge',source:'Crop:Crop C',target:'Nutrient:Intermediate B',relation:'increases',status:'corroborated',support_count:2,paper:'Paper A'}})
    assert.match(historicalEdge,/2 supporting article\(s\)/)
    const participating={candidate:{...claims[0],certainty:'conditional'},external_validation_target:'whole_pathway',
      pathway_participation:[{pathway_id:'pathway-abc',verdict:'pathway_supported',stale:false}]}
    const claimHtml=render(ClaimCard,{record:participating,index:0,defaultExpanded:true,onInspectPathway:()=>{}})
    for (const expected of ['1 assessed pathway','conditional','Pathway supported','pathway-abc','Inspect pathway','Whole-pathway target','Not evaluated as a standalone claim']) {
      assert.ok(claimHtml.includes(expected),`Missing claim participation text: ${expected}`)
    }
    const v44Toolbar=render(FindingsToolbar,{search:'',filter:'all',counts:{all:5,source:4,unvalidated:3,supported:0,review:1,rejected:1},shown:5})
    assert.doesNotMatch(v44Toolbar,/Historical external support/)
    const perf=render(ModelPerformance,{summary:{llm_calls:{total_requests:3},task_models:{pathway_assembly:'opencode-go/mimo-v2.6-flash'}}})
    assert.match(perf,/Pathway assembly/)
    assert.doesNotMatch(perf,/>pathway_assembly</)
    const breakdown=render(RunBreakdown,{summary:{extracted:10,schema_valid:10,source_entailed:8,pathway_eligible_claims:6,
      knowledge_building_mode:'pathway_only',semantic_stage:'removed',pathway_validation:{outcomes:{pathway_supported:2}},
      llm_calls:{total_requests:9,extraction:{requests:2,elapsed_seconds:5},source:{requests:3,elapsed_seconds:4},pathway_assembly:{requests:1,elapsed_seconds:2},validation:{requests:3,elapsed_seconds:8}},
      stage_timings_seconds:{extraction:5}}})
    assert.match(breakdown,/Whole-pathway external assessment/)
    assert.match(breakdown,/source 3 \/ assembly 1/)
    assert.doesNotMatch(breakdown,/batches of undefined/)
  })
  test('proof audit holds and arithmetic assistance stay visible without granting support',()=>{
    const held=structuredClone(record)
    held.assessment_scope='neighboring_claims'
    held.verdict='assessment_incomplete'
    held.proof_audit={passed:false,edge_checks:[{edge_id:'edge-0',decision:'partial',reason:'Extent is unproven.',proof_refs:[]}],connection_checks:[]}
    held.paper_assessments[0].calculation_results=[{proof_id:'proof-1',operation:'ratio_percent',operands:['50','200'],value_percent:'25'}]
    const html=render(PathwayAssessmentCard,{record:held})
    assert.match(html,/Independent external proof audit/)
    assert.match(html,/Extent is unproven/)
    assert.match(html,/no support admitted/)
    assert.match(html,/Arithmetic assistance/)
    assert.match(html,/25%/)
    assert.match(html,/Units and scientific comparability remain model judgements/)
  })
  test('local breakdown separates qualified compatible components from missing evidence without promoting support',()=>{
    const local=structuredClone(record)
    local.assessment_scope='neighboring_claims'
    local.target.connections=[{connection_id:'junction-1',upstream_edge_id:'edge-0',downstream_edge_id:'edge-1'}]
    local.assessment.edge_assessments=edges.map(edge=>({edge_id:edge.id,stance:'partially_supported',proof_refs:[]}))
    local.assessment.connection_assessments=[{connection_id:'junction-1',connection_kind:'contextual',compatibility:'compatible',proof_refs:[]}]
    let html=render(PathwayAssessmentCard,{record:local})
    assert.match(html,/Both relationships have support; junction compatible; original details unresolved/)
    assert.match(html,/does not fully validate the original wording or admit this pair/)
    assert.match(html,/Connection meaning/)
    assert.doesNotMatch(html,/<h3>Locally supported connection/)
    local.assessment.edge_assessments[0].stance='unresolved'
    html=render(PathwayAssessmentCard,{record:local})
    assert.match(html,/1 of 2 relationships have support; remaining claim evidence unresolved/)
    local.assessment.connection_assessments[0].connection_kind='exclusion'
    local.execution_status='pending'
    html=render(PathwayAssessmentCard,{record:local})
    assert.match(html,/Exclusion contrast; no positive pathway propagation/)
    assert.match(html,/Provisional breakdown/)
  })
} finally {await server.close()}
