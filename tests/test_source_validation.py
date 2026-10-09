import unittest
from types import SimpleNamespace

from backend.ontology_schema import schema_error
from backend.pipeline import page_sentences
from backend.semantic_validation import validate_claim_semantics
from backend.source_validation import validate_source_claim


class FakeClient:
    def __init__(self, response):
        self.settings = SimpleNamespace(model="test-model")
        self.response = response

    def chat_json(self, *_args, **_kwargs):
        return self.response


class ResponseSequenceClient:
    def __init__(self, responses):
        self.settings = SimpleNamespace(model="test-model")
        self.responses = iter(responses)

    def chat_json(self, *_args, **_kwargs):
        return next(self.responses)


class SourceValidationTests(unittest.TestCase):
    def test_bullets_remain_separate_evidence_units(self):
        self.assertEqual(page_sentences("Microbes improve soil. • Cadmium accumulates in rice. • Exposure harms people."),
                         ["Microbes improve soil.", "Cadmium accumulates in rice.", "Exposure harms people."])

    def test_controlled_vocabulary_accepts_known_labels(self):
        claim = {"subject_type": "AgriculturalPractice", "relation": "increases", "object_type": "Contaminant"}
        self.assertIsNone(schema_error(claim))

    def test_new_contaminant_and_biological_process_labels_are_valid(self):
        claim = {"subject_type": "Crop", "relation": "has_contaminant",
                 "object_type": "Contaminant"}
        self.assertIsNone(schema_error(claim))
        process_claim = {"subject_type": "Nutrient", "relation": "affects",
                         "object_type": "HumanBiologicalProcess"}
        self.assertIsNone(schema_error(process_claim))

    def test_semantic_gate_rejects_mistyped_consumption_claim(self):
        candidate = {"candidate_id": "semantic-1", "subject": "Organic food consumption",
                     "subject_type": "AgriculturalPractice", "relation": "associated_with",
                     "object": "lower lymphoma incidence", "object_type": "HumanHealthOutcome",
                     "certainty": "associative", "qualifiers": {},
                     "source_evidence_span": "Organic food consumption was associated with lower lymphoma incidence."}
        client = ResponseSequenceClient([
            {"subject_type": "HumanExposure", "object_type": "HumanHealthOutcome",
             "subject_ambiguous": False, "object_ambiguous": False, "rationale": "Consumption is exposure."},
            {"in_scope": True, "relation_match": True, "direction_match": True, "atomic_claim": True,
             "ambiguous": False, "suggested_relation": "associated_with", "rationale": "Direct association."},
        ])
        result = validate_claim_semantics(candidate, client)
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["suggested"]["subject_type"], "HumanExposure")

    def test_wrong_subject_cannot_pass_even_if_judge_says_entailed(self):
        span = "These aggregates improve soil structure and nutrient transport."
        claim = {"candidate_id": "c1", "subject": "Rhizosphere microorganisms", "relation": "affects",
                 "object": "nutrient transport", "source_evidence_span": span, "source_evidence_quote": span,
                 "source_evidence_sentence_numbers": [1]}
        response = {"decision": "entailed", "subject_match": True, "object_match": True, "relation_match": True,
                    "direction_match": True, "certainty_match": True, "type_match": True,
                    "subject_quote": "These aggregates", "relation_quote": "improve", "object_quote": "nutrient transport"}
        result = validate_source_claim(claim, FakeClient(response))
        self.assertEqual(result["decision"], "needs_review")

    def test_association_does_not_entail_reduction(self):
        span = "Phenolic content in vegetables is associated with lower cardiovascular risk."
        claim = {"candidate_id": "c2", "subject": "Phenolic content", "relation": "reduces",
                 "object": "cardiovascular risk", "source_evidence_span": span, "source_evidence_quote": span,
                 "source_evidence_sentence_numbers": [1]}
        response = {"decision": "entailed", "subject_match": True, "object_match": True, "relation_match": True,
                    "direction_match": True, "certainty_match": True, "type_match": True,
                    "subject_quote": "Phenolic content", "relation_quote": "associated with lower",
                    "object_quote": "cardiovascular risk"}
        self.assertEqual(validate_source_claim(claim, FakeClient(response))["decision"], "needs_review")


if __name__ == "__main__":
    unittest.main()
