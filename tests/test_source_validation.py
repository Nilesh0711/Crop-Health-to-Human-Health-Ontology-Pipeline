import unittest
from types import SimpleNamespace

from backend.ontology_schema import schema_error
from backend.pipeline import citation_numbers, page_sentences
from backend.source_validation import validate_source_claim


class FakeClient:
    def __init__(self, response):
        self.settings = SimpleNamespace(model="test-model")
        self.response = response

    def chat_json(self, *_args, **_kwargs):
        return self.response


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

    def test_trait_and_comparative_relations_are_in_controlled_vocabulary(self):
        self.assertIsNone(schema_error({"subject_type": "Crop", "relation": "has_trait", "object_type": "PlantTrait"}))
        self.assertIsNone(schema_error({"subject_type": "Crop", "relation": "higher_than", "object_type": "Crop"}))
        self.assertIsNone(schema_error({"subject_type": "Crop", "relation": "lower_than", "object_type": "Crop"}))

    def test_citation_numbers_expand_ranges_and_lists(self):
        self.assertEqual(citation_numbers("Reported in [6,25,27–29] and confirmed by [64-66]."),
                         [6, 25, 27, 28, 29, 64, 65, 66])

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

    def test_camelcase_legacy_entities_anchor_to_spaced_evidence(self):
        span = "Soil texture influences crop nutrient uptake in maize."
        claim = {"candidate_id": "c3", "subject": "SoilTexture", "relation": "affects",
                 "object": "CropNutrientUptake", "source_evidence_span": span,
                 "source_evidence_sentence_numbers": [1]}
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": True, "proof": {"subject": "Soil texture", "relation": "influences",
                                                           "object": "crop nutrient uptake"},
                    "rationale": "The evidence states the relationship."}
        self.assertEqual(validate_source_claim(claim, FakeClient(response))["decision"], "entailed")

    def test_explicit_source_mentions_override_canonical_entity_aliases(self):
        span = "Soil organic carbon increased phosphorus uptake in rice."
        claim = {"candidate_id": "c4", "subject": "SOC", "subject_source_mention": "soil organic carbon",
                 "relation": "increases", "relation_trigger": "increased",
                 "object": "PhosphorusUptake", "object_source_mention": "phosphorus uptake",
                 "source_evidence_span": span, "source_evidence_sentence_numbers": [1]}
        claim["source_context_packet"] = "Soil organic carbon (SOC) was measured before treatment. " + span
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": True, "proof": {"subject": "soil organic carbon", "relation": "increased",
                                                           "object": "phosphorus uptake"},
                    "rationale": "The stated source mentions and trigger are explicit."}
        self.assertEqual(validate_source_claim(claim, FakeClient(response))["decision"], "entailed")

    def test_source_surface_cannot_be_assigned_to_a_different_canonical_entity(self):
        span = "Wheat contains cadmium in grains collected from the field."
        claim = {"candidate_id": "c-canonical-mismatch", "subject": "Rice",
                 "subject_source_mention": "Wheat", "relation": "has_contaminant",
                 "relation_trigger": "contains", "object": "Cadmium",
                 "object_source_mention": "cadmium", "source_evidence_span": span,
                 "source_evidence_sentence_numbers": [1], "source_context_packet": span}
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": True, "proof": {"subject": "Wheat", "relation": "contains",
                                                           "object": "cadmium"},
                    "rationale": "The source phrase is explicit."}
        result = validate_source_claim(claim, FakeClient(response))
        self.assertEqual(result["decision"], "needs_review")
        self.assertIn("Claim subject is not safely aligned with its source surface mention", result["reasons"])

    def test_explicitly_defined_abbreviation_can_align_canonical_and_source_terms(self):
        span = "Soil organic carbon increased phosphorus uptake in rice."
        context = "Soil organic carbon (SOC) was measured before treatment. " + span
        claim = {"candidate_id": "c-defined-alias", "subject": "SOC",
                 "subject_source_mention": "soil organic carbon", "relation": "increases",
                 "relation_trigger": "increased", "object": "Phosphorus uptake",
                 "object_source_mention": "phosphorus uptake", "source_evidence_span": span,
                 "source_evidence_sentence_numbers": [1], "source_context_packet": context}
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": True, "proof": {"subject": "Soil organic carbon", "relation": "increased",
                                                           "object": "phosphorus uptake"},
                    "rationale": "The source explicitly defines SOC."}
        self.assertEqual(validate_source_claim(claim, FakeClient(response))["decision"], "entailed")

    def test_trigger_mapping_blocks_an_association_mislabelled_as_reduction(self):
        span = "Phenolic content is associated with lower cardiovascular risk."
        claim = {"candidate_id": "c5", "subject": "PhenolicContent",
                 "subject_source_mention": "Phenolic content", "relation": "reduces",
                 "relation_trigger": "associated with lower", "object": "CardiovascularRisk",
                 "object_source_mention": "cardiovascular risk", "source_evidence_span": span,
                 "source_evidence_sentence_numbers": [1]}
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": True, "proof": {"subject": "Phenolic content",
                                                           "relation": "associated with lower",
                                                           "object": "cardiovascular risk"},
                    "rationale": "The claims are related."}
        result = validate_source_claim(claim, FakeClient(response))
        self.assertEqual(result["decision"], "needs_review")
        self.assertIn("Associative evidence does not establish a causal or directional effect", result["reasons"])

    def test_contaminant_trigger_is_checked_against_relation_mapping(self):
        span = "Cadmium was detected in rice grains collected from the field."
        claim = {"candidate_id": "c6", "subject": "RiceGrains", "subject_source_mention": "rice grains",
                 "relation": "has_contaminant", "relation_trigger": "detected in", "object": "Cadmium",
                 "object_source_mention": "Cadmium", "source_evidence_span": span,
                 "source_evidence_sentence_numbers": [1]}
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": True, "proof": {"subject": "rice grains", "relation": "detected in",
                                                           "object": "Cadmium"},
                    "rationale": "The contaminant is explicitly detected in the crop material."}
        self.assertEqual(validate_source_claim(claim, FakeClient(response))["decision"], "entailed")

    def test_deficiency_relation_needs_a_deficiency_context_and_causal_trigger(self):
        span = "Iron deficiency caused anaemia among the study participants."
        claim = {"candidate_id": "c7", "subject": "IronDeficiency", "subject_source_mention": "Iron deficiency",
                 "relation": "deficiency_causes", "relation_trigger": "caused", "object": "Anaemia",
                 "object_source_mention": "anaemia", "source_evidence_span": span,
                 "source_evidence_sentence_numbers": [1]}
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": True, "proof": {"subject": "Iron deficiency", "relation": "caused",
                                                           "object": "anaemia"},
                    "rationale": "The deficiency and causal link are explicit."}
        self.assertEqual(validate_source_claim(claim, FakeClient(response))["decision"], "entailed")

    def test_directional_relation_with_causal_certainty_is_sent_to_review(self):
        span = "The ley phase was shown to reduce disease propagules in agricultural soil."
        claim = {"candidate_id": "c8", "subject": "Ley phase", "subject_source_mention": "ley phase",
                 "relation": "reduces", "relation_trigger": "was shown to reduce", "object": "Disease propagules",
                 "object_source_mention": "disease propagules", "certainty": "causal",
                 "source_evidence_span": span, "source_evidence_sentence_numbers": [1]}
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": False, "proof": {"subject": "ley phase", "relation": "was shown to reduce",
                                                            "object": "disease propagules"},
                    "rationale": "The direction is explicitly reported."}
        result = validate_source_claim(claim, FakeClient(response))
        self.assertEqual(result["decision"], "needs_review")
        self.assertIn("certainty_match was not confirmed", result["reasons"][0])

    def test_unlisted_but_anchored_scientific_trigger_is_not_vetoed(self):
        span = "Air temperature was identified as a positive driver for phenolic concentrations."
        claim = {"candidate_id": "c9", "subject": "Air temperature", "subject_source_mention": "Air temperature",
                 "relation": "increases", "relation_trigger": "positive driver for", "object": "Phenolic concentrations",
                 "object_source_mention": "phenolic concentrations", "certainty": "descriptive",
                 "source_evidence_span": span, "source_evidence_sentence_numbers": [1]}
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": True, "proof": {"subject": "Air temperature", "relation": "positive driver for",
                                                           "object": "phenolic concentrations"},
                    "rationale": "The source reports a positive direction."}
        self.assertEqual(validate_source_claim(claim, FakeClient(response))["decision"], "entailed")

    def test_hedged_source_cannot_pass_as_descriptive_even_if_judge_approves(self):
        span = "Silicon-based agro-wastes can reduce arsenic bioaccumulation in crops."
        claim = {"candidate_id": "c10", "subject": "Silicon-based agro-wastes",
                 "subject_source_mention": "Silicon-based agro-wastes", "relation": "reduces",
                 "relation_trigger": "reduce", "object": "arsenic bioaccumulation in crops",
                 "object_source_mention": "arsenic bioaccumulation in crops", "certainty": "descriptive",
                 "source_evidence_span": span, "source_evidence_sentence_numbers": [1]}
        response = {"decision": "entailed", "relation_match": True, "direction_match": True,
                    "certainty_match": True, "proof": {"subject": "Silicon-based agro-wastes",
                    "relation": "reduce", "object": "arsenic bioaccumulation in crops"},
                    "rationale": "The source states the directional relation."}
        result = validate_source_claim(claim, FakeClient(response))
        self.assertEqual(result["decision"], "needs_review")
        self.assertIn("Hedged source wording requires conditional certainty", result["reasons"])


if __name__ == "__main__":
    unittest.main()
