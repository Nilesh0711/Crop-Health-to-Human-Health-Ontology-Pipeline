"""DeepSeek assesses neighboring claims per paper; Pro judges local compatibility."""
from copy import deepcopy
from contextlib import nullcontext
import json

from backend.providers.structured_output import EvidenceSelectionError, IncompleteResponseError
from backend.providers.llm import LLMServiceError
from backend.stages.external_validation.evidence import packed_document, selected_context, sentences
from backend.stages.external_validation.arithmetic import CALCULATIONS_SCHEMA, calculate_from_proof


def obj(fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def arr(item, **bounds):
    return {'type': 'array', 'items': item, **bounds}


TEXT = {'type': 'string'}
BOOL = {'type': 'boolean'}
def choice(values):
    return {'type': 'string', 'enum': list(values)}


def _identity(rows, field, expected):
    if sorted(row[field] for row in rows) != sorted(expected):
        raise IncompleteResponseError(f'Every supplied {field} must appear exactly once')


def pathway_payload(target):
    context_pool, edges = {}, []
    for edge, original in zip(target['edges'], target['source_claims']):
        c = original['claim']
        text = c.get('source_local_context') or c.get('source_evidence_span') or ''
        key = str(len(context_pool)) if text not in context_pool.values() else next(k for k,v in context_pool.items() if v == text)
        context_pool[key] = text
        edges.append({'edge_id': edge['id'], 'source_node': edge['source'], 'target_node': edge['target'],
            **{k: c.get(k) for k in ('subject', 'relation', 'object', 'subject_type', 'object_type', 'claim_text', 'qualifiers', 'certainty')},
            'source_fidelity_context_only': {'quote': c.get('source_evidence_span'), 'context_ref': key}})
    return {'pathway_id': target['pathway_id'], 'description': target['description'], 'edges': edges,
            'connections': target['connections'], 'original_pdf_context_only': context_pool}


def paper_schema(target, document, edge_ids=None, retained_proof_count=0):
    edges = [e['id'] for e in target['edges']] if edge_ids is None else edge_ids
    links = [c['connection_id'] for c in target['connections']]
    # Bind proof selections to the actual chunk-local numbering in the input.
    # Paper reference numbers and sentence-pool keys are not local proof IDs.
    counts = {}
    for chunk in document.get('evidence_chunks', []):
        count = len(sentences(chunk.get('text')))
        if count:
            counts[chunk['chunk_id']] = count
    proof = obj({'proof_id': TEXT, 'chunk_id': choice(counts),
        'sentence_numbers': arr({'type': 'integer', 'minimum': 1}, minItems=1, maxItems=6, uniqueItems=True)})
    if counts:
        proof['allOf'] = [
            {'if': {'properties': {'chunk_id': {'const': identifier}}},
             'then': {'properties': {'sentence_numbers': {
                 'items': {'maximum': count}, 'maxItems': min(6, count)}}}}
            for identifier, count in counts.items()]
    else:
        proof = False
    schema = obj({'finding': TEXT,
        'edge_findings': arr(obj({'edge_id': choice(edges), 'stance': {
            **choice(['supports', 'contradicts', 'not_addressed', 'unclear']),
            'description': 'Direction only. Never use partial or partially_supported here; partial belongs to claim_match.'},
            'claim_match': {'type':'string','enum':['exact','partial','context_only','not_addressed'],
                'description':'Match to the full original proposition, including outcome, negation, quantifiers and descriptive details. Exact does not mean supported; stance records direction.'},
            'conditions_match': {'type': ['boolean', 'null'], 'description':
                'Scientific applicability to the claim: true when essential qualifiers are compatible, '
                'false for a known mismatch, null when not established. Supports/contradicts with false/null '
                'is contextual evidence only, never an applicable decisive finding.'},
            'proof_ids': arr(TEXT), 'reason': TEXT}) if edges else False, minItems=len(edges), maxItems=len(edges)),
        'connection_findings': arr(obj({'connection_id': choice(links), 'compatibility': choice(['compatible', 'incompatible', 'unresolved']),
            'proof_ids': arr(TEXT), 'reason': TEXT}), minItems=len(links), maxItems=len(links)),
        'proofs': arr(proof, maxItems=10 + retained_proof_count)})
    schema['properties']['calculations'] = CALCULATIONS_SCHEMA
    return schema


def ground_paper(target, document, answer):
    if answer['pathway_id'] != target['pathway_id'] or answer['evidence_document_id'] != document['evidence_document_id']:
        raise EvidenceSelectionError('Paper/pathway identity mismatch')
    _identity(answer['edge_findings'], 'edge_id', [e['id'] for e in target['edges']])
    _identity(answer['connection_findings'], 'connection_id', [c['connection_id'] for c in target['connections']])
    proof_ids = [p['proof_id'] for p in answer['proofs']]
    if len(set(proof_ids)) != len(proof_ids) or any(not p.strip() for p in proof_ids):
        raise EvidenceSelectionError('Proof identities must be unique and nonempty within their paper')
    result = deepcopy(answer)
    for proof in result['proofs']:
        selected, error = selected_context(document, proof)
        if error:
            chunk = next((c for c in document.get('evidence_chunks', []) if c.get('chunk_id') == proof.get('chunk_id')), None)
            bound = (f"valid local sentence numbers are 1-{len(sentences(chunk.get('text')))}"
                     if chunk else 'select an existing chunk_id from this paper')
            raise EvidenceSelectionError(f"Proof {proof.get('proof_id')} in chunk {proof.get('chunk_id')}: {error}; {bound}")
        if 'quoted_span' in proof and proof['quoted_span'] != selected['quoted_span']:
            raise EvidenceSelectionError('Stored proof differs from its paper-bound sentence selection')
        proof.update(selected)
        proof['evidence_document_id'] = document['evidence_document_id']
    for row in [*result['edge_findings'], *result['connection_findings']]:
        if not set(row['proof_ids']) <= set(proof_ids):
            raise EvidenceSelectionError('A finding cites a proof absent from its own paper')
        if (row.get('stance') in {'supports', 'contradicts'} or row.get('compatibility') in {'compatible', 'incompatible'}) and not row['proof_ids']:
            raise EvidenceSelectionError('A decisive finding needs exact paper-bound proof')
        # The model may report a general relationship with mismatched/unknown
        # conditions. Preserve both fields verbatim; synthesis may only admit an
        # applicable decisive edge finding when conditions_match is explicitly true.
    for row in result['edge_findings']:
        if row.get('claim_match') not in {'exact','partial','context_only','not_addressed'}:
            raise EvidenceSelectionError('Every edge finding must declare its match to the original proposition')
        if row['claim_match']=='not_addressed' and row['stance']!='not_addressed':
            raise EvidenceSelectionError('An unaddressed proposition cannot have a decisive stance')
    if result.get('calculations'):
        try:
            result['calculation_results'] = [calculate_from_proof(c, {p['proof_id']:p for p in result['proofs']})
                                             for c in result['calculations']]
        except ValueError as exc:
            raise EvidenceSelectionError(str(exc)) from exc
    result['paper'] = {k: document.get(k) for k in ('title','doi','evidence_access','evidence_level','independence_group','retrieval_route')}
    return result


PAPER_SYSTEM = ('Assess TWO neighboring qualified claims and their shared-entity connection against ONE paper. '
    'Judge the supplied edge_ids_to_assess independently from the other claim, using their source definitions and '
    'essential qualifiers. Retained findings are fixed, paper-grounded inputs; omit them from edge_findings. '
    'Do not impose a new partner claim condition on an unchanged claim. Assess the local connection separately. '
    'Compare shared entity, biological compartment, species, dose, timing, direction, certainty and conditions. '
    'Individual edge support and local condition compatibility do not prove a complete causal chain or a direct '
    'first-to-last effect; do not require a complete-chain experiment for this local assessment. '
    'Paper stance and conditions_match are separate: true means applicable conditions, false a meaningful mismatch, '
    'null unknown applicability. Supports/contradicts with false/null are context only; do not change applicability '
    'to obtain support. Absence of evidence is not contradiction. Original PDF context defines the claim but is '
    'not external proof. Respect association, containment and other non-causal predicates. '
    'Declare claim_match for every edge: exact for the same complete proposition, partial when only part or '
    'broader wording is addressed, context_only for a different or unestablished setting, not_addressed when absent. '
    'stance MUST be supports, contradicts, not_addressed or unclear. Never return stance=partial or '
    'partially_supported; those are not paper stance values. A partially matching supporting finding uses '
    '{"stance":"supports","claim_match":"partial","conditions_match":true} only if applicability is '
    'established; otherwise preserve false/null applicability. The separate synthesis uses partially_supported. '
    'An exact match can support OR contradict. Partial support cannot validate an unverified descriptor. '
    'Compare the actual outcome and measured metric, compartment, field versus controlled setting, developmental '
    'stage, population, modality, negation and magnitude words such as major/all. Do not silently add qualifiers '
    'to make a statement true. Explain missing details or mismatches briefly in reason. '
    'A mechanism not observed is not proof that its outcome cannot occur. Category membership, a related measure '
    'or a capability in one setting does not by itself establish the claimed outcome in another setting. '
    'Exact means scientific proposition equivalence, not verbatim wording or a single-sentence match. '
    'Combine the selected paper sentences only when together they establish the same claim under compatible '
    'conditions. Quantitative equivalence needs matching population, period, denominator and non-overlapping '
    'categories; terminology equivalence needs the same biological referent, not merely related structures. '
    'When a source claim uses broad food-security terminology (such as "threatens the nutritional value of food") '
    'and paper evidence establishes a specific food-safety or grain-suitability mechanism (such as mycotoxin '
    'contamination reducing food safety, processing quality, or grain use for food without measuring macronutrient loss), '
    'grade claim_match="partial" with conditions_match=true. Select edge-bound proof so synthesis can propose '
    'a reviewable terminology correction (e.g., refining nutritional value to food safety/suitability). '
    'Likewise, when an epidemiological claim includes narrative relative time framing anchored to the source review '
    '(such as "from the 1990s until today" or "for many years"), paper evidence establishing the historical onset '
    'and ongoing epidemic persistence ("since the 1990s", "over the past two decades") satisfies conditions_match=true, '
    'without requiring external papers to literally quote the relative words "until today". '
    'For multi-factorial agricultural and epidemiological drivers: when external evidence substantiates that '
    'the claimed practices (such as maize-wheat rotation and conservation tillage) are established causes or '
    'drivers of disease epidemics, mentioning additional contributing co-factors (such as favorable weather or '
    'cultivar susceptibility) does not reduce claim_match to partial; grade claim_match="exact" with conditions_match=true. '
    'For qualitative importance, severity, and agronomic symptoms: equivalent scientific framing represents proposition '
    'equivalence (such as "major concern primarily due to mycotoxins" vs "primary concern because of mycotoxins", '
    '"one of the most devastating grain diseases in Asia" vs "one of the most important grain diseases of rice in Asia", '
    'and "unfilled spikelets / chaffiness and lower grain weight" vs "unfilled and partially filled grains and lower grain weight"); '
    'grade claim_match="exact" with conditions_match=true. '
    'Keep genuine all/entire/major, negation and restrictions; never drop them as rhetoric. '
    'A broader class/setting can establish applicability only when the supplied evidence actually covers this '
    'specific material/population/condition and the reason explains that coverage. Membership alone or one '
    'example from a broader class is insufficient. Use null when applicability remains unestablished; never '
    'turn unknown into true merely to obtain partial support. '
    'Also select edge-bound proof for partial/context_only findings when the paper establishes terminology '
    'or a mechanism relevant to a possible correction, even if the original claim stance is unclear or '
    'not_addressed. Do not fabricate support for the original wording. These proofs enable review proposals. '
    'Interpret the actual predicates. An exclusion/absence link followed by another edge is an exclusion '
    'contrast, not evidence that the excluded factor enters the downstream mechanism. A susceptibility/host '
    'link can supply contextual host compatibility without asserting that susceptibility itself caused '
    'the downstream outcome. Association and affects do not automatically establish causation. '
    'For decisive findings select chunk_id and exact chunk-local sentence_numbers, never quote text or pool IDs. '
    '[4,6] means only sentences 4 and 6. Cite definition/context sentences separately when needed. Numbering '
    'restarts in each chunk. Cite retained proof IDs directly in findings and omit their entries from proofs. '
    'proofs contains only additional sentence selections with fresh IDs. Never change a retained ID selection. '
    'Every decisive finding needs paper-bound proof. Keep reasons short. Return JSON only.')

PAPER_SYSTEM += (' For numerical/fraction equivalence requiring arithmetic, return optional calculations '
    'using a proof_id, operation and 2-3 literal numeric strings selected from that proof. ratio_percent uses '
    '[numerator,denominator]; change_percent uses [before,after] for signed (after-before)/before; '
    'sum_ratio_percent uses [part1,part2,denominator]. Code computes the percentage; it does not check units, '
    'periods, uncertainty or scientific comparability. Never invent a denominator or use numbers from the '
    'originating PDF. Omit calculations if unnecessary. Examples/country lists do not establish a universal '
    'claim or susceptibility. A material numerical mismatch may be opposing evidence only when it is the '
    'same measure and setting; otherwise retain partial or unestablished evidence, not forced support.')


def _call(client, system, payload, schema, label, settings, check, budget, bindings=None):
    user = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    if len(user) > settings.external_input_max_characters:
        raise LLMServiceError(f'{label}: input exceeds pathway budget; evidence was not silently dropped')
    if hasattr(client, 'set_request_context'):
        client.set_request_context({'pathway_id': payload.get('pathway_id') or payload.get('pathway',{}).get('pathway_id'),
            'paper_count': len(payload.get('papers', [])) or 1,
            'pathway_edge_count':len(payload.get('pathway',{}).get('edges',[])), 'external_phase': label.split(':')[0]})
    def bind(answer):
        answer = deepcopy(answer)
        for key, value in (bindings or {}).items():
            answer.setdefault(key, value)
        return answer
    if bindings:
        system += (' Omit request-bound metadata fields from the response: ' + ', '.join(bindings)
                   + '. Code attaches them from this request. Keep every edge, connection and proof reference.')
    checked_answer, checked_result = None, None
    def validate(answer):
        nonlocal checked_answer, checked_result
        bound = bind(answer)
        result = check(bound)
        # Keep the exact successful response, not a reference that a provider's
        # recovery loop could subsequently mutate. Grounded proof is carried to
        # the caller instead of reconstructing the same sentences three times.
        checked_answer = deepcopy(bound)
        checked_result = bound if result is None else result
        return checked_result
    answer = client.chat_json(system, user, max_tokens=min(32768, max(budget, settings.external_min_output_budget)),
        answer_token_hint=budget, json_schema=schema, request_label=label, response_check=validate)
    answer = bind(answer)
    # Lightweight clients need not invoke response_check; a changed final
    # response must also be checked rather than reusing another attempt's proof.
    if answer == checked_answer:
        return checked_result
    try:
        return validate(answer)
    except EvidenceSelectionError as exc:
        # Keep the rejected model proposal for audit, separate from admitted proof.
        exc.review_response = deepcopy(answer)
        raise


def _merge_proofs(answer, retained):
    """Accept an idempotent echo; never guess what a conflicting label means."""
    identifiers = [p['proof_id'] for p in answer['proofs']]
    if len(set(identifiers)) != len(identifiers) or any(not p.strip() for p in identifiers):
        raise EvidenceSelectionError('Proof identities must be unique and nonempty within their paper')
    additions = []
    for proof in answer['proofs']:
        old = retained.get(proof['proof_id'])
        if old is None:
            additions.append(proof)
        elif (proof['chunk_id'] != old['chunk_id'] or proof['sentence_numbers'] != old['sentence_numbers']
              or ('quoted_span' in proof and proof['quoted_span'] != old.get('quoted_span'))):
            raise EvidenceSelectionError('A retained proof ID was reused for different paper-bound evidence')
        # Equal paper-local IDs + exact selections name the same immutable proof.
        # This is reference bookkeeping, not a change to scientific model output.
    if len(additions) > 10:
        raise EvidenceSelectionError('Return at most ten additional paper-bound proof selections')
    return [*retained.values(), *additions]


def judge_paper(client, target, document, settings, finding_cache=None):
    cached = finding_cache.lookup(target, document) if finding_cache else {}
    pending = [e['id'] for e in target['edges'] if e['id'] not in cached]
    proofs = {p['proof_id']: p for kept in cached.values() for p in kept['proofs']}
    payload = {'pathway': pathway_payload(target), 'paper': packed_document(document),
        'edge_ids_to_assess': pending, 'retained_edge_findings': [r['finding'] for r in cached.values()],
        'retained_proofs': [{k: p[k] for k in ('proof_id','chunk_id','sentence_numbers')} for p in proofs.values()]}
    retained_calculations = [c for kept in cached.values() for c in kept.get('calculations',[])]
    def validate(answer):
        complete = {**answer, 'edge_findings': [*[r['finding'] for r in cached.values()], *answer['edge_findings']],
                    'proofs': _merge_proofs(answer, proofs)}
        if retained_calculations or answer.get('calculations'):
            complete['calculations'] = list({json.dumps(c,sort_keys=True):c for c in
                [*retained_calculations,*answer.get('calculations',[])]}.values())
        return ground_paper(target, document, complete)
    with finding_cache.paper_slots if finding_cache else nullcontext():
        grounded = _call(client, PAPER_SYSTEM, payload, paper_schema(target, document, pending, len(proofs)),
            f"neighbor paper external: {target['pathway_id']} / {document['evidence_document_id']}", settings,
            validate, 2600, bindings={'pathway_id': target['pathway_id'], 'evidence_document_id': document['evidence_document_id']})
    grounded['reused_edge_findings'] = len(cached)
    if finding_cache:
        finding_cache.store(target, document, grounded)
    return grounded


def synthesis_schema(target, papers):
    refs = arr(obj({'evidence_document_id': choice([p['evidence_document_id'] for p in papers]), 'proof_id': TEXT}))
    nodes = sorted({e[k] for e in target['edges'] for k in ('source','target')})
    return obj({'verdict': choice(['connection_supported', 'components_supported', 'unresolved', 'contradicted', 'incompatible', 'out_of_scope']),
        'evidence_basis': choice(['local_connection_support', 'component_evidence_only', 'insufficient', 'conflicting']),
        'in_scope': BOOL, 'scope_reason': TEXT, 'synthesis': TEXT, 'limitations': arr(TEXT),
        'edge_assessments': arr(obj({'edge_id': choice([e['id'] for e in target['edges']]),
            'stance': choice(['supported','partially_supported','contradicted','unresolved']), 'proof_refs': refs, 'reason': TEXT}),
            minItems=len(target['edges']),maxItems=len(target['edges'])),
        'connection_assessments': arr(obj({'connection_id': choice([c['connection_id'] for c in target['connections']]),
            'connection_kind': choice(['mechanistic','contextual','exclusion','unresolved']),
            'compatibility': choice(['compatible','incompatible','unresolved']), 'proof_refs': refs, 'reason': TEXT}),
            minItems=len(target['connections']),maxItems=len(target['connections'])),
        'entity_roles': arr(obj({'node_id': choice(nodes),
            'contextual_role': TEXT, 'proposed_type': TEXT, 'resolved': BOOL, 'proof_refs': refs, 'reason': TEXT}),
            minItems=len(nodes),maxItems=len(nodes)),
        'corrections': arr(obj({'edge_id':choice([e['id'] for e in target['edges']]),
            'claim_text':{'type':'string','minLength':1},'reason':TEXT,'proof_refs':refs}),maxItems=len(target['edges']))})


def check_synthesis(target, papers, answer):
    if answer['pathway_id'] != target['pathway_id']:
        raise EvidenceSelectionError('Synthesis belongs to a different pathway')
    _identity(answer['edge_assessments'], 'edge_id', [e['id'] for e in target['edges']])
    _identity(answer['connection_assessments'], 'connection_id', [c['connection_id'] for c in target['connections']])
    _identity(answer['entity_roles'], 'node_id', {e[k] for e in target['edges'] for k in ('source','target')})
    allowed = {(p['evidence_document_id'], proof['proof_id']): proof for p in papers for proof in p['proofs']}
    def refs(row, field='proof_refs'):
        selected = [(r['evidence_document_id'], r['proof_id']) for r in row[field]]
        if any(r not in allowed for r in selected):
            raise EvidenceSelectionError('Synthesis invents a proof or moves it to another paper')
        return selected
    for edge in answer['edge_assessments']:
        selected = refs(edge)
        if edge['stance'] in {'supported','partially_supported','contradicted'}:
            expected = 'contradicts' if edge['stance']=='contradicted' else 'supports'
            exact_matching = {(p['evidence_document_id'], proof_id) for p in papers for row in p['edge_findings']
                              if row['edge_id']==edge['edge_id'] and row['stance']==expected
                              and row.get('conditions_match') is True
                              and row.get('claim_match') == 'exact'
                              for proof_id in row['proof_ids']}
            partial_matching = {(p['evidence_document_id'], proof_id) for p in papers for row in p['edge_findings']
                                if row['edge_id']==edge['edge_id'] and row['stance']==expected
                                and row.get('conditions_match') is True
                                and row.get('claim_match') in {'exact', 'partial'}
                                for proof_id in row['proof_ids']}
            if edge['stance'] == 'supported':
                papers_in_support = {doc_id for (doc_id, proof_id) in selected if (doc_id, proof_id) in partial_matching}
                has_support = any(r in exact_matching for r in selected) or len(papers_in_support) >= 2
            elif edge['stance'] == 'partially_supported':
                has_support = any(r in partial_matching for r in selected)
            else:
                has_support = any(r in exact_matching for r in selected)
            if not selected or not has_support:
                raise EvidenceSelectionError(
                    f"Edge {edge['edge_id']}: synthesis cannot invent or reverse an evidence-bound edge finding; "
                    'supported/contradicted requires an exact proposition match with conditions_match=true '
                    '(or joint support from multiple applicable papers). '
                    'Broader or incomplete wording may only receive partial support. '
                    'Keep false/null applicability unresolved rather than promoting contextual evidence')
    seen_corrections=set()
    by_edge={e['edge_id']:e for e in answer['edge_assessments']}
    for correction in answer.get('corrections',[]):
        edge_id=correction['edge_id']
        if edge_id not in by_edge or edge_id in seen_corrections:
            raise EvidenceSelectionError('A correction must identify one unique original edge')
        seen_corrections.add(edge_id)
        selected=refs(correction)
        bound={(p['evidence_document_id'],identifier) for p in papers for row in p['edge_findings']
               if row['edge_id']==edge_id for identifier in row['proof_ids']}
        if not selected or not set(selected) <= bound:
            raise EvidenceSelectionError('A proposed correction requires proof bound to its original edge')
        if not correction['claim_text'].strip() or by_edge[edge_id]['stance']=='supported':
            raise EvidenceSelectionError('A required wording correction cannot count as full support for the unchanged original claim')
    for connection in answer['connection_assessments']:
        selected = refs(connection)
        if connection['compatibility']!='unresolved' and not selected:
            raise EvidenceSelectionError('A decisive connection assessment needs paper-bound proof')
        link = next(c for c in target['connections'] if c['connection_id']==connection['connection_id'])
        allowed_connection = {(p['evidence_document_id'],proof_id) for p in papers
            for row in [*p['connection_findings'],*p['edge_findings']]
            if row.get('connection_id')==connection['connection_id'] or row.get('edge_id') in {link['upstream_edge_id'],link['downstream_edge_id']}
            for proof_id in row['proof_ids']}
        if connection['compatibility']!='unresolved' and not any(r in allowed_connection for r in refs(connection)):
            raise EvidenceSelectionError('Connection evidence must come from its actual adjacent steps or connection finding')
    for role in answer['entity_roles']:
        if role['resolved'] and not refs(role):
            raise EvidenceSelectionError('Resolved contextual roles require external evidence')
        refs(role)
    if answer['verdict']=='connection_supported':
        if (not answer['in_scope'] or answer['evidence_basis']!='local_connection_support'
                or any(e['stance']!='supported' for e in answer['edge_assessments'])
                or any(c['compatibility']!='compatible' or c['connection_kind'] not in {'mechanistic','contextual'}
                       for c in answer['connection_assessments'])):
            stances = ', '.join(f"{e['edge_id']}={e['stance']}" for e in answer['edge_assessments'])
            raise EvidenceSelectionError('Local support requires BOTH edge stances to equal supported '
                '(partially_supported is insufficient), in_scope=true, evidence_basis=local_connection_support '
                'and a compatible mechanistic/contextual connection. '
                f'Declared edge stances: {stances}. '
                'Preserve partial/unresolved findings; choose a consistent overall verdict rather than upgrading '
                'the claims to force support. Partial-only applicable evidence belongs in components_supported '
                'with evidence_basis=component_evidence_only. Exclusion or unresolved meaning cannot grant positive local support')
    if answer['verdict']=='out_of_scope' and answer['in_scope'] is not False:
        raise EvidenceSelectionError('Out-of-scope verdict conflicts with declared project scope')
    if answer['verdict']=='components_supported' and not any(e['stance'] in {'supported','partially_supported'} for e in answer['edge_assessments']):
        raise EvidenceSelectionError('Components-supported verdict requires at least one supported component')
    if answer['verdict']=='contradicted' and not any(e['stance']=='contradicted' for e in answer['edge_assessments']):
        raise EvidenceSelectionError('Pathway contradiction requires identified opposing evidence')
    if answer['verdict']=='incompatible' and not any(c['compatibility']=='incompatible' for c in answer['connection_assessments']):
        raise EvidenceSelectionError('Incompatible route must identify an incompatible connection')


SYNTHESIS_SYSTEM = ('Assess only TWO neighboring claims and their local connection using the supplied grounded paper '
    'findings. Return connection_supported when both qualified claims have applicable external support and the shared '
    'entity and locally applicable conditions are compatible. Evidence may come from different papers; no paper must '
    'demonstrate the complete pair as a mediated causal route. This outcome never validates a longer chain or a direct '
    'first-to-last effect. Do not introduce upstream/downstream claims outside this pair. '
    'Compare identity, compartment, crop/species, dose, timing and essential qualifiers; preserve non-causal relation '
    'meanings. Supports/contradicts with conditions_match=false/null are contextual findings only and cannot become '
    'decisive edge support or contradiction. Each decisive edge must cite matching paper proof of the same stance '
    'with applicability true. When multiple papers or proofs support an edge (such as a review quote plus a '
    'direct experimental study), cite all applicable supporting proofs in proof_refs so the subsequent proof audit '
    'sees the complete quotation evidence. Assess compatibility from actual adjacent findings, definitions and conditions, not '
    'name equality or plausibility alone. Unresolved compatibility keeps the pair unresolved/components_supported. '
    'Fully supported or contradicted edges require exact proposition matches with applicable conditions. '
    'Use partially_supported for a broader relationship with unverified details. Contextual or mismatched '
    'findings cannot veto applicable support unless they identify a comparable opposing result. '
    'Select the overall verdict consistently with the completed edge and connection assessments. '
    'connection_supported requires BOTH edge stances to be exactly supported, never partially_supported '
    'or unresolved. A compatible junction does not turn partial claims into fully supported claims. '
    'When only partial component support is established, use components_supported and '
    'evidence_basis=component_evidence_only unless the evidence establishes an opposing, incompatible '
    'or out-of-scope outcome. Never upgrade an edge stance just to obtain connection_supported. '
    'Partially_supported also requires at least one supports finding with conditions_match=true and '
    'claim_match=exact/partial for that edge. False/null applicability cannot ground partial support; retain '
    'it as contextual evidence and leave that edge unresolved. Exact is scientific equivalence, not a '
    'verbatim phrase or one-sentence requirement. Multiple grounded sentences may jointly establish the '
    'proposition, but numerical/terminological equivalence must preserve population, period, denominator, '
    'categories, biological referents and genuine quantifiers. '
    'Relative temporal anchors from review sources (such as "since the 1990s until today" or "for many years") '
    'are satisfied by papers documenting the ongoing multi-decade epidemic period without requiring verbatim '
    '"until today" phrasing. '
    'For multi-factorial agricultural drivers, mentioning additional co-factors (e.g. weather/cultivars alongside tillage/rotation) '
    'does not prevent full support. Equivalent qualitative importance and agronomic symptom framing (such as "major concern '
    'primarily due to mycotoxins", "devastating grain disease in Asia", and "unfilled spikelets/chaffiness and lower grain weight") '
    'substantiate exact edge support. '
    'Declare connection_kind from evidence and actual predicate meaning: mechanistic for an evidenced '
    'biological connection, contextual for host/susceptibility or other non-causal context, exclusion for '
    'an absence/exclusion contrast, unresolved when its meaning is unestablished. Exclusion findings remain '
    'visible but cannot grant connection_supported or become a positive propagation path. Assess host '
    'compatibility without requiring that susceptibility itself caused the outcome. No sign multiplication '
    'or direct first-to-last effect follows from these categories. '
    'Preserve the meaning of all/major/absence, distinguish quality metrics and biological settings, and never '
    'infer an absent outcome merely because one proposed mechanism was not observed. '
    'Original PDF agreement establishes source fidelity only, not external truth. '
    'Return corrections=[] unless terminology or scientific wording requires revision; then propose a short '
    'corrected claim with edge-bound external proof and a reason. This is a review proposal, not an automatic edit '
    'or support for the original wording. Never silently replace an original assertion with its correction. '
    'Consider edge-bound partial/contextual proof for a necessary terminology or mechanism correction. '
    'When external proof demonstrates that disease infection or mycotoxins reduce grain protein/nutrients, '
    'render food unsafe or unfit for consumption, or degrade cooking and eating quality, that evidence substantiates '
    'full edge support (stance="supported") for claims asserting threats to nutritional value or eating quality. '
    'Do not add absent species, mechanisms or intermediates from memory, and do not require a correction '
    'when no supplied proof justifies one. Keep all genuine scientific restrictions in the original. '
    'Preserve opposing findings, uncertainty and project scope. Report externally evidenced contextual roles/types '
    'for every supplied node; leave unresolved roles explicit. Original PDF context resolves meaning but cannot '
    'supply external proof. Cite exact supplied paper/proof pairs and never move evidence between papers. '
    'Give concise reasons and limitations. No numerical trust/confidence score. Return JSON only.')


def synthesize(client, target, papers, settings):
    from backend.domain.ontology import ENTITY_DEFINITIONS, PATHWAY_DESCRIPTION
    payload = {'pathway': pathway_payload(target), 'papers': papers,
               'project_domain': PATHWAY_DESCRIPTION, 'entity_definitions': ENTITY_DEFINITIONS}
    return _call(client, SYNTHESIS_SYSTEM, payload, synthesis_schema(target,papers),
        f"neighbor cross-paper synthesis: {target['pathway_id']} / {len(papers)} papers", settings,
        lambda answer: check_synthesis(target,papers,answer), 2500 + len(target['edges'])*500,
        bindings={'pathway_id': target['pathway_id']})
