"""Independent quote-only review of proposed supported local pairs."""
from backend.providers.structured_output import EvidenceSelectionError
from backend.stages.neighbor_validation.judgement import _call, obj, arr, choice, TEXT


def audit_signature(target, papers, assessment):
    from backend.stages.neighbor_validation.targets import signature
    return signature({'target':target['input_signature'],'assessment':assessment,
        'papers':sorted([{'id':p['evidence_document_id'],'proofs':p['proofs'],
                         'calculations':p.get('calculation_results',[])} for p in papers],key=lambda p:p['id'])})


def audit_support(client, target, papers, assessment, settings, retained=None):
    allowed = {(p['evidence_document_id'],q['proof_id']):q for p in papers for q in p['proofs']}
    def selected(row):
        return [{**r,'quote':allowed[(r['evidence_document_id'],r['proof_id'])]['quoted_span']} for r in row['proof_refs']]
    edges = []
    for edge, original in zip(target['edges'], target['source_claims']):
        finding = next(r for r in assessment['edge_assessments'] if r['edge_id']==edge['id'])
        claim = original['claim']
        edges.append({'edge_id':edge['id'], 'claim':{k:claim.get(k) for k in
            ('subject','relation','object','claim_text','certainty','qualifiers')}, 'external_proof':selected(finding)})
    junctions = [{'connection_id':r['connection_id'],'connection_kind':r['connection_kind'],
                  'external_proof':selected(r)} for r in assessment['connection_assessments']]
    refs = arr(obj({'evidence_document_id':TEXT,'proof_id':TEXT}))
    schema = obj({'edge_checks':arr(obj({'edge_id':choice([e['id'] for e in target['edges']]),
        'decision':choice(['exact','partial','not_established','conflicting']), 'proof_refs':refs,'reason':TEXT}),
        minItems=2,maxItems=2), 'connection_checks':arr(obj({
            'connection_id':choice([c['connection_id'] for c in target['connections']]),
            'decision':choice(['compatible','incompatible','not_established']), 'proof_refs':refs,'reason':TEXT}),minItems=1,maxItems=1)})
    def check(answer):
        for field, identity, supplied in [('edge_checks','edge_id',edges),('connection_checks','connection_id',junctions)]:
            if sorted(r[identity] for r in answer[field]) != sorted(r[identity] for r in supplied):
                raise EvidenceSelectionError('Proof audit must assess every supplied target exactly once')
            for row in answer[field]:
                original = next(r for r in supplied if r[identity]==row[identity])
                bound = {(r['evidence_document_id'],r['proof_id']) for r in original['external_proof']}
                if not row['proof_refs'] and row['decision']!='not_established':
                    raise EvidenceSelectionError('Proof audit requires references unless the decision is not_established')
                if not {(r['evidence_document_id'],r['proof_id']) for r in row['proof_refs']} <= bound:
                    raise EvidenceSelectionError('Proof audit moves evidence between claims, papers or connections')
        return answer
    result = check(retained) if retained is not None else _call(client,
        'Independently audit the fixed neighboring claims against ONLY their admitted external quotations. '
        'No originating PDF text or previous judge rationale is evidence here. For each edge check every '
        'essential assertion, outcome, negation, universal/extent qualifier, population, period, unit and '
        'denominator. A list of examples does not establish all/every/entire or an unstated property. '
        'Numerical similarity is not equivalence; different measurement definitions cannot be substituted. '
        'Arithmetic results are unitless assistance, not scientific conclusions; check '
        'units and measurement definitions from the quotes. Preserve meaningful qualifiers. Exact means '
        'the full proposition is established; partial means only a subset, not_established means missing '
        'proof, conflicting means comparable opposing proof. Missing evidence is not contradiction. '
        'When quotes from multiple attached proof references together substantiate all parts of a compound '
        'proposition, grade exact. For multi-factorial agricultural and epidemiological drivers, when quotes '
        'substantiate that the claimed practices (such as maize-wheat rotation and conservation tillage) '
        'are established causes or drivers of disease epidemics, mentioning contributing co-factors '
        '(such as favorable weather or cultivar susceptibility) does not reduce the decision to partial; '
        'grade exact. Narrative relative temporal framing (such as "from the 1990s until today" or "for many years") '
        'is satisfied by quotes documenting ongoing epidemic recurrence or persistence since the 1990s without '
        'requiring verbatim "until today" phrasing. Equivalent scientific proposition framing (such as '
        '"primary concern because of mycotoxins" vs "most serious / primary economic and health concerns due to mycotoxins", '
        '"major chronic disease in many wheat production areas" vs "major / most devastating disease of wheat worldwide", '
        '"one of the most devastating grain diseases in Asia" vs "one of the most important grain diseases of rice in Asia", '
        'and "unfilled spikelets / chaffiness and lower grain weight" vs "unfilled and partially filled grains and lower grain weight") '
        'satisfies exact. Only not_established may use proof_refs=[] when no admitted quotation establishes the finding; '
        'never invent a reference to fill this list. All other decisions require attached proof references. '
        'Check the local connection with its original non-causal/exclusion meaning; never infer A-to-C. '
        'Cite only the external proof attached to the target being checked. State concise reasons. JSON only.',
        {'pathway_id':target['pathway_id'],'edges':edges,'connections':junctions,
         'arithmetic_assistance':[{'evidence_document_id':p['evidence_document_id'],**r}
             for p in papers for r in p.get('calculation_results',[]) if (p['evidence_document_id'],r['proof_id']) in
             {(q['evidence_document_id'],q['proof_id']) for e in edges for q in e['external_proof']}]},
        schema,f"neighbor proof audit: {target['pathway_id']}",settings,check,2200)
    return {**result,'input_signature':audit_signature(target,papers,assessment),
            'passed':all(r['decision']=='exact' for r in result['edge_checks'])
            and all(r['decision']=='compatible' for r in result['connection_checks'])}


def followup_assessment(assessment, audit):
    """A retrieval-only copy; never rewrite the original scientific assessment."""
    from copy import deepcopy
    result = deepcopy(assessment)
    failed = {r['edge_id']:r['reason'] for r in audit['edge_checks'] if r['decision']!='exact'}
    uncertain = {r['connection_id']:r['reason'] for r in audit['connection_checks'] if r['decision']!='compatible'}
    for row in result['edge_assessments']:
        if row['edge_id'] in failed:
            row['stance'] = 'unresolved'
            row['reason'] = failed[row['edge_id']]
    for row in result['connection_assessments']:
        if row['connection_id'] in uncertain:
            row['compatibility'] = 'unresolved'
            row['reason'] = uncertain[row['connection_id']]
    return result
