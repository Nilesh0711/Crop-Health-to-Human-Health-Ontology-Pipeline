"""Snapshot legacy claims and compare a fresh run without treating AI labels as gold.

Examples (run from the repository root):
  python scripts/extraction_audit.py snapshot --output analysis/extraction_baseline.jsonl
  python scripts/extraction_audit.py compare --baseline analysis/extraction_baseline.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.ontology_schema import schema_error  # noqa: E402
from external_validator.config import ONTOLOGY_DIR, SCHEMA_VERSION, Settings  # noqa: E402
from external_validator.llm import OllamaClient  # noqa: E402
from external_validator.storage import read_jsonl, utc_now, write_jsonl  # noqa: E402
from backend.source_validation import validate_source_claim  # noqa: E402


def key_of(item: dict) -> str:
    return " | ".join(" ".join(str(item.get(field, "")).casefold().split())
                      for field in ("subject", "relation", "object"))


def snapshot(output: Path, model_review: bool, review_limit: int) -> None:
    if output.exists():
        raise SystemExit(f"Refusing to overwrite {output}; choose a new baseline path")
    client = OllamaClient(Settings()) if model_review else None
    rows: list[dict] = []
    reviewed = 0
    for folder in sorted(ONTOLOGY_DIR.iterdir()):
        if not folder.is_dir() or not (folder / "candidates.jsonl").exists():
            continue
        validations = {item.get("candidate_id"): item.get("validation_status")
                       for item in read_jsonl(folder / "validation_runs.jsonl")}
        for candidate in read_jsonl(folder / "candidates.jsonl"):
            row = {field: candidate.get(field) for field in (
                "candidate_id", "source_paper_title", "source_page", "subject", "relation", "object",
                "subject_type", "object_type", "source_evidence_span", "source_evidence_quote",
                "source_pdf_sha256", "schema_error", "scope_error")}
            row.update({"pdf_stem": folder.name, "claim_key": key_of(candidate),
                        "legacy_external_status": validations.get(candidate.get("candidate_id")),
                        "current_schema_error": schema_error(candidate),
                        "source_review": {"decision": "not_assessed", "rationale": "No independent source review was run."}})
            if client and not row["current_schema_error"] and (review_limit == 0 or reviewed < review_limit):
                try:
                    row["source_review"] = validate_source_claim(candidate, client)
                except Exception as exc:
                    row["source_review"] = {"decision": "needs_review", "rationale": f"Model check failed: {exc}"}
                reviewed += 1
                print(f"Reviewed {reviewed}: {folder.name}: {candidate.get('subject')} -> {candidate.get('object')}", flush=True)
            rows.append(row)
    write_jsonl(output, rows)
    counts = Counter(row["pdf_stem"] for row in rows)
    summary = {"created_at": utc_now(), "baseline_kind": "legacy run snapshot; AI review is provisional, not expert ground truth",
               "candidate_count": len(rows), "papers": counts,
               "legacy_schema_and_scope_pass": sum(not row["schema_error"] and not row["scope_error"] for row in rows),
               "current_controlled_vocabulary_pass": sum(not row["current_schema_error"] for row in rows),
               "source_review_decisions": Counter(row["source_review"]["decision"] for row in rows),
               "legacy_external_statuses": Counter(row["legacy_external_status"] or "not_validated" for row in rows)}
    output.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def compare(baseline: Path) -> None:
    rows = read_jsonl(baseline)
    by_paper: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_paper[row["pdf_stem"]].append(row)
    report: dict[str, dict] = {}
    for stem, old in sorted(by_paper.items()):
        folder = ONTOLOGY_DIR / stem
        fresh = read_jsonl(folder / "candidates.jsonl")
        results = read_jsonl(folder / "validation_runs.jsonl")
        old_keys = {row["claim_key"] for row in old}
        new_keys = {key_of(item) for item in fresh}
        report[stem] = {"fresh_current_run_detected": bool(fresh) and all(item.get("schema_version") == SCHEMA_VERSION for item in fresh),
                        "baseline_extracted": len(old), "current_extracted": len(fresh),
                        "baseline_legacy_schema_scope_pass": sum(not row["schema_error"] and not row["scope_error"] for row in old),
                        "current_controlled_vocabulary_pass": sum(not item.get("schema_error") for item in fresh),
                        "current_semantic_accepted": sum(item.get("semantic_validation", {}).get("decision") == "accepted" for item in fresh),
                        "current_source_entailed": sum(item.get("source_validation", {}).get("decision") == "entailed" for item in fresh),
                        "current_source_needs_review": sum(item.get("source_validation", {}).get("decision") == "needs_review" for item in fresh),
                        "current_external_support": sum(item.get("validation_status") in {"world_supported_pending_corroboration", "world_corroborated"} for item in results),
                        "claim_overlap": len(old_keys & new_keys), "new_claims": len(new_keys - old_keys),
                        "baseline_claims_absent": len(old_keys - new_keys)}
    print(json.dumps({"caution": "Counts and claim overlap are not precision or recall; expert truth labels are unavailable.",
                      "papers": report}, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--output", type=Path, default=ROOT / "analysis" / "extraction_baseline.jsonl")
    snap.add_argument("--model-review", action="store_true", help="Run provisional Qwen source review on eligible legacy claims")
    snap.add_argument("--review-limit", type=int, default=10, help="Maximum Qwen reviews; 0 means all")
    comp = sub.add_parser("compare")
    comp.add_argument("--baseline", type=Path, default=ROOT / "analysis" / "extraction_baseline.jsonl")
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot(args.output, args.model_review, args.review_limit)
    else:
        compare(args.baseline)


if __name__ == "__main__":
    main()
