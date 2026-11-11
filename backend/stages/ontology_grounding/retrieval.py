"""Hybrid retrieval nominates definitions; scores never establish identity."""
import json
import re
from collections import defaultdict

import numpy as np


def words(value):
    return set(re.findall(r'\w+', str(value).casefold()))


class ConceptIndex:
    def __init__(self, catalog, encoder=None):
        self.catalog, self.encoder = catalog, encoder
        self.ids = sorted(i for i, t in catalog.terms.items() if t['selectable'] and t['kind'] == 'class' and not t['deprecated'])
        self.vectors = None
        if encoder and self.ids:
            vectors = np.asarray(encoder.encode_texts([self.text(i) for i in self.ids]), dtype=float)
            if vectors.ndim != 2 or not np.isfinite(vectors).all() or np.any(np.linalg.norm(vectors, axis=1) == 0):
                raise ValueError('Ontology embedding index returned invalid vectors')
            self.vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    def text(self, iri):
        t = self.catalog.terms[iri]
        return json.dumps({k: t[k] for k in ('label', 'definition', 'synonyms')}, ensure_ascii=False)

    def retrieve(self, name, mention, context, top_k=8):
        ranks, signals = defaultdict(float), defaultdict(list)
        queries = {str(name).strip().casefold(), str(mention).strip().casefold()} - {''}
        query_words = words(' '.join(queries))
        lexical = []
        for iri in self.ids:
            term = self.catalog.terms[iri]
            aliases = {term['label'].casefold(), *(s.casefold() for s in term['synonyms'])}
            exact_aliases = {term['label'].casefold(), *(s.casefold() for s in term['exact_synonyms'])}
            if queries & exact_aliases:
                ranks[iri] += 100
                signals[iri].append('exact_label_or_synonym')
            elif queries & aliases:
                signals[iri].append('non_equivalence_synonym')
            term_words = words(' '.join(aliases))
            score = len(query_words & term_words) / max(1, len(query_words | term_words))
            lexical.append((score, iri))
        for rank, (score, iri) in enumerate(sorted(lexical, reverse=True)):
            if score:
                ranks[iri] += 1 / (10 + rank)
                signals[iri].append('lexical')
        if self.vectors is not None:
            query_vectors = np.asarray(self.encoder.encode_texts([name + ' ' + mention, context]), dtype=float)
            norms = np.linalg.norm(query_vectors, axis=1, keepdims=True)
            if not np.isfinite(query_vectors).all() or np.any(norms == 0):
                raise ValueError('Ontology query embeddings are invalid')
            for channel, vector in zip(('semantic_name', 'semantic_context'), query_vectors / norms):
                order = np.argsort(-(self.vectors @ vector))[:top_k]
                for rank, index in enumerate(order):
                    iri = self.ids[index]
                    ranks[iri] += 1 / (10 + rank)
                    signals[iri].append(channel)
        selected = sorted(ranks, key=lambda iri: (-ranks[iri], iri))
        exact = [i for i in selected if 'exact_label_or_synonym' in signals[i]]
        selected = list(dict.fromkeys([*exact, *selected[:top_k]]))
        return [{**self.catalog.brief(i), 'retrieval_signals': signals[i],
                 'retrieval_is_identity_proof': False} for i in selected]
