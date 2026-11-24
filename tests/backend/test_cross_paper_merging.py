"""Tests for deterministic two-arrow cross-paper merging with 3-tier qualifier guards."""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.shared.ontology_browser import create_router
from backend.stages.ontology_reasoning.merging import MERGING_POLICY, build_merged_chains


def _claim(
    cid: str,
    doc: str,
    subj: tuple[str, str, str, list[str]],
    rel: tuple[str, str, str],
    obj: tuple[str, str, str, list[str]],
    qualifiers: dict | None = None,
    eligible: bool = True,
) -> dict:
    s_txt, s_iri, s_map, s_anc = subj
    pa, r_iri, r_lbl = rel
    o_txt, o_iri, o_map, o_anc = obj
    return {
        'candidate_id': cid,
        'document_key': doc,
        'scope': 'in_scope',
        'assembly_eligible': eligible,
        'original_claim': {
            'subject': s_txt,
            'relation': r_lbl,
            'object': o_txt,
            'predicate_alias': pa,
            'qualifiers': qualifiers or {},
            'source_page': 1,
            'source_evidence_span': f'{s_txt} {r_lbl} {o_txt}.',
        },
        'bindings': {
            'subject': {
                'iri': s_iri,
                'label': s_txt,
                'mapping': s_map,
                'verified_ancestor_iris': s_anc,
            },
            'relation': {
                'iri': r_iri,
                'label': r_lbl,
                'mapping': 'exact',
                'predicate_alias': pa,
            },
            'object': {
                'iri': o_iri,
                'label': o_txt,
                'mapping': o_map,
                'verified_ancestor_iris': o_anc,
            },
        },
    }


def test_merging_builds_compatible_broader_and_subclass_connectors_and_blocks_cross_kingdom():
    cmo_p = 'http://purl.obolibrary.org/obo/CMO_0000542'
    oba_imt = 'http://purl.obolibrary.org/obo/OBA_2090004'
    food_add = 'http://purl.obolibrary.org/obo/FOODON_03412972'
    chem_comp = 'http://purl.obolibrary.org/obo/FOODON_03411041'
    proc_food = 'http://purl.obolibrary.org/obo/FOODON_03311737'
    bfo_cont = 'http://purl.obolibrary.org/obo/BFO_0000002'
    go_p_met = 'http://purl.obolibrary.org/obo/GO_0006793'
    calcinosis = 'http://purl.obolibrary.org/obo/MONDO_0002123'
    peco_p = 'http://purl.obolibrary.org/obo/PECO_0007397'
    food_potato = 'http://purl.obolibrary.org/obo/FOODON_03315354'
    to_p_content = 'http://purl.obolibrary.org/obo/TO_0001024'
    ecto_bs = 'http://purl.obolibrary.org/obo/ECTO_3000026'
    maxo_intake = 'http://purl.obolibrary.org/obo/MAXO_0001018'

    assertions = [
        # Chain 1: Calvo (FOODON_03412972 subclass of FOODON_03411041) -> Calvo -> Lampen (CMO_0000542 exact<->exact, context_shift)
        _claim(
            'c1', 'Calvo_2023_Phosphate',
            ('processed foods', proc_food, 'broader', [bfo_cont]),
            ('P7', 'http://purl.obolibrary.org/obo/BFO_0000051', 'has part'),
            ('phosphate additives', food_add, 'broader', [chem_comp, bfo_cont]),
            qualifiers={'population': 'US adults'},
        ),
        _claim(
            'c2', 'Calvo_2023_Phosphate',
            ('chemical food component', chem_comp, 'broader', [bfo_cont]),
            ('P3', 'http://purl.obolibrary.org/obo/RO_0002566', 'causally influences'),
            ('serum phosphate level', cmo_p, 'exact', [bfo_cont]),
            qualifiers={'population': 'US adults'},
        ),
        _claim(
            'c3', 'Lampen_2026_Phosphate',
            ('serum phosphate levels', cmo_p, 'exact', [bfo_cont]),
            ('P5', 'http://purl.obolibrary.org/obo/RO_0002610', 'correlated with'),
            ('carotid intima-media thickness', oba_imt, 'exact', [bfo_cont]),
            qualifiers={'population': '43 CKD children', 'metric': 'r = 0.715'},
        ),
        # Human P8 intake claim + potato crop claim to verify evidence gap & cross-kingdom block on GO_0006793
        _claim(
            'c4', 'Lampen_2026_Phosphate',
            ('dietary phosphate intake', maxo_intake, 'broader', [bfo_cont]),
            ('P8', 'http://purl.obolibrary.org/obo/RO_0002233', 'has input'),
            ('food additive', food_add, 'broader', [chem_comp, bfo_cont]),
            qualifiers={'population': 'humans'},
        ),
        _claim(
            'c5', 'Chea_2024_Potato',
            ('Bacillus subtilis', ecto_bs, 'broader', []),
            ('P11', 'http://purl.obolibrary.org/obo/RO_0002213', 'positively regulates'),
            ('phosphorus metabolic process', go_p_met, 'broader', []),
            qualifiers={'crop_or_species': 'potato', 'population': 'plants'},
        ),
        _claim(
            'c6', 'Lampen_2026_Phosphate',
            ('altered phosphorus metabolism', go_p_met, 'broader', []),
            ('P10', 'http://purl.obolibrary.org/obo/RO_0003302', 'causes or contributes to condition'),
            ('calcinosis', calcinosis, 'exact', [bfo_cont]),
            qualifiers={'population': 'uremic rats'},
        ),
        # Convergent hub on TO_0001024 across two potato papers (compatible context)
        _claim(
            'c7', 'Hu_2026_Potato',
            ('potato tuber', food_potato, 'exact', [bfo_cont]),
            ('P1', 'http://purl.obolibrary.org/obo/RO_0019001', 'positively regulates characteristic'),
            ('phosphorus content', to_p_content, 'broader', [bfo_cont]),
            qualifiers={'crop_or_species': 'potato'},
        ),
        _claim(
            'c8', 'Chea_2024_Potato',
            ('Bacillus subtilis', ecto_bs, 'broader', []),
            ('P1', 'http://purl.obolibrary.org/obo/RO_0019001', 'positively regulates characteristic'),
            ('phosphorus content', to_p_content, 'exact', [bfo_cont]),
            qualifiers={'crop_or_species': 'potato'},
        ),
    ]

    merged = build_merged_chains(assertions)
    assert merged['policy'] == MERGING_POLICY
    assert merged['summary']['blocked_cross_domain_pairs'] >= 1
    assert any('GO:0006793' in b['left_entity'] for b in merged['blocked_cross_domain'])

    # Cross-kingdom plant -> rat chain must NEVER appear
    for ch in merged['chains']:
        cids = [h['candidate_id'] for h in ch['hops']]
        assert not ('c5' in cids and 'c6' in cids)

    # 3-hop Calvo (subclass + broader + compatible) -> Calvo -> Lampen (context_shift) chain exists
    three_hop = [ch for ch in merged['chains'] if ch['hop_count'] == 3]
    assert three_hop
    target = three_hop[0]
    assert target['uses_broader'] is True
    assert target['has_hierarchy'] is True
    assert target['has_context_shift'] is True
    assert target['connectors'][0]['alignment_type'] == 'subclass'
    assert target['connectors'][0]['context_tier'] == 'compatible'
    assert target['connectors'][1]['alignment_type'] == 'same_concept'
    assert target['connectors'][1]['context_tier'] == 'context_shift'
    assert any(d['field'] == 'population' for d in target['connectors'][1]['differing_qualifiers'])

    # Convergent hub on FOODON_03412972 & TO_0001024 + explicit potato-to-intake gap
    hub_curies = {h['curie'] for h in merged['convergent_hubs']}
    assert {'FOODON:03412972', 'TO:0001024'} <= hub_curies
    assert merged['evidence_gaps'][0]['id'] == 'gap-potato-to-human-intake'


def test_merging_allows_food_bridge_and_p8_inverse_forward_chain():
    food_potato = 'http://purl.obolibrary.org/obo/FOODON_03315354'
    ecto_bs = 'http://purl.obolibrary.org/obo/ECTO_3000026'
    maxo_intake = 'http://purl.obolibrary.org/obo/MAXO_0001018'
    cmo_p = 'http://purl.obolibrary.org/obo/CMO_0000542'
    go_p_met = 'http://purl.obolibrary.org/obo/GO_0006793'
    calcinosis = 'http://purl.obolibrary.org/obo/MONDO_0002123'
    bfo_cont = 'http://purl.obolibrary.org/obo/BFO_0000002'

    assertions = [
        _claim(
            'c_crop', 'FoodResearch_2026',
            ('leaching', ecto_bs, 'exact', [bfo_cont]),
            ('P2', 'http://purl.obolibrary.org/obo/RO_0019002', 'negatively regulates characteristic'),
            ('potato (tuber)', food_potato, 'exact', [bfo_cont]),
            qualifiers={'crop_or_species': 'potato'},
        ),
        _claim(
            'c_intake', 'RenalReview_2024',
            ('dietary phosphate intake', maxo_intake, 'broader', [bfo_cont]),
            ('P8', 'http://purl.obolibrary.org/obo/RO_0002233', 'has input'),
            ('potato (tuber)', food_potato, 'exact', [bfo_cont]),
            qualifiers={'population': 'humans'},
        ),
        _claim(
            'c_serum', 'Lampen_2026_Phosphate',
            ('dietary phosphate intake', maxo_intake, 'broader', [bfo_cont]),
            ('P3', 'http://purl.obolibrary.org/obo/RO_0002566', 'causally influences'),
            ('serum phosphate levels', cmo_p, 'exact', [bfo_cont]),
            qualifiers={'population': 'CKD adults'},
        ),
        _claim(
            'c_go_plant', 'Chea_2024_Potato',
            ('Bacillus subtilis', ecto_bs, 'broader', []),
            ('P11', 'http://purl.obolibrary.org/obo/RO_0002213', 'positively regulates'),
            ('phosphorus metabolic process', go_p_met, 'broader', []),
            qualifiers={'crop_or_species': 'potato', 'population': 'plants'},
        ),
        _claim(
            'c_go_human', 'Lampen_2026_Phosphate',
            ('altered phosphorus metabolism', go_p_met, 'broader', []),
            ('P10', 'http://purl.obolibrary.org/obo/RO_0003302', 'causes or contributes to condition'),
            ('calcinosis', calcinosis, 'exact', [bfo_cont]),
            qualifiers={'population': 'uremic rats'},
        ),
    ]

    merged = build_merged_chains(assertions)
    assert merged['summary']['eligible_claims'] == 5
    assert merged['summary']['blocked_cross_domain_pairs'] >= 1
    assert any('GO:0006793' in b['left_entity'] for b in merged['blocked_cross_domain'])

    # No trivial inverse self-loop (c_intake -> c_intake or vice versa)
    for ch in merged['chains']:
        cids = [h['candidate_id'] for h in ch['hops']]
        assert len(cids) == len(set(cids))
        assert not ('c_go_plant' in cids and 'c_go_human' in cids)

    crop_to_human = [ch for ch in merged['chains'] if ch['domain'] == 'crop_to_human']
    assert crop_to_human
    three_hop = [ch for ch in crop_to_human if ch['hop_count'] == 3]
    assert three_hop
    chain = three_hop[0]
    assert [h['candidate_id'] for h in chain['hops']] == ['c_crop', 'c_intake', 'c_serum']
    assert chain['hops'][1]['predicate_alias'] == 'P8⁻¹'
    assert chain['hops'][1]['relation_iri'] == 'http://purl.obolibrary.org/obo/RO_0002352'
    assert chain['hops'][1]['relation_label'] == 'input of'
    assert all(g['id'] != 'gap-potato-to-human-intake' for g in merged['evidence_gaps'])


def test_merging_bridges_potato_to_intake_across_cdno_and_clears_gap():
    food_potato = 'http://purl.obolibrary.org/obo/FOODON_03315354'
    cdno_diet_p = 'http://purl.obolibrary.org/obo/CDNO_0000018'
    maxo_intake = 'http://purl.obolibrary.org/obo/MAXO_0001018'
    cmo_p = 'http://purl.obolibrary.org/obo/CMO_0000542'
    bfo_cont = 'http://purl.obolibrary.org/obo/BFO_0000002'

    assertions = [
        _claim(
            'c_lisciani', 'Lisciani_2025_Potato',
            ('potato', food_potato, 'exact', [bfo_cont]),
            ('P7', 'http://purl.obolibrary.org/obo/BFO_0000051', 'has part'),
            ('phosphorus', cdno_diet_p, 'broader', [bfo_cont]),
            qualifiers={'crop_or_species': 'potato'},
        ),
        _claim(
            'c_beals', 'Beals_2019_Potato',
            ('daily intake', maxo_intake, 'broader', [bfo_cont]),
            ('P8', 'http://purl.obolibrary.org/obo/RO_0002233', 'has input'),
            ('phosphorus', cdno_diet_p, 'exact', [bfo_cont]),
            qualifiers={'population': 'children and adolescents'},
        ),
        _claim(
            'c_lampen', 'Lampen_2026_Phosphate',
            ('daily phosphorus intake', maxo_intake, 'broader', [bfo_cont]),
            ('P3', 'http://purl.obolibrary.org/obo/RO_0002566', 'causally influences'),
            ('serum phosphate levels', cmo_p, 'exact', [bfo_cont]),
            qualifiers={'population': 'humans'},
        ),
    ]

    merged = build_merged_chains(assertions)
    c2h = [ch for ch in merged['chains'] if ch['domain'] == 'crop_to_human' and ch['hop_count'] == 3]
    assert c2h
    assert [h['candidate_id'] for h in c2h[0]['hops']] == ['c_lisciani', 'c_beals', 'c_lampen']
    assert c2h[0]['hops'][1]['predicate_alias'] == 'P8⁻¹'
    assert merged['evidence_gaps'] == []


def test_reasoning_route_exposes_merged_chains_while_formal_closure_stays_paused(tmp_path):
    snapshot_dir = tmp_path / '.ontology_grounding'
    snapshot_dir.mkdir(parents=True)
    merged_payload = build_merged_chains([])
    (snapshot_dir / 'snapshot.json').write_text(json.dumps({
        'version': 2,
        'assertions': [],
        'entities': [],
        'counts': {},
        'merged_chains': merged_payload,
    }), encoding='utf-8')
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path)
    app = FastAPI()
    app.include_router(create_router(workspace, read_only=True))
    with TestClient(app) as client:
        data = client.get('/api/ontology/reasoning').json()
    assert data['status'] == 'paused'
    assert data['merged_chains']['policy'] == MERGING_POLICY
