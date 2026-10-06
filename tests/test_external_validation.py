import unittest

import external_validation as validation


class ExternalValidationTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {
            "subject": "cadmium",
            "relation": "causes",
            "object": "kidney damage",
        }

    def test_parse_tuple_line_preserves_source_fields(self):
        parsed = validation.parse_tuple_line(
            "<cadmium, causes, kidney damage, Nutrient, HumanDisease, 0.90>"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["relation"], "causes")
        self.assertEqual(parsed["extraction_confidence"], 0.90)

    def test_query_contains_entities_and_relation_terms(self):
        query = validation.europe_pmc_query(self.candidate)
        self.assertIn('"cadmium"', query)
        self.assertIn('"kidney damage"', query)
        self.assertIn("cause", query)

    def test_xml_parser_ignores_reference_list_and_selects_relevant_chunk(self):
        xml = """<article><abstract><p>Cadmium exposure caused kidney damage in mice after repeated treatment, with renal injury observed in the exposed group.</p></abstract>
        <body><sec><p>Renal injury increased after cadmium exposure in the treated mice when compared with the matched control group.</p></sec></body>
        <ref-list><ref><p>Cadmium unrelated reference.</p></ref></ref-list></article>"""
        paragraphs = validation.extract_paragraphs(xml)
        self.assertEqual(len(paragraphs), 2)
        selected = validation.selected_evidence_chunks(self.candidate, "external-test", xml, 1)
        self.assertEqual(len(selected), 1)
        self.assertIn("cadmium", selected[0]["text"].casefold())
        self.assertIn("kidney damage", selected[0]["text"].casefold())

    def test_association_query_uses_association_terms(self):
        candidate = {**self.candidate, "relation": "associated_with"}
        query = validation.europe_pmc_query(candidate)
        self.assertIn("associated", query)

    def test_supported_decision_must_quote_a_stored_chunk(self):
        packet = {
            "evidence_documents": [
                {
                    "evidence_document_id": "external-1",
                    "evidence_chunks": [
                        {"chunk_id": "external-1:chunk-0", "text": "Cadmium caused kidney damage in mice."}
                    ],
                }
            ]
        }
        valid_decision = {
            "evidence_document_id": "external-1",
            "decision": "supported",
            "evidence": [
                {
                    "chunk_id": "external-1:chunk-0",
                    "quoted_span": "Cadmium caused kidney damage",
                }
            ],
        }
        invalid_decision = {
            "evidence_document_id": "external-1",
            "decision": "supported",
            "evidence": [{"chunk_id": "external-1:chunk-0", "quoted_span": "An invented quotation"}],
        }
        self.assertEqual(validation.decision_errors(packet, valid_decision), [])
        self.assertTrue(validation.decision_errors(packet, invalid_decision))


if __name__ == "__main__":
    unittest.main()
