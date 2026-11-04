"""Indexed candidate coverage, model-owned identity, and durable bounded scans."""
from copy import deepcopy
from dataclasses import replace
import importlib
import json

import numpy as np
import pytest

from backend.config.settings import Settings
from backend.shared.storage import read_jsonl
from backend.shared.artifacts import save_rows
from backend.stages.pathway_assembly.candidates import EndpointCandidateIndex, matching_signature
from backend.stages.pathway_assembly.assembly import assemble_pathways, read_assembly
from backend.stages.pathway_assembly.corpus import corpus_signature, collect_corpus
from tests.backend.test_pathway_assembly import saved_claim, Aligner


class SyntheticEncoder:
    """Known vectors for isolated matching contracts, not a scientific benchmark."""
    embedding_cache_hits = 0

    def __init__(self, aliases=None, supplied=None):
        self.aliases, self.supplied, self.names = aliases or {}, supplied, {}

    def encode_texts(self, texts):
        result = []
        for text in texts:
            name = json.loads(text)['name'] if text.startswith('{') else text
            name = self.aliases.get(name, name)
            if self.supplied:
                result.append(self.supplied[name])
            else:
                vector = np.zeros(256, dtype=np.float32)
                vector[self.names.setdefault(name, len(self.names))] = 1
                result.append(vector)
        return result


def endpoint(identifier, name, **extra):
    return dict(endpoint_id=identifier, name=name, role='subject', source_mention='',
                document_key=identifier, evidence=name, local_context=name,
                triplet={'subject': name, 'relation': 'affects', 'object': 'Process Q'},
                claim_text=name + ' affects Process Q.', qualifiers={'condition': 'Condition K'}, **extra)


def settings(**extra):
    return replace(Settings(), evidence_index_enabled=False, pathway_candidate_top_k=1,
                   pathway_candidate_expansion_k=3, pathway_candidate_small_registry=0,
                   pathway_assembly_batch_size=2, **extra)


def test_contextual_alias_candidates_cross_roles_and_provisional_types(tmp_path):
    rows = [endpoint('new', 'affected material', entity_type='Unresolved'),
            {**endpoint('old', 'Crop C material', entity_type='Crop'), 'role': 'object'},
            endpoint('other', 'Compound T')]
    original = deepcopy(rows)
    index = EndpointCandidateIndex(rows, ['new'], settings(), tmp_path,
        encoder=SyntheticEncoder({'affected material': 'Crop C material'}))
    selected, evidence = index.select([rows[0]], {'old': 'node-old'}, {'node-old': {}}, 1)
    assert selected == {'node-old'} and rows == original
    assert evidence['new'][0]['signals']
    assert index.metadata['similarity_is_identity_proof'] is False


def test_new_new_candidates_are_reciprocal_before_batches_are_processed(tmp_path):
    rows = [endpoint('A', 'alpha'), endpoint('B', 'beta'), endpoint('C', 'gamma')]
    encoder = SyntheticEncoder(supplied={'alpha': [1., 0.], 'beta': [.8, .6], 'gamma': [1., -.01]})
    index = EndpointCandidateIndex(rows, ['A', 'B', 'C'], settings(), tmp_path, encoder=encoder)
    # A's nearest neighbour is C; B's nearest is A. Reverse retrieval keeps A/B
    # eligible even if their claim order is reversed.
    selected, evidence = index.select([rows[0]], {'B': 'node-B'}, {'node-B': {}}, 1)
    assert selected == {'node-B'}
    assert any('reciprocal_name_vector' in row['signals'] for row in evidence['A'] if row['endpoint_id'] == 'B')


def test_exact_aliases_and_same_source_context_are_not_lost_to_top_k(tmp_path):
    rows = [endpoint('new', 'shared name')]
    rows += [endpoint(str(i), 'shared name') for i in range(6)]
    rows.append({**endpoint('context', 'different wording'), 'document_key': 'new', 'local_context': 'shared name'})
    index = EndpointCandidateIndex(rows, ['new'], settings(), tmp_path, encoder=SyntheticEncoder())
    assignments = {row['endpoint_id']: row['endpoint_id'] for row in rows[1:]}
    selected, _ = index.select([rows[0]], assignments, {key: {} for key in assignments}, 1)
    assert selected == set(assignments)


def test_bad_vectors_fail_visibly_and_settings_are_bounded(tmp_path):
    rows = [endpoint('one', 'one')]
    with pytest.raises(ValueError, match='zero vectors'):
        EndpointCandidateIndex(rows, ['one'], settings(), tmp_path,
            encoder=SyntheticEncoder(supplied={'one': [0., 0.]}))
    with pytest.raises(ValueError, match='top-k'):
        replace(Settings(), pathway_candidate_top_k=31, pathway_candidate_expansion_k=30)


@pytest.fixture
def synthetic_embedding(monkeypatch):
    def encode(self, texts):
        return SyntheticEncoder().encode_texts(texts)
    monkeypatch.setattr('backend.stages.evidence_ranking.passages.SemanticReranker.encode_texts', encode)


def corpus(root, count=16):
    for i in range(count):
        folder, claim = saved_claim(root, f'paper-{i:02}', f'Entity {i}', 'affects', f'Entity {i + 1}')
        claim.update(claim_text=f'Entity {i} affects Entity {i + 1} under Condition K.')
        claim['source_validation']['project_scope'] = {'decision': 'in_scope', 'rationale': 'Synthetic scope fixture.'}
        save_rows(folder / 'candidates.jsonl', [claim])


def test_indexed_and_exhaustive_agree_on_known_chain_and_reduce_registry_context(tmp_path, synthetic_embedding):
    indexed_root, exhaustive_root = tmp_path / 'indexed', tmp_path / 'exhaustive'
    corpus(indexed_root); corpus(exhaustive_root)
    indexed_client, exhaustive_client = Aligner(), Aligner()
    indexed = assemble_pathways(indexed_root, indexed_client, settings(), provisional=True)
    exhaustive = assemble_pathways(exhaustive_root, exhaustive_client,
        replace(settings(), pathway_candidate_strategy='exhaustive'), provisional=True)
    scientific = lambda value: [(e['source_label'], e['relation'], e['target_label'], e['qualifiers'])
                                for e in value['graph']['edges']]
    assert scientific(indexed) == scientific(exhaustive)
    assert indexed['counts']['entities'] == 17
    assert sum(len(c['registry_nodes']) for c in indexed_client.calls) < sum(len(c['registry_nodes']) for c in exhaustive_client.calls)
    assert any('claim_text_ref' in row for call in indexed_client.calls for row in call['incoming_endpoints'])
    assert indexed['candidate_selection']['exhaustive'] is False
    assert indexed['candidate_selection']['registry_entities_selected'] < indexed['candidate_selection']['registry_entities_considered']
    assert all('unsearched_registry_entities' in row for row in indexed['candidate_selection']['history'])
    calls = len(indexed_client.calls)
    assert assemble_pathways(indexed_root, indexed_client, settings(), provisional=True) == indexed
    assert len(indexed_client.calls) == calls


def test_interrupted_indexed_scan_resumes_and_keeps_original_claims(tmp_path, synthetic_embedding):
    corpus(tmp_path, 4)
    original = {str(path): path.read_bytes() for path in tmp_path.glob('*/candidates.jsonl')}
    from backend.providers.llm import LLMServiceError
    with pytest.raises(LLMServiceError):
        assemble_pathways(tmp_path, Aligner(fail_at=3), settings(), provisional=True)
    state = json.loads((tmp_path / '.provisional_pathways/checkpoint.json').read_text())
    assert state['cursor'] > 0 and state['status'] == 'failed'
    resumed_client = Aligner()
    resumed = assemble_pathways(tmp_path, resumed_client, settings(), provisional=True)
    assert resumed['counts']['claims'] == 4 and resumed['counts']['entities'] == 5
    assert original == {str(path): path.read_bytes() for path in tmp_path.glob('*/candidates.jsonl')}
    assert json.loads((tmp_path / '.provisional_pathways/candidate_index.json').read_text())['backend'] == 'exact-flat-cosine'


def test_completed_scan_is_not_repeated_after_checkpoint_boundary(tmp_path, synthetic_embedding, monkeypatch):
    corpus(tmp_path, 2)
    module = importlib.import_module('backend.stages.pathway_assembly.assembly')
    original = module.save_json
    interrupted = False
    def save(path, state):
        nonlocal interrupted
        original(path, state)
        if path.name == 'checkpoint.json' and state.get('scan_complete') and not interrupted:
            interrupted = True
            raise RuntimeError('Simulated process interruption after durable model result')
    monkeypatch.setattr(module, 'save_json', save)
    first = Aligner()
    with pytest.raises(RuntimeError, match='interruption'):
        assemble_pathways(tmp_path, first, settings(), provisional=True)
    monkeypatch.setattr(module, 'save_json', original)
    resumed = Aligner()
    snapshot = assemble_pathways(tmp_path, resumed, settings(), provisional=True)
    assert snapshot['counts']['entities'] == 3
    assert len(first.calls) + len(resumed.calls) == 2  # Two endpoint batches; completed first call not replayed.


def test_unmatched_endpoints_expand_once_and_resume_inside_expansion(tmp_path, monkeypatch):
    corpus(tmp_path, 2)
    module = importlib.import_module('backend.stages.pathway_assembly.assembly')
    class Selector:
        metadata = {'backend': 'synthetic-contract-test'}
        def __init__(self, *args): pass
        def select(self, batch, assignments, registry, k):
            if k > 1:
                return set(registry), {}
            wrong = min(registry, key=lambda node: registry[node]['label']) if registry else None
            evidence = {row['endpoint_id']: ([{'node_id': wrong, 'signals': {'exact_name': 0}}] if wrong else []) for row in batch}
            return ({wrong} if wrong else set()), evidence
    monkeypatch.setattr(module, 'EndpointCandidateIndex', Selector)
    from backend.providers.llm import LLMServiceError
    first = Aligner(fail_at=3)
    with pytest.raises(LLMServiceError):
        assemble_pathways(tmp_path, first, settings(), provisional=True)
    state = json.loads((tmp_path / '.provisional_pathways/checkpoint.json').read_text())
    assert state['selection_plan']['round'] == 1
    resumed_client = Aligner()
    snapshot = assemble_pathways(tmp_path, resumed_client, settings(), provisional=True)
    assert snapshot['counts']['entities'] == 3
    assert len(resumed_client.calls) == 1
    assert snapshot['candidate_selection']['history'][-1]['round'] == 1
    assert snapshot['candidate_selection']['exhaustive']


def test_even_identical_embeddings_cannot_merge_entities_without_model_selection(tmp_path, monkeypatch):
    corpus(tmp_path, 2)
    monkeypatch.setattr('backend.stages.evidence_ranking.passages.SemanticReranker.encode_texts',
                        lambda self, texts: [[1., 0.] for _ in texts])
    class RejectAll:
        def chat_json(self, system, user, **kwargs):
            data = json.loads(user)
            answer = {'endpoint_assignments': {}, 'registry_assignments': {n['node_id']: None for n in data['registry_nodes']}, 'groups': []}
            for number, endpoint in enumerate(data['incoming_endpoints'], 1):
                answer['endpoint_assignments'][endpoint['endpoint_id']] = number
                if endpoint.get('provisional_node_id'):
                    answer['registry_assignments'][endpoint['provisional_node_id']] = number
                answer['groups'].append({'group_id': number, 'canonical_label': endpoint['name'],
                                         'joins_existing_registry_node': bool(endpoint.get('provisional_node_id')),
                                         'explanation': 'The supplied contexts do not establish shared identity.'})
            kwargs['response_check'](answer)
            return answer
    result = assemble_pathways(tmp_path, RejectAll(), settings(), provisional=True)
    assert result['counts']['entities'] == 4
    assert result['graph']['edges'][0]['target'] != result['graph']['edges'][1]['source']


def test_strategy_and_candidate_budgets_invalidate_selection_checkpoint_identity():
    assert matching_signature(settings()) != matching_signature(replace(settings(), pathway_candidate_top_k=2))
    assert matching_signature(settings()) != matching_signature(replace(settings(), pathway_candidate_strategy='exhaustive'))


def test_legacy_complete_registry_stays_readable_and_can_seed_unchanged_identity(tmp_path, synthetic_embedding):
    corpus(tmp_path, 2)
    result = assemble_pathways(tmp_path, Aligner(), settings(), provisional=True)
    for row in result['endpoints']:
        row.pop('claim_text', None)
    result.pop('matching_signature')
    edges, _ = collect_corpus(tmp_path, provisional=True)
    result['input_signature'] = corpus_signature(edges, result['endpoints'], settings().pathway_assembly_model,
                                                settings().task_thinking['pathway_assembly'], provisional=True)
    from backend.shared.artifacts import save_json
    save_json(tmp_path / '.provisional_pathways/snapshot.json', result)
    view = read_assembly(tmp_path, settings().pathway_assembly_model, provisional=True)
    assert not view['stale'] and len(view['snapshot']['graph']['edges']) == 2
    (tmp_path / '.provisional_pathways/checkpoint.json').unlink()
    client = Aligner()
    restored = assemble_pathways(tmp_path, client, settings(), provisional=True)
    assert not client.calls and restored['candidate_selection']['reused_endpoint_count'] == 4
    folder = tmp_path / 'paper-00'
    claim = read_jsonl(folder / 'candidates.jsonl')[0]
    claim['claim_text'] += ' Changed assertion.'
    save_rows(folder / 'candidates.jsonl', [claim])
    view = read_assembly(tmp_path, settings().pathway_assembly_model, provisional=True)
    assert view['stale'] and len(view['snapshot']['graph']['edges']) == 1


def test_context_changes_alter_index_fingerprint_and_retrieve_fresh_vectors(tmp_path):
    rows = [endpoint('new', 'Material R'), endpoint('old', 'Material S')]
    first = EndpointCandidateIndex(rows, ['new'], settings(), tmp_path, encoder=SyntheticEncoder())
    changed = deepcopy(rows); changed[0]['qualifiers']['condition'] = 'Different treatment'
    second = EndpointCandidateIndex(changed, ['new'], settings(), tmp_path, encoder=SyntheticEncoder())
    assert first.metadata['corpus_fingerprint'] != second.metadata['corpus_fingerprint']


def test_small_registry_avoids_embedding_warmup_and_extra_judgements(tmp_path, monkeypatch):
    corpus(tmp_path, 3)
    def unexpected(*args, **kwargs):
        raise AssertionError('Small complete registry should not load embedding models')
    monkeypatch.setattr('backend.stages.evidence_ranking.passages.SemanticReranker.encode_texts', unexpected)
    client = Aligner()
    result = assemble_pathways(tmp_path, client,
        replace(settings(), pathway_candidate_small_registry=64), provisional=True)
    assert len(client.calls) == 3 and result['candidate_selection']['exhaustive']
    assert result['candidate_selection']['index'] is None


def test_incremental_assembly_preserves_inherited_bounded_coverage(tmp_path, synthetic_embedding, monkeypatch):
    corpus(tmp_path, 16)
    previous = assemble_pathways(tmp_path, Aligner(), settings(), provisional=True)
    assert previous['candidate_selection']['exhaustive'] is False
    corpus(tmp_path, 17)
    monkeypatch.setattr('backend.stages.pathway_assembly.assembly._small_registry_fits', lambda *args: True)
    result = assemble_pathways(tmp_path, Aligner(), settings(), provisional=True)
    coverage = result['candidate_selection']
    assert coverage['reused_endpoint_count'] == 32
    assert all(row['exhaustive'] for row in coverage['history'])
    assert coverage['inherited_candidate_coverage']['exhaustive'] is False
    assert coverage['exhaustive'] is False
