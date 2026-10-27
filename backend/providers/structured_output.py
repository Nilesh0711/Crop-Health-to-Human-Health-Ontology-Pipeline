"""Strict response parsing. This module never corrects model output locally."""
import json


class StructuredOutputError(ValueError):
    pass


class IncompleteResponseError(StructuredOutputError):
    """The answer is missing response content and needs regeneration."""


class SchemaValidationError(StructuredOutputError):
    """Parsed JSON violates the response contract; not a token truncation."""


class ResponseStructureError(StructuredOutputError):
    """A prematurely closed root or multiple values need fresh schema-bound output.

    Separator edits cannot safely establish which object owns the remaining
    fields. Never select the first parsed object or move fields locally.
    """


class DuplicateJSONPropertyError(SchemaValidationError):
    """Repeated properties require regeneration, not value-preserving syntax edits."""


class EvidenceSelectionError(StructuredOutputError):
    """Complete, schema-valid assessment has ungrounded proof selections.

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


def _reject_constant(value):
    raise StructuredOutputError(f'Non-JSON numeric constant: {value}')


def parse_json_object(content, schema=None):
    """Decode exactly the supplied JSON; retain the old tuple API for callers.

    Whitespace is part of JSON's grammar. Markdown, prose, missing separators
    and misplaced braces are errors, never automatically stripped or repaired.
    Schema validation belongs to the provider's response-completeness checks.
    """
    del schema
    if not isinstance(content, str) or not content.strip():
        raise IncompleteResponseError('Model returned an empty final answer')
    try:
        parsed = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
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
    if not isinstance(parsed, dict):
        raise IncompleteResponseError('Model response omitted the required root JSON object')
    return parsed, []
