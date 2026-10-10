import unittest

from backend.pipeline import REFERENCE_HEADING_RE, _section_name, source_surface_mention
from backend.relevance_validation import assess_text_quality, classify_document_scope


class FakeClient:
    def __init__(self, response):
        self.response = response

    def chat_json(self, *_args, **_kwargs):
        return self.response


class RelevanceValidationTests(unittest.TestCase):
    def test_source_surface_mention_returns_verbatim_evidence(self):
        evidence = "Soil texture strongly influences root growth in maize."
        self.assertEqual(source_surface_mention(evidence, "soil texture"), "Soil texture")
        self.assertEqual(source_surface_mention(evidence, "influences"), "influences")
        self.assertIsNone(source_surface_mention(evidence, "SoilTexture"))
        self.assertIsNone(source_surface_mention(evidence, "improves root growth"))

    def test_common_section_and_reference_headings_are_recognized(self):
        self.assertEqual(_section_name("1 Introduction"), "1 Introduction")
        self.assertEqual(_section_name("1. Introduction"), "1. Introduction")
        self.assertEqual(_section_name("2.1 Methods"), "2.1 Methods")
        self.assertIsNone(_section_name("2 Department of Agricultural and Environmental Sciences"))
        self.assertIsNone(_section_name("1 INT"))
        self.assertIsNone(_section_name("5. Stability of"))
        self.assertIsNone(_section_name("1. Deliberate ingestion of soil is"))
        self.assertIsNone(_section_name("2. Provide substrate for crop growth"))
        self.assertIsNotNone(REFERENCE_HEADING_RE.match("6. References"))
        self.assertIsNotNone(REFERENCE_HEADING_RE.match("5 Bibliography"))

    def test_text_quality_flags_fragmented_pdf_text_without_suppressing_it(self):
        text = " ".join(["Agricultural evidence links soil health to crops."] * 12)
        quality = assess_text_quality(text)
        self.assertEqual(quality["detected_quality"], "usable")

        degraded = " ".join(["agri ee cultural evidence shows soil effects."] * 12)
        self.assertEqual(assess_text_quality(degraded)["detected_quality"], "degraded")

    def test_document_scope_preserves_upstream_paper_and_detected_quality(self):
        packets = [{"section_heading": "Abstract", "sentences": [
            {"text": "Crop rotation increased soil microbial diversity and nutrient cycling."},
            {"text": "The study measured soil biological activity in maize fields."},
        ]}]
        result = classify_document_scope("Soil mechanisms", packets, FakeClient({
            "scope": "upstream_only", "text_quality": "usable", "rationale": "Mechanistic soil evidence.",
        }))
        self.assertEqual(result["scope"], "upstream_only")
        self.assertEqual(result["text_quality"], "usable")

    def test_document_scope_keeps_uncertainty_non_destructive(self):
        packets = [{"section_heading": "Abstract", "sentences": [
            {"text": "The analysis compares regional crop statistics and market outcomes."},
            {"text": "No mechanism is described in this short sample."},
        ]}]
        result = classify_document_scope("Regional analysis", packets, FakeClient({
            "scope": "uncertain", "text_quality": "usable", "rationale": "Not enough detail.",
        }))
        self.assertEqual(result["scope"], "uncertain")

    def test_model_quality_alone_cannot_abort_readable_pdf(self):
        packets = [{"section_heading": "Abstract", "sentences": [
            {"text": "Crop rotation increased soil microbial diversity and nutrient cycling in maize fields."},
            {"text": "The researchers measured a direct biological soil mechanism across replicated plots."},
        ]}]
        result = classify_document_scope("Readable paper", packets, FakeClient({
            "scope": "upstream_only", "text_quality": "unusable", "rationale": "Model was uncertain.",
        }))
        self.assertEqual(result["text_quality"], "degraded")
        self.assertEqual(result["model_text_quality"], "unusable")


if __name__ == "__main__":
    unittest.main()
