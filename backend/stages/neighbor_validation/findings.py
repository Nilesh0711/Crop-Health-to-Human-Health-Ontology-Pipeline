"""Exact, versioned claim-paper findings; similarity never authorizes reuse."""
from copy import deepcopy
from threading import RLock, BoundedSemaphore
from backend.stages.neighbor_validation.targets import POLICY, ASSESSMENT_CONTRACT, signature, claim_signature


def paper_input(document):
    """Scientific text/provenance, excluding query-specific ranking decorations."""
    result = {k:document.get(k) for k in ('evidence_document_id','title','doi','publication_year',
        'source_reference_number','evidence_access','evidence_level','independence_group')}
    fields = ('chunk_id','text','page_number','section_heading','source_kind','table_id',
              'table_headers','table_cells','table_footnotes','paragraph_references','figure_id','layout_verified')
    result['evidence_chunks'] = [{k:c[k] for k in fields if k in c}
        for c in sorted(document['evidence_chunks'],key=lambda c:c['chunk_id'])]
    return result


class FindingCache:
    def __init__(self, settings, persistent=None, assessment_id=None):
        self.settings, self.persistent = settings, persistent
        self.assessment_id = assessment_id
        self.memory, self.lock, self.hits = {}, RLock(), 0
        self.paper_slots = BoundedSemaphore(settings.external_paper_workers)

    def _key(self, original, edge, document):
        return 'neighbor-claim-paper:' + signature({'policy': POLICY,
            'assessment_contract': ASSESSMENT_CONTRACT,
            'assessment_id': self.assessment_id,
            'claim': claim_signature(original),
            'mapping': {k: edge.get(k) for k in ('source', 'target', 'source_label', 'target_label')},
            'paper': paper_input(document),
            'model': self.settings.external_paper_model, 'thinking': self.settings.task_thinking['external']})

    def lookup(self, target, document):
        result = {}
        with self.lock:
            for original, edge in zip(target['source_claims'], target['edges']):
                key = self._key(original, edge, document)
                value = self.memory.get(key)
                if value is None and self.persistent:
                    value = self.persistent.get(key, expiring=False)
                if value is None:
                    continue
                value = deepcopy(value)
                value['finding']['edge_id'] = edge['id']
                # Request-local proof names may collide between independently assessed claims.
                # Rename identifiers losslessly; exact paper/chunk/sentence grounding follows.
                remap = {p['proof_id']: 'retained-' + signature({
                    'paper': document['evidence_document_id'], 'chunk': p['chunk_id'],
                    'sentences': p['sentence_numbers']})[:24] for p in value['proofs']}
                for proof in value['proofs']:
                    proof['proof_id'] = remap[proof['proof_id']]
                value['finding']['proof_ids'] = [remap[i] for i in value['finding']['proof_ids']]
                for calculation in value.get('calculations',[]):
                    calculation['proof_id'] = remap[calculation['proof_id']]
                result[edge['id']] = value
                self.hits += 1
        return result

    def store(self, target, document, grounded):
        by_id = {r['edge_id']: r for r in grounded['edge_findings']}
        proofs = {p['proof_id']: p for p in grounded['proofs']}
        with self.lock:
            for original, edge in zip(target['source_claims'], target['edges']):
                finding = by_id[edge['id']]
                value = {'finding': deepcopy(finding),
                         'proofs': [deepcopy(proofs[i]) for i in dict.fromkeys(finding['proof_ids'])]}
                calculations = [c for c in grounded.get('calculations',[]) if c['proof_id'] in finding['proof_ids']]
                if calculations:
                    value['calculations'] = deepcopy(calculations)
                key = self._key(original, edge, document)
                self.memory[key] = value
                if self.persistent:
                    self.persistent.put(key, value)
