"""Application admission restrictions attributed to reviewed upstream annotations."""
import json
from pathlib import Path

USAGE_PATH = Path(__file__).resolve().parents[1] / 'resources/ontologies/property_usage.json'
USAGE_POLICY = 'upstream-property-usage-v1'
# Preserve the content-addressed catalogue identity when only its projection
# changes. New model-visible annotation handling has its own cache policy.
ANNOTATION_POLICY = 'upstream-annotations-and-usage-v2'
ANNOTATION_PROJECTION_POLICY = 'upstream-inverse-and-parent-annotation-projection-v4'


def usage_metadata():
    return json.loads(USAGE_PATH.read_text(encoding='utf-8'))


def direct_property_allowed(iri):
    restriction = usage_metadata()['restrictions'].get(iri)
    return not restriction or 'exact' in restriction['allowed_mappings']
