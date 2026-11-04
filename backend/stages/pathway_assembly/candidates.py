"""Retrieve contextual identity candidates; similarities never authorize merges."""
from collections import defaultdict
import json
import re
import unicodedata

import numpy as np

from backend.shared.cancellation import check_cancelled
from backend.shared.evidence_index import EvidenceIndex
from backend.shared.storage import stable_hash
from backend.stages.evidence_ranking.passages import SemanticReranker


CANDIDATE_POLICY = 'contextual-endpoint-candidates-v3'
SMALL_CONTEXT_CHARACTERS = 48000


def matching_settings(settings):
    return {'policy': CANDIDATE_POLICY, 'strategy': settings.pathway_candidate_strategy,
            'embedding_model': settings.embedding_model, 'top_k': settings.pathway_candidate_top_k,
            'expansion_k': settings.pathway_candidate_expansion_k,
            'small_registry': settings.pathway_candidate_small_registry,
            'small_context_characters': SMALL_CONTEXT_CHARACTERS}


def matching_signature(settings):
    return stable_hash(json.dumps(matching_settings(settings), sort_keys=True), 64)


def _name(value):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', str(value)).casefold()))


def _context(row):
    # Keep names/roles/restrictions before source text when the encoder truncates.
    # Full source context still reaches the LLM for every selected registry node.
    return json.dumps({k: row.get(k) for k in ('name', 'source_mention', 'role', 'triplet',
        'claim_text', 'qualifiers', 'section', 'evidence', 'local_context')},
        ensure_ascii=False, separators=(',', ':'))


class EndpointCandidateIndex:
    """Exact flat cosine index plus inverted names/context; no vector DB service.

    Both new and existing occurrences are indexed before resolving any new batch.
    Reciprocal new/new candidates prevent source order alone excluding a proposed
    match. Approximate semantic correctness/recall is never inferred from a score.
    """

    def __init__(self, endpoints, pending_ids, settings, root, *, encoder=None):
        self.rows = endpoints
        self.by_id = {row['endpoint_id']: i for i, row in enumerate(endpoints)}
        self.ranks = {identifier: {} for identifier in pending_ids}
        self.settings = settings
        encoder = encoder or SemanticReranker(settings)
        if settings.evidence_index_enabled:
            encoder.evidence_index = EvidenceIndex(root / '.evidence_index',
                max_bytes=settings.evidence_index_max_mb * 1024 * 1024,
                ttl_seconds=settings.evidence_index_ttl_hours * 3600)
        names = [' '.join(dict.fromkeys(filter(None, [row['name'], row.get('source_mention', '')])))
                 for row in endpoints]
        vectors = np.asarray(encoder.encode_texts([*names, *[_context(row) for row in endpoints]]), dtype=np.float32)
        if (vectors.ndim != 2 or len(vectors) != 2 * len(endpoints) or vectors.shape[1] < 1
                or not np.isfinite(vectors).all()):
            raise ValueError('Endpoint embedding index received invalid vectors; no identity decision was made')
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or np.any(norms <= 0):
            raise ValueError('Endpoint embedding index received zero vectors; no identity decision was made')
        vectors = vectors / norms
        self._build(pending_ids, names, vectors[:len(endpoints)], vectors[len(endpoints):])
        self.metadata = {'policy': CANDIDATE_POLICY, 'backend': 'exact-flat-cosine',
            'corpus_fingerprint': stable_hash(json.dumps(endpoints, ensure_ascii=False, sort_keys=True), 64),
            'occurrences': len(endpoints), 'pending_occurrences': len(pending_ids),
            'channels': ['name_vector', 'context_vector', 'lexical', 'exact_name', 'shared_source_context'],
            'similarity_is_identity_proof': False, 'embedding_cache_hits': encoder.embedding_cache_hits}
        self.metadata['embedding_identity'] = getattr(encoder, 'embedding_identity', None)

    def _add(self, identifier, partner, signal, rank):
        if identifier == partner:
            return
        if ':' in identifier and ':' in partner:
            if identifier.rsplit(':', 1)[0] == partner.rsplit(':', 1)[0]:
                return
        signals = self.ranks[identifier].setdefault(partner, {})
        signals[signal] = min(rank, signals.get(signal, rank))

    def _build(self, pending_ids, names, name_vectors, context_vectors):
        exact, words, source = defaultdict(set), defaultdict(set), defaultdict(set)
        token_sets = []
        for i, row in enumerate(self.rows):
            tokens = set(_name(names[i]).split())
            token_sets.append(tokens)
            for value in (row['name'], row.get('source_mention', '')):
                if _name(value):
                    exact[_name(value)].add(i)
            for token in tokens:
                words[token].add(i)
            text = row.get('local_context') or row.get('evidence')
            if text:
                source[(row.get('document_key'), text)].add(i)
        limit = self.settings.pathway_candidate_expansion_k
        for start in range(0, len(pending_ids), self.settings.embedding_batch_size):
            check_cancelled()
            batch = pending_ids[start:start + self.settings.embedding_batch_size]
            positions = [self.by_id[identifier] for identifier in batch]
            for channel, matrix in (('name_vector', name_vectors), ('context_vector', context_vectors)):
                scores = matrix[positions] @ matrix.T
                for identifier, i, row_scores in zip(batch, positions, scores):
                    row_scores[i] = -np.inf
                    order = np.lexsort((np.arange(len(self.rows)), -row_scores))[:min(limit, len(self.rows) - 1)]
                    for rank, j in enumerate(order, 1):
                        self._add(identifier, self.rows[j]['endpoint_id'], channel, rank)
            for identifier, i in zip(batch, positions):
                row = self.rows[i]
                for value in (row['name'], row.get('source_mention', '')):
                    for j in exact.get(_name(value), ()):
                        self._add(identifier, self.rows[j]['endpoint_id'], 'exact_name', 0)
                text = row.get('local_context') or row.get('evidence')
                for j in source.get((row.get('document_key'), text), ()):
                    self._add(identifier, self.rows[j]['endpoint_id'], 'shared_source_context', 0)
                possible = set().union(*(words[token] for token in token_sets[i])) - {i}
                lexical = sorted(possible, key=lambda j: (
                    -len(token_sets[i] & token_sets[j]) / max(1, len(token_sets[i] | token_sets[j])), j))[:limit]
                for rank, j in enumerate(lexical, 1):
                    self._add(identifier, self.rows[j]['endpoint_id'], 'lexical', rank)
        # Freeze forward results before adding their reciprocal new/new links.
        links = [(a, b, dict(signals)) for a, partners in self.ranks.items()
                 for b, signals in partners.items() if b in self.ranks]
        for a, b, signals in links:
            for channel, rank in signals.items():
                self._add(b, a, 'reciprocal_' + channel, rank)

    def select(self, batch, assignments, registry, k):
        selected, evidence = set(), {}
        for row in batch:
            identifier = row['endpoint_id']
            opposite_partner = None
            if ':' in identifier:
                edge_id, role = identifier.rsplit(':', 1)
                if role in ('subject', 'object'):
                    opposite_role = 'object' if role == 'subject' else 'subject'
                    opposite_partner = f"{edge_id}:{opposite_role}"
            hits = []
            for partner, signals in sorted(self.ranks[identifier].items()):
                accepted = {channel: rank for channel, rank in signals.items() if rank <= k}
                if not accepted:
                    continue
                node = assignments.get(partner)
                if node in registry:
                    if opposite_partner and opposite_partner in registry[node].get('endpoint_ids', []):
                        continue
                    selected.add(node)
                hits.append({'endpoint_id': partner, 'node_id': node, 'signals': accepted})
            evidence[identifier] = hits
        return selected, evidence
