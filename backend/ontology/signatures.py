"""Pure fingerprints shared by grounding, review and publication readers."""
import json
from backend.shared.storage import stable_hash


def claim_signature(candidate):
    return stable_hash(json.dumps({k: v for k, v in candidate.items()
        if k not in {'source_validation', 'semantic_validation'}}, sort_keys=True, ensure_ascii=False), 64)


def inventory_signature(root):
    paths = sorted([*root.glob('*/candidates.jsonl'), *root.glob('*/ontology_grounding.jsonl')])
    return stable_hash(json.dumps([(str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size) for p in paths]), 64)
