"""Tests for the SQLite-backed publication database."""
import json
from pathlib import Path
import pytest

from backend.viewer.db import build_publication_db, PublicationDB, init_db_schema
from backend.viewer.export import export_snapshot
from backend.shared.storage import stable_hash


@pytest.fixture
def mock_snapshot(tmp_path):
    root = tmp_path / 'snapshot'
    root.mkdir()
    (root / 'global').mkdir()
    doc_id = stable_hash('Paper1.pdf', 16)
    doc_dir = root / 'documents' / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / 'jobs').mkdir()

    # Manifest
    manifest = {
        'format': 'research-viewer-v1',
        'snapshot_id': 'snap-123',
        'created_at': '2026-11-04T00:00:00Z',
        'files': {},
        'documents': {doc_id: {'claims': f'documents/{doc_id}/claims.json'}},
        'jobs': {},
        'graphs': {},
        'pathways': {},
        'warnings': []
    }
    (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    # Catalog
    catalog = [{
        'id': doc_id,
        'name': 'Paper1.pdf',
        'title': 'Study On Rice Health',
        'status': 'completed',
        'summary': {'extracted': 2},
        'document_scope': {'in_scope': True},
        'can_resume': False,
        'latest_job_id': 'job-1',
        'active_job_id': None,
        'pdf_available': False
    }]
    (root / 'catalog.json').write_text(json.dumps(catalog), encoding='utf-8')

    # Claims
    claims = {
        'claims': [
            {
                'candidate': {
                    'candidate_id': 'cand-1',
                    'subject': 'Rice',
                    'relation': 'contains',
                    'object': 'Iron',
                    'source_context_packet': 'A' * 2000
                },
                'validation': {'decision': 'entailed'}
            },
            {
                'candidate': {
                    'candidate_id': 'cand-2',
                    'subject': 'Iron',
                    'relation': 'prevents',
                    'object': 'Anemia',
                    'source_context_packet': 'B' * 100
                },
                'validation': {'decision': 'entailed'}
            }
        ],
        'extraction_rejections': []
    }
    (doc_dir / 'claims.json').write_text(json.dumps(claims), encoding='utf-8')

    # Job
    (doc_dir / 'jobs' / 'job-1.json').write_text(json.dumps({
        'id': 'job-1',
        'pdf_id': doc_id,
        'status': 'completed',
        'started_at': '2026-11-04T01:00:00Z'
    }), encoding='utf-8')

    # Pathway assessments
    pa = {
        'records': [
            {
                'pathway_id': 'pw-1',
                'eligible': True,
                'stale': False,
                'verdict': 'connection_supported',
                'scientific_verdict': 'supported',
                'target': {
                    'edges': [{'id': 'e1', 'source': 'Rice', 'target': 'Iron'}],
                    'document_keys': ['Study On Rice Health']
                }
            }
        ]
    }
    (root / 'global' / 'pathway-assessments.json').write_text(json.dumps(pa), encoding='utf-8')

    # Pathways validated
    pv = {
        'policy': 'neighbor-pair-v1',
        'graph': {
            'nodes': [{'id': 'Rice'}, {'id': 'Iron'}],
            'edges': [{'id': 'e1', 'source': 'Rice', 'target': 'Iron', 'document_key': 'Study On Rice Health'}]
        }
    }
    (root / 'global' / 'pathways-validated.json').write_text(json.dumps(pv), encoding='utf-8')

    return root, doc_id


def test_build_publication_db_and_read(mock_snapshot):
    root, doc_id = mock_snapshot
    db_path = build_publication_db(root)
    assert db_path.is_file()
    assert db_path.stat().st_size > 0

    db = PublicationDB(db_path)
    assert db.is_available()

    # Catalog check
    raw_cat = db.get_catalog_raw()
    assert raw_cat is not None
    cat = json.loads(raw_cat)
    assert len(cat) == 1
    assert cat[0]['id'] == doc_id

    # Document check
    doc = db.get_document(doc_id)
    assert doc is not None
    assert doc['title'] == 'Study On Rice Health'

    # Claims check & context packet truncation check
    claims_raw = db.get_claims_response_raw(doc_id)
    assert claims_raw is not None
    claims_data = json.loads(claims_raw)
    assert len(claims_data['claims']) == 2
    cand1 = claims_data['claims'][0]['candidate']
    assert len(cand1['source_context_packet']) == 500  # Truncated from 2000 to 500

    # Paginated claims check
    p_claims_raw = db.get_claims_response_raw(doc_id, limit=1, offset=0)
    p_data = json.loads(p_claims_raw)
    assert len(p_data['claims']) == 1
    assert p_data['claims'][0]['candidate']['candidate_id'] == 'cand-1'

    # Global pathways check
    page = db.get_pathways_page(limit=10, max_hops=6, offset=0, view='validated')
    assert page is not None
    assert len(page['paths']) == 1
    assert page['pathway_assessments'][0]['pathway_id'] == 'pw-1'

    # Jobs check
    jobs = db.get_jobs(doc_id)
    assert len(jobs) == 1
    assert jobs[0]['id'] == 'job-1'
    assert jobs[0]['status'] == 'completed'
