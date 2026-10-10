import unittest

from external_validator.candidates import EVIDENCE_RE, parse_tuple_line
from external_validator.decision import decision_errors, judge_document
from external_validator.retrieval import extract_paragraphs, make_chunks
from external_validator.workflow import _bm25_scores, originating_source_match, translate_query


class ExternalValidationTests(unittest.TestCase):
    def test_independent_nli_can_only_veto_supported_decision(self):
        class JudgeClient:
            def chat_json(self, *_args, **_kwargs):
                return {"decision": "supported", "matches": {"subject": True, "object": True, "relation": True,
                        "direction": True, "certainty": True}, "evidence": [{"chunk_id": "e:chunk-0",
                        "sentence_numbers": [1], "subject_mention": "Cadmium", "object_mention": "kidney damage",
                        "relation_trigger": "increased", "stance": "supports"}], "rationale": "Supported."}

        class NLI:
            class Settings:
                nli_model = "test-nli"
            settings = Settings()
            def score(self, _premise, _hypothesis):
                return {"contradiction": 0.9, "entailment": 0.05, "neutral": 0.05}

        candidate = {"candidate_id": "nli", "subject": "Cadmium", "relation": "increases",
                     "object": "kidney damage", "subject_type": "Contaminant",
                     "object_type": "HumanHealthOutcome", "certainty": "descriptive", "qualifiers": {}}
        document = {"evidence_document_id": "e", "evidence_level": "independent_external",
                    "evidence_chunks": [{"chunk_id": "e:chunk-0",
                                         "text": "Cadmium increased kidney damage in exposed mice."}]}
        result = judge_document(JudgeClient(), candidate, document, nli_validator=NLI())
        self.assertEqual(result["decision"], "needs_review")
        self.assertEqual(result["nli"]["model"], "test-nli")

    def test_external_judge_derives_exact_quote_from_sentence_ids_and_accepts_narrow_alias(self):
        class JudgeClient:
            def chat_json(self, *_args, **_kwargs):
                return {
                    "decision": "supported",
                    "matches": {"subject": True, "object": True, "relation": True,
                                "direction": True, "certainty": True},
                    "evidence": [{
                        "chunk_id": "external-1:chunk-0", "sentence_numbers": [1],
                        "subject_mention": "Mulching practices", "object_mention": "soil water evaporation",
                        "relation_trigger": "reduce", "stance": "supports",
                    }],
                    "rationale": "The selected sentence directly supports the claim.",
                }

        candidate = {"candidate_id": "c-sentence", "subject": "mulching techniques", "relation": "reduces",
                     "object": "soil water evaporation", "subject_type": "AgriculturalPractice",
                     "object_type": "SoilCondition", "certainty": "causal", "qualifiers": {}}
        document = {"evidence_document_id": "external-1", "evidence_chunks": [{
            "chunk_id": "external-1:chunk-0",
            "text": "Mulching practices reduce soil water evaporation in dry fields. Crop growth also improved.",
        }]}
        result = judge_document(JudgeClient(), candidate, document)
        self.assertEqual(result["decision"], "supported")
        self.assertEqual(result["evidence"][0]["quoted_span"], "Mulching practices reduce soil water evaporation in dry fields.")

    def test_external_judge_does_not_treat_different_fertilizers_as_aliases(self):
        class JudgeClient:
            def chat_json(self, *_args, **_kwargs):
                return {
                    "decision": "supported",
                    "matches": {"subject": True, "object": True, "relation": True,
                                "direction": True, "certainty": True},
                    "evidence": [{
                        "chunk_id": "external-2:chunk-0", "sentence_numbers": [1],
                        "subject_mention": "Suspension fertilizer", "object_mention": "soil organic matter",
                        "relation_trigger": "increased", "stance": "supports",
                    }],
                    "rationale": "Claim support.",
                }

        candidate = {"candidate_id": "c-different", "subject": "Organic fertilizer", "relation": "increases",
                     "object": "soil organic matter", "subject_type": "AgriculturalPractice",
                     "object_type": "SoilCondition", "certainty": "causal", "qualifiers": {}}
        document = {"evidence_document_id": "external-2", "evidence_chunks": [{
            "chunk_id": "external-2:chunk-0",
            "text": "Suspension fertilizer increased soil organic matter in wheat fields.",
        }]}
        self.assertEqual(judge_document(JudgeClient(), candidate, document)["decision"], "needs_review")

    def test_external_judge_accepts_explicit_contaminant_detection_wording(self):
        class JudgeClient:
            def chat_json(self, *_args, **_kwargs):
                return {
                    "decision": "supported",
                    "matches": {"subject": True, "object": True, "relation": True,
                                "direction": True, "certainty": True},
                    "evidence": [{
                        "chunk_id": "external-3:chunk-0", "sentence_numbers": [1],
                        "subject_mention": "rice grains", "object_mention": "Cadmium",
                        "relation_trigger": "detected in", "stance": "supports",
                    }],
                    "rationale": "The contaminant is explicitly detected in the crop material.",
                }

        candidate = {"candidate_id": "c-contaminant", "subject": "Rice grains", "relation": "has_contaminant",
                     "object": "Cadmium", "subject_type": "Crop", "object_type": "Contaminant",
                     "certainty": "descriptive", "qualifiers": {}}
        document = {"evidence_document_id": "external-3", "evidence_chunks": [{
            "chunk_id": "external-3:chunk-0",
            "text": "Cadmium was detected in rice grains collected from the field.",
        }]}
        self.assertEqual(judge_document(JudgeClient(), candidate, document)["decision"], "supported")

    def test_external_judge_requires_a_causal_cue_for_deficiency_causes(self):
        class JudgeClient:
            def chat_json(self, *_args, **_kwargs):
                return {
                    "decision": "supported",
                    "matches": {"subject": True, "object": True, "relation": True,
                                "direction": True, "certainty": True},
                    "evidence": [{
                        "chunk_id": "external-4:chunk-0", "sentence_numbers": [1],
                        "subject_mention": "Iron deficiency", "object_mention": "anaemia",
                        "relation_trigger": "deficiency", "stance": "supports",
                    }],
                    "rationale": "Claim support.",
                }

        candidate = {"candidate_id": "c-deficiency", "subject": "Iron deficiency", "relation": "deficiency_causes",
                     "object": "anaemia", "subject_type": "Nutrient", "object_type": "HumanHealthOutcome",
                     "certainty": "causal", "qualifiers": {}}
        document = {"evidence_document_id": "external-4", "evidence_chunks": [{
            "chunk_id": "external-4:chunk-0",
            "text": "Iron deficiency and anaemia were measured in the study population.",
        }]}
        self.assertEqual(judge_document(JudgeClient(), candidate, document)["decision"], "needs_review")

    def test_external_query_does_not_receive_originating_title(self):
        class QueryClient:
            def chat_json(inner_self, system, user, **_kwargs):
                self.assertNotIn("Originating Paper Title", user)
                self.assertIn("Organic fertilizer", user)
                return {"query": '("organic fertilizer") AND ("soil phenolics")'}

        candidate = {"subject": "Organic fertilizer", "relation": "increases", "object": "soil phenolics",
                     "subject_type": "AgriculturalPractice", "object_type": "Nutrient", "qualifiers": {},
                     "source_paper_title": "Originating Paper Title"}
        result = translate_query(QueryClient(), candidate)
        self.assertNotIn("Originating Paper Title", result["query"])

    def test_originating_paper_is_excluded_by_identifier_or_conservative_title_variant(self):
        self.assertEqual(
            originating_source_match(
                {"source_doi": "https://doi.org/10.1000/example.1", "source_paper_title": "Different local title"},
                {"doi": "10.1000/example.1", "title": "Different indexed title"},
            ),
            "originating_paper_excluded_doi",
        )
        self.assertEqual(
            originating_source_match(
                {"source_paper_title": "Soil health and human health current status"},
                {"title": "Soil health and human health current status and future needs"},
            ),
            "originating_paper_excluded_title_containment",
        )
        self.assertIsNone(
            originating_source_match(
                {"source_paper_title": "Soil health and crop productivity"},
                {"title": "Human dietary exposure to cadmium"},
            )
        )

    def test_parse_tuple_line_reads_current_five_field_contract(self):
        parsed = parse_tuple_line("<cadmium exposure, causes, kidney damage, Contaminant, HumanExposure>")
        self.assertEqual(parsed["relation"], "causes")
        self.assertNotIn("extraction_confidence", parsed)

    def test_parse_tuple_line_accepts_legacy_confidence(self):
        parsed = parse_tuple_line("<cadmium exposure, causes, kidney damage, Contaminant, HumanExposure, 0.90>")
        self.assertEqual(parsed["legacy_extraction_confidence"], 0.90)

    def test_extraction_evidence_comment_contract(self):
        match = EVIDENCE_RE.match('# evidence: "Cadmium exposure caused kidney damage in mice."')
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "Cadmium exposure caused kidney damage in mice.")

    def test_xml_parser_excludes_reference_list(self):
        xml = """<article><abstract><p>Cadmium exposure caused kidney damage in mice after repeated treatment, with renal injury observed in the exposed group.</p></abstract><body><sec><p>Renal injury increased after cadmium exposure in the treated mice when compared with the matched control group.</p></sec></body><ref-list><ref><p>Cadmium unrelated reference.</p></ref></ref-list></article>"""
        paragraphs = extract_paragraphs(xml)
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual(len(make_chunks(paragraphs)), 2)

    def test_page_metadata_survives_chunking(self):
        chunks = make_chunks([{"page_number": 7, "text": "cadmium " * 100}], size=60, overlap=10)
        self.assertTrue(chunks)
        self.assertTrue(all(chunk["page_number"] == 7 for chunk in chunks))

    def test_bm25_prefers_exact_entity_pair_over_topical_distractor(self):
        scores = _bm25_scores(
            "ley phase weed pressure",
            ["A ley phase substantially reduced weed pressure in an organic rotation.",
             "Amaranth is a climate-resilient crop requiring weed management."],
        )
        self.assertGreater(scores[0], scores[1])

    def test_supported_decision_requires_a_stored_quote(self):
        packet = {"candidate": {"candidate_id": "c1", "subject": "Cadmium", "object": "kidney damage"}, "evidence_documents": [{"evidence_document_id": "external-1", "evidence_chunks": [{"chunk_id": "external-1:chunk-0", "text": "Cadmium exposure caused kidney damage in mice after treatment."}]}]}
        valid = {"candidate_id": "c1", "evidence_document_id": "external-1", "decision": "supported",
                 "subject_match": True, "object_match": True, "relation_match": True, "direction_match": True, "certainty_match": True,
                 "evidence": [{"chunk_id": "external-1:chunk-0", "quoted_span": "Cadmium exposure caused kidney damage in mice after treatment.", "stance": "supports"}]}
        invalid = {**valid, "evidence": [{"chunk_id": "external-1:chunk-0", "quoted_span": "Invented quotation", "stance": "supports"}]}
        self.assertEqual(decision_errors(packet, valid), [])
        self.assertTrue(decision_errors(packet, invalid))

    def test_related_quote_cannot_support_exact_claim(self):
        packet = {"candidate": {"candidate_id": "c2", "subject": "Organic fertilizer", "object": "soil organic matter"},
                  "evidence_documents": [{"evidence_document_id": "external-2", "evidence_chunks": [
                      {"chunk_id": "chunk-2", "text": "Suspension fertilizer increased soil organic matter in wheat fields."}]}]}
        decision = {"candidate_id": "c2", "evidence_document_id": "external-2", "decision": "supported",
                    "subject_match": True, "object_match": True, "relation_match": True, "direction_match": True, "certainty_match": True,
                    "evidence": [{"chunk_id": "chunk-2", "quoted_span": "Suspension fertilizer increased soil organic matter in wheat fields.", "stance": "supports"}]}
        self.assertIn("quoted evidence does not explicitly name the claim subject", decision_errors(packet, decision))


if __name__ == "__main__":
    unittest.main()
