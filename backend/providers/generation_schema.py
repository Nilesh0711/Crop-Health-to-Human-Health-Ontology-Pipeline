"""Adapt native decoder constraints without changing application validation."""
from copy import deepcopy


# Observed on the OpenCode Go DeepSeek route: some upstream grammar backends
# reject uniqueItems. Keep the full application schema and validate uniqueness
# after generation; do not downgrade format, change models or edit the answer.
UNSUPPORTED = {'deepseek-v4.1-flash': frozenset({'uniqueItems'})}


def for_model(schema, model):
    unsupported = UNSUPPORTED.get(model, frozenset())
    result = deepcopy(schema)
    def visit(node):
        if not isinstance(node, dict):
            return
        for key in unsupported:
            node.pop(key, None)
        # Descend through schema containers only. A property name or a literal
        # enum value called uniqueItems is data, not a removable keyword.
        for key in ('properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'):
            for child in node.get(key, {}).values():
                visit(child)
        for key in ('items', 'additionalProperties', 'contains', 'not', 'if', 'then', 'else',
                    'propertyNames', 'unevaluatedProperties', 'unevaluatedItems'):
            visit(node.get(key))
        for key in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
            for child in node.get(key, []):
                visit(child)
    visit(result)
    return result
