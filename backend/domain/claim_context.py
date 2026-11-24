"""Bounded source context and readable claim descriptions, without new inference."""

from __future__ import annotations

from typing import Any
from backend.domain.alignment import exact_in


def local_evidence_context(sentences: list[dict[str, Any]], numbers: list[int], window: int = 2) -> str:
    """Retain each cited cluster's section plus at most two neighboring sentences per side.

    Never pull an antecedent across a detected section boundary or copy long
    unrelated spans between distant non-adjacent citations. Sentence IDs remain
    local to the extraction packet and selected proof remains separate.
    """
    sorted_nums = sorted(set(numbers))
    if not sorted_nums:
        return ""
    clusters: list[list[int]] = [[sorted_nums[0]]]
    for num in sorted_nums[1:]:
        prev = clusters[-1][-1]
        same_heading = (
            str(sentences[num - 1].get("section_heading") or "")
            == str(sentences[prev - 1].get("section_heading") or "")
        )
        if same_heading and num - prev <= 2 * window:
            clusters[-1].append(num)
        else:
            clusters.append([num])

    lines: list[str] = []
    seen_indices: set[int] = set()
    last_heading: str | None = None
    for cluster in clusters:
        start, end = cluster[0] - 1, cluster[-1]
        heading = str(sentences[start].get("section_heading") or "")
        left, right = start, end
        while left > max(0, start - window) and sentences[left - 1].get("section_heading", "") == heading:
            left -= 1
        while right < min(len(sentences), end + window) and sentences[right].get("section_heading", "") == heading:
            right += 1
        indices = [index for index in range(left, right) if index not in seen_indices]
        if not indices:
            continue
        seen_indices.update(indices)
        if heading and heading != last_heading:
            lines.append(f"[LOCAL SECTION HEADING] {heading}.")
        last_heading = heading
        lines.extend(f"[{index + 1}] {sentences[index]['text']}" for index in indices)
    return "\n".join(lines)


def claim_description(item: dict[str, Any]) -> tuple[str, str]:
    """Model descriptions are untrusted until separately checked against source.

    Legacy/missing descriptions get a neutral rendering, not a model-invented
    explanation. Preserve certainty explicitly in this fallback representation.
    """
    proposed = " ".join(str(item.get("claim_text") or "").split())
    if proposed and len(proposed.split()) <= 80 and len(proposed) <= 900:
        return proposed, "extracted"
    qualifiers = "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in
                           (item.get("qualifiers") or {}).items() if value)
    text = f"{item['subject']} — {item['relation'].replace('_', ' ')} — {item['object']}."
    if item.get('certainty'):
        text += f" Wording: {item['certainty']}."
    return text + (f" Context: {qualifiers}." if qualifiers else ""), "structured_rendering"


def apply_source_provenance(candidate: dict[str, Any], source_result: dict[str, Any]) -> list[dict[str, str]]:
    """Apply the source judge's evidence-grounded role classification once.

    Only descriptive crop/material-contaminant occurrence edges are eligible.
    Conditions remain restrictions by default; no keyword/location stripping
    or claim-specific knowledge is used. Original values and grounding persist.
    """
    if (source_result.get('decision') != 'entailed' or candidate.get('relation') != 'has_contaminant'
            or candidate.get('certainty') != 'descriptive'):
        return []
    qualifiers = candidate.get('qualifiers') or {}
    changes = []
    proposals = source_result.get('occurrence_provenance_fields')
    for row in proposals if isinstance(proposals, list) else []:
        if not isinstance(row, dict) or row.get('field') not in {'condition', 'population'}:
            continue
        field, value = row['field'], row.get('value')
        if (not value or qualifiers.get(field) != value or not str(row.get('rationale') or '').strip()
                or not exact_in(row.get('evidence'), candidate.get('source_evidence_span'))):
            continue
        previous = str(candidate.get('source_context') or '').strip()
        if not exact_in(value, previous):
            candidate['source_context'] = '; '.join(part for part in (previous, value) if part)
        qualifiers[field] = ''
        change = {'field': field, 'moved_value': value, 'evidence': row['evidence'],
                  'reason': 'source_judge_occurrence_provenance', 'rationale': row['rationale']}
        changes.append(change)
    if changes:
        candidate['qualifiers'] = qualifiers
        candidate.setdefault('qualifier_adjustments', []).extend(changes)
    return changes
