import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import networkx as nx

from fastapi.testclient import TestClient

from backend.app import app
from backend.graph import graph_from_candidates, reasoning_paths, write_graphml
from backend.ontology_schema import pathway_error
from backend.pipeline import EXTRACTION_RESPONSE_SCHEMA, anchored_quote, extraction_shape_error
from external_validator.workflow import evidence_ledger


class WorkbenchTests(unittest.TestCase):
    def test_automatic_extraction_schema_blocks_causal_relations(self):
        relations = EXTRACTION_RESPONSE_SCHEMA["properties"]["claims"]["items"]["properties"]["relation"]["enum"]
        self.assertNotIn("causes", relations)
        self.assertNotIn("leads_to", relations)
        self.assertNotIn("deficiency_causes", relations)
        self.assertEqual(
            extraction_shape_error({"subject": "Cadmium", "relation": "causes", "object": "kidney damage"},
                                   "Cadmium caused kidney damage."),
            "relation_not_allowed_in_automatic_extraction",
        )

    def test_pathway_accepts_soil_composition_and_direct_exposure(self):
        self.assertIsNone(pathway_error({"subject_type": "SoilMaterial", "object_type": "Contaminant",
                                        "relation": "has_contaminant", "qualifiers": {}}))
        self.assertIsNone(pathway_error({"subject_type": "AgriculturalPractice", "object_type": "HumanExposure",
                                        "relation": "reduces", "qualifiers": {}}))

    def test_quote_anchor_recovers_original_punctuation(self):
        source = "Rice false smut reduced grain quality – particularly in exposed fields."
        proposed = "Rice false smut reduced grain quality � particularly in exposed fields."
        self.assertEqual(anchored_quote(source, proposed), source[:-1])
        self.assertIsNone(anchored_quote(source, "Rice false smut increased grain quality in exposed fields."))

    def test_extraction_shape_rejects_unqualified_comparison_and_purpose_outline(self):
        comparison = {"subject": "Organic crops", "object": "cadmium concentration", "qualifiers": {}}
        self.assertEqual(extraction_shape_error(comparison, "Organic crops had lower cadmium than conventional crops."),
                         "comparative_evidence_missing_comparator_or_metric")
        purpose = {"subject": "Organic amendments", "object": "soil suppressiveness", "qualifiers": {}}
        self.assertEqual(extraction_shape_error(purpose, "Use of organic amendments to increase soil suppressiveness."),
                         "purpose_or_recommendation_not_empirical_evidence")

    def test_api_lists_pdfs_without_running_pipeline(self):
        client = TestClient(app)
        response = client.get("/api/pdfs")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)
        for paper in response.json():
            self.assertIn("id", paper)
            self.assertIn("name", paper)
            self.assertIn("document_scope", paper)

    def test_graph_preserves_evidence_status_and_forward_paths(self):
        candidates = [
            {"candidate_id": "one", "subject": "Irrigation", "subject_type": "AgriculturalPractice",
             "object": "Soil moisture", "object_type": "SoilCondition", "relation": "increases",
             "schema_error": None, "source_paper_title": "Paper A", "source_evidence_quote": "Quote A"},
            {"candidate_id": "two", "subject": "Soil moisture", "subject_type": "SoilCondition",
             "object": "Iron-rich rice", "object_type": "Crop", "relation": "affects",
             "schema_error": None, "source_paper_title": "Paper B", "source_evidence_quote": "Quote B"},
            {"candidate_id": "three", "subject": "Iron-rich rice", "subject_type": "Crop",
             "object": "Iron content", "object_type": "Nutrient", "relation": "has_nutrient",
             "schema_error": None, "source_paper_title": "Paper C", "source_evidence_quote": "Quote C"},
            {"candidate_id": "four", "subject": "Iron content", "subject_type": "Nutrient",
             "object": "Dietary iron intake", "object_type": "HumanExposure", "relation": "increases",
             "schema_error": None, "source_paper_title": "Paper D", "source_evidence_quote": "Quote D"},
            {"candidate_id": "five", "subject": "Dietary iron intake", "subject_type": "HumanExposure",
             "object": "Iron deficiency anaemia", "object_type": "HumanHealthOutcome", "relation": "reduces",
             "schema_error": None, "source_paper_title": "Paper E", "source_evidence_quote": "Quote E"},
        ]
        results = [
            {"candidate_id": candidate["candidate_id"], "attempts": [{"decisions": [
                {"evidence_document_id": "article-1", "decision": "supported"},
                {"evidence_document_id": "article-2", "decision": "supported"},
            ]}]}
            for candidate in candidates
        ]
        graph = graph_from_candidates(candidates, results)
        self.assertEqual(graph["counts"]["corroborated"], 5)
        paths = reasoning_paths(graph)
        self.assertEqual(len(paths), 1)
        self.assertEqual([edge["id"] for edge in paths[0]], ["one", "two", "three", "four", "five"])
        with TemporaryDirectory() as directory:
            output = Path(directory) / "graph.graphml"
            write_graphml(graph, output)
            exported = nx.read_graphml(output)
            self.assertEqual(exported.number_of_edges(), 5)

    def test_evidence_ledger_counts_distinct_articles(self):
        record = {"candidate_id": "one", "candidate": {}, "validation_status": "world_supported_pending_corroboration",
                  "attempts": [{"decisions": [{"evidence_document_id": "article-1", "decision": "supported"}]},
                               {"decisions": [{"evidence_document_id": "article-1", "decision": "supported"}]}]}
        ledger = evidence_ledger([record])
        self.assertEqual(ledger[0]["support_count"], 1)

    def test_source_rejected_claim_never_enters_main_graph(self):
        candidate = {"candidate_id": "bad-source", "subject": "False smut", "subject_type": "PlantDisease",
                     "object": "Rice", "object_type": "Crop", "relation": "affects", "schema_error": None,
                     "scope_error": None, "source_validation": {"decision": "unsupported", "rationale": "Wrong PDF span"}}
        result = {"candidate_id": "bad-source", "attempts": [{"decisions": [
            {"evidence_document_id": "article-1", "decision": "supported"}]}]}
        self.assertEqual(graph_from_candidates([candidate], [result], view="main")["counts"]["edges"], 0)
        audit = graph_from_candidates([candidate], [result], view="audit")
        self.assertEqual(audit["edges"][0]["status"], "source_unsupported")

    def test_source_supported_graph_is_separate_from_external_graph(self):
        candidate = {"candidate_id": "source-only", "subject": "Ley phase", "subject_type": "AgriculturalPractice",
                     "object": "Weed pressure", "object_type": "PlantTrait", "relation": "reduces", "schema_error": None,
                     "semantic_validation": {"decision": "accepted"}, "source_validation": {"decision": "entailed"}}
        result = {"candidate_id": "source-only", "validation_status": "external_evidence_not_found",
                  "attempts": [{"decisions": []}]}
        self.assertEqual(graph_from_candidates([candidate], [result], view="main")["counts"]["edges"], 0)
        source = graph_from_candidates([candidate], [result], view="source")
        self.assertEqual(source["edges"][0]["status"], "source_supported")
        self.assertEqual(source["counts"]["source_supported"], 1)

    def test_source_reference_support_does_not_enter_independent_graph(self):
        candidate = {"candidate_id": "reference-only", "subject": "Rice", "subject_type": "Crop",
                     "object": "Cadmium", "object_type": "Contaminant", "relation": "has_contaminant",
                     "schema_error": None, "semantic_validation": {"decision": "accepted"},
                     "source_validation": {"decision": "entailed"}}
        result = {"candidate_id": "reference-only", "attempts": [{"decisions": [{
            "evidence_document_id": "cited-paper", "decision": "supported", "evidence_level": "source_reference"
        }]}], "support_summary": {"source_reference": 1, "independent_external": 0, "independent_groups": 0}}
        self.assertEqual(graph_from_candidates([candidate], [result], view="main")["counts"]["edges"], 0)
        audit = graph_from_candidates([candidate], [result], view="audit")
        self.assertEqual(audit["edges"][0]["status"], "source_reference_supported")


if __name__ == "__main__":
    unittest.main()
