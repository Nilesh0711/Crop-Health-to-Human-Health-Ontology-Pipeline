import unittest

from external_validator.candidates import EVIDENCE_RE, parse_tuple_line
from external_validator.decision import decision_errors
from external_validator.retrieval import extract_paragraphs, make_chunks
from external_validator.workflow import translate_query


class ExternalValidationTests(unittest.TestCase):
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
