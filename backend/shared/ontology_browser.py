"""A paginated term browser and auditable mapping review; no graph endpoint needed."""
from __future__ import annotations

import json
from collections import Counter
from typing import Literal
from pathlib import Path
from threading import RLock

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator

from backend.ontology.catalog import catalog
from backend.config.pipeline import ONTOLOGY_WORKSPACE_DIRECTORY as DIRECTORY
from backend.config.pipeline import POST_GROUNDING_STAGES_PAUSED, POST_GROUNDING_PAUSED_MESSAGE, stage_status
from backend.ontology.signatures import claim_signature, inventory_signature
from backend.shared.storage import read_jsonl, stable_hash, utc_now
from backend.shared.artifacts import save_rows
from backend.shared.jobs import ACTIVE_STATUSES
from backend.domain.extraction_review import refresh_eligibility
from backend.stages.ontology_grounding.grouping import flag_consistency
from backend.domain.abbreviations import collect_definitions, expand_claim_names

_cache, _cache_lock = {}, RLock()


def saved(path, default):
    if not path.exists():
        return default
    stat = path.stat()
    stamp = stat.st_mtime_ns, stat.st_size
    with _cache_lock:
        if str(path) not in _cache or _cache[str(path)][0] != stamp:
            _cache[str(path)] = stamp, json.loads(path.read_text(encoding='utf-8'))
        return _cache[str(path)][1]


def revision(row):
    return stable_hash(json.dumps(row, sort_keys=True, ensure_ascii=False), 64)


class MappingReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['subject', 'object', 'relation', 'scope', 'extraction']
    extraction_status: Literal['accepted', 'rejected'] | None = None
    iri: str | None = Field(default=None, max_length=1000)
    mapping: Literal['exact', 'broader', 'narrower', 'related', 'unresolved'] = 'exact'
    denotation: Literal['concept', 'particular', 'state'] = 'concept'
    scope: Literal['in_scope', 'out_of_scope', 'needs_review'] | None = None
    note: str = Field(min_length=3, max_length=2000)
    expected_revision: str = Field(min_length=64, max_length=64)

    @field_validator('note')
    @classmethod
    def meaningful_note(cls, value):
        value = value.strip()
        if len(value) < 3:
            raise ValueError('Provide a review note of at least three non-whitespace characters')
        return value


def create_router(workspace, *, read_only=False, snapshot_loader=None):
    router = APIRouter(prefix='/api/ontology')
    def read_saved(path, default):
        return snapshot_loader(path, default) if snapshot_loader else saved(path, default)
    def reference():
        try:
            return catalog()
        except (OSError, ValueError) as exc:
            raise HTTPException(503, f'Ontology reference unavailable: {exc}') from exc
    def snapshot():
        data = read_saved(workspace.ONTOLOGY_DIR / DIRECTORY / 'snapshot.json', {})
        if (data.get('catalog_signature') != reference().signature
                or data.get('rules_signature') != reference().rules_signature
                or not read_only and data.get('source_inventory') != inventory_signature(workspace.ONTOLOGY_DIR)):
            return {**data, 'stale': True}
        return data

    def display_fact(fact, ref):
        names = {'http://www.w3.org/2000/01/rdf-schema#subClassOf': 'is a subclass of',
                 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type': 'is an instance of',
                 'http://www.w3.org/2000/01/rdf-schema#subPropertyOf': 'is a subproperty of'}
        return {**fact, **{key + '_label': ref.terms.get(fact[key], {}).get('label') or names.get(fact[key]) or fact[key].rsplit('/', 1)[-1]
                         for key in ('subject', 'predicate', 'object')}}

    @router.get('/status')
    def status():
        ref, state = reference(), snapshot()
        return {'profile': ref.profile, 'catalog_signature': ref.signature, 'reference_terms': len(ref.terms),
                'term_counts': ref.selectable_counts, 'all_term_counts': ref.all_counts,
                'counts': state.get('counts', {}), 'stale': state.get('stale', False),
                'coverage': ref.manifest['coverage'], 'scientific_validation': 'paused',
                'reasoning': {} if POST_GROUNDING_STAGES_PAUSED else state.get('reasoning_counts', {}),
                'stage_status': stage_status(), 'completed_at': state.get('completed_at')}

    @router.get('/terms')
    def terms(q: str = '', kind: Literal['class', 'object_property', 'data_property'] = 'class',
              selectable: bool = False, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        return reference().search(q, kind, limit=limit, offset=offset, selectable=selectable)

    @router.get('/hierarchy')
    def hierarchy(kind: Literal['class', 'object_property', 'data_property'] = 'class', parent: str | None = None,
                  limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        ref = reference()
        allowed = {i for i, t in ref.terms.items() if t['selectable'] and t['kind'] == kind}
        visible = allowed | {a for i in allowed for a in ref.ancestors(i)}
        visible &= ref.terms.keys()
        rows = [ref.brief(i) for i in visible if i in ref.terms and ref.terms[i]['kind'] == kind
                and (i in ref.children[parent] if parent else not set(ref.terms[i]['parents']) & visible)]
        rows.sort(key=lambda r: (r['label'].casefold(), r['iri']))
        return {'terms': [{**r, 'child_count': len(ref.children[r['iri']] & visible),
                           'has_children': bool(ref.children[r['iri']] & visible)} for r in rows[offset:offset + limit]],
                'total': len(rows), 'offset': offset, 'limit': limit}

    def compute_lineage_paths(iri: str, visited: set[str] | None = None) -> list[list[dict]]:
        ref = reference()
        if visited is None:
            visited = set()
        if iri in visited or iri not in ref.terms:
            return []
        visited.add(iri)
        term_kind = ref.terms[iri].get('kind')
        parents = [p for p in ref.terms[iri].get('parents', []) if p in ref.terms and ref.terms[p].get('kind') == term_kind]
        if not parents:
            return [[ref.brief(iri)]]
        paths = []
        for p in parents:
            sub_paths = compute_lineage_paths(p, visited.copy())
            if not sub_paths:
                paths.append([ref.brief(p), ref.brief(iri)])
            else:
                for sp in sub_paths:
                    paths.append(sp + [ref.brief(iri)])
        return paths

    @router.get('/term')
    def term(iri: str):
        ref = reference()
        if iri not in ref.terms:
            raise HTTPException(404, 'Term not present in the frozen ontology reference')
        item = ref.brief(iri)
        children = [ref.brief(ch) for ch in sorted(ref.children.get(iri, set()),
                                                   key=lambda x: ref.terms.get(x, {}).get('label', '').casefold())
                    if ch in ref.terms and ref.terms[ch].get('kind') == item.get('kind')]
        return {**item, 'parents': [ref.brief(p) if p in ref.terms else {'iri': p, 'label': p} for p in item['parents']],
                'ancestors': list(ref.ancestors(iri)),
                'lineage_paths': compute_lineage_paths(iri),
                'children': children,
                'child_count': len(children),
                'has_children': bool(children),
                'catalog_signature': ref.signature,
                'assertion_count': sum(any(b.get('iri') == iri or b.get('canonical_iri') == ref.canonical_equivalent(iri)
                                           for b in r['bindings'].values()) for r in snapshot().get('assertions', []))}

    @router.get('/assertions')
    def assertions(iri: str | None = None, document: str | None = None, q: str = '',
                   scope: Literal['all', 'in_scope', 'out_of_scope', 'needs_review'] = 'all',
                   limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
        state = snapshot()
        canonical = reference().canonical_equivalent(iri) if iri else None
        rows = [r for r in state.get('assertions', []) if (not iri or any(b.get('iri') == iri or b.get('canonical_iri') == canonical for b in r['bindings'].values()))
                and (not document or r['document_key'] == document) and (scope == 'all' or r['scope'] == scope)
                and (not q or q.casefold() in json.dumps(r['original_claim'], ensure_ascii=False).casefold())]
        glossary = collect_definitions([r['original_claim'] for r in state.get('assertions', [])])
        return {'assertions': [{**r, 'original_claim': expand_claim_names(r['original_claim'], glossary),
                                'revision': revision(r)} for r in rows[offset:offset + limit]],
                'total': len(rows), 'offset': offset, 'limit': limit, 'stale': state.get('stale', False),
                'unprocessed_documents': [p.parent.name for p in workspace.ONTOLOGY_DIR.glob('*/candidates.jsonl')
                                          if not (p.parent / 'ontology_grounding.jsonl').exists()]}

    @router.get('/entities')
    def entities(q: str = '', document: str | None = None,
                 status: Literal['all', 'grounded', 'vocabulary_gap', 'alignment_review', 'classification', 'review'] = 'all',
                 limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0),
                 unique: bool = False):
        state = snapshot()
        assertions = {(r['document_key'], r['candidate_id']): r for r in state.get('assertions', [])}
        glossary = collect_definitions([r['original_claim'] for r in assertions.values()])
        raw_entities = state.get('entities', [])
        if unique:
            grouped = {}
            for e in raw_entities:
                norm_lbl = (e.get('label') or '').strip().casefold()
                key = (norm_lbl, e.get('iri') or '', e.get('mapping') or '')
                if key not in grouped:
                    grouped[key] = {
                        **e,
                        'id': f"uniq-{stable_hash(f'{key[0]}|{key[1]}|{key[2]}', 24)}",
                        'occurrences': list(e.get('occurrences', []))
                    }
                else:
                    grouped[key]['occurrences'].extend(e.get('occurrences', []))
            entity_list = list(grouped.values())
        else:
            entity_list = raw_entities
        found = []
        status_counts = Counter({'grounded': 0, 'vocabulary_gap': 0, 'alignment_review': 0, 'classification': 0, 'review': 0, 'all': 0})
        for entity in entity_list:
            occurrences = [o for o in entity['occurrences'] if not document or o['document_key'] == document]
            if not occurrences:
                continue
            if q and q.casefold() not in json.dumps([entity['label'], entity['iri'], occurrences], ensure_ascii=False).casefold():
                continue
            status_counts[entity['status']] += 1
            category = ('classification' if entity['mapping'] == 'broader' else
                        'review' if entity['mapping'] in {'related', 'narrower'} else entity['status'])
            if category in {'classification', 'review'}:
                status_counts[category] += 1
            status_counts['all'] += 1
            if status != 'all' and (category if status in {'classification', 'review'} else entity['status']) != status:
                continue
            found.append({**entity, 'occurrences': occurrences, 'occurrence_count': len(occurrences)})
        page = []
        for entity in found[offset:offset + limit]:
            occurrences = []
            for occurrence in entity['occurrences']:
                row = assertions.get((occurrence['document_key'], occurrence['candidate_id']))
                if not row:
                    continue
                claim = expand_claim_names(row['original_claim'], glossary)
                s = claim.get('subject', '')
                sc = claim.get('subject_context') or ''
                r = claim.get('relation', '')
                o = claim.get('object', '')
                oc = claim.get('object_context') or ''
                s_lbl = f"{s} ({sc})" if sc else s
                o_lbl = f"{o} ({oc})" if oc else o
                formatted_claim = claim.get('claim_text') or (f"{s_lbl} → {r} → {o_lbl}" if (s and r and o) else '')
                occurrences.append({
                    **occurrence,
                    'evidence': claim.get('source_evidence_span'),
                    'display_mention': claim.get(occurrence['role'], occurrence.get('source_mention')),
                    'endpoint_context': claim.get(occurrence['role'] + '_context') or '',
                    'claim_text': formatted_claim,
                    'claim': {
                        'candidate_id': occurrence['candidate_id'],
                        'subject': s,
                        'subject_context': sc,
                        'relation': r,
                        'object': o,
                        'object_context': oc,
                    },
                    'qualifiers': claim.get('qualifiers') or {},
                    'reason': row['bindings'][occurrence['role']]['reason'],
                })
            label = entity['label']
            if entity['status'] != 'grounded' and occurrences:
                label = occurrences[0]['display_mention'] or label
            page.append({**entity, 'label': label, 'occurrences': occurrences})
        return {'entities': page, 'total': len(found), 'offset': offset, 'limit': limit,
                'stale': state.get('stale', False), 'counts': state.get('counts', {}),
                'status_counts': dict(status_counts)}

    @router.get('/reasoning')
    def reasoning(iri: str | None = None, kind: Literal['derived', 'asserted', 'all'] = 'derived',
                  limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
        if POST_GROUNDING_STAGES_PAUSED:
            return {'status': 'paused', 'message': POST_GROUNDING_PAUSED_MESSAGE, 'facts': [],
                    'total': 0, 'coverage': {'assessed': False}, 'counts': {}, 'issues': [],
                    'offset': offset, 'limit': limit, 'scientific_validation': 'paused'}
        data = read_saved(workspace.ONTOLOGY_DIR / DIRECTORY / 'reasoning.json', {})
        facts = [r for r in data.get('facts', []) if (kind == 'all' or r['kind'] == kind)
                 and (not iri or iri in (r['subject'], r['predicate'], r['object']))]
        ref = reference()
        return {'facts': [display_fact(f, ref) for f in facts[offset:offset + limit]], 'total': len(facts), 'coverage': data.get('coverage', {}),
                'counts': data.get('counts', {}), 'issues': data.get('issues', []),
                'stale': snapshot().get('stale', False), 'offset': offset, 'limit': limit,
                'biological_rules': reference().profile.get('biological_rules', []),
                'scientific_validation': 'paused'}

    @router.get('/proof')
    def proof(id: str):
        if POST_GROUNDING_STAGES_PAUSED:
            raise HTTPException(409, POST_GROUNDING_PAUSED_MESSAGE)
        data = read_saved(workspace.ONTOLOGY_DIR / DIRECTORY / 'reasoning.json', {})
        facts = {f['id']: f for f in data.get('facts', [])}
        if id not in facts:
            raise HTTPException(404, 'Inference not found')
        found, pending = {}, [id]
        while pending:
            key = pending.pop()
            if key not in found:
                if key not in facts:
                    raise HTTPException(409, 'A premise is missing; rebuild reasoning')
                found[key] = facts[key]
                pending.extend(facts[key]['premises'])
        ref = reference()
        return {'conclusion': display_fact(facts[id], ref), 'proof': [display_fact(f, ref) for f in found.values()], 'scientific_validation': 'paused'}

    @router.get('/export')
    def export():
        if POST_GROUNDING_STAGES_PAUSED:
            raise HTTPException(409, 'RDF export is paused after ontology grounding; historical exports are preserved.')
        path = workspace.ONTOLOGY_DIR / DIRECTORY / 'assertions.ttl'
        if not path.exists():
            raise HTTPException(404, 'Ground saved claims first')
        return FileResponse(path, media_type='text/turtle', filename='ontology_assertions.ttl')

    @router.post('/pdfs/{pdf_id}/claims/{candidate_id}/review')
    def review(pdf_id: str, candidate_id: str, options: MappingReview):
        if read_only:
            raise HTTPException(403, 'Published ontology results are read-only')
        ref = reference()
        with workspace._lock:
            if any(j['status'] in ACTIVE_STATUSES for j in workspace._jobs.values()):
                raise HTTPException(409, 'Stop or finish the active run before changing a mapping')
            path = workspace._output_dir(workspace._pdf_path(pdf_id)) / 'ontology_grounding.jsonl'
            rows = read_jsonl(path)
            row = next((r for r in rows if r['candidate_id'] == candidate_id), None)
            if row is None:
                raise HTTPException(404, 'Ground this claim before reviewing its mapping')
            source = next((c for c in read_jsonl(path.parent / 'candidates.jsonl') if c['candidate_id'] == candidate_id), None)
            if source is None or source.get('schema_error') or claim_signature(source) != row['claim_signature']:
                raise HTTPException(409, 'The extracted claim changed. Reground it before reviewing.')
            if row['catalog_signature'] != ref.signature or revision(row) != options.expected_revision:
                raise HTTPException(409, 'The claim/reference changed. Refresh before reviewing.')
            history = {'at': utc_now(), 'role': options.role, 'note': options.note,
                       'before': row.get('extraction_review') if options.role == 'extraction' else row.get('scope') if options.role == 'scope' else dict(row['bindings'][options.role])}
            if options.role == 'extraction':
                if options.extraction_status is None:
                    raise HTTPException(422, 'Choose accepted or rejected extraction')
                row['extraction_review'] = {'status': options.extraction_status, 'note': options.note,
                    'claim_signature': row['claim_signature'], 'at': utc_now()}
            elif options.role == 'scope':
                if not options.scope:
                    raise HTTPException(422, 'Select a scope decision')
                row['scope'], row['scope_reason'] = options.scope, options.note
            else:
                term = ref.terms.get(options.iri)
                expected_kind = 'object_property' if options.role == 'relation' else 'class'
                if options.mapping != 'unresolved' and (term is None or not term['selectable'] or term['deprecated'] or term['kind'] != expected_kind):
                    raise HTTPException(422, 'Select a permitted concept/property from the active schema')
                if options.role == 'relation' and options.mapping not in {'exact', 'unresolved'}:
                    raise HTTPException(422, 'Relationship mapping must be exact or unresolved')
                b = row['bindings'][options.role]
                b.update(iri=options.iri if options.mapping != 'unresolved' else None,
                         label=term['label'] if term and options.mapping != 'unresolved' else None,
                         mapping=options.mapping, reason=options.note)
                b['canonical_iri'] = ref.canonical_equivalent(b['iri']) if b['iri'] else None
                if options.role != 'relation':
                    b['denotation'] = options.denotation
            row.setdefault('review_history', []).append(history)
            if options.role != 'extraction':
                row['mapping_review'] = 'manually_reviewed'
            refresh_eligibility(row)
            flag_consistency(rows)
            save_rows(path, rows)
            workspace.rebuild_ontology(workspace.ONTOLOGY_DIR, ref)
            label = 'Extraction review' if options.role == 'extraction' else 'Mapping'
            return {'message': label + ' saved. This is not scientific validation.', 'revision': revision(row)}
    return router
