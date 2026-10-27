"""Feed saved model decisions into the document proof guard for unit tests."""

from backend.stages.external_validation.proofs import guard_document_decision


def judge_document(client, candidate, document, nli_validator=None):
    return guard_document_decision(candidate, document, client.chat_json('', ''), nli_validator)
