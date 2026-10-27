"""Shared PDF structure markers tolerant of small-caps extraction spacing."""

# Small-caps PDF text can render References as ``ReF eReNCeS``. Match only
# horizontal spacing inside the word so prose on adjacent lines cannot join.
REFERENCE_HEADING_TEXT = "(?:" + "|".join(
    r"[ \t]*".join(word) for word in ("references", "bibliography")
) + ")"
