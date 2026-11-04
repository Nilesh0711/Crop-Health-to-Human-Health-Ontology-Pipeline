"""Bounded arithmetic on model-selected literal proof numbers, never truth labels."""
from decimal import Decimal, InvalidOperation, localcontext
import re


WORDS = dict(zip(('zero','one','two','three','four','five','six','seven','eight','nine','ten'), range(11)))
OPERATIONS = ('ratio_percent', 'change_percent', 'sum_ratio_percent')
CALCULATIONS_SCHEMA = {'type':'array','maxItems':6,'items':{
    'type':'object','properties':{'proof_id':{'type':'string'},
        'operation':{'type':'string','enum':list(OPERATIONS)},
        'operands':{'type':'array','items':{'type':'string','maxLength':40},'minItems':2,'maxItems':3}},
    'required':['proof_id','operation','operands'],'additionalProperties':False}}


def calculate_from_proof(calculation, proofs):
    """Verify literal operands, then calculate; units/applicability stay model decisions."""
    proof = proofs.get(calculation['proof_id'])
    if proof is None:
        raise ValueError('Arithmetic selects proof absent from its own paper')
    quote = proof['quoted_span']
    values = []
    for literal in calculation['operands']:
        is_word = literal.lower() in WORDS
        if len(literal) > 40 or (not is_word and not re.fullmatch(
                r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?', literal)):
            raise ValueError('Arithmetic accepts bounded numeric literals only')
        if not literal or not re.search(r'(?<![\w.,+-])' + re.escape(literal) + r'(?!\w|[.,]\d)', quote,
                                         flags=re.IGNORECASE if is_word else 0):
            raise ValueError('Arithmetic operand is not a literal number in its selected proof')
        try:
            number = (Decimal(WORDS[literal.lower()]) if is_word
                      else Decimal(literal.replace(',', '')))
        except InvalidOperation as exc:
            raise ValueError('Arithmetic accepts numeric literals only, not expressions') from exc
        if not number.is_finite() or abs(number.adjusted()) > 100:
            raise ValueError('Arithmetic operand exceeds the bounded numeric range')
        values.append(number)
    operation = calculation['operation']
    expected = 3 if operation == 'sum_ratio_percent' else 2
    if operation not in OPERATIONS or len(values) != expected:
        raise ValueError('Arithmetic operation and operand count disagree')
    denominator = values[-1] if operation != 'change_percent' else values[0]
    if not denominator:
        raise ValueError('Arithmetic denominator is zero')
    with localcontext() as context:
        context.prec = 28
        numerator = (values[1]-values[0] if operation == 'change_percent'
                     else values[0]+values[1] if operation == 'sum_ratio_percent' else values[0])
        result = numerator / denominator * 100
    return {**calculation, 'value_percent':str(result),
            'definition': 'signed (after-before)/before * 100' if operation == 'change_percent' else '(numerator/denominator) * 100',
            'units_and_scientific_comparability_checked':False}
