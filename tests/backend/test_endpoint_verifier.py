import json
from contextlib import closing
from hashlib import sha256

from backend.evaluation.endpoint_verifier import (
    WHITELISTED_TYPE_CHALLENGES,
    apply_relation_verification_decisions,
    apply_verified_decisions,
    experimental_relation_catalog,
    extract_treatment_methods_context,
    prepare_endpoint_verification_items,
    prepare_relation_verification_items,
    verify_passage_endpoints,
    verify_unresolved_relations,
)
from backend.evaluation.extract_then_ground import ground_extracted, should_use_extraction_thinking
from backend.evaluation.ontology_lookup import endpoint_candidates, open_lookup
from backend.ontology.catalog import catalog
from backend.providers.response_repair import validate_schema


def _verifier_index(tmp_path):
    owl = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:obo="http://purl.obolibrary.org/obo/"
         xmlns:oboInOwl="http://www.geneontology.org/formats/oboInOwl#">
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000015"><rdfs:label>process</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000020"><rdfs:label>specifically dependent continuant</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000511">
           <rdfs:label>phosphorus uptake</rdfs:label>
           <obo:IAO_0000115>Absorption of phosphorus by plant roots.</obo:IAO_0000115>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000078">
           <rdfs:label>root dry weight</rdfs:label>
           <obo:IAO_0000115>The average dry weight of the root determined in a study.</obo:IAO_0000115>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PECO_0007397">
           <rdfs:label>phosphorus nutrient exposure</rdfs:label>
           <oboInOwl:hasNarrowSynonym>phosphorus nutrient treatment narrow</oboInOwl:hasNarrowSynonym>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000015"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/GO_0040007">
           <rdfs:label>growth</rdfs:label>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000015"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PO_0025025"><rdfs:label>root system</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/CHEBI_28659">
           <rdfs:label>phosphorus atom</rdfs:label>
           <oboInOwl:hasExactSynonym>phosphorus</oboInOwl:hasExactSynonym>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/NCBITaxon_4113">
           <rdfs:label>Solanum tuberosum</rdfs:label>
           <oboInOwl:hasExactSynonym>potato</oboInOwl:hasExactSynonym>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PATO_0000586"><rdfs:label>increased size</rdfs:label></owl:Class>
         </rdf:RDF>"""
    (tmp_path / 'sample.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'sample': {'file': 'sample.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    return open_lookup(tmp_path)


def _claim(sid, subj, stype, rel, obj, otype, r_quote=None):
    return {
        'subject': subj,
        'subject_type': stype,
        'relation': rel,
        'object': obj,
        'object_type': otype,
        'assertion_status': 'affirmed',
        'qualifiers': {
            'crop_or_species': None, 'population': None, 'condition': None,
            'comparator': None, 'metric': None,
        },
        'support_sentence_ids': [],
        'subject_anchor': {'sentence_id': sid, 'quote': subj},
        'relation_anchor': {'sentence_id': sid, 'quote': r_quote or rel},
        'object_anchor': {'sentence_id': sid, 'quote': obj},
    }


def test_endpoint_verifier_whitelists_hard_gates_and_preserves_holds(tmp_path):
    reference = catalog()
    db, _ = _verifier_index(tmp_path)
    target = {
        'target_id': 't1',
        'target_sentence_numbers': [1],
        'sentences': [{'page': 1}],
        'source_abbreviation_definitions': [
            {'abbreviation': 'P', 'expansion': 'Phosphorus', 'quote': 'Phosphorus (P)', 'document': 'd1'},
        ],
    }
    visible = {
        1: 'Optimal P supply had an overall negative effect on potato root growth while root dry mass differed and shallow root system remained in 43.9% of potato cultivar Kuba with P uptake.',
    }

    with closing(db):
        # 1. Valid claim with root dry mass (permitted TO) and potato root growth (permitted GO)
        c1_raw = _claim(1, 'Optimal P supply', 'condition or exposure', 'had an overall negative effect on',
                        'potato root growth', 'biological process')
        c1, c1_cands = ground_extracted(c1_raw, target, visible, reference, db)
        c1['id'] = 'c1'

        # 2. P uptake typed ambiguously as biological process (crosses into whitelisted trait TO_0000511)
        #    while subject 'Optimal P supply' stays within condition or exposure
        c2_raw = _claim(1, 'Optimal P supply', 'condition or exposure', 'differed', 'P uptake', 'biological process')
        c2, c2_cands = ground_extracted(c2_raw, target, visible, reference, db)
        c2['id'] = 'c2'
        # Inject CHEBI_28659 into c2 subject candidates to prove condition_or_exposure cannot challenge into CHEBI
        c2_cands['subject'].append({'iri': 'http://purl.obolibrary.org/obo/CHEBI_28659', 'label': 'phosphorus atom',
                                    'definition': '', 'parents': [], 'class_source': 'sample.owl'})

        # 3. Held claim (43.9% pure measurement subject + intact shallow root system object)
        c3_raw = _claim(1, '43.9%', 'trait or measurement', 'remained', 'shallow root system', 'plant part')
        c3, c3_cands = ground_extracted(c3_raw, target, visible, reference, db)
        c3['id'] = 'c3'
        c3_cands['subject'] = [{'iri': 'http://purl.obolibrary.org/obo/PATO_0000586', 'label': 'increased size',
                                'definition': '', 'parents': [], 'class_source': 'sample.owl'}]

        # 4. Generic P chemical + cultivar hard gates
        c4_raw = _claim(1, 'potato cultivar Kuba', 'organism', 'had an overall negative effect on', 'P', 'chemical')
        c4, c4_cands = ground_extracted(c4_raw, target, visible, reference, db)
        c4['id'] = 'c4'
        c4_cands['subject'] = [{'iri': 'http://purl.obolibrary.org/obo/NCBITaxon_4113', 'label': 'Solanum tuberosum',
                                'definition': '', 'parents': [], 'class_source': 'sample.owl'}]

        claims = [c1, c2, c3, c4]
        cands_map = {'c1': c1_cands, 'c2': c2_cands, 'c3': c3_cands, 'c4': c4_cands}
        items = prepare_endpoint_verification_items(claims, cands_map, db, reference, targets_by_id={'t1': target})
        items_by_id = {it['item_id']: it for it in items}

        # Verify whitelisting: c2:subject has NO alternate CHEBI candidate; c2:object HAS whitelisted TO_0000511
        assert 'condition or exposure' not in WHITELISTED_TYPE_CHALLENGES
        assert all(c['namespace'] != 'CHEBI' for c in items_by_id['c2:subject']['alternate_type_candidates'])
        assert any(c['iri'].endswith('TO_0000511') and c['suggested_type'] == 'trait or measurement'
                   for c in items_by_id['c2:object']['alternate_type_candidates'])

        # Mock verifier LLM attempting BOTH valid mappings AND forbidden/hard-gated mappings
        class FakeVerifierClient:
            def chat_json(self, system, user_msg, **options):
                parsed = json.loads(user_msg)
                decs = []
                for it in parsed['items']:
                    iid = it['item_id']
                    if iid == 'c1:subject':
                        alias = next(c['alias'] for c in it['permitted_candidates'] if c['label'] == 'phosphorus nutrient exposure')
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': alias,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'LP/P supply exposure.'})
                    elif iid == 'c1:object':
                        alias = next(c['alias'] for c in it['permitted_candidates'] if c['label'] == 'growth')
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': alias,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Root growth is plant growth.'})
                    elif iid == 'c2:object':
                        alias = next(c['alias'] for c in it['whitelisted_alternate_candidates'] if c['label'] == 'phosphorus uptake')
                        decs.append({'item_id': iid, 'decision': 'exact', 'candidate_alias': alias,
                                     'type_challenged': True, 'revised_type': 'trait or measurement',
                                     'justification': 'Measured trait mention in passage.'})
                    elif iid == 'c3:subject':
                        # Attempt forbidden numeric percentage mapping -> hard gate must block
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': 'C1',
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Bad numeric attempt.'})
                    elif iid == 'c3:object':
                        # Intact endpoint on held claim -> valid broader PO:root system, while claim stays held!
                        alias = next(c['alias'] for c in it['permitted_candidates'] if c['label'] == 'root system')
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': alias,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Shallow root system is root system.'})
                    elif iid == 'c4:subject':
                        # Attempt species collapse on cultivar -> hard gate must block
                        decs.append({'item_id': iid, 'decision': 'exact', 'candidate_alias': 'C1',
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Bad cultivar collapse.'})
                    elif iid == 'c4:object':
                        # Attempt generic P -> phosphorus atom OR out-of-list alias C99 -> hard gate must block
                        decs.append({'item_id': iid, 'decision': 'exact', 'candidate_alias': 'C1',
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Bad generic P attempt.'})
                    else:
                        decs.append({'item_id': iid, 'decision': 'unresolved', 'candidate_alias': None,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Abstained.'})
                resp = {'decisions': decs}
                validate_schema(resp, options['json_schema'])
                return resp

        decisions = verify_passage_endpoints(items, FakeVerifierClient())
        arm2 = {row['id']: row for row in apply_verified_decisions(claims, decisions, items_by_id, db, reference)}

        # Arm 1 relation checks preserved + c1 now satisfies RO_0002305 occurrent -> occurrent!
        assert arm2['c1']['bindings']['subject']['iri'].endswith('PECO_0007397')
        assert arm2['c1']['bindings']['object']['iri'].endswith('GO_0040007')
        assert arm2['c1']['bindings']['relation']['iri'].endswith('RO_0002305')
        assert arm2['c1']['classified'] is True

        # Whitelisted override flagged explicitly
        assert arm2['c2']['bindings']['object']['iri'].endswith('TO_0000511')
        assert arm2['c2']['bindings']['object']['type_challenged'] is True
        assert arm2['c2']['bindings']['object']['revised_type'] == 'trait or measurement'

        # Held claim keeps review=='held' and abstains on 43.9%, while intact shallow root system maps cleanly
        assert arm2['c3']['bindings']['subject']['iri'] is None
        assert arm2['c3']['bindings']['object']['iri'].endswith('PO_0025025')
        assert arm2['c3']['review'] == 'held' and arm2['c3']['classified'] is False

        # Hard gates blocked cultivar collapse and generic elemental P
        assert arm2['c4']['bindings']['subject']['iri'] is None
        assert arm2['c4']['bindings']['object']['iri'] is None


def _extended_verifier_index(tmp_path):
    owl = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:obo="http://purl.obolibrary.org/obo/"
         xmlns:oboInOwl="http://www.geneontology.org/formats/oboInOwl#">
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000015"><rdfs:label>process</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000020"><rdfs:label>specifically dependent continuant</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PECO_0001006"><rdfs:label>phosphate exposure</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PECO_0001046">
           <rdfs:label>limited phosphate exposure</rdfs:label>
           <obo:IAO_0000115>A phosphate exposure (PECO:0001006) involving exposure to limiting amounts or absence of phosphate (CHEBI:18367).</obo:IAO_0000115>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/PECO_0001006"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PECO_0007397">
           <rdfs:label>phosphorus nutrient exposure</rdfs:label>
           <obo:IAO_0000115>The treatment involving exposure of plant to macronutrient phosphorus as supplement to study various types of responses.</obo:IAO_0000115>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000015"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/ENVO_01000254">
           <rdfs:label>environmental system</rdfs:label>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PO_0025007">
           <rdfs:label>collective plant organ structure</rdfs:label>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PO_0025025">
           <rdfs:label>root system</rdfs:label>
           <obo:IAO_0000115>A collective plant organ structure (PO:0025007) that produces root meristems.</obo:IAO_0000115>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/PO_0025007"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PO_0009005">
           <rdfs:label>root</rdfs:label>
           <obo:IAO_0000115>A plant axis that lacks shoot axis nodes and usually grows indeterminately.</obo:IAO_0000115>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000227">
           <rdfs:label>root length</rdfs:label>
           <obo:IAO_0000115>A root morphology trait which is the length of a root.</obo:IAO_0000115>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000078">
           <rdfs:label>root dry weight</rdfs:label>
           <obo:IAO_0000115>The average dry weight of the root determined in a study.</obo:IAO_0000115>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000985">
           <rdfs:label>root system morphology trait</rdfs:label>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/GO_0040007">
           <rdfs:label>growth</rdfs:label>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000015"/>
         </owl:Class>
         </rdf:RDF>"""
    (tmp_path / 'ext.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'ext': {'file': 'ext.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    return open_lookup(tmp_path)


def test_peco_supplementation_low_dose_and_limited_phosphate_gates(tmp_path):
    reference = catalog()
    db, _ = _extended_verifier_index(tmp_path)
    glossary = [
        {'abbreviation': 'P', 'expansion': 'Phosphorus', 'quote': 'Phosphorus (P)', 'document': 'd1'},
        {'abbreviation': 'LP', 'expansion': 'low phosphorus', 'quote': 'low phosphorus (LP)', 'document': 'd1'},
        {'abbreviation': 'HP', 'expansion': 'high phosphorus', 'quote': 'high phosphorus (HP)', 'document': 'd1'},
    ]
    t_no_methods = {
        'target_id': 't_no',
        'target_sentence_numbers': [1, 2],
        'sentences': [{'page': 1}, {'page': 1}],
        'source_abbreviation_definitions': glossary,
    }
    t_with_methods = {
        **t_no_methods,
        'target_id': 't_yes',
        'methods_context': 'P concentrations were adjusted to 0.5 mM (HP) and 0.1 mM (LP) using KH2PO4.',
    }
    visible = {
        1: 'P deficiency had a negative effect on root growth while LP treatment reduced 21 out of 22 RSA traits.',
        2: '15 traits differed between HP and LP environments under HP treatment.',
    }

    with closing(db):
        # Retrieval check: PECO_0001046 surfaces for LP treatment and P deficiency.
        lp_cands = endpoint_candidates(db, reference, 'LP treatment', [visible[1]], glossary)
        def_cands = endpoint_candidates(db, reference, 'P deficiency', [visible[1]], glossary)
        assert any(c['iri'].endswith('PECO_0001046') for c in lp_cands)
        assert any(c['iri'].endswith('PECO_0001046') for c in def_cands)

        c_def, c_def_cands = ground_extracted(
            _claim(1, 'P deficiency', 'condition or exposure', 'reduced', '21 out of 22 RSA traits', 'trait or measurement'),
            t_no_methods, visible, reference, db,
        )
        c_def['id'] = 'c_def'
        c_def_cands['object'].append({'iri': 'http://purl.obolibrary.org/obo/TO_0000985',
                                      'label': 'root system morphology trait', 'definition': '', 'parents': [],
                                      'class_source': 'ext.owl'})

        c_lp_no, c_lp_no_cands = ground_extracted(
            _claim(1, 'LP treatment', 'condition or exposure', 'reduced', 'root growth', 'biological process'),
            t_no_methods, visible, reference, db,
        )
        c_lp_no['id'] = 'c_lp_no'

        c_lp_supp, c_lp_supp_cands = ground_extracted(
            _claim(1, 'LP treatment', 'condition or exposure', 'reduced', 'root growth', 'biological process'),
            t_with_methods, visible, reference, db,
        )
        c_lp_supp['id'] = 'c_lp_supp'

        c_lp_phos, c_lp_phos_cands = ground_extracted(
            _claim(1, 'LP treatment', 'condition or exposure', 'reduced', 'root growth', 'biological process'),
            t_with_methods, visible, reference, db,
        )
        c_lp_phos['id'] = 'c_lp_phos'

        c_hp_lim, c_hp_lim_cands = ground_extracted(
            _claim(2, 'HP treatment', 'condition or exposure', 'differed', 'HP and LP environments', 'condition or exposure'),
            t_with_methods, visible, reference, db,
        )
        c_hp_lim['id'] = 'c_hp_lim'
        c_hp_lim_cands['subject'] = [
            {'iri': 'http://purl.obolibrary.org/obo/PECO_0001046', 'label': 'limited phosphate exposure',
             'definition': '', 'parents': [], 'class_source': 'ext.owl'},
        ]
        c_hp_lim_cands['object'] = [
            {'iri': 'http://purl.obolibrary.org/obo/ENVO_01000254', 'label': 'environmental system',
             'definition': '', 'parents': [], 'class_source': 'ext.owl'},
        ]

        claims = [c_def, c_lp_no, c_lp_supp, c_lp_phos, c_hp_lim]
        cands_map = {
            'c_def': c_def_cands, 'c_lp_no': c_lp_no_cands, 'c_lp_supp': c_lp_supp_cands,
            'c_lp_phos': c_lp_phos_cands, 'c_hp_lim': c_hp_lim_cands,
        }
        items = prepare_endpoint_verification_items(
            claims, cands_map, db, reference,
            targets_by_id={'t_no': t_no_methods, 't_yes': t_with_methods},
        )
        items_by_id = {it['item_id']: it for it in items}

        class MockPECOClient:
            def chat_json(self, system, user_msg, **options):
                decs = []
                for it in json.loads(user_msg)['items']:
                    iid = it['item_id']
                    if iid == 'c_def:subject':
                        # Deficiency attempting PECO_0007397 must be blocked
                        alias = next(c['alias'] for c in it['permitted_candidates'] if c['label'] == 'phosphorus nutrient exposure')
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': alias,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Deficiency attempt.'})
                    elif iid == 'c_def:object':
                        # Count phrase 21 out of 22 must be blocked
                        alias = next(c['alias'] for c in it['permitted_candidates'] if c['label'] == 'root system morphology trait')
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': alias,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Count attempt.'})
                    elif iid == 'c_lp_no:subject':
                        # Low-P without addition methods trying PECO_0007397 must be blocked
                        alias = next(c['alias'] for c in it['permitted_candidates'] if c['label'] == 'phosphorus nutrient exposure')
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': alias,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'LP without methods.'})
                    elif iid == 'c_lp_supp:subject':
                        # Low-P WITH KH2PO4 methods trying PECO_0007397 should pass
                        alias = next(c['alias'] for c in it['permitted_candidates'] if c['label'] == 'phosphorus nutrient exposure')
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': alias,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'KH2PO4 methods added P.'})
                    elif iid == 'c_lp_phos:subject':
                        # Low-P WITH KH2PO4 methods trying PECO_0001046 should pass
                        alias = next(c['alias'] for c in it['permitted_candidates'] if c['label'] == 'limited phosphate exposure')
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': alias,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'KH2PO4 phosphate form.'})
                    elif iid == 'c_hp_lim:subject':
                        # HP treatment cannot map to limited phosphate exposure
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': 'C1',
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Bad HP->limited.'})
                    elif iid == 'c_hp_lim:object':
                        # HP and LP contrast / ENVO_01000254 must be blocked
                        decs.append({'item_id': iid, 'decision': 'broader', 'candidate_alias': 'C1',
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Bad contrast.'})
                    else:
                        decs.append({'item_id': iid, 'decision': 'unresolved', 'candidate_alias': None,
                                     'type_challenged': False, 'revised_type': None, 'justification': 'Abstain.'})
                return {'decisions': decs}

        decs = verify_passage_endpoints(items, MockPECOClient())
        out = {r['id']: r for r in apply_verified_decisions(claims, decs, items_by_id, db, reference)}

        assert out['c_def']['bindings']['subject']['iri'] is None
        assert out['c_def']['bindings']['object']['iri'] is None
        assert out['c_lp_no']['bindings']['subject']['iri'] is None
        assert out['c_lp_supp']['bindings']['subject']['iri'].endswith('PECO_0007397')
        assert out['c_lp_phos']['bindings']['subject']['iri'].endswith('PECO_0001046')
        assert out['c_hp_lim']['bindings']['subject']['iri'] is None
        assert out['c_hp_lim']['bindings']['object']['iri'] is None

        # Also confirm PECO_0001046 blocks bare P deficiency BOTH without AND with KH2PO4 methods context!
        c_def_phos = {**c_def, 'id': 'c_def_phos'}
        it_def_phos = {**items_by_id['c_def:subject'], 'item_id': 'c_def_phos:subject', 'claim_id': 'c_def_phos'}
        p1046_alias = next(c['alias'] for c in it_def_phos['permitted_candidates'] if c['iri'].endswith('PECO_0001046'))
        res_phos = apply_verified_decisions(
            [c_def_phos],
            {'c_def_phos:subject': {'item_id': 'c_def_phos:subject', 'decision': 'broader',
                                   'candidate_alias': p1046_alias, 'type_challenged': False,
                                   'revised_type': None, 'justification': 'No phosphate form.'}},
            {'c_def_phos:subject': it_def_phos},
            db, reference,
        )
        assert res_phos[0]['bindings']['subject']['iri'] is None

        it_def_phos_methods = {**it_def_phos, 'methods_context': t_with_methods['methods_context']}
        res_phos_methods = apply_verified_decisions(
            [c_def_phos],
            {'c_def_phos:subject': {'item_id': 'c_def_phos:subject', 'decision': 'broader',
                                   'candidate_alias': p1046_alias, 'type_challenged': False,
                                   'revised_type': None, 'justification': 'Bare P deficiency even with methods.'}},
            {'c_def_phos:subject': it_def_phos_methods},
            db, reference,
        )
        assert res_phos_methods[0]['bindings']['subject']['iri'] is None


def test_collective_structure_retrieval_and_meronymy_guard(tmp_path):
    reference = catalog()
    db, _ = _extended_verifier_index(tmp_path)
    target = {'target_id': 't_net', 'target_sentence_numbers': [1], 'sentences': [{'page': 1}],
              'source_abbreviation_definitions': []}
    visible = {1: 'The root network had a significantly decreased root length under stress.'}

    with closing(db):
        cands = endpoint_candidates(db, reference, 'The root network', [visible[1]], ())
        assert cands and cands[0]['iri'].endswith('PO_0025025')
        assert any(c['iri'].endswith('PO_0009005') for c in cands)

        c_ok_raw = _claim(1, 'The root network', 'plant part', 'had a significantly decreased', 'root length', 'trait or measurement')
        c_ok, c_ok_cands = ground_extracted(c_ok_raw, target, visible, reference, db)
        c_ok['id'] = 'c_ok'

        # Shortlist offering ONLY single-organ part PO_0009005 must stay unresolved!
        c_part_raw = _claim(1, 'The root network', 'plant part', 'had a significantly decreased', 'root length', 'trait or measurement')
        c_part, _ = ground_extracted(c_part_raw, target, visible, reference, db)
        c_part['id'] = 'c_part'
        c_part_only_cands = {
            'subject': [{'iri': 'http://purl.obolibrary.org/obo/PO_0009005', 'label': 'root',
                         'definition': 'A plant axis that lacks shoot axis nodes.', 'parents': [], 'class_source': 'ext.owl'}],
            'object': [],
        }

        items = prepare_endpoint_verification_items(
            [c_ok, c_part], {'c_ok': c_ok_cands, 'c_part': c_part_only_cands}, db, reference,
        )
        items_by_id = {it['item_id']: it for it in items}

        decs = {
            'c_ok:subject': {
                'item_id': 'c_ok:subject', 'decision': 'exact',
                'candidate_alias': next(c['alias'] for c in items_by_id['c_ok:subject']['permitted_candidates']
                                        if c['iri'].endswith('PO_0025025')),
                'type_challenged': False, 'revised_type': None, 'justification': 'Collective root system.',
            },
            'c_part:subject': {
                'item_id': 'c_part:subject', 'decision': 'broader',
                'candidate_alias': 'C1',
                'type_challenged': False, 'revised_type': None, 'justification': 'Bad meronymy root attempt.',
            },
        }
        out = {r['id']: r for r in apply_verified_decisions([c_ok, c_part], decs, items_by_id, db, reference)}
        assert out['c_ok']['bindings']['subject']['iri'].endswith('PO_0025025')
        assert out['c_part']['bindings']['subject']['iri'] is None


def test_abbreviation_expansion_consistency_and_qualified_broader_downgrade(tmp_path):
    reference = catalog()
    db, _ = _extended_verifier_index(tmp_path)
    glossary = [
        {'abbreviation': 'TRL', 'expansion': 'Total root length', 'quote': 'Total root length (TRL)', 'document': 'd1'},
        {'abbreviation': 'LP', 'expansion': 'low phosphorus', 'quote': 'low phosphorus (LP)', 'document': 'd1'},
        {'abbreviation': 'HP', 'expansion': 'high phosphorus', 'quote': 'high phosphorus (HP)', 'document': 'd1'},
    ]
    target = {
        'target_id': 't_trl', 'target_sentence_numbers': [1, 2], 'sentences': [{'page': 1}, {'page': 1}],
        'source_abbreviation_definitions': glossary,
    }
    visible = {
        1: "Cardoso increased TRL while total root length, TRL of 'Cardoso', its shallow and sparsely branched root system, and root dry mass varied.",
        2: 'Average root dry mass differed between HP conditions, LP conditions, and Inoculation with B. subtilis.',
    }

    with closing(db):
        c1, c1_c = ground_extracted(_claim(1, 'root dry mass', 'trait or measurement', 'varied', 'TRL', 'trait or measurement'), target, visible, reference, db)
        c1['id'] = 'c1'
        c2, c2_c = ground_extracted(_claim(1, "TRL of 'Cardoso'", 'trait or measurement', 'varied', 'total root length', 'trait or measurement'), target, visible, reference, db)
        c2['id'] = 'c2'
        c3, c3_c = ground_extracted(_claim(1, 'its shallow and sparsely branched root system', 'plant part', 'varied', 'root dry mass', 'trait or measurement'), target, visible, reference, db)
        c3['id'] = 'c3'
        c4, c4_c = ground_extracted(_claim(2, 'Average root dry mass', 'trait or measurement', 'differed between', 'HP conditions', 'condition or exposure'), target, visible, reference, db)
        c4['id'] = 'c4'
        c5, c5_c = ground_extracted(_claim(2, 'LP conditions', 'condition or exposure', 'differed between', 'Inoculation with B. subtilis', 'condition or exposure'), target, {**visible}, reference, db)
        c5['id'] = 'c5'

        c1_c['subject'] = [{'iri': 'http://purl.obolibrary.org/obo/TO_0000078', 'label': 'root dry weight',
                            'definition': 'The average dry weight of the root determined in a study.', 'parents': [], 'class_source': 'ext.owl'}]
        c4_c['subject'] = [{'iri': 'http://purl.obolibrary.org/obo/TO_0000078', 'label': 'root dry weight',
                            'definition': 'The average dry weight of the root determined in a study.', 'parents': [], 'class_source': 'ext.owl'}]
        c4_c['object'] = [{'iri': 'http://purl.obolibrary.org/obo/PECO_0001006', 'label': 'phosphate exposure',
                           'definition': 'A phosphorus nutrient exposure (PECO:0007397) involving exposure to phosphate.',
                           'parents': [], 'class_source': 'peco.owl'}]
        c5_c['subject'] = [{'iri': 'http://purl.obolibrary.org/obo/PECO_0001046', 'label': 'limited phosphate exposure',
                           'definition': 'A phosphate exposure (PECO:0001006) involving exposure to limiting amounts or absence of phosphate (CHEBI:18367).',
                           'parents': ['http://purl.obolibrary.org/obo/PECO_0001006'], 'class_source': 'peco.owl'}]
        c5_c['object'] = [{'iri': 'http://purl.obolibrary.org/obo/ECTO_3000026', 'label': 'exposure to Bacillus subtilis',
                           'definition': 'A history of exposure to Bacillus subtilis.',
                           'parents': [], 'class_source': 'ecto.owl'}]
        # Offer single-root TO_0000227 as C1 and root-system TO_0000985 as C2 for cumulative TRL phrases
        for cand_holder in (c1_c['object'], c2_c['subject'], c2_c['object']):
            cand_holder[:] = [
                {'iri': 'http://purl.obolibrary.org/obo/TO_0000227', 'label': 'root length',
                 'definition': 'A root morphology trait which is the length of a root.', 'parents': [], 'class_source': 'ext.owl'},
                {'iri': 'http://purl.obolibrary.org/obo/TO_0000985', 'label': 'root system morphology trait',
                 'definition': 'A collective plant organ structure morphology trait which is a quality of a root system.',
                 'parents': [], 'class_source': 'ext.owl'},
            ]
        c3_c['subject'] = [{'iri': 'http://purl.obolibrary.org/obo/PO_0025025', 'label': 'root system',
                            'definition': 'A collective plant organ structure.', 'parents': ['http://purl.obolibrary.org/obo/PO_0025007'], 'class_source': 'ext.owl'}]
        c3_c['object'] = [{'iri': 'http://purl.obolibrary.org/obo/TO_0000078', 'label': 'root dry weight',
                           'definition': 'The average dry weight of the root determined in a study.', 'parents': [], 'class_source': 'ext.owl'}]

        target_with_methods = {
            **target,
            'target_sentence_numbers': [1, 2],
            'sentences': [{'page': 1}, {'page': 1}],
            'methods_context': 'Concentrations were adjusted to 0.5 mM (HP) and 0.1 mM (LP) using KH2PO4.',
        }
        claims = [c1, c2, c3, c4, c5]
        cands = {'c1': c1_c, 'c2': c2_c, 'c3': c3_c, 'c4': c4_c, 'c5': c5_c}
        items = prepare_endpoint_verification_items(claims, cands, db, reference, targets_by_id={'t_trl': target_with_methods})
        items_by_id = {it['item_id']: it for it in items}

        captured_payloads = []

        class CaptureExactClient:
            def chat_json(self, system, user_msg, **options):
                parsed = json.loads(user_msg)
                captured_payloads.extend(parsed['items'])
                # Try C1 (exact) everywhere to exercise hard gates and exact->broader qualifier downgrades
                return {'decisions': [
                    {'item_id': it['item_id'], 'decision': 'exact', 'candidate_alias': 'C1',
                     'type_challenged': False, 'revised_type': None, 'justification': 'Testing specificity.'}
                    for it in parsed['items']
                ]}

        decs = verify_passage_endpoints(items, CaptureExactClient())
        by_pay = {it['item_id']: it for it in captured_payloads}
        assert by_pay['c1:object']['expanded_endpoint'] == 'Total root length'
        assert by_pay['c1:object']['abbreviation_expansions'] == {'TRL': 'Total root length'}
        assert by_pay['c2:subject']['expanded_endpoint'] == "Total root length of 'Cardoso'"
        assert by_pay['c2:object']['expanded_endpoint'] == 'total root length'

        out = {r['id']: r for r in apply_verified_decisions(claims, decs, items_by_id, db, reference)}
        # Unaveraged 'root dry mass' -> TO_0000078 (average dry weight) MUST be blocked!
        assert out['c1']['bindings']['subject']['iri'] is None
        assert 'averaging evidence' in out['c1']['bindings']['subject']['reason']
        # Explicitly averaged 'Average root dry mass' -> TO_0000078 is allowed as exact
        assert out['c4']['bindings']['subject']['iri'].endswith('TO_0000078')
        assert out['c4']['bindings']['subject']['mapping'] == 'exact'
        assert out['c4']['bindings']['subject']['downgraded_from_exact'] is False
        # HP/LP conditions and Inoculation with B. subtilis downgrade exact -> broader
        assert out['c4']['bindings']['object']['iri'].endswith('PECO_0001006')
        assert out['c4']['bindings']['object']['mapping'] == 'broader'
        assert out['c4']['bindings']['object']['downgraded_from_exact'] is True
        assert out['c5']['bindings']['subject']['iri'].endswith('PECO_0001046')
        assert out['c5']['bindings']['subject']['mapping'] == 'broader'
        assert out['c5']['bindings']['subject']['downgraded_from_exact'] is True
        assert out['c5']['bindings']['object']['iri'].endswith('ECTO_3000026')
        assert out['c5']['bindings']['object']['mapping'] == 'broader'
        assert out['c5']['bindings']['object']['downgraded_from_exact'] is True
        # TO_0000227 (length of a single root PO:0009005) MUST be rejected for TRL / total root length!
        assert out['c1']['bindings']['object']['iri'] is None
        assert out['c2']['bindings']['subject']['iri'] is None
        assert out['c2']['bindings']['object']['iri'] is None
        # shallow and sparsely branched root system -> root system becomes broader
        assert out['c3']['bindings']['subject']['mapping'] == 'broader'
        assert out['c3']['bindings']['subject']['downgraded_from_exact'] is True

        # Mapping cumulative TRL / total root length to TO_0000985 (root system morphology trait) succeeds as broader!
        sys_decs = {
            **decs,
            'c1:object': {'item_id': 'c1:object', 'decision': 'exact', 'candidate_alias': 'C2',
                          'type_challenged': False, 'revised_type': None, 'justification': 'Root system trait.'},
        }
        out_sys = {r['id']: r for r in apply_verified_decisions(claims, sys_decs, items_by_id, db, reference)}
        assert out_sys['c1']['bindings']['object']['iri'].endswith('TO_0000985')
        assert out_sys['c1']['bindings']['object']['mapping'] == 'broader'

        # Explicitly verify an already-'broader' verifier decision is preserved as 'broader' (not flipped to 'exact')
        broader_decs = {
            **decs,
            'c4:subject': {'item_id': 'c4:subject', 'decision': 'broader', 'candidate_alias': 'C1',
                           'type_challenged': False, 'revised_type': None, 'justification': 'Already broader.'},
        }
        out_broader = {r['id']: r for r in apply_verified_decisions(claims, broader_decs, items_by_id, db, reference)}
        assert out_broader['c4']['bindings']['subject']['mapping'] == 'broader'
        assert out_broader['c4']['bindings']['subject']['downgraded_from_exact'] is False


def test_methods_context_extraction_and_selective_thinking():
    targets = [{
        'section_heading': 'Materials and methods',
        'sentences': [
            {'page': 1, 'section_heading': 'Introduction', 'text': 'Low P limits potato yield.'},
            {'page': 3, 'section_heading': 'Experimental setup',
             'text': 'P levels were adjusted with KH 2PO4 to 0.5 and 2 mg P L-1, labelled as P0.5 and P2.'},
        ],
    }]
    formatted, prov = extract_treatment_methods_context(targets)
    assert '[p. 3]' in formatted and 'KH 2PO4' in formatted
    assert len(prov) == 1 and prov[0]['page'] == 3

    # Selective thinking fires only for tightly coupled multi-percentage comparisons/transitions
    assert should_use_extraction_thinking(
        '4 target: P uptake of the small rooting type was in LP on average 19.5% and 23.7% less than that of intermediate and large ones, respectively.'
    ) is True
    assert should_use_extraction_thinking(
        '6 target: An optimal P supply not only affects the general growth of potato plants but also improves the quality of both tubers and starch.'
    ) is False


def test_experimental_relation_fallback_respects_evidence_types_and_controls(tmp_path):
    reference = catalog()
    pato_conc_iri = 'http://purl.obolibrary.org/obo/PATO_0000033'
    to_uptake_iri = 'http://purl.obolibrary.org/obo/TO_0000511'
    peco_supp_iri = 'http://purl.obolibrary.org/obo/PECO_0007397'
    go_growth_iri = 'http://purl.obolibrary.org/obo/GO_0040007'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000015"><rdfs:label>process</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000020"><rdfs:label>specifically dependent continuant</rdfs:label></owl:Class>
         <owl:Class rdf:about="{pato_conc_iri}"><rdfs:label>concentration of</rdfs:label><rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/></owl:Class>
         <owl:Class rdf:about="{to_uptake_iri}"><rdfs:label>phosphorus uptake</rdfs:label><rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/></owl:Class>
         <owl:Class rdf:about="{peco_supp_iri}"><rdfs:label>phosphorus nutrient exposure</rdfs:label><rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000015"/></owl:Class>
         <owl:Class rdf:about="{go_growth_iri}"><rdfs:label>growth</rdfs:label><rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000015"/></owl:Class>
         </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'ext_rel.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'ext_rel': {'file': 'ext_rel.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)

    def _mk_claim(cid, subj, s_iri, rel, obj, o_iri, *, status='affirmed', review='not_flagged', clause='', r_quote=None):
        return {
            'id': cid,
            'target_id': 't1',
            'subject': subj,
            'relation': rel,
            'object': obj,
            'qualifiers': {},
            'assertion_status': status,
            'source_evidence_span': clause,
            'local_clause': clause,
            'anchors': {
                'subject': {'sentence_id': 1, 'quote': subj, 'resolved_span': subj},
                'relation': {'sentence_id': 1, 'quote': r_quote or rel, 'resolved_span': r_quote or rel},
                'object': {'sentence_id': 1, 'quote': obj, 'resolved_span': obj},
            },
            'review': review,
            'hold_reasons': [{'code': 'hold'}] if review == 'held' else [],
            'bindings': {
                'subject': {'iri': s_iri, 'label': subj, 'mapping': 'broader' if s_iri else 'unresolved'},
                'relation': {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'none'},
                'object': {'iri': o_iri, 'label': obj, 'mapping': 'broader' if o_iri else 'unresolved'},
            },
            'classified': False,
        }

    with closing(db):
        c_cont = _mk_claim('c_cont', 'concentration of P', pato_conc_iri, 'affected', 'P uptake', to_uptake_iri,
                           clause='The concentration of P in the nutrient solution affected P uptake.')
        c_proc = _mk_claim('c_proc', 'optimal P supply', peco_supp_iri, 'affects', 'growth', go_growth_iri,
                           clause='Optimal P supply affects growth.')
        c_obs = _mk_claim('c_obs', 'root length', to_uptake_iri, 'decreased under', 'LP conditions', peco_supp_iri,
                          clause='root length decreased under LP conditions.')
        c_cmp = _mk_claim('c_cmp', 'P uptake', to_uptake_iri, 'was less than', 'intermediate', peco_supp_iri,
                          clause='P uptake was less than intermediate.')
        c_hedged = _mk_claim('c_hedged', 'P limitation', peco_supp_iri, 'can reduce', 'concentration', pato_conc_iri,
                             status='uncertain', clause='P limitation can reduce concentration.')
        c_nonsig = _mk_claim('c_nonsig', 'P shortage', peco_supp_iri, 'did not change significantly', 'ratio', pato_conc_iri,
                             status='non_significant', clause='P shortage did not change significantly ratio.')
        c_unres = _mk_claim('c_unres', 'P deficiency', None, 'affected', 'growth', go_growth_iri,
                            clause='P deficiency affected growth.')

        claims = [c_cont, c_proc, c_obs, c_cmp, c_hedged, c_nonsig, c_unres]
        items = prepare_relation_verification_items(claims, db, reference)
        assert [it['claim_id'] for it in items] == ['c_cont', 'c_proc']

        # Identify R22 (RO_0002566) and R17 (RO_0002410) from captured payload
        captured = {}

        class FallbackClient:
            def chat_json(self, system, user_msg, **options):
                payload = json.loads(user_msg)
                captured['payload'] = payload
                rels = payload['relations']
                r_caus_inf = next(k for k, v in rels.items() if v['label'] == 'causally influences')
                r_caus_rel = next(k for k, v in rels.items() if v['label'] == 'causally related to')
                out = []
                for ck, cdata in payload['claims'].items():
                    if cdata['claim_id'] == 'c_cont':
                        out.append({'claim_id': ck, 'mapping': 'exact', 'term': r_caus_inf,
                                    'reason': 'Experimental P concentration causally affected P uptake.'})
                    else:
                        out.append({'claim_id': ck, 'mapping': 'broader', 'term': r_caus_rel,
                                    'reason': 'Generic causal effect between processes.'})
                return {'results': out}

        decs = verify_unresolved_relations(items, FallbackClient(), reference)
        items_by_id = {it['claim_id']: it for it in items}
        res = {r['id']: r for r in apply_relation_verification_decisions(claims, decs, items_by_id, db, reference)}

        # c_cont -> RO_0002566 (downgraded exact -> broader for neutral 'affected', direct_use_allowed=True)
        assert res['c_cont']['bindings']['relation']['iri'].endswith('RO_0002566')
        assert res['c_cont']['bindings']['relation']['mapping'] == 'broader'
        assert res['c_cont']['bindings']['relation']['provenance']['direct_use_allowed'] is True
        assert res['c_cont']['bindings']['relation']['downgraded_from_exact'] is True
        # c_proc -> RO_0002410 (grouping_only=True, direct_use_allowed=False)
        assert res['c_proc']['bindings']['relation']['iri'].endswith('RO_0002410')
        assert res['c_proc']['bindings']['relation']['mapping'] == 'broader'
        assert res['c_proc']['bindings']['relation']['provenance']['grouping_only'] is True

        # Adversarial guard check: force RO_0002566 onto process->process (c_proc) and RO_0002410 onto observational (c_obs) -> BOTH MUST BLOCK!
        bad_decs = {
            'c_proc': {'iri': 'http://purl.obolibrary.org/obo/RO_0002566', 'label': 'causally influences', 'mapping': 'broader', 'reason': 'Bad type.'},
            'c_obs': {'iri': 'http://purl.obolibrary.org/obo/RO_0002410', 'label': 'causally related to', 'mapping': 'broader', 'reason': 'Bad obs.'},
        }
        adv = {r['id']: r for r in apply_relation_verification_decisions(
            [c_proc, c_obs], bad_decs,
            {'c_proc': items_by_id['c_proc'], 'c_obs': {'claim_id': 'c_obs'}},
            db, reference,
        )}
        assert adv['c_proc']['bindings']['relation']['iri'] is None
        assert adv['c_obs']['bindings']['relation']['iri'] is None


def test_signed_property_polarity_gate_blocks_wrong_direction(tmp_path):
    from backend.ontology.catalog import Catalog

    pilot_ont = tmp_path / 'pilot_ont'
    import shutil
    base_cat = catalog()
    shutil.copytree(base_cat.directory, pilot_ont)
    ttl = (
        '@prefix obo: <http://purl.obolibrary.org/obo/> .\n'
        '@prefix owl: <http://www.w3.org/2002/07/owl#> .\n'
        '@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n'
        'obo:RO_0019000 a owl:ObjectProperty ; rdfs:label "regulates characteristic" ; '
        'rdfs:subPropertyOf obo:RO_0002410 ; rdfs:domain obo:BFO_0000015 ; rdfs:range obo:PATO_0000001 .\n'
        'obo:RO_0019001 a owl:ObjectProperty ; rdfs:label "positively regulates characteristic" ; '
        'rdfs:subPropertyOf obo:RO_0019000 .\n'
        'obo:RO_0019002 a owl:ObjectProperty ; rdfs:label "negatively regulates characteristic" ; '
        'rdfs:subPropertyOf obo:RO_0019000 .\n'
    ).encode('utf-8')
    (pilot_ont / 'fragments' / 'ro_char_test.ttl').write_bytes(ttl)
    manifest = json.loads((pilot_ont / 'manifest.json').read_text(encoding='utf-8'))
    manifest['modules'].append({
        'ontology': 'RO', 'file': 'fragments/ro_char_test.ttl', 'format': 'turtle',
        'source': 'https://raw.githubusercontent.com/oborel/obo-relations/master/src/ontology/ro-edit.owl',
        'sha256': sha256(ttl).hexdigest(),
        'term_iris': [
            'http://purl.obolibrary.org/obo/RO_0019000',
            'http://purl.obolibrary.org/obo/RO_0019001',
            'http://purl.obolibrary.org/obo/RO_0019002',
        ],
    })
    (pilot_ont / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    profile = json.loads((pilot_ont / 'profile.json').read_text(encoding='utf-8'))
    profile['allowed_object_properties'].extend([
        'http://purl.obolibrary.org/obo/RO_0019001',
        'http://purl.obolibrary.org/obo/RO_0019002',
    ])
    (pilot_ont / 'profile.json').write_text(json.dumps(profile), encoding='utf-8')
    pilot_ref = Catalog(pilot_ont)

    idx_dir = tmp_path / 'idx'
    idx_dir.mkdir()
    ecto_iri = 'http://purl.obolibrary.org/obo/ECTO_3000026'
    to_shoot_iri = 'http://purl.obolibrary.org/obo/TO_0001024'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000015"><rdfs:label>process</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PATO_0000001"><rdfs:label>characteristic</rdfs:label></owl:Class>
         <owl:Class rdf:about="{ecto_iri}"><rdfs:label>exposure to Bacillus subtilis</rdfs:label><rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000015"/></owl:Class>
         <owl:Class rdf:about="{to_shoot_iri}"><rdfs:label>phosphorus content</rdfs:label><rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/PATO_0000001"/></owl:Class>
         </rdf:RDF>""".encode('utf-8')
    (idx_dir / 'ro_char_ep.owl').write_bytes(owl)
    (idx_dir / 'inventory.json').write_text(json.dumps({
        'ep': {'file': 'ro_char_ep.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(idx_dir)

    ev = ('Inoculation with B. subtilis improved P acquisition efficiency by 10% under low phosphorus levels (P0.5 and P2), '
          'particularly in Lady Claire, where shoot and root phosphorus contents increased by 13-25% and 4-13%, respectively.')
    claim_c3 = {
        'id': 'c3', 'target_id': 't1',
        'subject': 'Inoculation with B. subtilis', 'relation': 'increased', 'object': 'shoot phosphorus contents',
        'qualifiers': {'population': 'Lady Claire', 'condition': 'under low phosphorus levels (P0.5 and P2)', 'metric': '13-25%'},
        'assertion_status': 'affirmed', 'source_evidence_span': ev, 'local_clause': ev,
        'anchors': {
            'subject': {'sentence_id': 1, 'quote': 'Inoculation with B. subtilis', 'resolved_span': 'Inoculation with B. subtilis'},
            'relation': {'sentence_id': 1, 'quote': 'increased', 'resolved_span': 'increased'},
            'object': {'sentence_id': 1, 'quote': 'shoot phosphorus contents', 'resolved_span': 'shoot phosphorus contents'},
        },
        'review': 'not_flagged', 'hold_reasons': [],
        'bindings': {
            'subject': {'iri': ecto_iri, 'label': 'exposure to Bacillus subtilis', 'mapping': 'exact'},
            'relation': {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'none'},
            'object': {'iri': to_shoot_iri, 'label': 'phosphorus content', 'mapping': 'broader'},
        },
        'classified': False,
    }
    claim_neutral = {**claim_c3, 'id': 'c_neutral', 'relation': 'affects',
                     'anchors': {**claim_c3['anchors'], 'relation': {'sentence_id': 1, 'quote': 'affects', 'resolved_span': 'affects'}}}

    with closing(db):
        # 1) Forcing WRONG negative property (RO_0019002) onto "increased" MUST be blocked!
        bad_neg = {'c3': {'iri': 'http://purl.obolibrary.org/obo/RO_0019002', 'label': 'negatively regulates characteristic',
                          'mapping': 'exact', 'reason': 'Forced wrong negative polarity.'}}
        out_neg = apply_relation_verification_decisions([claim_c3], bad_neg, {'c3': {'claim_id': 'c3'}}, db, pilot_ref)
        assert out_neg[0]['bindings']['relation']['iri'] is None
        assert 'polarity' in out_neg[0]['bindings']['relation']['reason'].casefold()

        # 2) Forcing signed RO_0019001 onto neutral "affects" MUST be blocked!
        bad_neutral = {'c_neutral': {'iri': 'http://purl.obolibrary.org/obo/RO_0019001', 'label': 'positively regulates characteristic',
                                     'mapping': 'exact', 'reason': 'Forced positive onto neutral affects.'}}
        out_neu = apply_relation_verification_decisions([claim_neutral], bad_neutral, {'c_neutral': {'claim_id': 'c_neutral'}}, db, pilot_ref)
        assert out_neu[0]['bindings']['relation']['iri'] is None
        assert 'polarity' in out_neu[0]['bindings']['relation']['reason'].casefold()

        # 3) Matching positive property RO_0019001 onto "increased" passes with compatible BFO types!
        ok_pos = {'c3': {'iri': 'http://purl.obolibrary.org/obo/RO_0019001', 'label': 'positively regulates characteristic',
                         'mapping': 'exact', 'reason': 'Process increased shoot phosphorus content trait.'}}
        out_ok = apply_relation_verification_decisions([claim_c3], ok_pos, {'c3': {'claim_id': 'c3'}}, db, pilot_ref)
        assert out_ok[0]['bindings']['relation']['iri'].endswith('RO_0019001')
        assert out_ok[0]['bindings']['relation']['mapping'] == 'exact'
        assert out_ok[0]['classified'] is True

    # Already-promoted properties reuse the active catalog without a pilot copy.
    exp_24 = experimental_relation_catalog(base_cat, tmp_path / 'exp_24_cat')
    assert len(base_cat.profile['allowed_object_properties']) == 102
    assert len(exp_24.profile['allowed_object_properties']) == 102
    assert exp_24 is base_cat


def test_passive_causal_reversal_efficiency_collapse_and_bundled_trait_gates(tmp_path):
    reference = catalog()
    to_uptake_iri = 'http://purl.obolibrary.org/obo/TO_0000511'
    to_morph_iri = 'http://purl.obolibrary.org/obo/TO_0000985'
    to_root_morph_iri = 'http://purl.obolibrary.org/obo/TO_0000043'
    ecto_iri = 'http://purl.obolibrary.org/obo/ECTO_3000026'
    bfo_proc = 'http://purl.obolibrary.org/obo/BFO_0000015'
    bfo_sdc = 'http://purl.obolibrary.org/obo/BFO_0000020'
    pato_char = 'http://purl.obolibrary.org/obo/PATO_0000001'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
         <owl:Class rdf:about="{bfo_proc}"><rdfs:label>process</rdfs:label></owl:Class>
         <owl:Class rdf:about="{bfo_sdc}"><rdfs:label>specifically dependent continuant</rdfs:label></owl:Class>
         <owl:Class rdf:about="{pato_char}"><rdfs:label>characteristic</rdfs:label><rdfs:subClassOf rdf:resource="{bfo_sdc}"/></owl:Class>
         <owl:Class rdf:about="{to_uptake_iri}"><rdfs:label>phosphorus uptake</rdfs:label><rdfs:subClassOf rdf:resource="{pato_char}"/></owl:Class>
         <owl:Class rdf:about="{to_morph_iri}"><rdfs:label>root system morphology trait</rdfs:label><rdfs:subClassOf rdf:resource="{pato_char}"/></owl:Class>
         <owl:Class rdf:about="{to_root_morph_iri}"><rdfs:label>root morphology trait</rdfs:label><rdfs:subClassOf rdf:resource="{pato_char}"/></owl:Class>
         <owl:Class rdf:about="{ecto_iri}"><rdfs:label>exposure to Bacillus subtilis</rdfs:label><rdfs:subClassOf rdf:resource="{bfo_proc}"/></owl:Class>
         </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'gates.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'gates': {'file': 'gates.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)

    pupe_target = {
        'target_id': 't_pupe',
        'target_sentence_numbers': [1, 2, 3],
        'sentences': [
            {'page': 1, 'text': 'Enhancements in plant PUpE are shaped by alterations in root morphology.'},
            {'page': 1, 'text': 'Total root length and root surface area correlated with P uptake.'},
            {'page': 1, 'text': 'Inoculation with B. subtilis enhanced root morphology under P deficiency.'},
        ],
        'source_abbreviation_definitions': [
            {'abbreviation': 'PUpE', 'expansion': 'P uptake efficiency', 'quote': 'P uptake efficiency (PUpE)', 'document': 'd1'},
            {'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'd1'},
        ],
    }

    c_pupe = _claim(1, 'Enhancements in plant PUpE', 'trait or measurement', 'are shaped by', 'alterations in root morphology', 'trait or measurement')
    c_bundle = _claim(2, 'Total root length and root surface area', 'trait or measurement', 'correlated with', 'phosphorus uptake', 'trait or measurement')
    c_morph = _claim(3, 'Inoculation with B. subtilis', 'condition or exposure', 'enhanced', 'root morphology', 'trait or measurement')

    with closing(db):
        g_pupe, g_pupe_c = ground_extracted(c_pupe, pupe_target, {i: s['text'] for i, s in enumerate(pupe_target['sentences'], 1)}, reference, db)
        g_pupe['id'] = 'g_pupe'
        g_pupe_c['subject'] = [{'iri': to_uptake_iri, 'label': 'phosphorus uptake', 'definition': 'phosphorus ion uptake', 'parents': [], 'class_source': 'to.owl'}]
        g_pupe_c['object'] = [{'iri': to_morph_iri, 'label': 'root system morphology trait', 'definition': '', 'parents': [], 'class_source': 'to.owl'}]

        g_bundle, g_bundle_c = ground_extracted({**c_bundle, 'object': 'P uptake', 'object_anchor': {'sentence_id': 2, 'quote': 'P uptake'}},
                                                pupe_target, {i: s['text'] for i, s in enumerate(pupe_target['sentences'], 1)}, reference, db)
        g_bundle['id'] = 'g_bundle'
        g_bundle_c['subject'] = [{'iri': to_morph_iri, 'label': 'root system morphology trait', 'definition': '', 'parents': [], 'class_source': 'to.owl'}]
        g_bundle_c['object'] = [{'iri': to_uptake_iri, 'label': 'phosphorus uptake', 'definition': '', 'parents': [], 'class_source': 'to.owl'}]

        # 1) Efficiency hard gate blocks PUpE -> phosphorus uptake; bundled gate blocks Total root length and root surface area!
        items = prepare_endpoint_verification_items([g_pupe, g_bundle], {'g_pupe': g_pupe_c, 'g_bundle': g_bundle_c}, db, reference, targets_by_id={'t_pupe': pupe_target})
        items_by_id = {it['item_id']: it for it in items}
        decs = {
            'g_pupe:subject': {'item_id': 'g_pupe:subject', 'decision': 'broader', 'candidate_alias': 'C1',
                               'type_challenged': False, 'revised_type': None, 'justification': 'Bad efficiency collapse.'},
            'g_pupe:object': {'item_id': 'g_pupe:object', 'decision': 'broader', 'candidate_alias': 'C1',
                              'type_challenged': False, 'revised_type': None, 'justification': 'Morphology trait.'},
            'g_bundle:subject': {'item_id': 'g_bundle:subject', 'decision': 'broader', 'candidate_alias': 'C1',
                                 'type_challenged': False, 'revised_type': None, 'justification': 'Bad bundled collapse.'},
        }
        ep_out = {r['id']: r for r in apply_verified_decisions([g_pupe, g_bundle], decs, items_by_id, db, reference)}
        assert ep_out['g_pupe']['bindings']['subject']['iri'] is None
        assert 'efficiency' in ep_out['g_pupe']['bindings']['subject']['reason'].casefold()
        assert ep_out['g_bundle']['bindings']['subject']['iri'] is None
        assert 'bundled' in ep_out['g_bundle']['bindings']['subject']['reason'].casefold()

        # 2) Passive relation gate: even if BOTH endpoints are resolved, forcing forward RO:0002566 onto 'are shaped by' is BLOCKED,
        #    while inverse RO:0002559 (causally influenced by) passes; and active 'shaped' blocks RO:0002559!
        g_passive_resolved = {
            **g_pupe, 'id': 'g_passive_res',
            'bindings': {
                'subject': {'iri': to_uptake_iri, 'label': 'phosphorus uptake', 'mapping': 'broader'},
                'relation': {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'none'},
                'object': {'iri': to_morph_iri, 'label': 'root system morphology trait', 'mapping': 'broader'},
            },
        }
        rel_items = prepare_relation_verification_items([g_passive_resolved], db, reference)
        assert [it['claim_id'] for it in rel_items] == ['g_passive_res']
        bad_fwd = apply_relation_verification_decisions(
            [g_passive_resolved],
            {'g_passive_res': {'iri': 'http://purl.obolibrary.org/obo/RO_0002566', 'label': 'causally influences',
                               'mapping': 'broader', 'reason': 'Wrong forward direction on passive are shaped by.'}},
            {it['claim_id']: it for it in rel_items}, db, reference,
        )
        assert bad_fwd[0]['bindings']['relation']['iri'] is None
        assert 'passive' in bad_fwd[0]['bindings']['relation']['reason'].casefold()

        ok_inv = apply_relation_verification_decisions(
            [g_passive_resolved],
            {'g_passive_res': {'iri': 'http://purl.obolibrary.org/obo/RO_0002559', 'label': 'causally influenced by',
                               'mapping': 'broader', 'reason': 'Passive are shaped by takes inverse causally influenced by.'}},
            {it['claim_id']: it for it in rel_items}, db, reference,
        )
        assert ok_inv[0]['bindings']['relation']['iri'].endswith('RO_0002559')

        # 3) Non-scalar morphology gate: 'enhanced root morphology' cannot take RO:0019001!
        exp_24 = experimental_relation_catalog(reference, tmp_path / 'exp_24_cat2')
        g_morph_res = {
            **c_morph, 'id': 'g_morph_res', 'target_id': 't_pupe',
            'assertion_status': 'affirmed', 'review': 'not_flagged', 'hold_reasons': [],
            'source_evidence_span': pupe_target['sentences'][2]['text'],
            'local_clause': pupe_target['sentences'][2]['text'],
            'anchors': {
                'subject': {'sentence_id': 3, 'quote': 'Inoculation with B. subtilis', 'resolved_span': 'Inoculation with B. subtilis'},
                'relation': {'sentence_id': 3, 'quote': 'enhanced', 'resolved_span': 'enhanced'},
                'object': {'sentence_id': 3, 'quote': 'root morphology', 'resolved_span': 'root morphology'},
            },
            'bindings': {
                'subject': {'iri': ecto_iri, 'label': 'exposure to Bacillus subtilis', 'mapping': 'broader'},
                'relation': {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'none'},
                'object': {'iri': to_root_morph_iri, 'label': 'root morphology trait', 'mapping': 'exact'},
            },
        }
        morph_items = prepare_relation_verification_items([g_morph_res], db, exp_24)
        bad_morph = apply_relation_verification_decisions(
            [g_morph_res],
            {'g_morph_res': {'iri': 'http://purl.obolibrary.org/obo/RO_0019001', 'label': 'positively regulates characteristic',
                             'mapping': 'exact', 'reason': 'Unquantified morphology enhancement.'}},
            {it['claim_id']: it for it in morph_items}, db, exp_24,
        )
        assert bad_morph[0]['bindings']['relation']['iri'] is None

        # 4) Shared broader class (TO:0000511) across distinct measurements ('specific P uptake' vs 'total P uptake')
        #    keeps distinct concept_keys and non-self-link RO:0002610, whereas identical-concept self-links are blocked.
        s_corr = 'In both cultivars, specific P uptake and root efficiency displayed a positive correlation with total P uptake.'
        corr_t = {
            'target_id': 't_corr', 'target_sentence_numbers': [1],
            'sentences': [{'page': 6, 'text': s_corr}],
            'source_abbreviation_definitions': [{'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'd1'}],
        }
        p_distinct = _claim(1, 'specific P uptake', 'trait or measurement', 'displayed a positive correlation with', 'total P uptake', 'trait or measurement')
        g_dist, g_dist_c = ground_extracted(p_distinct, corr_t, {1: s_corr}, reference, db)
        g_dist['id'] = 'g_dist'
        g_dist_c['subject'] = [{'iri': to_uptake_iri, 'label': 'phosphorus uptake', 'definition': '', 'parents': [], 'class_source': 'to.owl'}]
        g_dist_c['object'] = [{'iri': to_uptake_iri, 'label': 'phosphorus uptake', 'definition': '', 'parents': [], 'class_source': 'to.owl'}]
        dist_items = prepare_endpoint_verification_items([g_dist], {'g_dist': g_dist_c}, db, reference, targets_by_id={'t_corr': corr_t})
        dist_res = apply_verified_decisions(
            [g_dist],
            {
                'g_dist:subject': {'item_id': 'g_dist:subject', 'decision': 'broader', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Specific P uptake is narrower than phosphorus uptake.'},
                'g_dist:object': {'item_id': 'g_dist:object', 'decision': 'broader', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Total P uptake is broader under phosphorus uptake.'},
            },
            {it['item_id']: it for it in dist_items}, db, reference,
        )[0]
        assert dist_res['bindings']['subject']['iri'] == to_uptake_iri
        assert dist_res['bindings']['object']['iri'] == to_uptake_iri
        assert dist_res['bindings']['subject']['concept_key'] != dist_res['bindings']['object']['concept_key']
        assert dist_res['bindings']['subject']['shared_concept'] is False
        assert dist_res['bindings']['relation']['iri'].endswith('RO_0002610')
        assert dist_res['bindings']['relation']['provenance']['self_link'] is False
        assert dist_res['bindings']['relation']['provenance']['shared_broader_class_distinct_concepts'] is True
        assert dist_res['classified'] is True

        # Identical-concept self-link ('phosphorus uptake' exact vs 'P uptake' exact -> same exact TO:0000511) is blocked.
        s_self = 'phosphorus uptake correlated with P uptake.'
        self_t = {'target_id': 't_self', 'target_sentence_numbers': [1], 'sentences': [{'page': 1, 'text': s_self}],
                  'source_abbreviation_definitions': [{'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'd1'}]}
        p_self = _claim(1, 'phosphorus uptake', 'trait or measurement', 'correlated with', 'P uptake', 'trait or measurement')
        g_self, _ = ground_extracted(p_self, self_t, {1: s_self}, reference, db)
        assert g_self['bindings']['subject']['iri'] == to_uptake_iri and g_self['bindings']['subject']['mapping'] == 'exact'
        assert g_self['bindings']['object']['iri'] == to_uptake_iri and g_self['bindings']['object']['mapping'] == 'exact'
        assert g_self['bindings']['relation']['iri'] is None
        assert 'self-link' in g_self['bindings']['relation']['reason']
        assert g_self['classified'] is False

        assert 'morphology' in bad_morph[0]['bindings']['relation']['reason'].casefold()


def test_study_category_pato_size_guard_and_better_modifier_demotion(tmp_path):
    """PATO:0000586/587 represent high/low relative size (not change); quoted study categories must not collapse onto them, and 'better' demotes exact."""
    reference = catalog()
    pato_inc = 'http://purl.obolibrary.org/obo/PATO_0000586'
    pato_dec = 'http://purl.obolibrary.org/obo/PATO_0000587'
    to_uptake = 'http://purl.obolibrary.org/obo/TO_0000511'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
         <owl:Class rdf:about="{pato_inc}"><rdfs:label>increased size</rdfs:label></owl:Class>
         <owl:Class rdf:about="{pato_dec}"><rdfs:label>decreased size</rdfs:label></owl:Class>
         <owl:Class rdf:about="{to_uptake}"><rdfs:label>phosphorus uptake</rdfs:label></owl:Class>
         </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'pato_size.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'pato_size': {'file': 'pato_size.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)

    target = {
        'target_id': 't_size',
        'target_sentence_numbers': [1, 2, 3, 4],
        'sentences': [
            {'page': 9, 'text': '37.3% of all genotypes catego - rized as “small” in the HP treatment remained small in LP.'},
            {'page': 9, 'text': '15.2% of genotypes developed “large” RSAs under LP.'},
            {'page': 10, 'text': 'An increased RSA resulted in bet - ter P-uptake in potato.'},
            {'page': 10, 'text': 'Large root size was associated with deeper rooting.'},
        ],
        'source_abbreviation_definitions': [{'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'd1'}],
    }

    c_small = {
        **_claim(1, 'all genotypes catego - rized as “small” in the HP treatment', 'organism', 'remained', 'small in LP', 'trait or measurement'),
        'id': 'c_small', 'target_id': 't_size',
    }
    c_large = {
        **_claim(2, 'genotypes', 'organism', 'developed', '“large” RSAs', 'trait or measurement'),
        'id': 'c_large', 'target_id': 't_size',
    }
    c_better = {
        **_claim(3, 'RSA', 'trait or measurement', 'resulted in', 'bet - ter P-uptake', 'trait or measurement'),
        'id': 'c_better', 'target_id': 't_size',
    }
    c_gen = {
        **_claim(4, 'Large root size', 'trait or measurement', 'was associated with', 'deeper rooting', 'trait or measurement'),
        'id': 'c_gen', 'target_id': 't_size',
    }

    with closing(db):
        s_map = {i: s['text'] for i, s in enumerate(target['sentences'], 1)}
        rows, cands = [], {}
        for cid, prop in (('c_small', c_small), ('c_large', c_large), ('c_better', c_better), ('c_gen', c_gen)):
            row, ec = ground_extracted(prop, target, s_map, reference, db)
            row['id'] = cid
            rows.append(row)
            cands[cid] = ec

        cands['c_small']['object'] = [{'iri': pato_dec, 'label': 'decreased size', 'definition': 'A size quality which is relatively low.', 'parents': [], 'class_source': 'pato.owl'}]
        cands['c_large']['object'] = [{'iri': pato_inc, 'label': 'increased size', 'definition': 'A size quality which is relatively high.', 'parents': [], 'class_source': 'pato.owl'}]
        cands['c_better']['object'] = [{'iri': to_uptake, 'label': 'phosphorus uptake', 'definition': '', 'parents': [], 'class_source': 'to.owl'}]
        cands['c_gen']['subject'] = [{'iri': pato_inc, 'label': 'increased size', 'definition': 'A size quality which is relatively high.', 'parents': [], 'class_source': 'pato.owl'}]

        items = prepare_endpoint_verification_items(rows, cands, db, reference, targets_by_id={'t_size': target})
        decs = {
            'c_small:object': {'item_id': 'c_small:object', 'decision': 'broader', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Small category.'},
            'c_large:object': {'item_id': 'c_large:object', 'decision': 'broader', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Large RSA category.'},
            'c_better:object': {'item_id': 'c_better:object', 'decision': 'exact', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'P uptake.'},
            'c_gen:subject': {'item_id': 'c_gen:subject', 'decision': 'broader', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'High root size quality.'},
        }
        out = {r['id']: r for r in apply_verified_decisions(rows, decs, {it['item_id']: it for it in items}, db, reference)}

        assert out['c_small']['bindings']['object']['iri'] is None
        assert 'category' in out['c_small']['bindings']['object']['reason'].casefold()
        assert out['c_large']['bindings']['object']['iri'] is None
        assert 'category' in out['c_large']['bindings']['object']['reason'].casefold()
        assert out['c_better']['bindings']['object']['iri'] == to_uptake
        assert out['c_better']['bindings']['object']['mapping'] == 'broader'
        assert out['c_better']['bindings']['object']['downgraded_from_exact'] is True
        assert out['c_gen']['bindings']['subject']['iri'] == pato_inc


def test_organ_specificity_demotion_even_when_verifier_says_exact(tmp_path):
    from backend.evaluation.comparison_pilot import ground_trait_location_components

    to_p_content = 'http://purl.obolibrary.org/obo/TO_0001024'
    to_root_p = 'http://purl.obolibrary.org/obo/TO_0001124'
    po_shoot = 'http://purl.obolibrary.org/obo/PO_0009006'
    po_root = 'http://purl.obolibrary.org/obo/PO_0009005'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:obo="http://purl.obolibrary.org/obo/">
        <owl:Class rdf:about="{to_p_content}">
          <rdfs:label>phosphorus content</rdfs:label>
          <obo:IAO_0000115>A phosphorus content trait in a plant structure.</obo:IAO_0000115>
        </owl:Class>
        <owl:Class rdf:about="{to_root_p}">
          <rdfs:label>root phosphorus content</rdfs:label>
          <obo:IAO_0000115>The phosphorus content in a root.</obo:IAO_0000115>
        </owl:Class>
        <owl:Class rdf:about="{po_shoot}"><rdfs:label>shoot system</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_root}"><rdfs:label>root</rdfs:label></owl:Class>
        </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'organ.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'organ': {'file': 'organ.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)
    reference = catalog()
    target = {
        'target_id': 't_organ',
        'target_sentence_numbers': [1, 2],
        'target_page_start': 7,
        'target_page_end': 7,
        'sentences': [
            {'page': 7, 'text': 'B. subtilis inoculation increased P content in shoots by 13-25%.'},
            {'page': 7, 'text': 'B. subtilis inoculation increased root phosphorus contents by 4-13%.'},
        ],
        'source_abbreviation_definitions': [{'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'd1'}],
    }
    b6df5bc6 = {
        **_claim(1, 'B. subtilis inoculation', 'condition or exposure', 'increased', 'P content in shoots', 'trait or measurement'),
        'id': 'b6df5bc6', 'target_id': 't_organ',
    }
    c_root = {
        **_claim(2, 'B. subtilis inoculation', 'condition or exposure', 'increased', 'root phosphorus contents', 'trait or measurement'),
        'id': '00e4df1c', 'target_id': 't_organ',
    }

    with closing(db):
        s_map = {i: s['text'] for i, s in enumerate(target['sentences'], 1)}
        row_shoot, ec_shoot = ground_extracted(b6df5bc6, target, s_map, reference, db)
        row_shoot['id'] = 'b6df5bc6'
        row_root, ec_root = ground_extracted(c_root, target, s_map, reference, db)
        row_root['id'] = '00e4df1c'
        row_root['bindings']['object'] = {'iri': None, 'label': None, 'mapping': 'unresolved'}

        cands = {
            'b6df5bc6': {**ec_shoot, 'object': [{'iri': to_p_content, 'label': 'phosphorus content', 'definition': 'A phosphorus content trait in a plant structure.', 'parents': [], 'class_source': 'to.owl'}]},
            '00e4df1c': {**ec_root, 'object': [{'iri': to_root_p, 'label': 'root phosphorus content', 'definition': 'The phosphorus content in a root.', 'parents': [], 'class_source': 'to.owl'}]},
        }
        items = prepare_endpoint_verification_items([row_shoot, row_root], cands, db, reference, targets_by_id={'t_organ': target})
        decs = {
            'b6df5bc6:object': {'item_id': 'b6df5bc6:object', 'decision': 'exact', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Shoot is a plant structure.'},
            '00e4df1c:object': {'item_id': '00e4df1c:object', 'decision': 'exact', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Root phosphorus content exact.'},
        }
        out = {r['id']: r for r in apply_verified_decisions([row_shoot, row_root], decs, {it['item_id']: it for it in items}, db, reference)}

        assert out['b6df5bc6']['bindings']['object']['iri'] == to_p_content
        assert out['b6df5bc6']['bindings']['object']['mapping'] == 'broader'
        assert out['b6df5bc6']['bindings']['object']['downgraded_from_exact'] is True
        assert out['00e4df1c']['bindings']['object']['iri'] == to_root_p
        assert out['00e4df1c']['bindings']['object']['mapping'] == 'exact'

        shoot_comp = ground_trait_location_components('P content in shoots', out['b6df5bc6']['bindings']['object'], db)
        root_comp = ground_trait_location_components('root phosphorus contents', out['00e4df1c']['bindings']['object'], db)
        assert shoot_comp['status'] == 'grounded_components'
        assert shoot_comp['summary'] == 'phosphorus content grounded; shoot location grounded'
        assert shoot_comp['location']['iri'] == po_shoot
        assert shoot_comp['shared_concept'] is False
        assert root_comp['status'] == 'named_exact'
        assert root_comp['shared_concept'] is True


def test_concentration_content_hard_gate_and_dry_matter_broader_demotion(tmp_path):
    to_root_content = 'http://purl.obolibrary.org/obo/TO_0001124'
    to_p_conc = 'http://purl.obolibrary.org/obo/TO_0006045'
    co_shoot_dmc = 'https://cropontology.org/rdf/CO_330:0000700'
    cdno_sugar_conc = 'http://purl.obolibrary.org/obo/CDNO_0200034'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:obo="http://purl.obolibrary.org/obo/">
        <owl:Class rdf:about="{to_root_content}"><rdfs:label>root phosphorus content</rdfs:label></owl:Class>
        <owl:Class rdf:about="{to_p_conc}"><rdfs:label>phosphorus concentration</rdfs:label></owl:Class>
        <owl:Class rdf:about="{co_shoot_dmc}"><rdfs:label>Shoot system dry matter content</rdfs:label></owl:Class>
        <owl:Class rdf:about="{cdno_sugar_conc}"><rdfs:label>concentration of free sugar in material entity</rdfs:label></owl:Class>
        </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'qty.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'qty': {'file': 'qty.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)
    reference = catalog()
    defs = [
        {'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'd1'},
        {'abbreviation': 'DM', 'expansion': 'dry matter', 'quote': 'dry matter (DM)', 'document': 'd1'},
    ]
    target = {
        'target_id': 't_qty',
        'target_sentence_numbers': [1, 2, 3, 4, 5],
        'target_page_start': 5,
        'target_page_end': 5,
        'sentences': [
            {'page': 5, 'text': 'LP decreased root P concentration in roots.'},
            {'page': 5, 'text': 'P30 increased shoot P content.'},
            {'page': 5, 'text': 'LP altered sugar content in leaves.'},
            {'page': 5, 'text': 'LP reduced shoot DM.'},
            {'page': 5, 'text': 'LP reduced shoot dry matter content.'},
        ],
        'source_abbreviation_definitions': defs,
    }
    rows = [
        {**_claim(1, 'LP', 'condition or exposure', 'decreased', 'root P concentration', 'trait or measurement'), 'id': 'r_conc_to_cont', 'target_id': 't_qty'},
        {**_claim(2, 'P30', 'condition or exposure', 'increased', 'shoot P content', 'trait or measurement'), 'id': 'r_cont_to_conc', 'target_id': 't_qty'},
        {**_claim(3, 'LP', 'condition or exposure', 'altered', 'sugar content', 'food composition'), 'id': 'r_sugar_cont_to_conc', 'target_id': 't_qty'},
        {**_claim(1, 'LP', 'condition or exposure', 'decreased', 'root P concentration', 'trait or measurement'), 'id': 'r_conc_ok', 'target_id': 't_qty'},
        {**_claim(4, 'LP', 'condition or exposure', 'reduced', 'shoot DM', 'trait or measurement'), 'id': 'r_dm', 'target_id': 't_qty'},
        {**_claim(5, 'LP', 'condition or exposure', 'reduced', 'shoot dry matter content', 'trait or measurement'), 'id': 'r_dmc_exact', 'target_id': 't_qty'},
    ]
    cands_map = {
        'r_conc_to_cont': {'object': [{'iri': to_root_content, 'label': 'root phosphorus content', 'definition': '', 'parents': [], 'class_source': 'qty.owl'}]},
        'r_cont_to_conc': {'object': [{'iri': to_p_conc, 'label': 'phosphorus concentration', 'definition': '', 'parents': [], 'class_source': 'qty.owl'}]},
        'r_sugar_cont_to_conc': {'object': [{'iri': cdno_sugar_conc, 'label': 'concentration of free sugar in material entity', 'definition': '', 'parents': [], 'class_source': 'qty.owl'}]},
        'r_conc_ok': {'object': [{'iri': to_p_conc, 'label': 'phosphorus concentration', 'definition': '', 'parents': [], 'class_source': 'qty.owl'}]},
        'r_dm': {'object': [{'iri': co_shoot_dmc, 'label': 'Shoot system dry matter content', 'definition': '', 'parents': [], 'class_source': 'qty.owl'}]},
        'r_dmc_exact': {'object': [{'iri': co_shoot_dmc, 'label': 'Shoot system dry matter content', 'definition': '', 'parents': [], 'class_source': 'qty.owl'}]},
    }
    decs = {
        'r_conc_to_cont:object': {'item_id': 'r_conc_to_cont:object', 'decision': 'broader', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Root phosphorus trait.'},
        'r_cont_to_conc:object': {'item_id': 'r_cont_to_conc:object', 'decision': 'broader', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'P trait.'},
        'r_sugar_cont_to_conc:object': {'item_id': 'r_sugar_cont_to_conc:object', 'decision': 'broader', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Sugar trait.'},
        'r_conc_ok:object': {'item_id': 'r_conc_ok:object', 'decision': 'exact', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Phosphorus concentration.'},
        'r_dm:object': {'item_id': 'r_dm:object', 'decision': 'exact', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Shoot dry matter matches CO.'},
        'r_dmc_exact:object': {'item_id': 'r_dmc_exact:object', 'decision': 'exact', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Exact match.'},
    }
    with closing(db):
        s_map = {i: s['text'] for i, s in enumerate(target['sentences'], 1)}
        prepared, cands = [], {}
        for row in rows:
            r_out, r_cands = ground_extracted(row, target, s_map, reference, db)
            r_out['id'] = row['id']
            r_out['bindings']['object'] = {'iri': None, 'label': None, 'mapping': 'unresolved'}
            r_out['bindings']['subject'] = {'iri': 'http://purl.obolibrary.org/obo/PECO_0001046', 'label': 'limited phosphate exposure', 'mapping': 'broader'}
            prepared.append(r_out)
            cands[row['id']] = {**r_cands, **cands_map[row['id']]}
        items = prepare_endpoint_verification_items(prepared, cands, db, reference, targets_by_id={'t_qty': target})
        out = {r['id']: r for r in apply_verified_decisions(prepared, decs, {it['item_id']: it for it in items}, db, reference)}

        assert out['r_conc_to_cont']['bindings']['object']['iri'] is None
        assert 'concentration endpoint' in out['r_conc_to_cont']['bindings']['object']['reason'].casefold()
        assert out['r_cont_to_conc']['bindings']['object']['iri'] is None
        assert 'content endpoint' in out['r_cont_to_conc']['bindings']['object']['reason'].casefold()
        assert out['r_sugar_cont_to_conc']['bindings']['object']['iri'] is None
        assert 'content endpoint' in out['r_sugar_cont_to_conc']['bindings']['object']['reason'].casefold()
        assert out['r_conc_ok']['bindings']['object']['iri'] == to_p_conc
        assert out['r_conc_ok']['bindings']['object']['mapping'] == 'broader'
        assert out['r_dm']['bindings']['object']['iri'] == co_shoot_dmc
        assert out['r_dm']['bindings']['object']['mapping'] == 'broader'
        assert out['r_dm']['bindings']['object']['downgraded_from_exact'] is True
        assert out['r_dmc_exact']['bindings']['object']['mapping'] == 'exact'


def test_sugar_concentration_prefilter_methods_and_pato_fallback(tmp_path):
    pato_conc = 'http://purl.obolibrary.org/obo/PATO_0000033'
    pato_young = 'http://purl.obolibrary.org/obo/PATO_0000309'
    to_sugar_cont = 'http://purl.obolibrary.org/obo/TO_0000333'
    to_p_conc = 'http://purl.obolibrary.org/obo/TO_0006045'
    cdno_free = 'http://purl.obolibrary.org/obo/CDNO_0200034'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
        <owl:Class rdf:about="{pato_conc}"><rdfs:label>concentration of</rdfs:label></owl:Class>
        <owl:Class rdf:about="{pato_young}"><rdfs:label>young</rdfs:label></owl:Class>
        <owl:Class rdf:about="{to_sugar_cont}"><rdfs:label>sugar content</rdfs:label></owl:Class>
        <owl:Class rdf:about="{to_p_conc}"><rdfs:label>phosphorus concentration</rdfs:label></owl:Class>
        <owl:Class rdf:about="{cdno_free}"><rdfs:label>concentration of free sugar in material entity</rdfs:label></owl:Class>
        </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'sugar.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'sugar': {'file': 'sugar.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)
    reference = catalog()
    targets = [{
        'target_id': 't_sugar',
        'target_sentence_numbers': [3],
        'target_page_start': 7,
        'target_page_end': 7,
        'section_heading': 'Results',
        'sentences': [
            {'page': 3, 'section_heading': 'Experimental setup', 'text': 'P levels were adjusted with KH 2PO4 to P0.5 and P30.'},
            {'page': 3, 'section_heading': 'Phosphorus and sugar analyses in plant tissue',
             'text': 'The soluble sugar concentration within young leaves, old leaves, stem, and roots was determined according to Chea et al. [24].'},
            {'page': 7, 'section_heading': 'Results', 'text': 'At P30, sugar concentration in young leaves of Lady Claire surpassed Milva.'},
        ],
        'source_abbreviation_definitions': [],
    }]
    methods_ctx, prov = extract_treatment_methods_context(targets)
    assert 'KH 2PO4' in methods_ctx and 'soluble sugar concentration' in methods_ctx
    assert len(prov) == 2

    target = targets[0]
    target['methods_context'] = methods_ctx
    target['methods_provenance'] = prov
    with closing(db):
        s_map = {i: s['text'] for i, s in enumerate(target['sentences'], 1)}
        row_sugar, r_cands = ground_extracted(
            {**_claim(3, 'Lady Claire', 'organism', 'surpassed', 'sugar concentration in young leaves', 'trait or measurement'),
             'id': 'r_sugar', 'target_id': 't_sugar'},
            target, s_map, reference, db,
        )
        row_sugar['id'] = 'r_sugar'
        row_sugar['bindings']['subject'] = {'iri': 'http://purl.obolibrary.org/obo/NCBITaxon_4113', 'label': 'Solanum tuberosum', 'mapping': 'exact'}
        row_sugar['bindings']['object'] = {'iri': None, 'label': None, 'mapping': 'unresolved'}

        # Trap candidates ahead of PATO_0000033 MUST be pre-filtered, promoting PATO_0000033 to C1
        traps_and_pato = [
            {'iri': pato_young, 'label': 'young', 'class_source': 'sugar.owl'},
            {'iri': to_p_conc, 'label': 'phosphorus concentration', 'class_source': 'sugar.owl'},
            {'iri': to_sugar_cont, 'label': 'sugar content', 'class_source': 'sugar.owl'},
            {'iri': pato_conc, 'label': 'concentration of', 'class_source': 'sugar.owl'},
            {'iri': cdno_free, 'label': 'concentration of free sugar in material entity', 'class_source': 'sugar.owl'},
        ]
        items = prepare_endpoint_verification_items(
            [row_sugar],
            {'r_sugar': {**r_cands, 'object': traps_and_pato}},
            db, reference, targets_by_id={'t_sugar': target},
        )
        obj_item = next(it for it in items if it['item_id'] == 'r_sugar:object')
        assert [c['iri'] for c in obj_item['permitted_candidates']] == [pato_conc]
        assert [c['iri'] for c in obj_item['alternate_type_candidates']] == []

        # Choosing C1 with 'exact' MUST downgrade PATO_0000033 to 'broader'
        decs = {'r_sugar:object': {'item_id': 'r_sugar:object', 'decision': 'exact', 'candidate_alias': 'C1', 'type_challenged': False, 'revised_type': None, 'justification': 'Quantity component.'}}
        out = apply_verified_decisions([row_sugar], decs, {it['item_id']: it for it in items}, db, reference)
        assert out[0]['bindings']['object']['iri'] == pato_conc
        assert out[0]['bindings']['object']['mapping'] == 'broader'
        assert out[0]['bindings']['object']['downgraded_from_exact'] is True


def test_shoot_phosphorus_content_consistency_and_negative_control(tmp_path):
    from backend.evaluation.comparison_pilot import ground_trait_location_components
    from backend.evaluation.experimental_retrieval import (
        combined_endpoint_candidates,
        fts5_bm25_candidates,
        open_or_build_scoped_fts5,
    )

    to_shoot_p = 'http://purl.obolibrary.org/obo/TO_0020114'
    to_p_cont = 'http://purl.obolibrary.org/obo/TO_0001024'
    to_root_p = 'http://purl.obolibrary.org/obo/TO_0001124'
    to_tuber_p = 'http://purl.obolibrary.org/obo/TO_0003081'
    to_p_conc = 'http://purl.obolibrary.org/obo/TO_0006045'
    po_shoot = 'http://purl.obolibrary.org/obo/PO_0009006'
    po_root = 'http://purl.obolibrary.org/obo/PO_0009005'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:obo="http://purl.obolibrary.org/obo/">
        <owl:Class rdf:about="{to_shoot_p}">
          <rdfs:label>shoot system phosphorus content</rdfs:label>
          <obo:IAO_0000115>A phosphorus content trait (TO:0001024) which is the phosphorus cation (CHEBI:33467) composition in a shoot system (PO:0009006).</obo:IAO_0000115>
        </owl:Class>
        <owl:Class rdf:about="{to_p_cont}">
          <rdfs:label>phosphorus content</rdfs:label>
          <obo:IAO_0000115>A mineral and ion content trait in a plant structure.</obo:IAO_0000115>
        </owl:Class>
        <owl:Class rdf:about="{to_root_p}">
          <rdfs:label>root phosphorus content</rdfs:label>
          <obo:IAO_0000115>Phosphorus content in a root.</obo:IAO_0000115>
        </owl:Class>
        <owl:Class rdf:about="{to_tuber_p}">
          <rdfs:label>subterranean shoot axis tuber phosphorus content</rdfs:label>
          <obo:IAO_0000115>Phosphorus content in a tuber.</obo:IAO_0000115>
        </owl:Class>
        <owl:Class rdf:about="{to_p_conc}">
          <rdfs:label>phosphorus concentration</rdfs:label>
          <obo:IAO_0000115>Concentration of phosphorus in some plant structure.</obo:IAO_0000115>
        </owl:Class>
        <owl:Class rdf:about="{po_shoot}"><rdfs:label>shoot system</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_root}"><rdfs:label>root</rdfs:label></owl:Class>
        </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'to.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'to': {'file': 'to.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)
    reference = catalog()
    exp_ref = experimental_relation_catalog(reference, tmp_path / '_exp_cat')
    defs = [{'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'd1'}]
    target = {
        'target_id': 't_shoot',
        'target_sentence_numbers': [1, 2],
        'target_page_start': 5,
        'target_page_end': 5,
        'sentences': [
            {'page': 5, 'text': 'At P30, Lady Claire exhibited an increased P content in shoots and shoot phosphorus contents.'},
            {'page': 5, 'text': 'Meanwhile, phosphorus concentration in shoots did not differ significantly.'},
        ],
        'source_abbreviation_definitions': defs,
    }
    empty_fml = {'by_exact': {}, 'by_signature': {}}

    with closing(db):
        sconn = open_or_build_scoped_fts5(db, exp_ref, tmp_path / 'scoped.sqlite3')
        with closing(sconn):
            for phrase in ('shoot phosphorus contents', 'P content in shoots'):
                c1 = endpoint_candidates(sconn, exp_ref, phrase, [target['sentences'][0]['text']], defs, limit=10)
                c3 = fts5_bm25_candidates(sconn, phrase, 'trait or measurement', defs, limit=10)
                comb = combined_endpoint_candidates(
                    sconn, exp_ref, empty_fml, phrase, 'trait or measurement',
                    [target['sentences'][0]['text']], defs, limit=10,
                )
                assert any(c['iri'] == to_shoot_p for c in c1), (phrase, c1)
                assert any(c['iri'] == to_shoot_p for c in c3), (phrase, c3)
                assert any(c['iri'] == to_shoot_p for c in comb[:4]), (phrase, comb)

            # Negative control: phosphorus concentration in shoots MUST reject content classes & keep TO:0006045
            neg_cands = combined_endpoint_candidates(
                sconn, exp_ref, empty_fml, 'phosphorus concentration in shoots', 'trait or measurement',
                [target['sentences'][1]['text']], defs, limit=10,
            )
            s_map = {i: s['text'] for i, s in enumerate(target['sentences'], 1)}
            r_shoot, r_shoot_cands = ground_extracted(
                {**_claim(1, 'Lady Claire', 'organism', 'exhibited an increased', 'P content in shoots', 'trait or measurement'),
                 'id': 'r_shoot', 'target_id': 't_shoot'},
                target, s_map, exp_ref, db,
            )
            r_shoot['id'] = 'r_shoot'
            r_shoot['bindings']['subject'] = {'iri': 'http://purl.obolibrary.org/obo/NCBITaxon_4113', 'label': 'Solanum tuberosum', 'mapping': 'exact'}
            r_shoot['bindings']['object'] = {'iri': None, 'label': None, 'mapping': 'unresolved'}

            r_neg, _ = ground_extracted(
                {**_claim(2, 'Lady Claire', 'organism', 'did not differ significantly', 'phosphorus concentration in shoots', 'trait or measurement'),
                 'id': 'r_neg', 'target_id': 't_shoot',
                 'subject_anchor': {'sentence_id': 1, 'quote': 'Lady Claire'},
                 'relation_anchor': {'sentence_id': 2, 'quote': 'did not differ significantly'},
                 'object_anchor': {'sentence_id': 2, 'quote': 'phosphorus concentration in shoots'}},
                target, s_map, exp_ref, db,
            )
            r_neg['id'] = 'r_neg'
            r_neg['bindings']['subject'] = {'iri': 'http://purl.obolibrary.org/obo/NCBITaxon_4113', 'label': 'Solanum tuberosum', 'mapping': 'exact'}
            r_neg['bindings']['object'] = {'iri': None, 'label': None, 'mapping': 'unresolved'}

            shoot_cands = combined_endpoint_candidates(
                sconn, exp_ref, empty_fml, 'P content in shoots', 'trait or measurement',
                [target['sentences'][0]['text']], defs, limit=10,
            )
            items = prepare_endpoint_verification_items(
                [r_shoot, r_neg],
                {'r_shoot': {**r_shoot_cands, 'object': shoot_cands}, 'r_neg': {**r_shoot_cands, 'object': neg_cands}},
                db, exp_ref, targets_by_id={'t_shoot': target},
            )
            it_shoot = next(it for it in items if it['item_id'] == 'r_shoot:object')
            it_neg = next(it for it in items if it['item_id'] == 'r_neg:object')
            # Tuber and root disjoint organs are filtered out from shoot item; TO_0020114 is content-blocked on negative control
            assert all(c['iri'] not in {to_tuber_p, to_root_p} for c in it_shoot['permitted_candidates'])
            assert any(c['iri'] == to_shoot_p for c in it_shoot['permitted_candidates'])
            assert all(c['iri'] not in {to_shoot_p, to_p_cont} for c in it_neg['permitted_candidates'])
            assert any(c['iri'] == to_p_conc for c in it_neg['permitted_candidates'])

            # Hard gate ALSO blocks TO_0020114 if forced onto negative control
            shoot_alias = next(c['alias'] for c in it_shoot['permitted_candidates'] if c['iri'] == to_shoot_p)
            it_neg_forced = {**it_neg, 'alias_map': {**it_neg['alias_map'], 'C_FORCE': {'iri': to_shoot_p, 'label': 'shoot system phosphorus content', 'namespace': 'TO', 'class_source': 'to.owl', 'type_role': 'permitted'}}}
            out = apply_verified_decisions(
                [r_shoot, r_neg],
                {
                    'r_shoot:object': {'item_id': 'r_shoot:object', 'decision': 'broader', 'candidate_alias': shoot_alias, 'type_challenged': False, 'revised_type': None, 'justification': 'Pre-composed shoot P content.'},
                    'r_neg:object': {'item_id': 'r_neg:object', 'decision': 'broader', 'candidate_alias': 'C_FORCE', 'type_challenged': False, 'revised_type': None, 'justification': 'Invalid shoot content on concentration.'},
                },
                {'r_shoot:object': it_shoot, 'r_neg:object': it_neg_forced},
                db, exp_ref,
            )
            by_id = {row['id']: row for row in out}
            assert by_id['r_shoot']['bindings']['object']['iri'] == to_shoot_p
            assert by_id['r_neg']['bindings']['object']['iri'] is None
            assert 'concentration endpoint' in by_id['r_neg']['bindings']['object']['reason'].casefold()

            # Unified structured representation for both 'shoot phosphorus contents' and 'P content in shoots'
            g1 = ground_trait_location_components('shoot phosphorus contents', {'iri': to_shoot_p, 'label': 'shoot system phosphorus content', 'mapping': 'broader'}, db)
            g2 = ground_trait_location_components('P content in shoots', by_id['r_shoot']['bindings']['object'], db)
            assert g1['trait']['iri'] == g2['trait']['iri'] == to_shoot_p
            assert g1['location']['iri'] == g2['location']['iri'] == po_shoot


