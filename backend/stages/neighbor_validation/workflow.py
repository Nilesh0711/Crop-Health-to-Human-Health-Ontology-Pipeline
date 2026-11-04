"""Resumable neighboring-pair assessment with shared paper-bound findings."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import replace
import json
import time
import threading
import re

from backend.shared.artifacts import save_json
from backend.shared.storage import utc_now
from backend.shared.cancellation import check_cancelled, submit_work
from backend.providers.structured_output import EvidenceSelectionError
from backend.stages.neighbor_validation.targets import POLICY, ASSESSMENT_CONTRACT, signature
from backend.stages.neighbor_validation.judgement import judge_paper, synthesize, ground_paper, check_synthesis
from backend.stages.neighbor_validation.proof_audit import audit_support, audit_signature, followup_assessment
from backend.stages.neighbor_validation.retrieval import retrieve_pathway
from backend.stages.query_planning.neighbors import translate_pathway, followup_queries


def _read(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def config_signature(target, settings, assessment_id=None):
    return signature({'policy': POLICY, 'assessment_contract': ASSESSMENT_CONTRACT,
        'target': target['input_signature'], 'models': {
        'query':settings.query_model,'paper':settings.external_paper_model,'synthesis':settings.external_model},
        'thinking': {k:settings.task_thinking[k] for k in ('query','external')},
        'retrieval': [settings.external_top_k,settings.external_top_chunks,settings.embedding_model,
                      settings.embedding_query_instruction,settings.cross_encoder_model,
                      settings.cross_encoder_candidates,settings.max_fulltext_bytes,
                      settings.pathway_followup_papers],
        'generation': [settings.external_input_max_characters,settings.external_min_output_budget],
                **({'assessment_id': assessment_id} if assessment_id else {})})


def _merge_document(previous, document):
    """Merge a repeated paper while preserving its immutable chunk identities."""
    chunks = {c['chunk_id']: c for c in previous['evidence_chunks']}
    for chunk in document['evidence_chunks']:
        if chunk['chunk_id'] in chunks and chunks[chunk['chunk_id']]['text'] != chunk['text']:
            raise ValueError('Pathway follow-up changed text under an existing paper/chunk identity')
        chunks.setdefault(chunk['chunk_id'], chunk)
    return {**document, 'evidence_document_id': previous['evidence_document_id'],
            'evidence_chunks': list(chunks.values()),
            'retrieval_alias_ids': sorted({previous['evidence_document_id'], document['evidence_document_id'],
                                          *previous.get('retrieval_alias_ids', [])}),
            'source_reference_ids': sorted(set(previous.get('source_reference_ids', [])) |
                                           set(document.get('source_reference_ids', [])))}


class _PaperAssessmentSession:
    """Own paper-ready work and checkpoints across the initial/follow-up rounds."""

    def __init__(self, target, client, settings, directory, record, publish, lock, finding_cache):
        self.target, self.client, self.settings = target, client, settings
        self.directory, self.record, self.publish, self.lock = directory, record, publish, lock
        self.finding_cache = finding_cache
        self.papers, self.documents = {}, {}
        self.paper_errors, self.latest_paths, self.callback_errors = {}, {}, {}
        self.stats = {'reused_edge_findings': 0, 'paper_calls': 0, 'reused_paper_calls': 0, 'synthesis_calls': 0,
                      'reused_synthesis_calls':0, 'followup_rounds': 0, 'proof_audit_calls':0, 'reused_proof_audits':0}

    def _paper_path(self, document):
        from backend.stages.neighbor_validation.findings import paper_input
        scientific = paper_input(document)
        key = signature({'policy': POLICY, 'assessment_contract': ASSESSMENT_CONTRACT,
                         'target': self.target['input_signature'], 'document': scientific,
                         'model': self.settings.external_paper_model, 'thinking': self.settings.task_thinking['external']})
        return self.directory / 'paper_assessments' / (key + '.json')

    def _publish_finding(self, identifier, finding):
        self.papers[identifier] = finding
        self.paper_errors.pop(identifier, None)
        self.record['paper_assessments'] = list(self.papers.values())
        self.record['evidence_documents'] = list(self.documents.values())
        self.record['paper_assessment_errors'] = list(self.paper_errors.values())
        self.publish()

    def _store(self, future, identifier, path):
        if future.cancelled():
            return
        error = future.exception()
        if error is not None and not isinstance(error, EvidenceSelectionError):
            return  # The pipeline owner must propagate provider/storage/programming errors.
        with self.lock:
            if path in self.stored:
                return
            if error is not None:
                if self.latest_paths.get(identifier) == path:
                    self.paper_errors[identifier] = {'evidence_document_id':identifier,
                        'title':self.documents[identifier].get('title'),
                        'status':'evidence_needs_review', 'error':str(error),
                        'review_response':getattr(error,'review_response',None)}
                    self.record['paper_assessment_errors'] = list(self.paper_errors.values())
                    self.record['evidence_documents'] = list(self.documents.values())
                    self.publish()
                self.stored.add(path)
                return
            finding = future.result()
            self.stats['reused_edge_findings'] += finding.get('reused_edge_findings', 0)
            save_json(path, finding)
            # A slower initial packet must not overwrite a newer expanded packet
            # for the same paper just because it finishes last.
            if self.latest_paths.get(identifier) == path:
                self._publish_finding(identifier, finding)
            self.stored.add(path)

    def _completed(self, future, identifier, path):
        try:
            self._store(future, identifier, path)
        except BaseException as exc:
            # Future callbacks do not propagate exceptions to future.result().
            # Preserve checkpoint failures for the owner instead of logging and losing them.
            with self.lock:
                self.callback_errors[path] = exc

    def _ready(self, document):
        with self.lock:
            check_cancelled()
            from backend.stages.literature_retrieval.workflow import _normal_identifier
            doi = _normal_identifier(document.get('doi'))
            previous = self.documents.get(document['evidence_document_id']) or next((d for d in self.documents.values()
                if doi and _normal_identifier(d.get('doi')) == doi), None)
            if previous:
                document = _merge_document(previous, document)
            identifier = document['evidence_document_id']
            self.documents[identifier] = document
            path = self._paper_path(document)
            self.latest_paths[identifier] = path
            if path in self.seen:
                return
            self.seen.add(path)
            old = _read(path)
            if old is not None:
                finding = ground_paper(self.target, document, old)
                if self.finding_cache:
                    self.finding_cache.store(self.target, document, finding)
                self.stats['reused_paper_calls'] += 1
                self._publish_finding(identifier, finding)
            else:
                future = submit_work(self.workers, judge_paper, self.client, self.target,
                                     document, self.settings, self.finding_cache)
                self.pending[future] = (identifier, path)
                self.stats['paper_calls'] += 1
                future.add_done_callback(lambda f: self._completed(f, identifier, path))

    def run_round(self, query, round_settings, external_client, ranker, cache, progress):
        retrieval_key = signature({'policy': POLICY, 'assessment_contract': ASSESSMENT_CONTRACT,
            'target': self.target['input_signature'], 'queries': query,
            'selection': [round_settings.external_top_k, round_settings.external_top_chunks],
            'models': [self.settings.embedding_model, self.settings.cross_encoder_model]})
        retrieval_path = self.directory / 'retrieval_packets' / (retrieval_key + '.json')
        retained = _read(retrieval_path)
        self.pending, self.seen, self.stored = {}, set(), set()
        with ThreadPoolExecutor(max_workers=self.settings.external_paper_workers, thread_name_prefix='pathway-paper') as workers:
            self.workers = workers
            if retained and not retained.get('retrieval_pending'):
                result = retained
                for document in result['evidence_documents']:
                    self._ready(document)
            else:
                result = retrieve_pathway(self.target, query, external_client, ranker, round_settings, cache, progress, self._ready)
                save_json(retrieval_path, result)
            for future in as_completed(self.pending):
                check_cancelled()
                identifier, path = self.pending[future]
                if not isinstance(future.exception(), EvidenceSelectionError):
                    future.result()  # Provider errors still propagate on the pipeline owner.
                self._store(future, identifier, path)
                with self.lock:
                    if path in self.callback_errors:
                        raise self.callback_errors[path]
        return result


def _publish_record(record, folder, checkpoint, lock):
    with lock:
        paper_ids = {p['evidence_document_id'] for p in record['paper_assessments']}
        used_aliases = {identifier for document in record.get('evidence_documents', [])
                       if document['evidence_document_id'] in paper_ids
                       for identifier in [document['evidence_document_id'], *document.get('retrieval_alias_ids', [])]}
        for packet in record['retrieval_rounds']:
            for citation in packet.get('citation_ledger', []):
                if used_aliases.intersection(citation.get('evidence_document_ids', [])):
                    citation.update(included_in_judgement=True, status='included_in_judgement')
        record['updated_at'] = utc_now()
        save_json(folder / 'assessment.json', record)
        checkpoint(deepcopy(record))


def assess_pathway(target, query_client, external_client, ranker, settings, cache, directory,
                   checkpoint, progress=lambda message: None, finding_cache=None, *, assessment_id=None):
    started = time.monotonic()
    if assessment_id:
        if not isinstance(assessment_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', assessment_id):
            raise ValueError('Invalid external assessment identity')
        directory = directory / 'reassessments' / assessment_id
    fingerprint = config_signature(target, settings, assessment_id)
    folder = directory / fingerprint
    folder.mkdir(parents=True, exist_ok=True)
    (directory / 'paper_assessments').mkdir(parents=True, exist_ok=True)
    (directory / 'query_plans').mkdir(parents=True, exist_ok=True)
    (directory / 'retrieval_packets').mkdir(parents=True, exist_ok=True)
    previous = _read(folder / 'assessment.json')
    if previous and previous.get('validation_signature') == fingerprint and previous.get('verdict') not in {
            'pipeline_error','assessment_incomplete','queued','in_progress'} and (
            previous.get('verdict')!='connection_supported' or (
                previous.get('proof_audit',{}).get('passed') is True and
                previous['proof_audit'].get('input_signature') == audit_signature(
                    target,previous.get('paper_assessments',[]),previous.get('assessment',{})))):
        checkpoint(previous)
        return previous
    record = {'pathway_id': target['pathway_id'], 'target': target, 'policy': POLICY,
              'assessment_contract': ASSESSMENT_CONTRACT,
              **({'assessment_id': assessment_id} if assessment_id else {}),
              'validation_signature': fingerprint, 'verdict':'in_progress', 'started_at':utc_now(),
              'individually_externally_validated':False, 'paper_assessments':[], 'retrieval_rounds':[],
              'whole_pathway_assessed':False, 'assessment_scope':'neighboring_claims',
              'whole_chain_assessment':'not_assessed'}
    record_lock = threading.RLock()
    def publish():
        _publish_record(record, folder, checkpoint, record_lock)
    publish()
    try:
        query_key = signature({'policy':POLICY,'assessment_contract':ASSESSMENT_CONTRACT,'target':target['input_signature'],
                               'model':settings.query_model,'thinking':settings.task_thinking['query']})
        plan_path = directory / 'query_plans' / (query_key+'.json')
        plan = _read(plan_path)
        if plan is None:
            from backend.stages.neighbor_validation.targets import claim_signature
            retained, claim_paths = {}, {}
            for original in target['source_claims']:
                key = signature({'policy':POLICY,'assessment_contract':ASSESSMENT_CONTRACT,'claim':claim_signature(original),
                    'model':settings.query_model,'thinking':settings.task_thinking['query']})
                path = directory / 'query_plans' / ('claim-' + key + '.json')
                claim_paths[original['edge_id']] = path
                saved = _read(path)
                if saved:
                    retained[original['edge_id']] = {**saved, 'target_id':original['edge_id']}
            progress('Planning searches for neighboring claims and their local connection')
            plan = translate_pathway(query_client, target, retained)
            save_json(plan_path, plan)
            for query in plan['queries']:
                if query['target_id'] in claim_paths:
                    save_json(claim_paths[query['target_id']], query)
        session = _PaperAssessmentSession(target, external_client.for_model(settings.external_paper_model),
            settings, directory, record, publish, record_lock, finding_cache)
        papers, documents, stats = session.papers, session.documents, session.stats
        def synthesize_current():
            ordered=sorted(papers.values(),key=lambda paper:paper['evidence_document_id'])
            path=folder/'synthesis'/(signature(ordered)+'.json')
            saved=_read(path)
            if saved is not None:
                check_synthesis(target,ordered,saved)
                stats['reused_synthesis_calls']+=1
                return saved
            result=synthesize(external_client,target,ordered,settings)
            save_json(path,result)
            stats['synthesis_calls']+=1
            return result
        def run_round(query, round_settings):
            return session.run_round(query, round_settings, external_client, ranker, cache, progress)
        def audit_current(answer):
            if answer['verdict']!='connection_supported':
                record.pop('proof_audit',None)
                return None
            ordered = sorted(papers.values(),key=lambda paper:paper['evidence_document_id'])
            path = folder / 'proof_audits' / (audit_signature(target,ordered,answer)+'.json')
            saved = _read(path)
            progress('Auditing proposed support against admitted external quotations')
            audit = audit_support(external_client,target,ordered,answer,settings,retained=saved)
            stats['reused_proof_audits' if saved is not None else 'proof_audit_calls'] += 1
            save_json(path,audit)
            record['proof_audit'] = audit
            publish()
            return audit
        initial = run_round(plan,settings)
        rounds = [initial]
        record['retrieval_rounds'] = rounds
        record['paper_assessments'] = list(papers.values())
        record['evidence_documents'] = list(documents.values())
        publish()
        if papers:
            answer = synthesize_current()
            record['assessment_history'] = [answer]
            record.update(assessment=answer,scientific_verdict=answer['verdict'],evidence_basis=answer['evidence_basis'])
            publish()
            audit = audit_current(answer)
            held_by_audit = bool(audit and not audit['passed'])
            if ((answer['verdict'] in {'components_supported','unresolved'} or held_by_audit) and settings.pathway_followup_papers
                    and (not initial.get('retrieval_pending') or bool(papers)) and not session.paper_errors):
                progress('One targeted follow-up search for unresolved neighboring evidence')
                followup = _read(folder / 'followup_query.json')
                if followup is None:
                    followup = followup_queries(query_client,target,followup_assessment(answer,audit) if held_by_audit else answer)
                    save_json(folder / 'followup_query.json',followup)
                if followup['queries']:
                    extra = run_round(followup,replace(settings,external_top_k=settings.pathway_followup_papers))
                    rounds.append(extra); stats['followup_rounds'] = 1
                    answer = synthesize_current()
                    audit = audit_current(answer)
                    held_by_audit = bool(audit and not audit['passed'])
                    if answer != record['assessment_history'][-1]:
                        record['assessment_history'].append(answer)
            record['assessment'] = answer
            record['scientific_verdict'] = answer['verdict']
            record['verdict'] = answer['verdict']
            record['evidence_basis'] = answer['evidence_basis']
            if held_by_audit:
                record['verdict'] = 'assessment_incomplete'
                record['reason'] = 'Independent proof audit requires review; original synthesis is preserved and no support is admitted'
        else:
            record['verdict'] = 'unresolved'
            record['reason'] = 'No accessible external paper evidence; absence is not a scientific rejection'
        if any(r.get('retrieval_pending') for r in rounds):
            record['verdict'] = 'assessment_incomplete'
            record['reason'] = 'Some searches/citations are pending; no completed local support is admitted'
        if session.paper_errors:
            record['verdict'] = 'assessment_incomplete'
            record['reason'] = 'Some paper proof selections need review; valid findings are retained and no local support is admitted'
            progress(f"{len(session.paper_errors)} paper assessment(s) held for evidence review; other pairs continue")
        record.update(retrieval_rounds=rounds, paper_assessments=list(papers.values()),
            execution_status='pending' if record['verdict']=='assessment_incomplete' else 'complete',
            evidence_documents=list(documents.values()),
            whole_pathway_assessed=False, local_connection_assessed=record['verdict'] not in {'assessment_incomplete','pipeline_error'},
            completed_at=utc_now(), elapsed_seconds=round(time.monotonic()-started,3), calls=stats,
            experimentally_demonstrated=False)
        publish()
        return record
    except EvidenceSelectionError as exc:
        record.update(verdict='assessment_incomplete',error=str(exc),
            execution_status='pending',
            reason='Evidence assessment needs review; invalid proof is excluded and other pairs can continue',
            review_response=getattr(exc,'review_response',None),local_connection_assessed=False,
            elapsed_seconds=round(time.monotonic()-started,3))
        publish()
        progress('Local synthesis held for evidence review; completed paper findings are preserved')
        return record
    except BaseException as exc:
        from backend.shared.cancellation import PipelineStopped
        record.update(verdict='assessment_incomplete' if isinstance(exc,PipelineStopped) else 'pipeline_error',
                      execution_status='stopped' if isinstance(exc,PipelineStopped) else 'failed',
                      error=str(exc), elapsed_seconds=round(time.monotonic()-started,3))
        publish()
        raise
