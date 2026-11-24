"""Build a lean, pre-indexed JSON + PDF dataset for static GitHub Pages hosting."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import uuid
from typing import Any

from backend.config.pipeline import (
    POST_GROUNDING_PAUSED_MESSAGE,
    POST_GROUNDING_STAGES_PAUSED,
    stage_status,
)
from backend.domain.abbreviations import collect_definitions, expand_claim_names
from backend.ontology.catalog import catalog
from backend.stages.ontology_grounding.source_context import definition_context_stale
from backend.shared.storage import stable_hash
from backend.viewer.export import ACTIVE, check_source_files, clean, display_api, source_inventory
from backend.viewer.snapshot import digest, manifest_id

STATIC_FORMAT = 'static-pages-viewer-v1'
DISPLAY_CLAIM_KEYS = frozenset({
    'candidate_id',
    'subject',
    'relation',
    'object',
    'subject_context',
    'object_context',
    'predicate_alias',
    'predicate_set_version',
    'proposed_relation_iri',
    'proposed_relation_id',
    'proposed_relation_display_label',
    'proposed_relation_mapping',
    'proposed_relation_directed',
    'relation_trigger',
    'extraction_review_status',
    'extraction_warnings',
    'qualifiers',
    'measured_comparison',
    'source_page',
    'source_evidence_span',
    'source_evidence_sentence_numbers',
    'source_evidence_section_heading',
    'claim_text',
    'source_references',
    'source_validation',
})


def _prune_claim(claim: dict[str, Any] | Any) -> dict[str, Any] | Any:
    if not isinstance(claim, dict):
        return claim
    return {k: v for k, v in claim.items() if k in DISPLAY_CLAIM_KEYS and v not in (None, '', {}, [])}


def _curie_id(iri: str) -> str:
    tail = iri.rsplit('/', 1)[-1]
    if iri.startswith('http://identifiers.org/hgnc/'):
        return f'HGNC:{tail}'
    if iri.startswith('http://identifiers.org/ncbigene/'):
        return f'NCBIGene:{tail}'
    if iri.startswith('https://swisslipids.org/rdf/') and '_' in tail:
        return tail.replace('_', ':', 1)
    return tail.replace('_', ':', 1) if '/obo/' in iri and '_' in tail else tail


def _catalog_export(ref: Any, assertions: list[dict[str, Any]], *, inventory: Any = None) -> dict[str, Any]:
    terms: dict[str, dict[str, Any]] = {}
    sources_index: dict[str, int] = {}
    sources_list: list[str] = []

    def source_id(url: str) -> int:
        if url not in sources_index:
            sources_index[url] = len(sources_list)
            sources_list.append(url)
        return sources_index[url]

    for iri, term in ref.terms.items():
        if term.get('deprecated'):
            continue
        kind = term.get('kind')
        children = sorted(
            [ch for ch in ref.children.get(iri, ()) if ch in ref.terms and not ref.terms[ch].get('deprecated') and ref.terms[ch].get('kind') == kind],
            key=lambda x: (ref.terms[x]['label'].casefold(), x),
        )
        parents = [p for p in term.get('parents', []) if p in ref.terms and not ref.terms[p].get('deprecated') and ref.terms[p].get('kind') == kind]
        canonical_iri = ref.canonical_equivalent(iri)
        assertion_count = sum(
            any(b.get('iri') == iri or b.get('canonical_iri') == canonical_iri for b in row.get('bindings', {}).values())
            for row in assertions
        )
        entry: dict[str, Any] = {
            'iri': iri,
            'id': term.get('id', ''),
            'label': term.get('label', ''),
            'kind': kind,
            'origin': term.get('origin', ''),
            'selectable': bool(term.get('selectable')),
            'canonical_iri': canonical_iri,
            'assertion_count': assertion_count,
        }
        if term.get('definition'):
            entry['definition'] = term['definition']
        for field in ('exact_synonyms', 'related_synonyms', 'broad_synonyms', 'narrow_synonyms', 'domain', 'range',
                      'usage_notes', 'comments', 'annotation_provenance', 'allowed_mappings',
                      'inverse_of', 'inverse_provenance', 'obo_unique_labels'):
            val = term.get(field)
            if val:
                entry[field] = val
        if 'direct_use_allowed' in term:
            entry['direct_use_allowed'] = term['direct_use_allowed']
        if parents:
            entry['parents'] = parents
        if children:
            entry['children'] = children
        if term.get('sources'):
            entry['source_ids'] = [source_id(u) for u in term['sources']]
        terms[iri] = entry
    def add_inventory_term(term_dict: dict[str, Any]) -> None:
        iri = term_dict['iri']
        if iri in terms:
            return
        assertion_count = sum(
            any(b.get('iri') == iri or b.get('canonical_iri') == iri for b in r.get('bindings', {}).values())
            for r in assertions
        )
        src_vals = term_dict.get('sources') or ([term_dict['source']] if term_dict.get('source') else [])
        entry = {
            **term_dict,
            'canonical_iri': term_dict.get('canonical_iri') or iri,
            'selectable': bool(term_dict.get('selectable')),
            'id': _curie_id(iri),
            'assertion_count': assertion_count,
            'origin': 'established_ontology',
        }
        if src_vals and 'source_ids' not in entry:
            entry['source_ids'] = [source_id(u) for u in src_vals]
        terms[iri] = entry

    pending_parents: list[str] = []
    for row in assertions:
        for role in ('subject', 'object'):
            term = row['bindings'].get(role, {}).get('retrieved_term')
            if term and term.get('iri'):
                if term['iri'] not in terms:
                    add_inventory_term(term)
                pending_parents.extend(terms[term['iri']].get('parents') or ())

    if inventory is not None:
        seen_parents: set[str] = set()
        while pending_parents:
            parent_iri = pending_parents.pop()
            if parent_iri in terms or parent_iri in seen_parents:
                continue
            seen_parents.add(parent_iri)
            parent_term = inventory.get_term(parent_iri)
            if parent_term:
                add_inventory_term(parent_term)
                pending_parents.extend(parent_term.get('parents') or ())
    return {'catalog_signature': ref.signature, 'sources': sources_list, 'terms': terms}


def _slim_assertion(row: dict[str, Any], glossary: dict[str, Any]) -> dict[str, Any]:
    claim = _prune_claim(expand_claim_names(row.get('original_claim') or {}, glossary))
    bindings = {
        role: {k: v for k, v in binding.items() if k not in {'candidate_iris', 'candidate_terms'}} if isinstance(binding, dict) else binding
        for role, binding in row.get('bindings', {}).items()
    }
    return {
        'candidate_id': row.get('candidate_id'),
        'claim_signature': row.get('claim_signature'),
        'document_key': row.get('document_key'),
        'scope': row.get('scope'),
        'scope_reason': row.get('scope_reason'),
        'bindings': bindings,
        'original_claim': claim,
        'comparison_assessment': row.get('comparison_assessment'),
        'extraction_warnings': row.get('extraction_warnings', []),
        'mapping_consistency_warnings': row.get('mapping_consistency_warnings', []),
        'extraction_review': row.get('extraction_review'),
        'extraction_review_status': row.get('extraction_review_status'),
        'property_type_check': row.get('property_type_check'),
        'mapping_review_warnings': row.get('mapping_review_warnings', []),
        'review_history': row.get('review_history', []),
        'mapping_review': row.get('mapping_review'),
        'scientific_validation': row.get('scientific_validation'),
        'assembly_eligible': row.get('assembly_eligible'),
        'eligibility_policy': row.get('eligibility_policy'),
        'classification_policy': row.get('classification_policy'),
        'inventory_signature': row.get('inventory_signature'),
        'class_selection_policy': row.get('class_selection_policy'),
        'annotation_policy': row.get('annotation_policy'),
        'annotation_projection_policy': row.get('annotation_projection_policy'),
        'property_usage_policy': row.get('property_usage_policy'),
        'consistency_policy': row.get('consistency_policy'),
        'policy': row.get('policy'),
    }


def _display_fact(fact: dict[str, Any], ref: Any) -> dict[str, Any]:
    names = {
        'http://www.w3.org/2000/01/rdf-schema#subClassOf': 'is a subclass of',
        'http://www.w3.org/1999/02/22-rdf-syntax-ns#type': 'is an instance of',
        'http://www.w3.org/2000/01/rdf-schema#subPropertyOf': 'is a subproperty of',
    }
    return {
        **fact,
        **{
            key + '_label': ref.terms.get(fact[key], {}).get('label') or names.get(fact[key]) or fact[key].rsplit('/', 1)[-1]
            for key in ('subject', 'predicate', 'object')
        },
    }


def export_static_site(ontology, pdf_dir, output, *, include_pdfs: bool = False, replace: bool = False) -> dict[str, Any]:
    ontology, pdf_dir, output = Path(ontology).resolve(), Path(pdf_dir).resolve(), Path(output).resolve()
    if not ontology.is_dir():
        raise ValueError('Ontology directory does not exist')
    if output in {ontology, pdf_dir} or output.is_relative_to(ontology) or output.is_relative_to(pdf_dir) or ontology.is_relative_to(output) or pdf_dir.is_relative_to(output):
        raise ValueError('Static export destination must be separate from source data')
    if output.exists() and not replace:
        raise ValueError('Static data directory already exists; use --replace to export an updated build')

    before = source_inventory(ontology, pdf_dir)
    check_source_files(before)
    folders = sorted(
        p for p in ontology.iterdir()
        if p.is_dir() and not p.name.startswith('.')
        and any((p / n).exists() for n in ('candidates.jsonl', 'summary.json', 'run_state.json', 'document_scope.json'))
    )
    if not folders:
        raise ValueError('No saved document runs to export')

    registry = {stable_hash(p.name + '.pdf', 16): pdf_dir / (p.name + '.pdf') for p in folders}
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.static-building-', dir=output.parent))
    manifest: dict[str, Any] = {
        'format': STATIC_FORMAT,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'documents': {},
        'files': {},
    }

    def write(relative: str, value: Any, *, binary: bool = False) -> str:
        data = value if binary else json.dumps(clean(value), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest['files'][relative] = {'size': len(data), 'sha256': digest(data)}
        return relative

    try:
        ref = catalog()
        with display_api(ontology, pdf_dir, registry) as api:
            from backend.config.settings import APPLICATION_VERSION, SCHEMA_VERSION, Settings

            settings = Settings()
            pipeline_info = {
                'schema_version': SCHEMA_VERSION,
                'version': APPLICATION_VERSION,
                'stage_status': stage_status(),
                'manual_grounding': {'enabled': True, 'model': settings.pathway_assembly_model, 'ready': False},
                'task_models': settings.task_models,
                'external_phase_models': settings.external_phase_models,
                'knowledge_building_mode': settings.knowledge_building_mode,
            }
            pdfs_catalog = []
            for identifier, pdf in registry.items():
                folder = ontology / pdf.stem
                state = api._read_json(folder / 'run_state.json', {})
                if state.get('status') in ACTIVE:
                    raise ValueError(f'{pdf.stem}: latest run is marked active; stop/finish it locally before publishing')
                summary = api._summary_with_coverage(folder)
                status = state.get('status') or (summary or {}).get('run_status', 'not_started')
                pdfs_catalog.append({
                    'id': identifier,
                    'name': pdf.name,
                    'title': pdf.stem,
                    'size_bytes': pdf.stat().st_size if pdf.is_file() else 0,
                    'status': status,
                    'summary': summary,
                    'document_scope': api._read_json(folder / 'document_scope.json'),
                    'can_resume': False,
                    'latest_job_id': state.get('id'),
                    'active_job_id': None,
                    'pdf_available': bool(include_pdfs and pdf.is_file()),
                    'snapshot_only': True,
                })
                raw_claims = api.pdf_claims(identifier)
                slim_claims = []
                for row in raw_claims['claims']:
                    slim_claims.append({
                        **row,
                        'candidate': _prune_claim(row.get('candidate')),
                        'repair_eligibility': {'allowed': False, 'reason': 'Read-only static publication'},
                    })
                entry_files = {
                    'claims': write(f'pdfs/{identifier}/claims.json', {'claims': slim_claims, 'total': raw_claims.get('total', len(slim_claims))}),
                    'logs': write(f'pdfs/{identifier}/logs.json', api.pdf_logs(identifier)),
                }
                if include_pdfs and pdf.is_file():
                    entry_files['pdf'] = write(f'pdfs/{identifier}/source.pdf', pdf.read_bytes(), binary=True)
                manifest['documents'][identifier] = entry_files

            snap_path = ontology / '.ontology_grounding' / 'snapshot.json'
            snap = json.loads(snap_path.read_text(encoding='utf-8')) if snap_path.is_file() else {}
            raw_assertions = snap.get('assertions', [])
            stale = (
                snap.get('catalog_signature') != ref.signature
                or snap.get('rules_signature') != ref.rules_signature
                or any(definition_context_stale(row) for row in raw_assertions)
            )
            glossary = collect_definitions([r.get('original_claim', {}) for r in raw_assertions])
            slim_assertions = [_slim_assertion(r, glossary) for r in raw_assertions]

            try:
                from backend.ontology.inventory import open_inventory
                inv = open_inventory()
            except Exception:
                inv = None
            exported_catalog = _catalog_export(ref, slim_assertions, inventory=inv)
            exported_class_count = sum(1 for t in exported_catalog['terms'].values() if t.get('kind') == 'class')

            status_payload = {
                'profile': ref.profile,
                'catalog_signature': ref.signature,
                'reference_terms': len(exported_catalog['terms']),
                'term_counts': {**ref.selectable_counts, 'class': exported_class_count},
                'all_term_counts': {**ref.all_counts, 'class': exported_class_count},
                'searchable_classes': exported_class_count,
                'class_source': 'static_catalog',
                'counts': snap.get('counts', {}),
                'stale': stale,
                'coverage': ref.manifest['coverage'],
                'scientific_validation': 'paused',
                'reasoning': {} if POST_GROUNDING_STAGES_PAUSED else snap.get('reasoning_counts', {}),
                'stage_status': stage_status(),
                'completed_at': snap.get('completed_at'),
            }

            if POST_GROUNDING_STAGES_PAUSED:
                reasoning_payload = {
                    'status': 'paused',
                    'message': POST_GROUNDING_PAUSED_MESSAGE,
                    'facts': [],
                    'total': 0,
                    'coverage': {'assessed': False},
                    'counts': {},
                    'issues': [],
                    'offset': 0,
                    'limit': 25,
                    'scientific_validation': 'paused',
                    'proofs': {},
                    **({'merged_chains': snap['merged_chains']} if snap.get('merged_chains') is not None else {}),
                }
            else:
                r_path = ontology / '.ontology_grounding' / 'reasoning.json'
                r_data = json.loads(r_path.read_text(encoding='utf-8')) if r_path.is_file() else {}
                facts = [_display_fact(f, ref) for f in r_data.get('facts', [])]
                reasoning_payload = {
                    'facts': facts,
                    'total': len(facts),
                    'coverage': r_data.get('coverage', {}),
                    'counts': r_data.get('counts', {}),
                    'issues': r_data.get('issues', []),
                    'stale': stale,
                    'biological_rules': ref.profile.get('biological_rules', []),
                    'scientific_validation': 'paused',
                    **({'merged_chains': snap['merged_chains']} if snap.get('merged_chains') is not None else {}),
                }

            write('pdfs.json', pdfs_catalog)
            write('ontology-status.json', status_payload)
            write('ontology-catalog.json', exported_catalog)
            from backend.ontology.registry import extraction_inventory
            write('ontology-assertions.json', {'assertions': slim_assertions, 'stale': stale,
                                             'extraction_inventory': extraction_inventory(ontology)})
            write('ontology-entities.json', {
                'entities': snap.get('entities', []),
                'counts': snap.get('counts', {}),
                'stale': stale,
            })
            write('ontology-reasoning.json', reasoning_payload)

            write('health.json', {
                'status': 'ok',
                'mode': 'static',
                'capabilities': {'read_only': True, 'execute': False, 'upload': False},
                'snapshot': {
                    'created_at': manifest['created_at'],
                    'documents': len(pdfs_catalog),
                },
                'pipeline': pipeline_info,
            })

            if source_inventory(ontology, pdf_dir) != before:
                raise ValueError('Source files changed during export; finish local work and retry')

            manifest['snapshot_id'] = manifest_id(manifest)
            (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')

            backup = None
            if output.exists():
                backup = output.with_name('.snapshot-previous-' + uuid.uuid4().hex)
                output.rename(backup)
            try:
                staging.rename(output)
            except Exception:
                if backup:
                    backup.rename(output)
                raise
            return {
                'snapshot_id': manifest['snapshot_id'],
                'documents': len(pdfs_catalog),
                'files': len(manifest['files']),
                'bytes': sum(row['size'] for row in manifest['files'].values()),
                'max_file_bytes': max((row['size'] for row in manifest['files'].values()), default=0),
                'output': str(output),
                'previous_snapshot': str(backup) if backup else None,
            }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
