import unittest

from external_validator.llm import LLMError, OpenCodeClient


class OpenCodeClientTests(unittest.TestCase):
    def test_uses_final_text_event_and_ignores_other_jsonl_events(self):
        output = "\n".join((
            '{"type":"step_start","part":{}}',
            '{"type":"text","part":{"text":"{\\"claims\\":[]}"}}',
            '{"type":"step_finish","part":{}}',
        ))
        self.assertEqual(OpenCodeClient._final_text_event(output), '{"claims":[]}')

    def test_rejects_event_stream_without_model_text(self):
        with self.assertRaises(LLMError):
            OpenCodeClient._final_text_event('{"type":"step_start","part":{}}')
