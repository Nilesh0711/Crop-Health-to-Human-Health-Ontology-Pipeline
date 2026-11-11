"""Exercise static GitHub Pages export without running models."""
import json

import pytest

from backend.shared.storage import stable_hash
from backend.viewer.static_export import STATIC_FORMAT, export_static_site


def save(path, value, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r) for r in value) if jsonl else json.dumps(value), encoding='utf-8')


@pytest.fixture
def static_workspace(tmp_path):
    ontology, pdfs, output = tmp_path / 'ontology', tmp_path / 'pdf', tmp_path / 'pages_data'
    folder = ontology / 'Study P'
    folder.mkdir(parents=True)
    pdfs.mkdir()
    (pdfs / 'Study P.pdf').write_bytes(b'%PDF-1.4\nfixture-static')
    pdf_id = stable_hash('Study P.pdf', 16)
    heavy_packet = 'x' * 5000
    candidate = {
        'candidate_id': 'cand-1',
        'subject': 'root',
        'relation': 'is part of',
        'object': 'root system',
        'source_page': 2,
        'source_evidence_span': 'A root is part of a root system.',
        'source_context_packet': heavy_packet,
        'source_reading_context': 'Long raw packet context',
        'source_evidence_records': [{'sentence_id': 1, 'text': 'A root is part of a root system.'}],
        'source_evidence_fragments': [{'fragment': 'A root'}],
        'qualifiers': {'crop_or_species': 'Solanum tuberosum'},
    }
    save(folder / 'candidates.jsonl', [candidate], jsonl=True)
    save(folder / 'summary.json', {'run_status': 'completed', 'extracted': 1})
    save(folder / 'run_state.json', {'id': 'job-p', 'status': 'completed'})
    save(folder / 'run_log.jsonl', [{'level': 'info', 'message': 'Token oc_sk_secret123', 'at': '2026-01-01'}], jsonl=True)
    save(folder / 'jobs' / 'job-p.json', {'id': 'job-p', 'pdf_id': pdf_id, 'status': 'completed'})

    from backend.ontology.catalog import catalog
    ref = catalog()
    snapshot = {
        'catalog_signature': ref.signature,
        'rules_signature': ref.rules_signature,
        'completed_at': '2026-11-11T00:00:00Z',
        'counts': {'claims': 1, 'entities': 2},
        'assertions': [
            {
                'candidate_id': 'cand-1',
                'document_key': 'Study P',
                'scope': 'in_scope',
                'scope_reason': 'Fits crop nutrition scope.',
                'original_claim': candidate,
                'bindings': {
                    'subject': {
                        'iri': 'http://purl.obolibrary.org/obo/PO_0009005',
                        'canonical_iri': 'http://purl.obolibrary.org/obo/PO_0009005',
                        'label': 'root',
                        'mapping': 'exact',
                        'denotation': 'concept',
                        'reason': 'Exact root match.',
                        'candidate_iris': ['http://purl.obolibrary.org/obo/PO_0009005'] * 50,
                    },
                    'object': {
                        'iri': 'http://purl.obolibrary.org/obo/PO_0025025',
                        'canonical_iri': 'http://purl.obolibrary.org/obo/PO_0025025',
                        'label': 'root system',
                        'mapping': 'exact',
                        'denotation': 'concept',
                        'reason': 'Exact root system match.',
                        'candidate_iris': ['http://purl.obolibrary.org/obo/PO_0025025'] * 50,
                    },
                    'relation': {
                        'iri': 'http://purl.obolibrary.org/obo/BFO_0000050',
                        'canonical_iri': 'http://purl.obolibrary.org/obo/BFO_0000050',
                        'label': 'part of',
                        'mapping': 'exact',
                        'reason': 'Exact part of relation.',
                    },
                },
            }
        ],
        'entities': [
            {
                'id': 'ent-1',
                'label': 'root',
                'iri': 'http://purl.obolibrary.org/obo/PO_0009005',
                'concept_label': 'root',
                'kind': 'concept',
                'mapping': 'exact',
                'status': 'grounded',
                'occurrences': [{'document_key': 'Study P', 'candidate_id': 'cand-1', 'role': 'subject', 'source_mention': 'root'}],
            }
        ],
    }
    save(ontology / '.ontology_grounding' / 'snapshot.json', snapshot)
    return ontology, pdfs, output, pdf_id


def test_static_export_prunes_bloat_and_preserves_contract(static_workspace):
    ontology, pdfs, output, pdf_id = static_workspace
    result = export_static_site(ontology, pdfs, output, include_pdfs=True)
    assert result['documents'] == 1

    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['format'] == STATIC_FORMAT

    health = json.loads((output / 'health.json').read_text(encoding='utf-8'))
    assert health['mode'] == 'static'
    assert health['capabilities'] == {'read_only': True, 'execute': False, 'upload': False}

    pdf_rows = json.loads((output / 'pdfs.json').read_text(encoding='utf-8'))
    assert pdf_rows[0]['pdf_available'] is True
    assert (output / 'pdfs' / pdf_id / 'source.pdf').read_bytes().startswith(b'%PDF-1.4')

    logs = json.loads((output / 'pdfs' / pdf_id / 'logs.json').read_text(encoding='utf-8'))
    assert logs[0]['message'] == 'Token [redacted credential]'

    assertions = json.loads((output / 'ontology-assertions.json').read_text(encoding='utf-8'))
    assert assertions['stale'] is False
    row = assertions['assertions'][0]
    assert 'source_context_packet' not in row['original_claim']
    assert 'source_local_context' not in row['original_claim']
    assert 'candidate_iris' not in row['bindings']['subject']
    assert row['original_claim']['source_evidence_span'] == 'A root is part of a root system.'

    claims = json.loads((output / 'pdfs' / pdf_id / 'claims.json').read_text(encoding='utf-8'))
    assert claims['total'] == 1
    assert 'source_context_packet' not in claims['claims'][0]['candidate']

    cat_data = json.loads((output / 'ontology-catalog.json').read_text(encoding='utf-8'))
    assert isinstance(cat_data.get('sources'), list)
    root_term = cat_data['terms']['http://purl.obolibrary.org/obo/PO_0009005']
    assert root_term['label'] == 'root'
    assert root_term['assertion_count'] == 1
    assert 'lineage_paths' not in root_term
    assert 'ancestors' not in root_term


def test_static_export_replace_and_no_pdf_flag(static_workspace):
    ontology, pdfs, output, pdf_id = static_workspace
    export_static_site(ontology, pdfs, output, include_pdfs=False)
    pdf_rows = json.loads((output / 'pdfs.json').read_text(encoding='utf-8'))
    assert pdf_rows[0]['pdf_available'] is False
    assert not (output / 'pdfs' / pdf_id / 'source.pdf').exists()

    with pytest.raises(ValueError, match='already exists'):
        export_static_site(ontology, pdfs, output)

    replaced = export_static_site(ontology, pdfs, output, include_pdfs=True, replace=True)
    assert replaced['previous_snapshot'] is not None
    assert (output / 'pdfs' / pdf_id / 'source.pdf').exists()
