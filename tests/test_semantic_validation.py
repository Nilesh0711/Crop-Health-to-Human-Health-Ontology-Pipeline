"""Focused tests for semantic validation actions and safe type normalization."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.semantic_validation import validate_claim_semantics


class ResponseSequenceClient:
    def __init__(self, responses):
        self.settings = SimpleNamespace(model="test-model")
        self.responses = iter(responses)

    def chat_json(self, *_args, **_kwargs):
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


def type_response(subject_type, object_type, *, ambiguous=False):
    return {
        "subject_type": subject_type,
        "object_type": object_type,
        "ambiguous": ambiguous,
        "rationale": "Type classification.",
    }


def relation_response(**overrides):
    value = {
        "in_scope": True,
        "relation_match": True,
        "direction_match": True,
        "atomic_claim": True,
        "qualifiers_complete": True,
        "ambiguous": False,
        "suggested_relation": "associated_with",
        "rationale": "Relation classification.",
    }
    value.update(overrides)
    return value


class SemanticValidationTests(unittest.TestCase):
    def test_unambiguous_type_only_correction_is_accepted_without_mutating_candidate(self):
        claim = candidate()
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome"),
            relation_response(),
        ]))

        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(result["action"], "normalize_types")
        self.assertEqual(result["normalized_types"], {
            "subject_type": "HumanExposure",
            "object_type": "HumanHealthOutcome",
        })
        self.assertEqual(result["type_normalization"]["changed"], ["subject_type"])
        self.assertEqual(result["suggested"]["subject_type"], "HumanExposure")
        self.assertNotIn("reasons", result)
        self.assertEqual(claim["subject_type"], "AgriculturalPractice")

    def test_correct_types_and_semantics_are_accepted_without_normalization(self):
        claim = candidate(subject_type="HumanExposure")
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome"),
            relation_response(),
        ]))

        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(result["action"], "accept")
        self.assertNotIn("normalized_types", result)
        self.assertNotIn("suggested", result)

    def test_relation_failure_is_rejected_and_never_normalizes_types(self):
        claim = candidate()
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome"),
            relation_response(relation_match=False, suggested_relation="reduces"),
        ]))

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["action"], "reject")
        self.assertNotIn("normalized_types", result)
        self.assertEqual(claim["subject_type"], "AgriculturalPractice")
        self.assertIn("relation_match was not confirmed", result["reasons"])

    def test_direction_failure_is_rejected_and_never_normalizes_types(self):
        claim = candidate()
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome"),
            relation_response(direction_match=False),
        ]))

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["action"], "reject")
        self.assertNotIn("normalized_types", result)
        self.assertIn("direction_match was not confirmed", result["reasons"])

    def test_scope_failure_is_rejected_and_never_normalizes_types(self):
        claim = candidate()
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome"),
            relation_response(in_scope=False),
        ]))

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["action"], "reject")
        self.assertNotIn("normalized_types", result)
        self.assertIn("in_scope was not confirmed", result["reasons"])

    def test_ambiguous_type_correction_remains_in_review(self):
        claim = candidate()
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("HumanExposure", "HumanHealthOutcome", ambiguous=True),
            relation_response(),
        ]))

        self.assertEqual(result["decision"], "needs_review")
        self.assertEqual(result["action"], "review")
        self.assertNotIn("normalized_types", result)
        self.assertEqual(claim["subject_type"], "AgriculturalPractice")

    def test_pathway_shape_rejects_programme_to_programme_edge(self):
        claim = candidate(subject="Organic breeding programme", subject_type="AgriculturalPractice",
                          relation="has_trait", object="farmer participatory", object_type="AgriculturalPractice")
        result = validate_claim_semantics(claim, ResponseSequenceClient([
            type_response("AgriculturalPractice", "AgriculturalPractice"),
            relation_response(),
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
            relation_response(suggested_relation="lower_than"),
        ]))

        self.assertEqual(result["decision"], "rejected")
        self.assertTrue(any("lower_than requires both comparator and metric" in reason
                            for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
