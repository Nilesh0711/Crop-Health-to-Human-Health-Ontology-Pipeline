"""Strict response parsing. This module never corrects model output locally."""
import json


class StructuredOutputError(ValueError):
    pass


class IncompleteResponseError(StructuredOutputError):
    """The answer is missing response content and needs regeneration."""


class SchemaValidationError(StructuredOutputError):
    """Parsed JSON violates the response contract; not a token truncation."""


class NullAnnotationError(SchemaValidationError):
    """Only undeclared null-valued object fields prevent schema acceptance."""

    def __init__(self, message, paths):
        super().__init__(message)
        self.paths = paths


class ResponseStructureError(StructuredOutputError):
    """Ambiguous nesting or invalid quoted values need fresh schema-bound output.

    Separator edits cannot safely establish which object owns the remaining
    fields or reinterpret string contents. Never select the first parsed object,
    relax JSON string parsing, or move fields locally.
    """


class DuplicateJSONPropertyError(SchemaValidationError):
    """Repeated properties require regeneration, not value-preserving syntax edits."""


class EvidenceSelectionError(StructuredOutputError):
    """Complete, schema-valid assessment has ungrounded or inconsistent findings.

    Guarded evidence callers must withhold that proof and retain it for review.
    After the bounded recovery, this is reviewable data, not a service outage.
    """


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONPropertyError(f'Duplicate JSON property: {key}')
        result[key] = value
    return result


class _ObjectPairs(list):
    """Distinguish JSON objects from arrays until their paths are known."""


def _extract_anchor_duplicates(value, path, adjustments):
    if isinstance(value, _ObjectPairs):
        result = {}
        for key, child in value:
            field_path = (*path, key)
            if key in result:
                if (len(path) == 2 and path[0] == 'claims' and isinstance(path[1], int)
                        and key == 'relation_anchor' and isinstance(child, str) and result[key] == child):
                    adjustments.append({'kind': 'identical_duplicate_relation_anchor',
                                        'path': list(field_path)})
                    continue
                raise DuplicateJSONPropertyError(f'Duplicate JSON property: {key}')
            result[key] = _extract_anchor_duplicates(child, field_path, adjustments)
        return result
    if isinstance(value, list):
        return [_extract_anchor_duplicates(child, (*path, i), adjustments) for i, child in enumerate(value)]
    return value


def _reject_constant(value):
    raise StructuredOutputError(f'Non-JSON numeric constant: {value}')


def parse_json_object(content, schema=None, *, allow_identical_claim_anchors=False):
    """Decode exactly the supplied JSON; retain the old tuple API for callers.

    Whitespace is part of JSON's grammar. Markdown, prose, missing separators
    and misplaced braces are errors. The opt-in extraction exception records an
    identical repeated anchor without changing any quote or choosing a value.
    Schema validation belongs to the provider's response-completeness checks.
    """
    del schema
    if not isinstance(content, str) or not content.strip():
        raise IncompleteResponseError('Model returned an empty final answer')
    try:
        parsed = json.loads(content, object_pairs_hook=_ObjectPairs if allow_identical_claim_anchors else _unique_object,
                            parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        if exc.msg.startswith('Invalid control character'):
            # A raw control character is inside a quoted value. Our formatting
            # repair may edit separators outside strings, not reinterpret their
            # contents or silently accept a permissively decoded answer.
            raise ResponseStructureError(
                'Model returned invalid JSON: unescaped control character inside a string; '
                'separator-only repair cannot certify string contents; a fresh schema-bound response is required '
                f'(line {exc.lineno}, column {exc.colno})') from exc
        remaining = content[exc.pos:].strip()
        incomplete_literal = remaining in {'', 't', 'tr', 'tru', 'f', 'fa', 'fal', 'fals', 'n', 'nu', 'nul', '-'}
        missing_value = exc.msg == 'Expecting value' and incomplete_literal
        missing_field = exc.msg.startswith('Expecting property name') and not remaining and content.rstrip().endswith(('{', ','))
        if exc.msg == 'Extra data' and remaining.strip(' \t\r\n}]'):
            raise ResponseStructureError(
                'Model returned invalid JSON: content follows a closed root object; '
                'object/array ownership is ambiguous and requires regeneration from original evidence '
                f'(line {exc.lineno}, column {exc.colno})') from exc
        error_class = IncompleteResponseError if ('Unterminated string' in exc.msg or missing_value or missing_field) else StructuredOutputError
        raise error_class(f'Model returned invalid JSON: {exc.msg} '
                          f'(line {exc.lineno}, column {exc.colno})') from exc
    adjustments = []
    if allow_identical_claim_anchors:
        parsed = _extract_anchor_duplicates(parsed, (), adjustments)
    if not isinstance(parsed, dict):
        raise IncompleteResponseError('Model response omitted the required root JSON object')
    return parsed, adjustments
