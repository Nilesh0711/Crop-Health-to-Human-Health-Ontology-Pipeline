"""Local modality cues shared by extraction and evidence validation."""
import re

_CONDITIONAL_CUE = re.compile(
    r"\b(?:may|might|can|could|possibly|potentially|likely|appears?|seems?|suggest\w*|thought)\b",
    re.IGNORECASE,
)

def uses_conditional_modality(evidence: object, trigger: object = "") -> bool:
    """Detect an explicit hedge local to the relation trigger.

    This is intentionally lexical and local. It does not guess scientific
    certainty; it only prevents phrases such as ``can reduce`` or ``may
    increase`` from being promoted to an unqualified descriptive finding.
    """
    text = str(evidence or "")
    relation = str(trigger or "").strip()
    if relation:
        match = re.search(re.escape(relation), text, flags=re.IGNORECASE)
        if match:
            # A modal can precede a long subject phrase by more than 90
            # characters (``Drones can ... thereby reducing pesticides``).
            # Inspect the complete sentence containing the trigger instead of
            # accidentally promoting such a claim to an unhedged finding.
            sentence_start = max(text.rfind(".", 0, match.start()), text.rfind("?", 0, match.start()),
                                 text.rfind("!", 0, match.start())) + 1
            following = [position for marker in ".?!"
                         if (position := text.find(marker, match.end())) >= 0]
            sentence_end = min(following) + 1 if following else len(text)
            text = text[sentence_start:sentence_end]
    return bool(_CONDITIONAL_CUE.search(text))
