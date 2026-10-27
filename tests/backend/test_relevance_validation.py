import unittest

from backend.stages.pdf_parsing.constants import REFERENCE_HEADING_RE
from backend.stages.pdf_parsing.packets import _section_name
from backend.shared.text import source_surface_mention
from backend.stages.pdf_parsing.quality import assess_text_quality


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





if __name__ == "__main__":
    unittest.main()
