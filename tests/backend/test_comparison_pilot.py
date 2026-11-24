"""Unit tests for v4 MeasuredComparison projection, strict pairing validation, and Trait+Location components."""
from contextlib import closing
from hashlib import sha256
import json

from backend.evaluation.comparison_pilot import (
    assess_comparison_row,
    ground_trait_location_components,
    project_saved_comparisons_zero_llm,
    validate_comparison_row,
)
from backend.evaluation.ontology_lookup import open_lookup
from backend.ontology.catalog import catalog


def _mini_db(tmp_path):
    to_uptake = 'http://purl.obolibrary.org/obo/TO_0000511'
    to_p_content = 'http://purl.obolibrary.org/obo/TO_0001024'
    to_root_p = 'http://purl.obolibrary.org/obo/TO_0001124'
    po_root = 'http://purl.obolibrary.org/obo/PO_0009005'
    po_shoot = 'http://purl.obolibrary.org/obo/PO_0009006'
    peco_lp = 'http://purl.obolibrary.org/obo/PECO_0001046'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:obo="http://purl.obolibrary.org/obo/">
         <owl:Class rdf:about="{to_uptake}"><rdfs:label>phosphorus uptake</rdfs:label></owl:Class>
         <owl:Class rdf:about="{to_p_content}"><rdfs:label>phosphorus content</rdfs:label></owl:Class>
         <owl:Class rdf:about="{to_root_p}"><rdfs:label>root phosphorus content</rdfs:label></owl:Class>
         <owl:Class rdf:about="{po_root}"><rdfs:label>root</rdfs:label></owl:Class>
         <owl:Class rdf:about="{po_shoot}"><rdfs:label>shoot system</rdfs:label></owl:Class>
         <owl:Class rdf:about="{peco_lp}">
           <rdfs:label>limited phosphate exposure</rdfs:label>
           <obo:IAO_0000115>A phosphate exposure involving limited phosphate.</obo:IAO_0000115>
         </owl:Class>
         </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'pilot.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'pilot': {'file': 'pilot.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)
    return db


def test_zero_llm_case7_projection_and_swap_failure(tmp_path):
    db = _mini_db(tmp_path)
    ref = catalog()
    sent = 'P uptake of the small rooting type was in LP on average 19.5% and 23.7% less than that of the intermediate and large ones, respectively.'
    methods = 'P concentrations were adjusted to 0.5 mM (HP) and 0.1 mM (LP) using KH2PO4.'
    claims = [
        {
            'id': '002ca48f', 'target_id': 't7', 'source_page': 10,
            'subject': 'P uptake of the small rooting type',
            'relation': 'was less than',
            'object': 'that of the intermediate',
            'qualifiers': {'crop_or_species': 'potato', 'condition': 'in LP', 'comparator': 'intermediate', 'metric': 'on average 19.5%'},
            'anchors': {'object': {'sentence_id': 1, 'quote': 'that of the intermediate and large ones'}},
            'source_evidence_span': sent,
        },
        {
            'id': 'd553ebb7', 'target_id': 't7', 'source_page': 10,
            'subject': 'P uptake of the small rooting type',
            'relation': 'was less than',
            'object': 'that of the large ones',
            'qualifiers': {'crop_or_species': 'potato', 'condition': 'in LP', 'comparator': 'large ones', 'metric': 'on average 23.7%'},
            'anchors': {'object': {'sentence_id': 1, 'quote': 'that of the intermediate and large ones'}},
            'source_evidence_span': sent,
        },
    ]
    targets = {'t7': {'source_abbreviation_definitions': [{'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'd1'}]}}

    with closing(db):
        rows = project_saved_comparisons_zero_llm(claims, methods_context=methods, db=db, reference=ref, targets_by_id=targets)
        assert len(rows) == 2
        assert all(r['complete'] for r in rows)
        assert rows[0]['measured_trait']['iri'].endswith('TO_0000511')
        assert rows[0]['study_group'] == 'small rooting type'
        assert rows[0]['comparator_label'] == 'intermediate rooting type'
        assert rows[0]['reported_difference'] == '19.5% lower on average'
        assert rows[0]['condition']['iri'].endswith('PECO_0001046')

        assert rows[1]['comparator_label'] == 'large rooting type'
        assert rows[1]['reported_difference'] == '23.7% lower on average'

        # Without Methods KH2PO4 proof, PECO:0001046 must be withheld.
        no_methods = project_saved_comparisons_zero_llm(claims, methods_context='', db=db, reference=ref, targets_by_id=targets)
        assert all(r['condition']['iri'] is None for r in no_methods)

        # Swapping 19.5% and 23.7% MUST fail even when every ontology IRI is valid!
        swapped = {**rows[0], 'reported_difference': '23.7% lower on average'}
        ok, issues = validate_comparison_row(swapped, source_text=sent, methods_context=methods)
        assert ok is False and any('paired mismatch' in i.casefold() for i in issues)

        # Trait + Location regression check: root named exact vs. shoot component-grounded (no CHEBI form invented)
        root_comp = ground_trait_location_components(
            'root phosphorus contents',
            {'iri': 'http://purl.obolibrary.org/obo/TO_0001124', 'label': 'root phosphorus content', 'mapping': 'exact'},
            db,
        )
        assert root_comp['status'] == 'named_exact'
        assert root_comp['shared_concept'] is True

        shoot_comp = ground_trait_location_components(
            'shoot phosphorus contents',
            {'iri': 'http://purl.obolibrary.org/obo/TO_0001024', 'label': 'phosphorus content', 'mapping': 'broader'},
            db,
        )
        assert shoot_comp['status'] == 'grounded_components'
        assert shoot_comp['summary'] == 'phosphorus content grounded; shoot location grounded'
        assert shoot_comp['trait']['iri'].endswith('TO_0001024')
        assert shoot_comp['location']['iri'].endswith('PO_0009006')
        assert shoot_comp['chemical_form_specified'] is False
        assert shoot_comp['shared_concept'] is False

        # Direct organ check even if binding still says exact (b6df5bc6)
        forced_exact_shoot = ground_trait_location_components(
            'P content in shoots',
            {'iri': 'http://purl.obolibrary.org/obo/TO_0001024', 'label': 'phosphorus content', 'mapping': 'exact'},
            db,
        )
        assert forced_exact_shoot['status'] == 'grounded_components'
        assert forced_exact_shoot['trait']['mapping'] == 'broader'
        assert forced_exact_shoot['trait']['downgraded_from_exact'] is True
        assert forced_exact_shoot['location']['iri'].endswith('PO_0009006')
        assert forced_exact_shoot['shared_concept'] is False


def test_comparison_roles_and_full_sentence_validation():
    k_sent_79 = "However, ’Kuba’ strongly increased TRL in the LP treatment while TRL of ’Cardoso’ remained constant."
    base_trait = {'phrase': 'TRL', 'iri': 'http://purl.obolibrary.org/obo/TO_0000985', 'label': 'root system morphology trait', 'mapping': 'broader'}
    lp_cond = {'phrase': 'LP treatment', 'iri': 'http://purl.obolibrary.org/obo/PECO_0001046', 'label': 'limited phosphate exposure'}
    methods = '[p. 3] P concentrations were adjusted to 0.5 mM (HP) and 0.1 mM (LP) using KH2PO4.'

    # 1. Condition masquerading as comparator ('LP treatment') MUST fail
    bad_cond_as_comp = {
        'comparison_axis': 'condition',
        'measured_trait': base_trait,
        'study_group': "'Kuba'",
        'baseline_status': 'explicit',
        'comparator_label': 'LP treatment',
        'comparator_quote': 'in the LP treatment',
        'direction': 'higher',
        'reported_difference': 'strongly increased TRL',
        'condition': lp_cond,
        'source_sentence': k_sent_79,
    }
    ok1, issues1 = validate_comparison_row(bad_cond_as_comp, source_text=k_sent_79, methods_context=methods)
    assert ok1 is False and any('role error' in i.casefold() for i in issues1)

    # 2. Study group copied as comparator_quote ('Cardoso' instead of 'Kuba') MUST fail
    k_sent_78 = "Total root length (TRL) of the two cultivars ’Cardoso’ and ’Kuba’ was measured at the end of an experiment with a duration of seven weeks. ’Car - doso’ had a significantly higher TRL across different P-nutrition levels, which matches with the results in the HP treatment of the present study."
    bad_self_quote = {
        'comparison_axis': 'group',
        'measured_trait': base_trait,
        'study_group': "'Cardoso'",
        'baseline_status': 'explicit',
        'comparator_label': "'Kuba'",
        'comparator_quote': "'Cardoso'",
        'direction': 'higher',
        'reported_difference': 'significantly higher TRL',
        'condition': {'phrase': 'different P-nutrition levels', 'iri': None, 'label': None},
        'source_sentence': k_sent_78,
    }
    ok2, issues2 = validate_comparison_row(bad_self_quote, source_text=k_sent_78, methods_context=methods)
    assert ok2 is False and any('copies study_group' in i.casefold() for i in issues2)

    # 3. Valid unspecified-baseline finding MUST be preserved distinctly from explicit-baseline completeness
    valid_unspecified = {
        'comparison_axis': 'condition',
        'measured_trait': base_trait,
        'study_group': 'Cardoso',
        'baseline_status': 'unspecified',
        'comparator_label': None,
        'comparator_quote': None,
        'direction': 'unchanged',
        'reported_difference': 'remained constant',
        'condition': lp_cond,
        'source_sentence': k_sent_79,
    }
    ok3, issues3 = validate_comparison_row(valid_unspecified, source_text=k_sent_79, methods_context=methods)
    assert ok3 is True, issues3

    # 4. Positive control: Chea Milva vs Lady Claire > 60 chars (no false [:60] rejection)
    chea_sent = 'However, the IAA concentration in the roots of Milva was consistently higher than that in Lady Claire across all P treatments (Table 2).'
    positive_ctrl = {
        'comparison_axis': 'group',
        'measured_trait': {'phrase': 'IAA concentration in roots', 'iri': 'http://purl.obolibrary.org/obo/PATO_0000033', 'label': 'concentration of', 'mapping': 'broader'},
        'study_group': 'Milva',
        'baseline_status': 'explicit',
        'comparator_label': 'Lady Claire',
        'comparator_quote': 'that in Lady Claire',
        'direction': 'higher',
        'reported_difference': 'consistently higher',
        'condition': {'phrase': 'across all P treatments', 'iri': None, 'label': None},
        'source_sentence': chea_sent,
    }
    ok4, issues4 = validate_comparison_row(positive_ctrl, source_text=chea_sent, methods_context='')
    assert ok4 is True, issues4


def test_coordinated_organ_hold_atomic_s144_and_meaning_holds(tmp_path):
    to_conc = 'http://purl.obolibrary.org/obo/TO_0006045'
    to_cont = 'http://purl.obolibrary.org/obo/TO_0001024'
    to_root_cont = 'http://purl.obolibrary.org/obo/TO_0001124'
    to_rs_ratio = 'http://purl.obolibrary.org/obo/TO_0000278'
    po_leaf = 'http://purl.obolibrary.org/obo/PO_0025034'
    po_root = 'http://purl.obolibrary.org/obo/PO_0009005'
    po_shoot = 'http://purl.obolibrary.org/obo/PO_0009006'
    po_stem = 'http://purl.obolibrary.org/obo/PO_0009047'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
        <owl:Class rdf:about="{to_conc}"><rdfs:label>phosphorus concentration</rdfs:label></owl:Class>
        <owl:Class rdf:about="{to_cont}"><rdfs:label>phosphorus content</rdfs:label></owl:Class>
        <owl:Class rdf:about="{to_root_cont}"><rdfs:label>root phosphorus content</rdfs:label></owl:Class>
        <owl:Class rdf:about="{to_rs_ratio}"><rdfs:label>root to shoot ratio</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_leaf}"><rdfs:label>leaf</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_root}"><rdfs:label>root</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_shoot}"><rdfs:label>shoot system</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_stem}"><rdfs:label>stem</rdfs:label></owl:Class>
        </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'rich.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'rich': {'file': 'rich.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)

    with closing(db):
        # Bundled coordinated phrases MUST hold BEFORE component grounding
        for bundled in (
            'P concentration in old leaves and roots',
            'P content in shoots and roots',
            'young and old leaves P concentration',
            'sugar concentration and content',
        ):
            res = ground_trait_location_components(
                bundled,
                {'iri': to_conc, 'label': 'phosphorus concentration', 'mapping': 'broader'},
                db,
            )
            assert res['status'] == 'held_bundled_organs', bundled
            assert res['trait']['iri'] is None
            assert res['location'] is None

        # Ratio phrases and stems remain intact
        rs = ground_trait_location_components(
            'root-to-shoot ratio',
            {'iri': to_rs_ratio, 'label': 'root to shoot ratio', 'mapping': 'exact'},
            db,
        )
        assert rs['status'] == 'named_exact'

        stem_comp = ground_trait_location_components(
            'sugar concentration in stems',
            {'iri': to_conc, 'label': 'phosphorus concentration', 'mapping': 'broader'},
            db,
        )
        assert stem_comp['status'] == 'grounded_components'
        assert stem_comp['location']['iri'] == po_stem

        # Chea s.144 acceptance: 4 atomic rows preserving 'old' on leaves
        s144 = 'However, at P30, Lady Claire exhibited a higher P concentration in old leaves and roots and an increased P content in shoots and roots.'
        old_leaf_comp = ground_trait_location_components(
            'P concentration in old leaves',
            {'iri': to_conc, 'label': 'phosphorus concentration', 'mapping': 'exact'},
            db,
        )
        assert old_leaf_comp['status'] == 'grounded_components'
        assert old_leaf_comp['location']['iri'] == po_leaf
        assert old_leaf_comp['location']['qualifier'] == 'old'
        assert old_leaf_comp['location_qualifier'] == 'old'
        assert old_leaf_comp['trait']['mapping'] == 'broader'

        atomic_specs = [
            ('P concentration in old leaves', old_leaf_comp, 'higher', 'higher P concentration'),
            ('P concentration in roots', ground_trait_location_components('P concentration in roots', {'iri': to_conc, 'label': 'phosphorus concentration', 'mapping': 'broader'}, db), 'higher', 'higher P concentration'),
            ('P content in shoots', ground_trait_location_components('P content in shoots', {'iri': to_cont, 'label': 'phosphorus content', 'mapping': 'broader'}, db), 'higher', 'increased P content'),
            ('P content in roots', ground_trait_location_components('P content in roots', {'iri': to_root_cont, 'label': 'root phosphorus content', 'mapping': 'exact'}, db), 'higher', 'increased P content'),
        ]
        for phrase, comp, direction, diff in atomic_specs:
            row = {
                'comparison_axis': 'group',
                'measured_trait': {'phrase': phrase, **comp['trait'], 'component_status': comp['status'], 'location': comp['location']},
                'study_group': 'Lady Claire',
                'baseline_status': 'unspecified',
                'comparator_label': None,
                'comparator_quote': None,
                'direction': direction,
                'reported_difference': diff,
                'condition': {'phrase': 'at P30', 'iri': None, 'label': None},
                'source_sentence': s144,
            }
            assessed = assess_comparison_row(row, source_text=s144, methods_context='')
            assert assessed['complete'] is True, assessed
            assert assessed['complete_unspecified_baseline'] is True
            assert assessed['review'] == 'not_flagged'

        # Bundled row fails even if upstream tried to attach a trait IRI
        bundled_row = {
            'comparison_axis': 'group',
            'measured_trait': {'phrase': 'P concentration in old leaves and roots', 'iri': None, 'label': None, 'component_status': 'held_bundled_organs'},
            'study_group': 'Lady Claire',
            'baseline_status': 'unspecified',
            'comparator_label': None,
            'comparator_quote': None,
            'direction': 'higher',
            'reported_difference': 'higher P concentration',
            'condition': {'phrase': 'at P30', 'iri': None, 'label': None},
            'source_sentence': s144,
        }
        bundled_assessed = assess_comparison_row(bundled_row, source_text=s144, methods_context='')
        assert bundled_assessed['complete'] is False
        assert bundled_assessed['review'] == 'held'
        assert 'possible_bundled_outcomes' in bundled_assessed['hold_reasons']

        # Chea s.48: PAE 10% incomplete row STILL gets review='held' (lost_non_significance), while shoots/roots do NOT
        s48 = (
            'B. subtilis inoculation significantly increased P content in shoots by 13-25% and root phosphorus contents by 4-13% '
            'at P0.5 and P2 as well as PAE by 10% at P0.5, though this increase was not statistically significant '
            'Fur - thermore, B. subtilis inoculation increased the relative expression of StPHT1;1 and StPHT2;1 in the roots.'
        )
        pae_incomplete = {
            'comparison_axis': 'group',
            'measured_trait': {'phrase': 'PAE', 'iri': None, 'label': None},
            'study_group': 'B. subtilis inoculation',
            'baseline_status': 'unspecified',
            'comparator_label': None,
            'comparator_quote': None,
            'direction': 'higher',
            'reported_difference': '10% higher',
            'condition': {'phrase': 'at P0.5', 'iri': None, 'label': None},
            'source_sentence': s48,
        }
        pae_assessed = assess_comparison_row(pae_incomplete, source_text=s48, methods_context='')
        assert pae_assessed['complete'] is False
        assert pae_assessed['review'] == 'held'
        assert 'lost_non_significance' in pae_assessed['hold_reasons']

        shoot_ok = assess_comparison_row({
            'comparison_axis': 'group',
            'measured_trait': {'phrase': 'P content in shoots', 'iri': to_cont, 'label': 'phosphorus content'},
            'study_group': 'B. subtilis inoculation',
            'baseline_status': 'unspecified',
            'comparator_label': None,
            'comparator_quote': None,
            'direction': 'higher',
            'reported_difference': '13-25% higher',
            'condition': {'phrase': 'at P0.5 and P2', 'iri': None, 'label': None},
            'source_sentence': s48,
        }, source_text=s48, methods_context='')
        assert shoot_ok['review'] == 'not_flagged'
        assert shoot_ok['complete'] is True

        # Chea s.139: 'from 25 DAT to 42 DAT' with baseline_status='unspecified' MUST hold
        s139 = 'Therefore, potato plants tend to absorb more P with plant development from 25 DAT to 42 DAT.'
        s139_unspec = assess_comparison_row({
            'comparison_axis': 'condition',
            'measured_trait': {'phrase': 'P uptake', 'iri': to_cont, 'label': 'phosphorus content'},
            'study_group': 'potato plants',
            'baseline_status': 'unspecified',
            'comparator_label': None,
            'comparator_quote': None,
            'direction': 'higher',
            'reported_difference': 'more P',
            'condition': {'phrase': 'from 25 DAT to 42 DAT', 'iri': None, 'label': None},
            'source_sentence': s139,
        }, source_text=s139, methods_context='')
        assert s139_unspec['complete'] is False
        assert s139_unspec['review'] == 'held'
        assert 'missed_explicit_baseline' in s139_unspec['hold_reasons']

        s139_explicit = assess_comparison_row({
            'comparison_axis': 'condition',
            'measured_trait': {'phrase': 'P uptake', 'iri': to_cont, 'label': 'phosphorus content'},
            'study_group': 'potato plants at 42 DAT',
            'baseline_status': 'explicit',
            'comparator_label': '25 DAT',
            'comparator_quote': 'from 25 DAT to 42 DAT',
            'direction': 'higher',
            'reported_difference': 'more P absorbed',
            'condition': {'phrase': 'plant development', 'iri': None, 'label': None},
            'source_sentence': s139,
        }, source_text=s139, methods_context='')
        assert s139_explicit['complete_explicit_baseline'] is True
        assert s139_explicit['review'] == 'not_flagged'

        # Quantity mismatch hold even if passed pre-grounded
        qty_bad = assess_comparison_row({
            'comparison_axis': 'group',
            'measured_trait': {'phrase': 'root P concentration', 'iri': to_root_cont, 'label': 'root phosphorus content'},
            'study_group': 'Lady Claire',
            'baseline_status': 'unspecified',
            'comparator_label': None,
            'comparator_quote': None,
            'direction': 'higher',
            'reported_difference': 'higher P concentration',
            'condition': {'phrase': 'at P30', 'iri': None, 'label': None},
            'source_sentence': s144,
        }, source_text=s144, methods_context='')
        assert qty_bad['complete'] is False
        assert 'concentration_content_shift' in qty_bad['hold_reasons']

        # Leading 'While ..., ' subordinate negation MUST NOT false-hold the main clause
        s_while = 'While P levels did not significantly affect IAA concentrations in the roots of either cultivar, Milva exhibited greater variability in IAA levels across different P levels.'
        while_main = assess_comparison_row({
            'comparison_axis': 'group',
            'measured_trait': {'phrase': 'variability in IAA levels', 'iri': to_conc, 'label': 'phosphorus concentration'},
            'study_group': 'Milva',
            'baseline_status': 'unspecified',
            'comparator_label': None,
            'comparator_quote': None,
            'direction': 'higher',
            'reported_difference': 'greater variability',
            'condition': {'phrase': 'across different P levels', 'iri': None, 'label': None},
            'source_sentence': s_while,
        }, source_text=s_while, methods_context='')
        assert while_main['review'] == 'not_flagged'
        assert while_main['hold_reasons'] == []

        # Chea s.144 borrowing neighbor s.145 quote MUST fail validation, whereas unspecified passes
        s144_borrowed = assess_comparison_row({
            'comparison_axis': 'group',
            'measured_trait': {'phrase': 'P concentration in old leaves', **old_leaf_comp['trait'], 'component_status': old_leaf_comp['status'], 'location': old_leaf_comp['location']},
            'study_group': 'Lady Claire',
            'baseline_status': 'explicit',
            'comparator_label': 'Milva',
            'comparator_quote': 'than that in Milva',
            'direction': 'higher',
            'reported_difference': 'higher',
            'condition': {'phrase': 'P30', 'iri': None, 'label': None},
            'source_sentence': s144,
        }, source_text=s144, methods_context='')
        assert s144_borrowed['complete'] is False
        assert any('comparator_quote must appear verbatim' in iss for iss in s144_borrowed['validation_issues'])


def test_contradictory_significance_routing_and_sugar_components(tmp_path):
    from backend.evaluation.comparison_pilot import partition_comparisons_and_overall_effects

    pato_conc = 'http://purl.obolibrary.org/obo/PATO_0000033'
    to_conc = 'http://purl.obolibrary.org/obo/TO_0006045'
    po_leaf = 'http://purl.obolibrary.org/obo/PO_0025034'
    po_stem = 'http://purl.obolibrary.org/obo/PO_0009047'
    po_root = 'http://purl.obolibrary.org/obo/PO_0009005'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
        <owl:Class rdf:about="{pato_conc}"><rdfs:label>concentration of</rdfs:label></owl:Class>
        <owl:Class rdf:about="{to_conc}"><rdfs:label>phosphorus concentration</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_leaf}"><rdfs:label>leaf</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_stem}"><rdfs:label>stem</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_root}"><rdfs:label>root</rdfs:label></owl:Class>
        </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'sugar_pilot.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'sugar_pilot': {'file': 'sugar_pilot.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)

    chea_methods = (
        '[p. 3] The soluble sugar concentration within young leaves, old leaves, stem, and roots '
        'was determined according to the procedures detailed in Chea et al. [24].'
    )
    s146 = 'In this study, PAE, PUpE, PUtE, and PUE were sig - nificantly affected by P levels (Table 1).'
    contradictory = {
        'id': 'c_anova',
        'target_id': 't146',
        'source_page': 5,
        'comparison_axis': 'condition',
        'measured_trait': {'phrase': 'PAE', 'iri': None, 'label': None},
        'study_group': 'potato plants',
        'baseline_status': 'unspecified',
        'comparator_label': None,
        'comparator_quote': None,
        'direction': 'no_significant_difference',
        'statistical_significance': 'significant',
        'reported_difference': 'significantly affected by P levels',
        'condition': {'phrase': 'P levels', 'iri': None, 'label': None},
        'source_sentence': s146,
        'source_quote': s146,
    }
    pos_ns = {
        'id': 'pos_ns',
        'target_id': 't143',
        'source_page': 5,
        'comparison_axis': 'group',
        'measured_trait': {'phrase': 'P concentration', 'iri': to_conc, 'label': 'phosphorus concentration', 'mapping': 'broader'},
        'study_group': 'cultivars',
        'baseline_status': 'unspecified',
        'comparator_label': None,
        'comparator_quote': None,
        'direction': 'no_significant_difference',
        'statistical_significance': 'not_significant',
        'reported_difference': 'no significant difference',
        'condition': {'phrase': 'P0.5 and P2', 'iri': None, 'label': None},
        'source_sentence': 'There was no significant difference in P concentration among the cultivars under P0.5 and P2.',
        'source_quote': 'There was no significant difference in P concentration among the cultivars under P0.5 and P2.',
    }
    pos_inc_ns = {
        'id': 'pos_inc_ns',
        'target_id': 't48',
        'source_page': 7,
        'comparison_axis': 'group',
        'measured_trait': {'phrase': 'PAE', 'iri': None, 'label': None},
        'study_group': 'B. subtilis inoculation',
        'baseline_status': 'unspecified',
        'comparator_label': None,
        'comparator_quote': None,
        'direction': 'no_significant_difference',
        'statistical_significance': 'not_significant',
        'reported_difference': '10% increase, though this increase was not statistically significant',
        'condition': {'phrase': 'P0.5', 'iri': None, 'label': None},
        'source_sentence': 'B. subtilis inoculation increased PAE by 10% at P0.5, though this increase was not statistically significant.',
        'source_quote': 'PAE by 10% at P0.5, though this increase was not statistically significant.',
    }

    assessed_anova = assess_comparison_row(contradictory, source_text=s146, methods_context=chea_methods)
    assert assessed_anova['complete'] is False
    assert assessed_anova['review'] == 'held'
    assert 'contradictory_significance_and_direction' in assessed_anova['hold_reasons']

    directional, routed = partition_comparisons_and_overall_effects(
        [contradictory, pos_ns, pos_inc_ns],
        methods_context=chea_methods,
    )
    assert len(routed) == 1 and routed[0]['record_type'] == 'undirected_overall_effect'
    assert 'contradictory_significance_and_direction' in routed[0]['raw_hold_reasons']
    assert [d['id'] for d in directional] == ['pos_ns', 'pos_inc_ns']
    assert all(d['review'] == 'not_flagged' and d['hold_reasons'] == [] for d in directional)

    with closing(db):
        comp_y_leaf = ground_trait_location_components(
            'sugar concentration in young leaves',
            {'iri': pato_conc, 'label': 'concentration of', 'mapping': 'broader'},
            db,
            methods_context=chea_methods,
            source_text='At 42 DAT, Milva had a diminished sugar concentration in young leaves compared to Lady Claire.',
        )
        assert comp_y_leaf['status'] == 'grounded_components'
        assert comp_y_leaf['trait']['iri'] == pato_conc
        assert comp_y_leaf['location']['iri'] == po_leaf
        assert comp_y_leaf['location']['qualifier'] == 'young'
        assert comp_y_leaf['analyte']['phrase'] == 'soluble sugar'
        assert comp_y_leaf['analyte']['status'] == 'source_wording'
        assert comp_y_leaf['analyte']['provenance'] == 'methods_context'
        assert comp_y_leaf['shared_concept'] is False

        comp_stem = ground_trait_location_components(
            'sugar concentration in stem',
            {'iri': pato_conc, 'label': 'concentration of', 'mapping': 'broader'},
            db,
            methods_context=chea_methods,
            source_text='Milva possessed a heightened sugar concentration in the stem.',
        )
        assert comp_stem['location']['iri'] == po_stem and comp_stem['analyte']['phrase'] == 'soluble sugar'

        comp_root = ground_trait_location_components(
            'sugar concentration in roots',
            {'iri': pato_conc, 'label': 'concentration of', 'mapping': 'broader'},
            db,
            methods_context=chea_methods,
            source_text='Lady Claire surpassed Milva in roots.',
        )
        assert comp_root['location']['iri'] == po_root and comp_root['analyte']['phrase'] == 'soluble sugar'


def test_tightened_completeness_direction_hold_ratio_efficiency_and_table_stitch(tmp_path):
    from pathlib import Path
    from backend.evaluation.comparison_pilot import verify_and_stitch_table_interrupted_targets

    pato_ratio = 'http://purl.obolibrary.org/obo/PATO_0001470'
    pato_eff = 'http://purl.obolibrary.org/obo/PATO_0001029'
    pato_acc = 'http://purl.obolibrary.org/obo/PATO_0002269'
    go_growth = 'http://purl.obolibrary.org/obo/GO_0040007'
    po_root = 'http://purl.obolibrary.org/obo/PO_0009005'
    po_shoot = 'http://purl.obolibrary.org/obo/PO_0009006'
    owl = f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
        <owl:Class rdf:about="{pato_ratio}"><rdfs:label>ratio</rdfs:label></owl:Class>
        <owl:Class rdf:about="{pato_eff}"><rdfs:label>efficiency</rdfs:label></owl:Class>
        <owl:Class rdf:about="{pato_acc}"><rdfs:label>accumulation</rdfs:label></owl:Class>
        <owl:Class rdf:about="{go_growth}"><rdfs:label>growth</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_root}"><rdfs:label>root</rdfs:label></owl:Class>
        <owl:Class rdf:about="{po_shoot}"><rdfs:label>shoot system</rdfs:label></owl:Class>
        </rdf:RDF>""".encode('utf-8')
    (tmp_path / 'tight.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'tight': {'file': 'tight.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    db, _ = open_lookup(tmp_path)

    with closing(db):
        # 1. Attenuated treatment response ('less influenced by B. subtilis') mapped to unchanged MUST be HELD
        s_less = 'In contrast, the P-efficient cultivar Milva exhibited tolerance to P deficiency, with its growth less influenced by the plant growth-promoting B. subtilis.'
        held_less = assess_comparison_row({
            'comparison_axis': 'group',
            'measured_trait': {'phrase': 'growth', 'iri': go_growth, 'label': 'growth', 'mapping': 'exact', 'component_status': 'named_exact'},
            'study_group': 'Milva',
            'baseline_status': 'unspecified',
            'comparator_label': None,
            'comparator_quote': None,
            'direction': 'unchanged',
            'statistical_significance': 'not_reported',
            'reported_difference': 'less influenced by B. subtilis',
            'condition': {'phrase': None, 'iri': None, 'label': None},
            'source_sentence': s_less,
            'source_quote': s_less,
        }, source_text=s_less, methods_context='')
        assert held_less['complete'] is False
        assert held_less['review'] == 'held'
        assert 'attenuated_treatment_response_as_unchanged' in held_less['hold_reasons']

        # 2. root-to-shoot ratio -> PATO_0001470: saved single-organ form AND ordered form both stay partial & unheld
        s_ratio = 'However, we did not observe that P shortage changed root-to-shoot ratios significantly.'
        raw_single_organ = {
            'comparison_axis': 'condition',
            'measured_trait': {
                'phrase': 'root-to-shoot ratio',
                'iri': pato_ratio, 'label': 'ratio', 'mapping': 'broader',
                'component_status': 'grounded_components',
                'location': {'iri': po_root, 'label': 'root'},
                'grounded_components': [{'role': 'trait', 'iri': pato_ratio, 'label': 'ratio'}, {'role': 'location', 'iri': po_root, 'label': 'root'}],
            },
            'study_group': 'potato',
            'baseline_status': 'unspecified',
            'comparator_label': None,
            'comparator_quote': None,
            'direction': 'no_significant_difference',
            'statistical_significance': 'not_significant',
            'reported_difference': 'did not change significantly',
            'condition': {'phrase': 'P shortage', 'iri': None, 'label': None},
            'source_sentence': s_ratio,
            'source_quote': s_ratio,
        }
        raw_assessed = assess_comparison_row(raw_single_organ, source_text=s_ratio)
        assert raw_assessed['complete'] is False
        assert raw_assessed['partial_grounded'] is True
        assert raw_assessed['review'] == 'not_flagged'
        assert any('lacks both ordered ratio terms' in iss for iss in raw_assessed['validation_issues'])

        regrounded_rs = ground_trait_location_components('root-to-shoot ratio', {'iri': pato_ratio, 'label': 'ratio', 'mapping': 'broader'}, db)
        assert regrounded_rs['status'] == 'partial_components'
        assert regrounded_rs['grounding_completeness'] == 'partial'
        assert regrounded_rs['location'] is None
        assert regrounded_rs['ratio_components']['ordered_pair_complete'] is True
        assert regrounded_rs['ratio_components']['numerator']['iri'] == po_root and regrounded_rs['ratio_components']['numerator']['order'] == 1
        assert regrounded_rs['ratio_components']['denominator']['iri'] == po_shoot and regrounded_rs['ratio_components']['denominator']['order'] == 2
        assert regrounded_rs['concept_identity'] == f'ratio({po_root}/{po_shoot})'

        regrounded_sr = ground_trait_location_components('shoot-to-root ratio', {'iri': pato_ratio, 'label': 'ratio', 'mapping': 'broader'}, db)
        assert regrounded_sr['concept_identity'] == f'ratio({po_shoot}/{po_root})'
        assert regrounded_sr['concept_identity'] != regrounded_rs['concept_identity']

        rs_assessed = assess_comparison_row({
            **raw_single_organ,
            'measured_trait': {
                'phrase': 'root-to-shoot ratio', **regrounded_rs['trait'],
                'component_status': regrounded_rs['status'], 'grounding_completeness': regrounded_rs['grounding_completeness'],
                'location': regrounded_rs['location'], 'ratio_components': regrounded_rs['ratio_components'],
                'concept_identity': regrounded_rs['concept_identity'], 'grounded_components': regrounded_rs['grounded_components'],
            },
        }, source_text=s_ratio)
        assert rs_assessed['complete'] is False
        assert rs_assessed['partial_grounded'] is True
        assert rs_assessed['review'] == 'not_flagged'

        # 3. Generic efficiency / accumulation -> partial_broader with distinct trait identities
        pute = ground_trait_location_components('PUtE', {'iri': pato_eff, 'label': 'efficiency', 'mapping': 'broader'}, db)
        pae = ground_trait_location_components('PAE', {'iri': pato_eff, 'label': 'efficiency', 'mapping': 'broader'}, db)
        pupe = ground_trait_location_components('PUpE', {'iri': pato_eff, 'label': 'efficiency', 'mapping': 'broader'}, db)
        pacc = ground_trait_location_components('P accumulation', {'iri': pato_acc, 'label': 'accumulation', 'mapping': 'broader'}, db)
        assert pute['status'] == 'partial_broader' and pute['grounding_completeness'] == 'partial'
        assert len({pute['concept_identity'], pae['concept_identity'], pupe['concept_identity'], pacc['concept_identity']}) == 4
        pute_assessed = assess_comparison_row({
            'comparison_axis': 'group',
            'measured_trait': {'phrase': 'PUtE', **pute['trait'], 'component_status': pute['status'], 'grounding_completeness': pute['grounding_completeness'], 'concept_identity': pute['concept_identity']},
            'study_group': 'Milva', 'baseline_status': 'explicit', 'comparator_label': 'Lady Claire', 'comparator_quote': 'that of Lady Claire',
            'direction': 'higher', 'reported_difference': 'considerably exceeded', 'condition': {'phrase': 'P2', 'iri': None, 'label': None},
            'source_sentence': 'The PUtE of Milva considerably exceeded that of Lady Claire under P2.',
        }, source_text='The PUtE of Milva considerably exceeded that of Lady Claire under P2.')
        assert pute_assessed['complete'] is False and pute_assessed['partial_grounded'] is True and pute_assessed['review'] == 'not_flagged'

        # 4. Interrupted pages 7-9 sentence stitching with real Chea PDF verification
        chea_pdf = Path('pdf/Chea_2024_Potato_Phosphorus_Nutrition.pdf')
        syn_targets = [{
            'target_id': 't_p7', 'target_sentence_numbers': [1], 'target_page_start': 7, 'target_page_end': 7,
            'sentences': [
                {'page': 7, 'text': 'The sugar content in the roots of Lady Claire plants inoculated with B. subtilis was Table 1 P status in different parts'},
                {'page': 8, 'text': 'Fig. 2 Relative expression'},
                {'page': 9, 'text': 'Page 9 of 15 20–33% higher than that in non-inoculated plants across both P levels and in Milva at P0.5 (Table 4). Discussion A recent study...'},
            ],
        }, {
            'target_id': 't_p9', 'target_sentence_numbers': [3], 'target_page_start': 9, 'target_page_end': 9,
            'sentences': [
                {'page': 7, 'text': 'The sugar content in the roots of Lady Claire plants inoculated with B. subtilis was Table 1 P status in different parts'},
                {'page': 8, 'text': 'Fig. 2 Relative expression'},
                {'page': 9, 'text': 'Page 9 of 15 20–33% higher than that in non-inoculated plants across both P levels and in Milva at P0.5 (Table 4). Discussion A recent study...'},
            ],
        }]
        stitched_tgts, stitches = verify_and_stitch_table_interrupted_targets(
            syn_targets, pdf_path=chea_pdf if chea_pdf.is_file() else None,
        )
        assert len(stitches) == 1
        assert stitches[0]['prefix_page'] == 7 and stitches[0]['continuation_page'] == 9
        assert stitched_tgts[0]['source_pages'] == [7, 9] and stitched_tgts[0]['page_span'] == '7-9'
        assert stitched_tgts[0]['sentences'][0]['text'].startswith('The sugar content in the roots of Lady Claire plants inoculated with B. subtilis was 20–33% higher')
        assert stitched_tgts[1]['sentences'][2]['text'] == 'Discussion A recent study...'
