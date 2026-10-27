"""Behavioral regressions for batched, evidence-grounded validation."""

import copy
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.stages.claim_extraction.deduplication import deduplicate_source_claims
from backend.stages.source_validation.checker import validate_source_claim
from backend.pipelines.document import run_pipeline
from backend.domain.alignment import grounded_alignment
from backend.shared.batching import iter_batched_judgments
from backend.shared.cache import RunCache
from backend.config.settings import Settings
from backend.providers.llm import LLMServiceError, OpenCodeClient
from tests.external.helpers import judge_document
from backend.stages.evidence_ranking.passages import SemanticReranker
from backend.stages.literature_retrieval.clients import make_chunks, scholarly_sentences
from backend.stages.literature_retrieval.workflow import _citation_results, retrieve_round
from backend.stages.query_planning.planner import evidence_query_plan, translate_queries


def alignment(canonical, mention, evidence, kind="same_concept"):
    return {"canonical": canonical, "mention": mention, "evidence": evidence,
            "kind": kind, "explanation": "The quoted wording refers to this same measured endpoint."}


class ReplyClient:
    def __init__(self, reply):
        self.settings = Settings()
        self.reply, self.calls = reply, []

    def chat_json(self, system, user, **options):
        self.calls.append((system, user, options))
        return copy.deepcopy(self.reply)


def source_claim(identifier="one"):
    return {"candidate_id": identifier, "subject": "organic farming", "subject_type": "AgriculturalPractice",
            "relation": "reduces", "object": "soil pollution", "object_type": "SoilCondition",
            "certainty": "descriptive", "qualifiers": {}, "subject_source_mention": "organic farming",
            "object_source_mention": "soil and water pollution", "relation_trigger": "reduces",
            "source_evidence_sentence_numbers": [1],
            "source_evidence_span": "Organic farming reduces soil and water pollution in the study area."}


def source_reply():
    span = source_claim()["source_evidence_span"]
    return {"decision": "entailed", "relation_match": True, "direction_match": True,
            "abbreviation_review": {"status": "clear", "corrections": [], "unresolved": []},
            "certainty_match": True, "rationale": "The coordinated object includes soil pollution.",
            "proof": {"subject": "Organic farming", "relation": "reduces", "object": "soil and water pollution"},
            "entity_alignment": {
                "subject": alignment("organic farming", "Organic farming", span),
                "object": alignment("soil pollution", "soil and water pollution", span, "coordination")}}


class EvidenceAlignmentTests(unittest.TestCase):
    def test_coordinated_object_can_pass_source_with_grounded_semantic_alignment(self):
        result = validate_source_claim(source_claim(), ReplyClient(source_reply()))
        self.assertEqual(result["decision"], "entailed")
        self.assertIn("entity_alignment", result)

    def test_invented_alignment_or_wrong_canonical_never_overrides_source_guard(self):
        for field, value in (("evidence", "A sentence that was not supplied."), ("canonical", "air pollution"),
                             ("kind", "different")):
            reply = source_reply()
            reply["entity_alignment"]["object"][field] = value
            self.assertEqual(validate_source_claim(source_claim(), ReplyClient(reply))["decision"], "needs_review")

    def test_property_alignment_requires_the_actual_property_evidence(self):
        quote = "The treatment makes nutrient Q available for plant uptake."
        proof = alignment("nutrient Q availability", "nutrient Q", quote)
        self.assertTrue(grounded_alignment(proof, "nutrient Q availability", quote))
        self.assertFalse(grounded_alignment(proof, "nutrient Q availability", "The treatment contains nutrient Q."))

    def test_context_reference_requires_a_quoted_antecedent(self):
        context = "Mulching was applied. It reduced evaporation from soil."
        proof = alignment("Mulching", "It", context, "context_reference")
        self.assertTrue(grounded_alignment(proof, "Mulching", "It reduced evaporation from soil.", context))
        self.assertFalse(grounded_alignment(proof, "Mulching", "It reduced evaporation from soil.", "Unrelated context."))


class BatchValidationTests(unittest.TestCase):
    def test_corrected_batch_still_rejects_a_quote_absent_from_its_own_source(self):
        claims = [source_claim('one'), source_claim('two')]
        claims[1]['source_evidence_span'] = 'Organic farming was introduced to the study area.'
        rows = [{'candidate_id': claim['candidate_id'], **source_reply(),
                 'claim_text_match': True, 'occurrence_provenance_fields': []} for claim in claims]
        response = json.dumps({'results': rows}, separators=(',', ':'))
        # Reproduce the real failure: a nested annotation's closing brace was
        # generated inside its string, rather than after the closing quote.
        annotation = json.dumps(rows[1]['entity_alignment']['subject'], separators=(',', ':'))
        response = response.replace(annotation, annotation[:-2] + '}"', 1)
        metrics = []
        client = OpenCodeClient(Settings(), task='source', on_request=metrics.append)
        patch_response = json.dumps({'edits': [{'before': annotation[:-2] + '}"', 'after': annotation}]})
        with patch.object(client._inference, 'chat', side_effect=[
                {'content': response, 'finish_reason': 'stop'},
                {'content': patch_response, 'finish_reason': 'stop'}]) as run:
            results = list(iter_batched_judgments(claims, client, validate_source_claim))
        self.assertEqual(run.call_count, 2)
        self.assertTrue(metrics[1]['response_format_adjustments'])
        self.assertEqual([result['decision'] for _, result in results], ['entailed', 'needs_review'])
        self.assertTrue(results[1][1]['reasons'])

    def test_failed_query_row_is_explicit_and_does_not_trigger_a_model_retry(self):
        client = ReplyClient({"results": []})
        planned = translate_queries([source_claim()], client)
        self.assertTrue(planned['one']['batch_error'])
        self.assertEqual(len(client.calls), 1)
    def test_provider_access_failure_stops_batch_instead_of_marking_claims_for_review(self):
        client = ReplyClient({})
        client.chat_json = Mock(side_effect=LLMServiceError("Provider access denied"))
        with self.assertRaises(LLMServiceError):
            list(iter_batched_judgments([source_claim()], client, validate_source_claim))
    def test_one_batch_runs_each_source_answer_through_its_own_guards(self):
        claims = [source_claim("one"), source_claim("two")]
        claims[1]["source_evidence_span"] = "Organic farming was introduced to the study area."
        reply = {"results": [{"candidate_id": claim["candidate_id"], **source_reply()} for claim in reversed(claims)]}
        client = ReplyClient(reply)
        results = list(iter_batched_judgments(claims, client, validate_source_claim, 3))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual([result["decision"] for _, result in results], ["entailed", "needs_review"])
        self.assertEqual([candidate["candidate_id"] for candidate, _ in results], ["one", "two"])

    def test_duplicate_or_missing_batch_ids_cannot_be_assigned_to_other_claims(self):
        claims = [source_claim("one"), source_claim("two")]
        response = {"results": [{"candidate_id": "one", **source_reply()}, {"candidate_id": "one", **source_reply()}]}
        results = list(iter_batched_judgments(claims, ReplyClient(response), validate_source_claim))
        self.assertTrue(all(result["decision"] == "needs_review" and result["batch_error"] for _, result in results))



class DeduplicationTests(unittest.TestCase):
    def test_same_span_wording_duplicates_preserve_provenance(self):
        first = {**source_claim(), "object": "nitrogen content in soil", "certainty": "causal",
                 "source_evidence_records": [{"packet": 1}]}
        second = {**first, "candidate_id": "two", "object": "nitrogen content in the soil",
                  "certainty": "causal", "source_evidence_records": [{"packet": 2}]}
        unique, count = deduplicate_source_claims([first, second])
        self.assertEqual((len(unique), count), (1, 1))
        self.assertEqual(unique[0]["certainty"], "causal")
        self.assertEqual(unique[0]["deduplicated_candidate_ids"], ["two"])
        self.assertEqual(len(unique[0]["source_evidence_records"]), 2)

    def test_different_modality_is_retained_for_model_review(self):
        first = {**source_claim(), 'certainty': 'causal'}
        second = {**first, 'candidate_id': 'two', 'certainty': 'descriptive'}
        unique, count = deduplicate_source_claims([first, second])
        self.assertEqual((len(unique), count), (2, 0))
        self.assertEqual([c['certainty'] for c in unique], ['causal', 'descriptive'])

    def test_different_crop_context_or_source_span_is_never_merged(self):
        first = source_claim()
        second = {**first, "candidate_id": "two", "qualifiers": {"crop_or_species": "barley"}}
        third = {**first, "candidate_id": "three", "source_evidence_span": "A different experiment."}
        unique, count = deduplicate_source_claims([first, second, third])
        self.assertEqual((len(unique), count), (3, 0))


def external_fixture(decision="supported"):
    claim = {"candidate_id": "external-one", "subject": "continuous cropping", "relation": "increases",
             "object": "soil fungal pathogens", "certainty": "descriptive", "qualifiers": {}}
    quote = "Continuous cropping significantly increased soil pathogenic fungi in the experimental plots."
    document = {"evidence_document_id": "paper", "evidence_level": "independent_external",
                "evidence_chunks": [{"chunk_id": "passage", "text": quote}]}
    reply = {"decision": decision, "matches": {key: True for key in ("subject", "object", "relation", "direction", "certainty")},
             "rationale": "The evidence states this increase.", "evidence": [{"chunk_id": "passage",
                 "sentence_numbers": [1], "subject_mention": "Continuous cropping",
                 "object_mention": "soil pathogenic fungi", "relation_trigger": "increased", "stance": "supports"}],
             "entity_alignment": {"subject": alignment("continuous cropping", "Continuous cropping", quote),
                                  "object": alignment("soil fungal pathogens", "soil pathogenic fungi", quote)},
             "qualifier_alignment": {}}
    return claim, document, reply


class ExternalEvidenceContractTests(unittest.TestCase):
    def test_short_complete_scientific_sentence_is_not_rejected_for_word_count(self):
        claim = {"candidate_id": "short", "subject": "Mulching", "relation": "reduces",
                 "object": "soil evaporation", "qualifiers": {}}
        document = {"evidence_document_id": "short-paper", "evidence_chunks": [{
                    "chunk_id": "short-chunk", "text": "Mulching reduces soil evaporation."}]}
        reply = {"decision": "supported", "matches": {key: True for key in
                 ("subject", "object", "relation", "direction", "certainty")}, "rationale": "Direct result.",
                 "evidence": [{"chunk_id": "short-chunk", "sentence_numbers": [1],
                               "subject_mention": "Mulching", "object_mention": "soil evaporation",
                               "relation_trigger": "reduces", "stance": "supports"}]}
        self.assertEqual(judge_document(ReplyClient(reply), claim, document)["decision"], "supported")
    def test_generic_semantic_alignment_accepts_rephrasing_and_keeps_exact_quote(self):
        claim, document, reply = external_fixture()
        result = judge_document(ReplyClient(reply), claim, document)
        self.assertEqual(result["decision"], "supported")
        self.assertEqual(result["evidence"][0]["quoted_span"], document["evidence_chunks"][0]["text"])

    def test_invented_or_different_entity_alignment_is_rejected_and_audited(self):
        claim, document, reply = external_fixture()
        reply["entity_alignment"]["object"]["kind"] = "different"
        result = judge_document(ReplyClient(reply), claim, document)
        self.assertEqual(result["decision"], "needs_review")
        self.assertIn("rejected_model_decision", result)

    def test_unrelated_association_in_a_neighboring_clause_does_not_override_direct_result(self):
        claim, document, reply = external_fixture()
        document["evidence_chunks"][0]["text"] += " Grain size was correlated with rainfall."
        reply["evidence"][0]["sentence_numbers"] = [1, 2]
        self.assertEqual(judge_document(ReplyClient(reply), claim, document)["decision"], "supported")

    def test_three_consecutive_sentences_can_supply_antecedent_and_result(self):
        claim, document, reply = external_fixture()
        document["evidence_chunks"][0]["text"] = "The plots were sampled in autumn. " + document["evidence_chunks"][0]["text"] + " Soil was sampled again the following year."
        reply["evidence"][0]["sentence_numbers"] = [1, 2, 3]
        self.assertEqual(judge_document(ReplyClient(reply), claim, document)["decision"], "supported")
        reply["evidence"][0]["sentence_numbers"] = [1, 3]
        completed = judge_document(ReplyClient(reply), claim, document)
        self.assertEqual(completed['decision'], 'supported')
        self.assertEqual(completed['evidence'][0]['sentence_numbers'], [1, 2, 3])
        self.assertEqual(completed['evidence'][0]['selection_adjustment']['original_sentence_numbers'], [1, 3])

    def test_high_nli_score_cannot_create_support_without_selected_evidence(self):
        claim, document, reply = external_fixture("insufficient")
        reply["evidence"] = []
        nli = SimpleNamespace(settings=SimpleNamespace(nli_model="test", enable_nli_promotion=True),
                              score=Mock(return_value={"entailment": 0.99, "contradiction": 0.0}))
        result = judge_document(ReplyClient(reply), claim, document, nli_validator=nli)
        self.assertEqual(result["decision"], "insufficient")
        nli.score.assert_not_called()

    def test_nli_promotion_requires_enabled_setting_and_full_support_contract(self):
        for enabled, relation_match, expected in ((False, True, "needs_review"),
                                                   (True, False, "needs_review"), (True, True, "supported")):
            claim, document, reply = external_fixture("needs_review")
            reply["matches"]["relation"] = relation_match
            nli = SimpleNamespace(settings=SimpleNamespace(nli_model="test", enable_nli_promotion=enabled),
                                  score=Mock(return_value={"entailment": 0.99, "contradiction": 0.0}))
            result = judge_document(ReplyClient(reply), claim, document, nli_validator=nli)
            self.assertEqual(result["decision"], expected)
            if expected == "supported":
                self.assertTrue(result["evidence"])


class RetrievalEfficiencyTests(unittest.TestCase):
    def test_citation_metadata_is_reused_without_reusing_local_reference_numbers(self):
        cache = RunCache()
        first = {"source_references": [{"reference_number": 1, "citation_text": "A specific source study."}]}
        second = {"source_references": [{"reference_number": 9, "citation_text": "A specific source study."}]}
        with patch('backend.stages.literature_retrieval.workflow.resolve_bibliographic_reference', return_value={'doi': '10.1000/study'}) as resolve, \
                patch('backend.stages.literature_retrieval.workflow.search', return_value=[{'doi': '10.1000/study', 'title': 'Study'}]) as search, \
                patch('backend.stages.literature_retrieval.workflow.openalex_open_access_copy', return_value={'doi': '10.1000/study'}) as openalex:
            rows1 = _citation_results(first, Settings(), [], cache)
            rows2 = _citation_results(second, Settings(), [], cache)
        self.assertEqual((resolve.call_count, search.call_count, openalex.call_count), (1, 1, 1))
        self.assertEqual(rows1[0]['_source_reference_number'], 1)
        self.assertEqual(rows2[0]['_source_reference_number'], 9)
    def test_openalex_uses_the_planned_plain_text_query(self):
        plan = evidence_query_plan(source_claim(), {"query": "test AND query", "openalex_query": "organic farming soil contamination"})
        self.assertEqual(plan["openalex"][0], "organic farming soil contamination")

    def test_sentence_chunks_do_not_cut_results_or_scholarly_abbreviations(self):
        text = "Smith et al. studied the intervention. The outcome increased significantly in all treated plots. Additional observations were reported later."
        chunks = make_chunks([text], size=12, overlap=4)
        self.assertTrue(all(chunk.endswith('.') for chunk in chunks))
        self.assertTrue(any("outcome increased significantly" in chunk for chunk in chunks))
        self.assertEqual(len(scholarly_sentences(text)), 3)

    def test_run_cache_is_bounded_and_reuses_successful_work(self):
        cache = RunCache(max_bytes=50)
        compute = Mock(return_value={"text": "a" * 20})
        cache.get_or_compute("one", compute)
        cache.get_or_compute("one", compute)
        self.assertEqual(compute.call_count, 1)
        cache.get_or_compute("two", compute)
        self.assertLessEqual(cache.stats["bytes"], 50)
        self.assertEqual(cache.stats["hits"], 1)

    def test_index_queries_overlap_and_are_cached_across_claims(self):
        barrier = threading.Barrier(4)
        entered = 0
        entered_lock = threading.Lock()
        def search(_query, _settings, _limit):
            nonlocal entered
            with entered_lock:
                entered += 1
                first_wave = entered <= 4
            if first_wave:
                barrier.wait(timeout=3)
            return [{"source": "MED", "id": "1234", "doi": "10.1000/shared", "title": "Organic farming reduces soil pollution",
                     "abstractText": "Organic farming reduced soil pollution in the experimental plots. The results were confirmed across several repeated sampling dates."}]
        ranker = SimpleNamespace(settings=Settings(), rank_texts=lambda _q, texts: [1.0] * len(texts),
                                 cross_scores=lambda _q, texts: [1.0] * len(texts),
                                 select=lambda _q, chunks, key, _n, **_kwargs: [{"chunk_id": key + ':0', "text": str(chunks[0])}])
        settings, cache = replace(Settings(), retrieval_workers=4), RunCache()
        with patch('backend.stages.literature_retrieval.workflow.search', side_effect=search) as pmc, \
                patch('backend.stages.literature_retrieval.workflow.search_openalex', side_effect=search) as openalex, \
                patch('backend.stages.literature_retrieval.workflow._citation_results', return_value=[]):
            first = retrieve_round(source_claim(), None, ranker, settings, 1, 1,
                                   query_translation={"query": "farming AND pollution"}, cache=cache)
            second = retrieve_round(source_claim(), None, ranker, settings, 1, 1,
                                    query_translation={"query": "farming AND pollution"}, cache=cache)
        self.assertEqual((pmc.call_count, openalex.call_count), (len(first["query_plan"]["europe_pmc"]), len(first["query_plan"]["openalex"])))
        self.assertEqual(len(first["evidence_documents"]), 1)
        self.assertEqual(first["evidence_documents"][0]["doi"], second["evidence_documents"][0]["doi"])
        self.assertGreater(cache.stats["hits"], 0)

    def test_embedding_cache_reuses_document_vectors_across_queries(self):
        class Vector:
            def __matmul__(self, _other):
                return 1.0
        model = Mock()
        model.encode.side_effect = lambda values, **_kw: [Vector() for _ in values]
        ranker = SemanticReranker(Settings())
        ranker._embedding_runtime = ('sentence-transformers', model)
        ranker.rank_texts('first query', ['shared passage'])
        ranker.rank_texts('second query', ['shared passage'])
        self.assertEqual(len(model.encode.call_args_list[1].args[0]), 1)
        self.assertGreaterEqual(ranker.embedding_cache_hits, 1)


class PipelineIntegrationTests(unittest.TestCase):
    def test_run_wires_batches_deduplication_query_plan_and_graph_without_live_services(self):
        settings = replace(Settings(), enable_nli=False, validation_batch_size=3, query_batch_size=6)
        task_routes = {}

        class PipelineClient:
            def __init__(self, _settings, model=None, **_kwargs):
                self.settings, self.model, self.requests = _settings, model, 0

            @property
            def request_metrics(self):
                return {"requests": self.requests, "elapsed_seconds": 0.0}

            def health_check(self):
                return {"models": [model.split('#')[0] for model in settings.task_models.values()]}

            def chat_json(self, system, user, **_kwargs):
                self.requests += 1
                items = json.loads(user)["items"]
                if "skeptical source-evidence verifier" in system:
                    task_routes['source'] = self.model
                    result = source_reply()
                else:
                    task_routes['query'] = self.model
                    result = {"query": "organic farming AND soil pollution", "openalex_query": "organic farming soil pollution"}
                return {"results": [{"candidate_id": item["candidate_id"], **result} for item in items]}

        candidate = {**source_claim(), "schema_error": None, "source_page": 1, "source_paper_title": "Paper",
                     "source_document_id": "local-paper", "source_pdf_sha256": "hash"}
        duplicate = {**copy.deepcopy(candidate), "candidate_id": "duplicate", "object": "the soil pollution"}
        graph_result = {"candidate_id": "one", "validation_status": "world_independent_supported_pending_corroboration",
                        "attempts": [{"decisions": [{"decision": "supported", "evidence_document_id": "external",
                                                     "evidence_level": "independent_external"}]}]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "sample.pdf"
            pdf.write_bytes(b"test fixture; PDF parser is replaced by pre-extracted text")
            with patch('backend.pipelines.document.ONTOLOGY_DIR', root / 'outputs'), \
                    patch('backend.pipelines.document.Settings', return_value=settings), \
                    patch('backend.pipelines.document.OpenCodeClient', PipelineClient), \
                    patch('backend.pipelines.document.iter_pdf_context_packets', return_value=[{"sentences": [{"text": "This readable source passage contains agricultural evidence for direct crop biological mechanisms and food contamination outcomes."}]}]), \
                    patch('backend.pipelines.document.inspect_pdf', return_value={'regions': [], 'pages': [], 'total_pages': 1, 'selected_pages': 1, 'pages_with_text': 1, 'pages_with_warnings': 0, 'chapter_boundaries': []}), \
                    patch('backend.pipelines.document.PdfReader'), \
                    patch('backend.pipelines.document.source_article_identifiers', return_value={}), \
                    patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Paper'), \
                    patch('backend.pipelines.document.extract_claims', return_value=([candidate, duplicate], {}, [])) as extract, \
                    patch('backend.pipelines.document.assemble_pathways', return_value={'counts':{}}), \
                    patch('backend.pipelines.document.SemanticReranker') as ranker, \
                    patch('backend.pipelines.document.prepare_evidence', return_value={'evidence_documents': []}), \
                    patch('backend.pipelines.document.validate_candidate', return_value=graph_result) as external:
                ranker.return_value.embedding_cache_hits = 0
                summary = run_pipeline(pdf, 0, 0, 3, lambda *_args: None)
            self.assertEqual(summary["canonical_duplicates_removed"], 1)
            self.assertEqual(summary["extracted"], 1)
            self.assertEqual(summary["source_entailed"], 1)
            self.assertEqual(summary["graph"]["edges"], 1)
            self.assertEqual(summary["llm_calls"]["total_requests"], 2)
            self.assertEqual(extract.call_args.args[1].model, settings.extraction_model)
            self.assertEqual(task_routes, {'source': settings.source_model, 'query': settings.query_model})
            self.assertEqual(external.call_args.args[1].model, settings.external_model)
            self.assertNotIn('priority_client', external.call_args.kwargs)
            self.assertEqual(summary['semantic_stage'], 'removed')
            self.assertEqual(summary['task_models'], settings.task_models)
            self.assertEqual(set(summary['llm_reasoning_policy']), set(settings.task_models))
            self.assertEqual(external.call_args.kwargs["query_translation"]["openalex_query"], "organic farming soil pollution")
            self.assertIsInstance(external.call_args.kwargs["cache"], RunCache)
            self.assertTrue((root / 'outputs' / 'sample' / 'source_validation.jsonl').exists())
            self.assertIn('external_validation', summary['stage_timings_seconds'])
