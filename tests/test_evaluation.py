import unittest

from backend.evaluation import evaluation_sample, score_evaluation
from backend.vocabulary import normalize_entity


class EvaluationTests(unittest.TestCase):
    def test_evaluation_sample_keeps_predictions_separate_from_blank_gold(self):
        candidate = {
            "candidate_id": "one", "subject": "rice", "relation": "has_contaminant", "object": "cadmium",
            "subject_type": "Crop", "object_type": "Contaminant", "certainty": "descriptive", "qualifiers": {},
            "source_evidence_span": "Cadmium was detected in rice.", "schema_error": None,
            "semantic_validation": {"decision": "accepted"}, "source_validation": {"decision": "entailed"},
        }
        records = evaluation_sample([candidate], [], 10)
        self.assertEqual(records[0]["prediction"]["status"], "external_pending")
        self.assertIsNone(records[0]["gold"]["valid_triple"])

    def test_scoring_uses_only_completed_gold_fields(self):
        record = {
            "claim": {"relation": "has_contaminant", "subject_type": "Crop", "object_type": "Contaminant"},
            "prediction": {"semantic": "accepted", "source_entailment": "entailed",
                           "retrieved_document_ids": ["paper-a", "paper-b"]},
            "gold": {"valid_triple": True, "relation": "has_contaminant", "subject_type": "Crop",
                     "object_type": "Contaminant", "source_entailment": "entailed",
                     "relevant_evidence_document_ids": ["paper-b"], "reviewer": "reviewer-1"},
        }
        metrics = score_evaluation([record], 2)
        self.assertEqual(metrics["triple_validity_accuracy"], 1.0)
        self.assertEqual(metrics["triple_validity_precision"], 1.0)
        self.assertEqual(metrics["triple_validity_recall"], 1.0)
        self.assertEqual(metrics["triple_validity_f1"], 1.0)
        self.assertEqual(metrics["retrieval_recall@2"], 1.0)

    def test_exact_chebi_normalization_is_auditable(self):
        result = normalize_entity("Cadmium", "Contaminant")
        self.assertEqual(result["vocabulary_id"], "CHEBI:22977")
        self.assertEqual(result["match_method"], "exact_alias")


if __name__ == "__main__":
    unittest.main()

