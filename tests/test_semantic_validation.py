"""Focused tests for semantic validation actions and safe type normalization."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.semantic_validation import validate_claim_semantics


class ResponseSequenceClient:
    def __init__(self, responses):
        self.settings = SimpleNamespace(model="test-model")
        self.responses = iter(responses)
        self.calls = []

    def chat_json(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return next(self.responses)


def candidate(**overrides):
    value = {
        "candidate_id": "semantic-1",
        "subject": "Organic food consumption",
        "subject_type": "AgriculturalPractice",
        "relation": "associated_with",
        "object": "lower lymphoma incidence",
        "object_type": "HumanHealthOutcome",
        "certainty": "associative",
        "qualifiers": {},
        "source_evidence_span": "Organic food consumption was associated with lower lymphoma incidence.",
    }
    value.update(overrides)
    return value


def type_response(subject_type, object_type, *, ambiguous=False, in_scope=True):
    return {
        "subject_type": subject_type,
        "object_type": object_type,
        "in_scope": in_scope,
        "ambiguous": ambiguous,
        "rationale": "Type classification.",
    }


class SemanticValidationTests(unittest.TestCase):
    def test_unambiguous_type_only_correction_is_accepted_without_mutating_candidate(self):
        claim = candidate()
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome"),
        ]))

        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(result["action"], "normalize_types")
        self.assertEqual(result["normalized_types"], {
            "subject_type": "HumanExposure",
            "object_type": "HumanHealthOutcome",
        })
        self.assertEqual(result["type_normalization"]["changed"], ["subject_type"])
        self.assertNotIn("reasons", result)
        self.assertEqual(claim["subject_type"], "AgriculturalPractice")

    def test_correct_types_and_semantics_are_accepted_without_normalization(self):
        claim = candidate(subject_type="HumanExposure")
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome"),
        ]))

        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(result["action"], "accept")
        self.assertNotIn("normalized_types", result)
        self.assertNotIn("suggested", result)

    def test_local_section_heading_is_supplied_to_semantic_check(self):
        claim = candidate(subject="False smut", subject_type="PlantDisease",
                          object="hybrid rice cultivation", object_type="AgriculturalPractice",
                          source_evidence_span="Disease intensity was associated with hybrid rice cultivation.",
                          source_evidence_section_heading="False smut of rice: Asia, today")
        client = ResponseSequenceClient([
            type_response("PlantDisease", "AgriculturalPractice"),
        ])
        result = validate_claim_semantics(claim, client)

        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(len(client.calls), 1)
        self.assertIn("[LOCAL SECTION HEADING] False smut of rice: Asia, today.", client.calls[0][0][1])

    def test_classifier_cannot_veto_valid_crop_edge_by_typing_its_farming_context(self):
        claim = candidate(subject="maize", subject_type="Crop", relation="has_contaminant",
                          object="fumonisins", object_type="Contaminant",
                          subject_source_mention="maize-based subsistence farming")
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("AgriculturalPractice", "Contaminant"),
        ]))
        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(result["action"], "accept")
        self.assertEqual(result["type_disagreement"]["classified"]["subject_type"], "AgriculturalPractice")

    def test_scope_failure_is_rejected_and_never_normalizes_types(self):
        claim = candidate()
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome", in_scope=False),
        ]))

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["action"], "reject")
        self.assertNotIn("normalized_types", result)
        self.assertIn("Claim is outside the project pathway", result["reasons"])

    def test_ambiguous_type_correction_remains_in_review(self):
        claim = candidate()
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome", ambiguous=True),
        ]))

        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(result["action"], "accept")
        self.assertNotIn("normalized_types", result)
        self.assertEqual(claim["subject_type"], "AgriculturalPractice")

    def test_pathway_shape_rejects_programme_to_programme_edge(self):
        claim = candidate(subject="Organic breeding programme", subject_type="AgriculturalPractice",
                          relation="has_trait", object="farmer participatory", object_type="AgriculturalPractice")
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("AgriculturalPractice", "AgriculturalPractice"),
        ]))

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["action"], "reject")
        self.assertTrue(any("outside the accepted soil-to-human-health pathway" in reason
                            for reason in result["reasons"]))

    def test_missing_comparative_qualifiers_are_rejected(self):
        claim = candidate(subject="Organic crops", subject_type="Crop", relation="lower_than",
                          object="Conventional crops", object_type="Crop", qualifiers={})
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("Crop", "Crop"),
        ]))

        self.assertEqual(result["decision"], "rejected")
        self.assertTrue(any("lower_than requires both comparator and metric" in reason
                            for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
