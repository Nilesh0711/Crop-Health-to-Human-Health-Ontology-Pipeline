"""Deterministic cross-paper pathway merging with two-arrow semantics.

Separates extracted directional influence edges (``flow``, rendered with ``↓``)
from ontology-based similarity/hierarchy connectors (``alignment``, rendered
with ``↔``) while preserving full study qualifiers and enforcing 3-tier
qualifier compatibility:

1. ``compatible``: same biological kingdom/domain and no conflicting study qualifiers.
2. ``context_shift``: same kingdom/domain, differing population/condition/cultivar/comparator
   qualifiers preserved and explicitly badged on the connector.
3. ``blocked``: conflicting biological kingdom (e.g. ``plant`` vs ``human_animal``),
   never connected.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from backend.shared.storage import stable_hash

MERGING_POLICY = 'qualified-two-arrow-merge-v1'

TRIVIAL_UPPER_IRIS = frozenset({
    'http://purl.obolibrary.org/obo/BFO_0000001',
    'http://purl.obolibrary.org/obo/BFO_0000002',
    'http://purl.obolibrary.org/obo/BFO_0000003',
    'http://purl.obolibrary.org/obo/BFO_0000004',
    'http://purl.obolibrary.org/obo/BFO_000015',
    'http://purl.obolibrary.org/obo/BFO_0000015',
    'http://purl.obolibrary.org/obo/BFO_0000016',
    'http://purl.obolibrary.org/obo/BFO_0000017',
    'http://purl.obolibrary.org/obo/BFO_0000019',
    'http://purl.obolibrary.org/obo/BFO_0000020',
    'http://purl.obolibrary.org/obo/BFO_0000031',
    'http://purl.obolibrary.org/obo/BFO_0000040',
    'http://purl.obolibrary.org/obo/PATO_0000001',
    'http://purl.obolibrary.org/obo/GO_0008150',
    'http://purl.obolibrary.org/obo/GO_0005575',
    'http://purl.obolibrary.org/obo/GO_0003674',
    'http://purl.obolibrary.org/obo/TO_0000387',
    'http://purl.obolibrary.org/obo/HP_0000118',
    'http://purl.obolibrary.org/obo/HP_0000001',
    'http://purl.obolibrary.org/obo/MONDO_0000001',
    'http://purl.obolibrary.org/obo/FOODON_00002403',
    'http://purl.obolibrary.org/obo/FOODON_00001002',
    'http://purl.obolibrary.org/obo/FOODON_00001714',
    'http://purl.obolibrary.org/obo/ECTO_0000001',
    'http://purl.obolibrary.org/obo/ECTO_8000001',
    'http://purl.obolibrary.org/obo/PECO_0007359',
    'http://purl.obolibrary.org/obo/PECO_0001001',
    'http://purl.obolibrary.org/obo/CHEBI_24431',
    'http://purl.obolibrary.org/obo/CMO_0000000',
    'http://purl.obolibrary.org/obo/OBA_0000001',
})

_HUMAN_ANIMAL_RE = re.compile(
    r'\b(?:human|humans|patient|patients|dialysis|children|child|adult|adults|'
    r'men|women|infant|infants|toddler|toddlers|adolescent|adolescents|'
    r'rat|rats|mice|mouse|uremic|ckd|renal|nephro\w*|cardio\w*|serum|blood|'
    r'urine|urinary|carotid|hyperphosphat\w*|parathyroid|calcitriol|fgf23|'
    r'fibroblast growth factor|calcinosis|dietary|food additive\w*|'
    r'processed food\w*|ultra-processed|intake|meat|dairy|bread|milk)\b',
    re.I,
)
_PLANT_RE = re.compile(
    r'\b(?:potato|potatoes|solanum|tuber|tubers|plant|plants|cultivar\w*|'
    r'root|roots|shoot|shoots|stolon\w*|seedling\w*|pgpr|rhizobacteria|'
    r'bacillus subtilis|soil|photosynthetic|chlorophyll|starch|biomass|yield)\b',
    re.I,
)
_HUMAN_PREFIXES = ('CMO_', 'HP_', 'MONDO_', 'MAXO_', 'NBO_', 'PR_')
_PLANT_PREFIXES = ('TO_', 'CO_', 'PECO_', 'PO_')
_FOOD_BRIDGE_PREFIXES = ('FOODON:', 'CDNO:')
_FOOD_BRIDGE_CURIES = frozenset({'CHEBI:26020', 'CHEBI:24838', 'CHEBI:17401', 'CHEBI:28659'})
_RO_HAS_INPUT = 'http://purl.obolibrary.org/obo/RO_0002233'
_RO_INPUT_OF = 'http://purl.obolibrary.org/obo/RO_0002352'
_CONTEXT_FIELDS = ('crop_or_species', 'population', 'condition', 'comparator')


def _is_food_bridge_node(node: dict) -> bool:
    curie = node.get('curie') or ''
    return curie.startswith(_FOOD_BRIDGE_PREFIXES) or curie in _FOOD_BRIDGE_CURIES


def _curie(iri: str | None) -> str:
    if not iri:
        return ''
    tail = iri.rsplit('/', 1)[-1]
    if '_' in tail and ':' not in tail:
        prefix, local = tail.split('_', 1)
        if prefix in {'BFO', 'RO', 'PO', 'TO', 'GO', 'SO', 'CHEBI', 'ENVO', 'PECO',
                      'PATO', 'NCBITaxon', 'FOODON', 'HP', 'MONDO', 'ECTO', 'CMO',
                      'OBA', 'PR', 'CDNO', 'MAXO', 'NBO', 'ONS', 'UO'}:
            return f'{prefix}:{local}'
    return tail


def _short_id(iri: str | None) -> str:
    return (iri or '').rsplit('/', 1)[-1]


def _paper_short(document_key: str) -> str:
    parts = (document_key or '').split('_')
    if len(parts) >= 2 and parts[1].isdigit():
        return f'{parts[0]} {parts[1]}'
    return parts[0] or document_key


def _ancestors(binding: dict, reference=None) -> set[str]:
    iri = binding.get('iri')
    if not iri:
        return set()
    anc = set(binding.get('verified_ancestor_iris') or ())
    if reference is not None and hasattr(reference, 'ancestors') and iri in getattr(reference, 'terms', {}):
        anc.update(reference.ancestors(iri))
    anc.add(iri)
    return anc


def _infer_domain(row: dict) -> str:
    orig = row.get('original_claim') or {}
    bindings = row.get('bindings') or {}
    sb = bindings.get('subject') or {}
    ob = bindings.get('object') or {}
    s_short = _short_id(sb.get('iri'))
    o_short = _short_id(ob.get('iri'))
    qual = orig.get('qualifiers') or {}
    text_blob = ' '.join(
        str(v) for v in (
            row.get('document_key') or '',
            orig.get('subject') or '',
            orig.get('object') or '',
            json.dumps(qual, ensure_ascii=False),
        )
    )
    # Plant cooking (e.g. Lisciani potato microwave cooking -> TO_0001024) stays plant/crop.
    if (
        ('potato' in text_blob.casefold() or any(p.startswith(_PLANT_PREFIXES) for p in (s_short, o_short)))
        and not any(p.startswith(_HUMAN_PREFIXES) for p in (s_short, o_short))
        and _PLANT_RE.search(text_blob)
    ):
        return 'plant'
    if (
        any(p.startswith(_HUMAN_PREFIXES) for p in (s_short, o_short))
        or _HUMAN_ANIMAL_RE.search(text_blob)
        or any(p.startswith('FOODON_') for p in (s_short, o_short))
    ):
        return 'human_animal'
    if any(p.startswith(_PLANT_PREFIXES) for p in (s_short, o_short)) or _PLANT_RE.search(text_blob):
        return 'plant'
    return 'shared'


def _qualifier_compatibility(q1: dict, q2: dict) -> tuple[str, list[dict]]:
    diffs: list[dict] = []
    for field in _CONTEXT_FIELDS:
        v1 = str(q1.get(field) or '').strip()
        v2 = str(q2.get(field) or '').strip()
        n1, n2 = v1.casefold(), v2.casefold()
        if v1 and v2 and n1 != n2:
            diffs.append({'field': field, 'left': v1, 'right': v2})
        elif field == 'population' and bool(v1) != bool(v2):
            diffs.append({'field': field, 'left': v1 or '(unspecified)', 'right': v2 or '(unspecified)'})
    tier = 'context_shift' if diffs else 'compatible'
    return tier, diffs


def _connector_between(c1: dict, c2: dict) -> tuple[dict | None, bool]:
    """Return (connector_dict, blocked_cross_domain)."""
    o1 = c1['object']
    s2 = c2['subject']
    o_iri, s_iri = o1['iri'], s2['iri']

    alignment_type = None
    explanation = ''
    if o_iri == s_iri and o_iri not in TRIVIAL_UPPER_IRIS:
        alignment_type = 'same_concept'
        explanation = f"Exact canonical class alignment on {o1['curie']} ({o1['label']})"
    elif o_iri in (s2['ancestors'] - TRIVIAL_UPPER_IRIS):
        alignment_type = 'superclass'
        explanation = f"{o1['curie']} ({o1['label']}) is a superclass of {s2['curie']} ({s2['label']})"
    elif s_iri in (o1['ancestors'] - TRIVIAL_UPPER_IRIS):
        alignment_type = 'subclass'
        explanation = f"{o1['curie']} ({o1['label']}) is a subclass of {s2['curie']} ({s2['label']})"

    if alignment_type is None:
        return None, False

    cross_domain = c1['domain'] != c2['domain'] and 'shared' not in {c1['domain'], c2['domain']}
    food_bridge = _is_food_bridge_node(o1) and _is_food_bridge_node(s2)
    if cross_domain and not food_bridge:
        return None, True

    context_tier, diffs = _qualifier_compatibility(c1['qualifiers'], c2['qualifiers'])
    uses_broader = (o1['mapping'] == 'broader') or (s2['mapping'] == 'broader')
    return {
        'kind': 'alignment',
        'arrow': '↔',
        'alignment_type': alignment_type,
        'explanation': explanation,
        'from_iri': o_iri,
        'from_id': o1['curie'],
        'from_label': o1['label'],
        'from_mapping': o1['mapping'],
        'to_iri': s_iri,
        'to_id': s2['curie'],
        'to_label': s2['label'],
        'to_mapping': s2['mapping'],
        'mapping_pair': f"{o1['mapping']} ↔ {s2['mapping']}",
        'uses_broader': uses_broader,
        'context_tier': context_tier,
        'differing_qualifiers': diffs,
    }, False


def _hop_record(c: dict) -> dict:
    return {
        'candidate_id': c['candidate_id'],
        'document_key': c['document_key'],
        'paper_short': c['paper_short'],
        'domain': c['domain'],
        'predicate_alias': c['predicate_alias'],
        'relation_iri': c['relation_iri'],
        'relation_label': c['relation_label'],
        'subject': {
            'mention': c['subject']['mention'],
            'iri': c['subject']['iri'],
            'id': c['subject']['curie'],
            'label': c['subject']['label'],
            'mapping': c['subject']['mapping'],
        },
        'object': {
            'mention': c['object']['mention'],
            'iri': c['object']['iri'],
            'id': c['object']['curie'],
            'label': c['object']['label'],
            'mapping': c['object']['mapping'],
        },
        'qualifiers': c['qualifiers'],
        'source_page': c['source_page'],
        'quote': c['quote'],
    }


def _hub_domain_matches(item_domain: str, iri: str) -> str:
    short = _short_id(iri)
    if short.startswith(_PLANT_PREFIXES):
        return 'plant'
    if short.startswith(_HUMAN_PREFIXES):
        return 'human_animal'
    return item_domain


def build_merged_chains(assertions: list[dict], reference=None, *, max_hops: int = 4, max_chains: int = 80) -> dict[str, Any]:
    """Build deterministic multi-paper chains using ↓ flow and ↔ similarity connectors."""
    eligible_claims: list[dict] = []
    for row in assertions or []:
        if row.get('scope') != 'in_scope' or not row.get('assembly_eligible'):
            continue
        bindings = row.get('bindings') or {}
        sb = bindings.get('subject') or {}
        ob = bindings.get('object') or {}
        rb = bindings.get('relation') or {}
        if not sb.get('iri') or not ob.get('iri') or not rb.get('iri'):
            continue
        orig = row.get('original_claim') or {}
        domain = _infer_domain(row)
        eligible_claims.append({
            'candidate_id': row.get('candidate_id') or '',
            'document_key': row.get('document_key') or '',
            'paper_short': _paper_short(row.get('document_key') or ''),
            'domain': domain,
            'predicate_alias': orig.get('predicate_alias') or rb.get('predicate_alias') or '',
            'relation_iri': rb.get('iri'),
            'relation_label': orig.get('relation') or rb.get('label') or _curie(rb.get('iri')),
            'qualifiers': dict(orig.get('qualifiers') or {}),
            'source_page': orig.get('source_page'),
            'quote': orig.get('source_evidence_span') or '',
            'subject': {
                'mention': orig.get('subject') or sb.get('label') or '',
                'iri': sb['iri'],
                'curie': _curie(sb['iri']),
                'label': sb.get('label') or _curie(sb['iri']),
                'mapping': sb.get('mapping') or 'unresolved',
                'ancestors': _ancestors(sb, reference),
            },
            'object': {
                'mention': orig.get('object') or ob.get('label') or '',
                'iri': ob['iri'],
                'curie': _curie(ob['iri']),
                'label': ob.get('label') or _curie(ob['iri']),
                'mapping': ob.get('mapping') or 'unresolved',
                'ancestors': _ancestors(ob, reference),
            },
        })

    # Traversal edges include the upstream RO inverse of P8 (has input <-> input of)
    # without double-counting the underlying eligible claim or convergent hubs.
    traversal_edges: list[dict] = []
    for c in eligible_claims:
        traversal_edges.append(c)
        if c['predicate_alias'] == 'P8' or c['relation_iri'] == _RO_HAS_INPUT:
            traversal_edges.append({
                **c,
                'predicate_alias': 'P8⁻¹',
                'relation_iri': _RO_INPUT_OF,
                'relation_label': 'input of',
                'subject': c['object'],
                'object': c['subject'],
            })

    adj: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    blocked_pairs: list[dict] = []
    seen_blocked_sigs: set[tuple[str, str, str, str]] = set()

    for i, c1 in enumerate(traversal_edges):
        for j, c2 in enumerate(traversal_edges):
            if i == j or c1['candidate_id'] == c2['candidate_id']:
                continue
            conn, blocked = _connector_between(c1, c2)
            if blocked:
                sig = (c1['document_key'], c2['document_key'], c1['object']['curie'], c2['subject']['curie'])
                if sig not in seen_blocked_sigs:
                    seen_blocked_sigs.add(sig)
                    blocked_pairs.append({
                        'left_paper': c1['paper_short'],
                        'left_domain': c1['domain'],
                        'left_entity': f"{c1['object']['curie']} ({c1['object']['label']})",
                        'left_qualifiers': c1['qualifiers'],
                        'right_paper': c2['paper_short'],
                        'right_domain': c2['domain'],
                        'right_entity': f"{c2['subject']['curie']} ({c2['subject']['label']})",
                        'right_qualifiers': c2['qualifiers'],
                        'reason': f"Blocked cross-domain join ({c1['domain']} ↔ {c2['domain']})",
                    })
                continue
            if conn is not None:
                adj[i].append((j, conn))

    raw_paths: list[tuple[list[int], list[dict]]] = []

    def dfs(path: list[int], conns: list[dict]) -> None:
        last_idx = path[-1]
        last_claim = traversal_edges[last_idx]
        seen_cids = {traversal_edges[k]['candidate_id'] for k in path}
        extended = False
        if len(path) < max_hops:
            for nxt_idx, conn in adj.get(last_idx, ()):
                if nxt_idx in path:
                    continue
                nxt_claim = traversal_edges[nxt_idx]
                if nxt_claim['candidate_id'] in seen_cids:
                    continue
                if {nxt_claim['subject']['iri'], nxt_claim['object']['iri']} == {
                    last_claim['subject']['iri'], last_claim['object']['iri']
                }:
                    continue
                extended = True
                dfs(path + [nxt_idx], conns + [conn])
        if len(path) >= 2 and not extended:
            if len({traversal_edges[k]['document_key'] for k in path}) >= 2:
                raw_paths.append((path, conns))

    for start_idx in range(len(traversal_edges)):
        dfs([start_idx], [])

    seen_sigs: set[tuple] = set()
    chains: list[dict] = []
    for path, conns in raw_paths:
        hops = [_hop_record(traversal_edges[k]) for k in path]
        sig = (
            tuple((h['subject']['iri'], h['predicate_alias'], h['object']['iri']) for h in hops),
            tuple(cn['alignment_type'] for cn in conns),
        )
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        papers = []
        for h in hops:
            if not papers or papers[-1] != h['paper_short']:
                papers.append(h['paper_short'])
        distinct_papers = list(dict.fromkeys(h['document_key'] for h in hops))
        uses_broader = any(
            h['subject']['mapping'] == 'broader' or h['object']['mapping'] == 'broader'
            for h in hops
        ) or any(cn['uses_broader'] for cn in conns)
        has_hierarchy = any(cn['alignment_type'] != 'same_concept' for cn in conns)
        has_context_shift = any(cn['context_tier'] == 'context_shift' for cn in conns)
        context_shift_count = sum(1 for cn in conns if cn['context_tier'] == 'context_shift')
        domain_set = {h['domain'] for h in hops} - {'shared'}
        domain = next(iter(domain_set)) if len(domain_set) == 1 else ('crop_to_human' if domain_set == {'plant', 'human_animal'} else 'mixed')
        chain_id = 'chain-' + stable_hash(json.dumps(sig, sort_keys=True), 20)
        chains.append({
            'id': chain_id,
            'domain': domain,
            'hop_count': len(hops),
            'paper_count': len(distinct_papers),
            'paper_trail': papers,
            'uses_broader': uses_broader,
            'has_hierarchy': has_hierarchy,
            'has_context_shift': has_context_shift,
            'context_shift_count': context_shift_count,
            'hops': hops,
            'connectors': conns,
        })

    chains.sort(key=lambda ch: (
        -ch['paper_count'],
        -ch['hop_count'],
        ch['context_shift_count'],
        ch['uses_broader'],
        ch['id'],
    ))
    chains = chains[:max_chains]

    # Convergent multi-paper hubs (different papers feeding into same non-trivial canonical target)
    by_target: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in eligible_claims:
        o_iri = c['object']['iri']
        if o_iri in TRIVIAL_UPPER_IRIS:
            continue
        dkey = _hub_domain_matches(c['domain'], o_iri)
        by_target[(dkey, o_iri)].append(c)

    convergent_hubs: list[dict] = []
    for (dom, iri), items in sorted(by_target.items(), key=lambda kv: (-len({x['document_key'] for x in kv[1]}), kv[0][1])):
        doc_keys = sorted({x['document_key'] for x in items})
        if len(doc_keys) < 2:
            continue
        sample_obj = items[0]['object']
        incoming = []
        seen_inc = set()
        for x in items:
            inc_sig = (x['document_key'], x['subject']['iri'], x['predicate_alias'], x['object']['mapping'])
            if inc_sig in seen_inc:
                continue
            seen_inc.add(inc_sig)
            incoming.append({
                'candidate_id': x['candidate_id'],
                'document_key': x['document_key'],
                'paper_short': x['paper_short'],
                'predicate_alias': x['predicate_alias'],
                'relation_label': x['relation_label'],
                'subject_id': x['subject']['curie'],
                'subject_label': x['subject']['label'],
                'subject_mapping': x['subject']['mapping'],
                'object_mapping': x['object']['mapping'],
                'qualifiers': x['qualifiers'],
                'quote': x['quote'],
            })
        convergent_hubs.append({
            'id': 'hub-' + stable_hash(f'{dom}|{iri}', 16),
            'domain': dom,
            'iri': iri,
            'curie': sample_obj['curie'],
            'label': sample_obj['label'],
            'paper_count': len(doc_keys),
            'papers': [_paper_short(d) for d in doc_keys],
            'uses_broader': any(inc['object_mapping'] == 'broader' or inc['subject_mapping'] == 'broader' for inc in incoming),
            'incoming': incoming,
        })

    # Detect explicit crop-to-human intake evidence gap
    has_potato_claims = any(
        'potato' in json.dumps(c['qualifiers']).casefold()
        or c['subject']['iri'] == 'http://purl.obolibrary.org/obo/FOODON_03315354'
        or c['object']['iri'] == 'http://purl.obolibrary.org/obo/FOODON_03315354'
        for c in eligible_claims
    )
    has_human_intake = any(
        c['predicate_alias'] == 'P8' and c['domain'] == 'human_animal'
        for c in eligible_claims
    )
    has_direct_potato_intake = any(
        c['domain'] == 'human_animal'
        and 'http://purl.obolibrary.org/obo/FOODON_03315354' in {c['subject']['iri'], c['object']['iri']}
        for c in eligible_claims
    )
    has_crop_to_human_potato_bridge = any(
        ch['domain'] == 'crop_to_human'
        and any('http://purl.obolibrary.org/obo/FOODON_03315354' in {h['subject']['iri'], h['object']['iri']} for h in ch['hops'])
        for ch in chains
    )
    evidence_gaps: list[dict] = []
    if has_potato_claims and has_human_intake and not (has_direct_potato_intake or has_crop_to_human_potato_bridge):
        evidence_gaps.append({
            'id': 'gap-potato-to-human-intake',
            'from_node': 'FOODON:03315354 (potato (tuber)) / TO:0001024 (phosphorus content)',
            'to_node': 'MAXO:0001018 (nutrition intake) / ECTO:9001345 (exposure to phosphate)',
            'status': 'missing_bridge_evidence',
            'summary': (
                'Crop and cooking claims reach potato phosphorus content (TO:0001024), while human papers link '
                'food groups (cereals, dairy, meat) and phosphate additives (FOODON:03412972) into dietary '
                'phosphate intake. No eligible causal or P8 has-input claim directly connects potato tubers '
                'specifically to human dietary phosphate intake.'
            ),
        })

    return {
        'policy': MERGING_POLICY,
        'summary': {
            'eligible_claims': len(eligible_claims),
            'cross_paper_chains': len(chains),
            'compatible_chains': sum(1 for ch in chains if not ch['has_context_shift']),
            'context_shift_chains': sum(1 for ch in chains if ch['has_context_shift']),
            'exact_only_chains': sum(1 for ch in chains if not ch['uses_broader']),
            'broader_included_chains': sum(1 for ch in chains if ch['uses_broader']),
            'hierarchy_chains': sum(1 for ch in chains if ch['has_hierarchy']),
            'convergent_hubs': len(convergent_hubs),
            'blocked_cross_domain_pairs': len(blocked_pairs),
            'evidence_gaps': len(evidence_gaps),
        },
        'chains': chains,
        'convergent_hubs': convergent_hubs,
        'blocked_cross_domain': blocked_pairs[:20],
        'evidence_gaps': evidence_gaps,
    }
