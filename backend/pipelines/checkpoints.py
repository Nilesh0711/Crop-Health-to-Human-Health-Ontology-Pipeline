"""Validate persisted work before resuming; never infer a scientific decision."""
import json

from backend.config.settings import SCHEMA_VERSION
from backend.shared.artifacts import save_json
from backend.shared.storage import file_hash, read_jsonl, stable_hash
from backend.stages.claim_extraction.targeting import TARGETING_VERSION


def read_metadata(output):
    path = output / 'pipeline_checkpoint.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def claim_stage_signature(candidate, model, thinking=True):
    fields = {key: value for key, value in candidate.items()
              if key not in {'source_validation', 'semantic_validation'}}
    policy = {} if thinking else {'thinking': 'disabled'}
    return stable_hash(json.dumps({'candidate': fields, 'model': model, 'schema': SCHEMA_VERSION, **policy},
                                 ensure_ascii=False, sort_keys=True), 64)


def save_checkpoint(output, metadata):
    save_json(output / 'pipeline_checkpoint.json', metadata)


def _recover_atomic_expansion(metadata, candidates, output):
    """Admit only children declared in a durable source-checked operation journal."""
    originals = metadata.get('candidate_ids')
    ids = [c['candidate_id'] for c in candidates]
    journals = {r['operation_id']:r for r in read_jsonl(output / 'claim_revisions.jsonl')
                if r.get('action')=='reextract' and r.get('proposed_candidates')}
    derived = []
    by_id = {c['candidate_id']:c for c in candidates}
    for child in candidates:
        if originals is not None and child['candidate_id'] in originals:
            continue
        operation = journals.get(child.get('reextraction_operation_id'))
        if operation is None:
            if originals is None:
                continue
            return metadata
        parent = by_id.get(child.get('parent_candidate_id'),{})
        declared = next((c for c in operation['proposed_candidates'] if c['candidate_id']==child['candidate_id']),None)
        scientific = lambda c: {k:v for k,v in c.items() if k not in {'source_validation','semantic_validation','superseded_by'}}
        if (not declared or scientific(declared)!=scientific(child)
                or operation['candidate_id']!=child.get('parent_candidate_id')
                or child['candidate_id'] not in parent.get('superseded_by',[])):
            return metadata
        derived.append(child['candidate_id'])
    base = [i for i in ids if i not in derived]
    if derived and (base==originals if originals is not None else len(base)==metadata.get('expected_candidate_count')):
        return {**metadata,'candidate_ids':ids,'expected_candidate_count':len(ids)}
    return metadata


def resume_metadata(output, pdf, settings, max_pages, claims_per_packet, *, extraction_mode=None, target_words=None):
    """Also admit a complete saved extraction with explicit provenance checks."""
    metadata = read_metadata(output)
    if metadata is None:
        summary_path, quality_path = output / 'summary.json', output / 'pdf_quality.json'
        if not summary_path.exists() or not quality_path.exists():
            raise ValueError('No completed extraction checkpoint is available; start a new run')
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        quality = json.loads(quality_path.read_text(encoding='utf-8'))
        packets = read_jsonl(output / 'extraction_packets.jsonl')
        if not packets or any(row.get('status') != 'completed' for row in packets):
            raise ValueError('Saved extraction is incomplete; start a new run to inspect all selected text')
        selected_pages = quality.get('selected_pages')
        if selected_pages != (min(max_pages, quality['total_pages']) if max_pages else quality['total_pages']):
            raise ValueError('The saved extraction used a different page selection')
        metadata = {'schema_version': summary.get('schema_version'), 'pdf_sha256': summary.get('pdf_sha256'),
                    'task_models': summary.get('task_models'), 'max_pages': max_pages,
                    'claims_per_packet': summary.get('claims_per_packet'), 'extraction_complete': True,
                    'imported_extraction': True, 'rejection_counts': summary.get('extraction_rejection_counts') or {},
                    'canonical_duplicates': summary.get('canonical_duplicates_removed', 0),
                    'expected_candidate_count': summary.get('extracted')}
        metadata.update(thinking=summary.get('thinking') or {task: True for task in settings.task_models}, extraction_mode=summary.get('extraction_mode', 'whole_packet'),
                        extraction_target_words=summary.get('extraction_target_words'),
                        extraction_generation_budget=summary.get('extraction_generation_budget'),
                        targeting_version=summary.get('targeting_version', TARGETING_VERSION))
    # Downstream synthesis/audit and assembly assignments do not change the
    # completed extraction. Their own scientific fingerprints invalidate reuse.
    downstream = {'pathway_assembly','external'}
    core_models = {k:v for k,v in settings.task_models.items() if k not in downstream}
    saved_models = {k:v for k,v in (metadata.get('task_models') or {}).items() if k not in downstream}
    if (metadata.get('schema_version') != SCHEMA_VERSION or metadata.get('pdf_sha256') != file_hash(pdf)
            or saved_models != core_models):
        raise ValueError('Checkpoint PDF, pipeline version or selected models changed; start a new run')
    if metadata.get('max_pages') != max_pages:
        raise ValueError('Resume requires the original page selection')
    from backend.stages.pdf_parsing.layout import PARSING_POLICY, LEGACY_PARSING_POLICY
    if metadata.get('parsing_policy',LEGACY_PARSING_POLICY) not in {PARSING_POLICY,LEGACY_PARSING_POLICY}:
        raise ValueError('Saved PDF parsing policy is unavailable; use a fresh extraction')
    if metadata.get('claims_per_packet') not in (None, claims_per_packet):
        raise ValueError('Resume requires the original extraction claim limit per packet')
    if (metadata.get('thinking') or {}).get('extraction', True) != settings.task_thinking['extraction']:
        raise ValueError('Resume requires the original extraction Thinking setting; start a new run to change it')
    saved_mode = metadata.get('extraction_mode', 'whole_packet')
    if extraction_mode is not None and extraction_mode != saved_mode:
        raise ValueError('Resume requires the original extraction mode; start a new run to change it')
    if saved_mode == 'target_context':
        if metadata.get('targeting_version') != TARGETING_VERSION:
            raise ValueError('Extraction targeting version changed; start a new run')
        if target_words is not None and metadata.get('extraction_target_words') != target_words:
            raise ValueError('Resume requires the original extraction target size')
    if not metadata.get('extraction_complete') or not (output / 'candidates.jsonl').is_file():
        packets = read_jsonl(output / 'extraction_packets.jsonl')
        completed = [p for p in packets if p.get('status') == 'completed']
        if completed:
            return {**metadata, 'partial_extraction': True, 'completed_targets': len(completed), 'total_targets': len(packets)}
        raise ValueError('Extraction has not completed; start a new run')
    metadata = dict(metadata)
    metadata.pop('partial_extraction', None)
    metadata.pop('completed_targets', None)
    metadata.pop('total_targets', None)
    metadata = dict(metadata)
    metadata.pop('partial_extraction', None)
    metadata.pop('completed_targets', None)
    metadata.pop('total_targets', None)
    candidates = read_jsonl(output / 'candidates.jsonl')
    metadata = _recover_atomic_expansion(metadata,candidates,output)
    if metadata.get('imported_extraction') and metadata.get('expected_candidate_count') != len(candidates):
        raise ValueError('Saved candidate count does not match its saved summary')
    ids = [row.get('candidate_id') for row in candidates]
    if len(set(ids)) != len(ids) or any(not isinstance(identifier, str) for identifier in ids):
        raise ValueError('Saved candidate identifiers are missing or duplicated')
    if 'candidate_ids' in metadata and metadata['candidate_ids'] != ids:
        raise ValueError('Saved candidates do not match the extraction checkpoint')
    required = {'candidate_id', 'subject', 'relation', 'object', 'subject_type', 'object_type', 'schema_error'}
    if any(not required <= row.keys() or row.get('source_pdf_sha256') != metadata['pdf_sha256'] for row in candidates):
        raise ValueError('Saved candidates do not have complete matching PDF provenance')
    return metadata


def resume_available(output):
    """Cheap UI hint; the resume endpoint performs the full checks again."""
    try:
        metadata = read_metadata(output)
        if metadata and metadata.get('extraction_complete'):
            return True
        packets = read_jsonl(output / 'extraction_packets.jsonl')
        if packets and any(row.get('status') == 'completed' for row in packets):
            return True
        return bool(packets and (output / 'candidates.jsonl').is_file())
    except (ValueError, OSError):
        return False
