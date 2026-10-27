import unittest

from backend.cli.candidates import EVIDENCE_RE, parse_tuple_line
from backend.stages.external_validation.proofs import decision_errors
from tests.external.helpers import judge_document
from backend.domain.claims import generate_hypothesis
from backend.stages.literature_retrieval.clients import (
    europe_pmc_open_access_pdf,
    extract_paragraphs,
    is_open_access,
    make_chunks,
)
from backend.stages.evidence_ranking.articles import _bm25_scores, metadata_evidence_coverage
from backend.stages.literature_retrieval.workflow import _citation_reserved_order, originating_source_match
from backend.stages.query_planning.planner import evidence_query_plan, translate_query


class ExternalValidationTests(unittest.TestCase):
    def test_retrieval_sentence_uses_claim_and_required_context_without_an_llm(self):
        sentence = generate_hypothesis({
            "subject": "infected grains", "relation": "has_contaminant", "object": "mycotoxins",
            "qualifiers": {"condition": "Fusarium head blight", "population": ""},
        })
        self.assertIn("infected grains has contaminant mycotoxins", sentence)
        self.assertIn("condition: Fusarium head blight", sentence)

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

    def test_external_judge_accepts_a_named_ochratoxin_member_for_the_family_endpoint(self):
        class JudgeClient:
            def chat_json(inner_self, _system, user, **_kwargs):
                self.assertNotIn("permitted_object_evidence_names", user)
                return {
                    "decision": "supported",
                    "matches": {"subject": True, "object": True, "relation": True,
                                "direction": True, "certainty": True},
                    "evidence": [{
                        "chunk_id": "external-ota:chunk-0", "sentence_numbers": [1],
                        "subject_mention": "maize", "object_mention": "Ochratoxin A",
                        "relation_trigger": "contamination", "stance": "supports",
                    }],
                    "rationale": "The named ochratoxin member was detected in maize.",
                }

        candidate = {"candidate_id": "c-ota", "subject": "maize", "relation": "has_contaminant",
                     "object": "ochratoxins", "subject_type": "Crop", "object_type": "Contaminant",
                     "certainty": "descriptive", "qualifiers": {}}
        document = {"evidence_document_id": "external-ota", "evidence_chunks": [{
            "chunk_id": "external-ota:chunk-0",
            "text": "Ochratoxin A contamination was detected in maize samples from Romanian farms.",
        }]}
        self.assertEqual(judge_document(JudgeClient(), candidate, document)["decision"], "supported")

    def test_external_judge_accepts_mycotoxin_increase_in_infected_grains(self):
        class JudgeClient:
            def chat_json(inner_self, _system, user, **_kwargs):
                return {
                    "decision": "supported",
                    "matches": {"subject": True, "object": True, "relation": True,
                                "direction": True, "certainty": True},
                    "evidence": [{
                        "chunk_id": "external-fhb:chunk-0", "sentence_numbers": [1],
                        "subject_mention": "FHB-infected barley, wheat, rye, and triticale grains",
                        "object_mention": "trichothecene mycotoxins",
                        "relation_trigger": "large increases", "stance": "supports",
                    }],
                    "rationale": "The selected sentence explicitly reports mycotoxins in infected grains.",
                }

        candidate = {"candidate_id": "c-fhb", "subject": "infected grains",
                     "relation": "has_contaminant", "object": "mycotoxins",
                     "subject_type": "Crop", "object_type": "Contaminant",
                     "certainty": "conditional", "qualifiers": {"condition": "Fusarium head blight"}}
        document = {"evidence_document_id": "external-fhb", "evidence_chunks": [{
            "chunk_id": "external-fhb:chunk-0",
            "text": ("FHB-infected barley, wheat, rye, and triticale grains exhibited large increases in "
                     "trichothecene mycotoxins following malting. Fusarium spp. accounted for most fungal hyphae."),
        }]}
        from backend.stages.external_validation.proofs import _numbered_chunks
        chunks = _numbered_chunks(document, candidate)
        self.assertEqual(chunks[0]['sentences'][0]['number'], 1)
        self.assertIn('Fusarium spp. accounted', chunks[0]['sentences'][1]['text'])
        self.assertEqual(chunks[0]['priority_sentence_numbers'], [1])
        self.assertEqual(judge_document(JudgeClient(), candidate, document)["decision"], "supported")

    def test_external_judge_requires_explicit_disease_condition(self):
        class JudgeClient:
            def chat_json(self, *_args, **_kwargs):
                return {
                    "decision": "supported",
                    "matches": {"subject": True, "object": True, "relation": True,
                                "direction": True, "certainty": True},
                    "evidence": [{
                        "chunk_id": "external-oat:chunk-0", "sentence_numbers": [1],
                        "subject_mention": "infected grains", "object_mention": "mycotoxins",
                        "relation_trigger": "produce", "stance": "supports",
                    }],
                    "rationale": "General infected-grain evidence.",
                }

        candidate = {"candidate_id": "c-condition", "subject": "infected grains",
                     "relation": "has_contaminant", "object": "mycotoxins",
                     "subject_type": "Crop", "object_type": "Contaminant",
                     "certainty": "conditional", "qualifiers": {"condition": "Fusarium head blight"}}
        document = {"evidence_document_id": "external-oat", "evidence_chunks": [{
            "chunk_id": "external-oat:chunk-0",
            "text": "Once the infected grains produce mycotoxins, their food safety requires monitoring.",
        }]}
        self.assertEqual(judge_document(JudgeClient(), candidate, document)["decision"], "needs_review")

    def test_external_judge_cannot_support_when_model_reports_missing_causal_relation(self):
        class JudgeClient:
            def chat_json(self, *_args, **_kwargs):
                return {
                    "decision": "supported",
                    "matches": {"subject": True, "object": True, "relation": False,
                                "direction": True, "certainty": True},
                    "evidence": [{
                        "chunk_id": "external-4:chunk-0", "sentence_numbers": [1],
                        "subject_mention": "Iron deficiency", "object_mention": "anaemia",
                        "relation_trigger": "deficiency", "stance": "supports",
                    }],
                    "rationale": "Co-measurement does not establish the claimed causal relation.",
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

    def test_plant_disease_query_receives_a_disambiguating_type_hint(self):
        class QueryClient:
            def chat_json(self, *_args, **_kwargs):
                return {"query": 'brown spot AND rice AND "grain quality"'}

        candidate = {"subject": "brown spot", "relation": "affects", "object": "grain quality",
                     "subject_type": "PlantDisease", "object_type": "PlantTrait", "qualifiers": {}}
        result = translate_query(QueryClient(), candidate)
        self.assertIn("disease OR infection", result["query"])
        self.assertIn('"brown spot"', result["query"])
        self.assertIn("disease_type_hint_added", result["query_strategy"])
        self.assertIn("disease_phrase_quoted", result["query_strategy"])

    def test_association_query_plan_adds_measurement_terms_without_rewriting_claim(self):
        candidate = {"subject": "False smut", "relation": "associated_with",
                     "object": "hybrid rice cultivation", "subject_type": "PlantDisease",
                     "object_type": "AgriculturalPractice", "qualifiers": {}}
        plan = evidence_query_plan(candidate, {"query": '("False smut" AND hybrid rice cultivation)'})
        self.assertEqual(plan["europe_pmc"][0], '("False smut" AND hybrid rice cultivation)')
        self.assertIn('"False smut" AND "hybrid rice"', plan["europe_pmc"][1])
        self.assertIn("incidence", plan["europe_pmc"][1])
        self.assertIn("severity", plan["openalex"][1])

    def test_contaminant_query_plan_includes_generic_singular_form(self):
        candidate = {"subject": "maize", "relation": "has_contaminant", "object": "ochratoxins",
                     "subject_type": "Crop", "object_type": "Contaminant", "qualifiers": {}}
        plan = evidence_query_plan(candidate, {"query": 'maize AND ochratoxins'})
        self.assertIn('"ochratoxin"', plan["europe_pmc"][1])
        self.assertIn("ochratoxin", plan["openalex"][1])

    def test_metadata_coverage_accepts_vetted_toxin_member_for_family_endpoint(self):
        candidate = {"subject": "maize", "relation": "has_contaminant", "object": "ochratoxins",
                     "subject_type": "Crop", "object_type": "Contaminant", "qualifiers": {}}
        exact_member = "Detection of Ochratoxin A in Maize"
        self.assertGreaterEqual(metadata_evidence_coverage(candidate, exact_member, exact_member), 4)

    def test_query_plan_keeps_explicit_disease_condition(self):
        candidate = {"subject": "infected grains", "relation": "has_contaminant", "object": "mycotoxins",
                     "subject_type": "Crop", "object_type": "Contaminant",
                     "qualifiers": {"condition": "Fusarium head blight"}}
        plan = evidence_query_plan(candidate, {"query": "infected grains AND mycotoxins"})
        self.assertIn('"Fusarium head blight"', plan["europe_pmc"][1])
        self.assertIn("Fusarium head blight", plan["openalex"][1])
        self.assertEqual(plan["openalex"][0], "infected grains mycotoxins Fusarium head blight")

    def test_metadata_coverage_prefers_exact_disease_material_study(self):
        candidate = {"subject": "infected grains", "relation": "has_contaminant", "object": "mycotoxins",
                     "qualifiers": {"condition": "Fusarium head blight"}}
        exact = "Fusarium Head Blight–Infected Grains and Elevated Mycotoxin Production"
        topical = "Review of fungal pathogens causing wheat head blight and mycotoxin accumulation"
        self.assertGreater(metadata_evidence_coverage(candidate, exact),
                           metadata_evidence_coverage(candidate, topical))

    def test_metadata_coverage_gives_exact_title_priority_over_a_broad_review(self):
        candidate = {"subject": "infected grains", "relation": "has_contaminant", "object": "mycotoxins",
                     "qualifiers": {"condition": "Fusarium head blight"}}
        exact_title = "Fusarium Head Blight-Infected Grains and Elevated Mycotoxin Production"
        broad_title = "Fusarium Head Blight, Mycotoxins and Strategies for Their Reduction"
        self.assertGreater(
            metadata_evidence_coverage(candidate, exact_title, exact_title),
            metadata_evidence_coverage(candidate, broad_title, broad_title),
        )

    def test_explicit_europe_pmc_oa_pdf_is_usable(self):
        result = {"isOpenAccess": "N", "fullTextUrlList": {"fullTextUrl": [{
            "availabilityCode": "OA", "documentStyle": "pdf",
            "url": "https://example.org/article.pdf",
        }]}}
        self.assertEqual(europe_pmc_open_access_pdf(result), "https://example.org/article.pdf")
        self.assertTrue(is_open_access(result))

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

    def test_exact_source_citation_receives_a_reserved_evidence_slot(self):
        broad = (0.9, ({"_retrieval_route": "broad_search", "id": "broad"}, [], ""))
        cited = (0.1, ({"_retrieval_route": "source_citation", "id": "cited"}, [], ""))
        ordered = _citation_reserved_order([broad, cited], 1)
        self.assertEqual(ordered[0][1][0]["id"], "cited")

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
