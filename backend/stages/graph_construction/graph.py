"""Build visual graph snapshots from extraction and external validation results."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import networkx as nx
from backend.domain.ontology import external_project_scope


def _edge_status(candidate: dict[str, Any], result: dict[str, Any]) -> tuple[str, set[str], set[str], set[str]]:
    """Derive an auditable state without using model self-confidence."""
    if candidate.get("schema_error"):
        return "rejected", set(), set(), set()
    semantic_decision = candidate.get("semantic_validation", {}).get("decision")
    if semantic_decision in {"rejected", "needs_review"}:
        return ("semantic_rejected" if semantic_decision == "rejected" else "semantic_needs_review"), set(), set(), set()
    source_decision = candidate.get("source_validation", {}).get("decision")
    if source_decision in {"unsupported", "needs_review"}:
        return ("source_unsupported" if source_decision == "unsupported" else "source_needs_review"), set(), set(), set()
    if external_project_scope(result) is False:
        return 'out_of_scope', set(), set(), set()
    decisions = [decision for attempt in result.get("attempts", []) for decision in attempt.get("decisions", [])]
    supporting = {decision["evidence_document_id"] for decision in decisions if decision.get("decision") == "supported"}
    reference_supporting = {decision["evidence_document_id"] for decision in decisions
                            if decision.get("decision") == "supported"
                            and decision.get("evidence_level") == "source_reference"}
    # Legacy decisions predate evidence-level metadata and were produced only
    # by broad retrieval, so they remain independent for backward-compatible
    # graph inspection.
    independent_supporting = supporting - reference_supporting
    contradicting = {decision["evidence_document_id"] for decision in decisions if decision.get("decision") == "contradicted"}
    if result.get("validation_status") == "pipeline_error":
        return "error", supporting, contradicting, reference_supporting
    if result.get('validation_status') in {'external_support_needs_review', 'external_assessment_needs_review', 'external_in_progress'}:
        return 'external_needs_review', supporting, contradicting, reference_supporting
    if contradicting:
        return ("contested" if supporting else "contradicted"), supporting, contradicting, reference_supporting
    independent_group_count = (result.get("support_summary") or {}).get("independent_groups")
    if independent_group_count is None:
        independent_group_count = len(independent_supporting)
    if independent_group_count >= 2:
        return "corroborated", supporting, contradicting, reference_supporting
    if independent_supporting:
        return "single_source", supporting, contradicting, reference_supporting
    if reference_supporting:
        return "source_reference_supported", supporting, contradicting, reference_supporting
    if result.get("validation_status") in {"external_evidence_not_found", "world_insufficient", "world_insufficient_after_retry"}:
        return "not_found", supporting, contradicting, reference_supporting
    return ("not_found" if result else "extracted"), supporting, contradicting, reference_supporting


def graph_from_candidates(candidates: list[dict[str, Any]], results: list[dict[str, Any]] | None = None,
                          view: str = "main") -> dict[str, Any]:
    """Build the external, source-supported, or rejected/audit graph layer."""
    if view not in {"main", "source", "audit"}:
        raise ValueError("view must be 'main', 'source', or 'audit'")
    statuses = {item["candidate_id"]: item for item in results or []}
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for candidate in candidates:
        result = statuses.get(candidate["candidate_id"], {})
        status, supporting_articles, contradicting_articles, reference_articles = _edge_status(candidate, result)
        externally_supported = status in {"single_source", "corroborated"}
        source_supported = (not candidate.get('schema_error')
                            and candidate.get("semantic_validation", {}).get("decision") in {"accepted", "not_run"}
                            and candidate.get("source_validation", {}).get("decision") == "entailed")
        if (view == "main" and not externally_supported) or (view == "source" and not source_supported) or (view == "audit" and externally_supported):
            continue
        display_status = "source_supported" if view == "source" else status
        source = f"{candidate['subject_type']}:{candidate['subject'].casefold()}"
        target = f"{candidate['object_type']}:{candidate['object'].casefold()}"
        nodes.setdefault(source, {"id": source, "label": candidate["subject"], "type": candidate["subject_type"]})
        nodes.setdefault(target, {"id": target, "label": candidate["object"], "type": candidate["object_type"]})
        edges.append({"id": candidate["candidate_id"], "source": source, "target": target,
                      "relation": candidate["relation"], "status": display_status,
                      "claim_text": candidate.get("claim_text", ""),
                      "claim_text_source_verified": ((candidate.get("source_validation") or {}).get("decision") == "entailed"
                                                     and (candidate.get("source_validation") or {}).get("claim_text_match") is True),
                      "certainty": candidate.get("certainty", ""), "qualifiers": candidate.get("qualifiers") or {},
                      "source_page": candidate.get("source_page"),
                      "source_section": candidate.get("source_evidence_section_heading", ""),
                      "external_status": status,
                      "support_count": len(supporting_articles), "contradiction_count": len(contradicting_articles),
                      "independent_support_count": len(supporting_articles - reference_articles),
                      "source_reference_support_count": len(reference_articles),
                      "paper": candidate.get("source_paper_title"),
                      "quote": candidate.get("source_evidence_span") or candidate.get("source_evidence_quote"),
                      "rejection_reason": candidate.get("schema_error")
                          or ((candidate.get("semantic_validation") or {}).get("rationale")
                              if candidate.get("semantic_validation", {}).get("decision") in {"rejected", "needs_review"} else None)
                          or (candidate.get("source_validation") or {}).get("rationale")})
    return {"nodes": list(nodes.values()), "edges": edges,
            "counts": {"nodes": len(nodes), "edges": len(edges),
                       "corroborated": sum(edge["status"] == "corroborated" for edge in edges),
                       "supported": sum(edge["status"] in {"single_source", "corroborated"} for edge in edges),
                       "source_reference_supported": sum(edge["status"] == "source_reference_supported" for edge in edges),
                       "source_supported": sum(edge["status"] == "source_supported" for edge in edges)}}


def merge_graphs(graphs: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    for graph in graphs:
        for node in graph.get("nodes", []):
            nodes[node["id"]] = node
        for edge in graph.get("edges", []):
            edges[edge["id"]] = edge
    return {"nodes": list(nodes.values()), "edges": list(edges.values()),
            "counts": {"nodes": len(nodes), "edges": len(edges),
                       "corroborated": sum(edge["status"] == "corroborated" for edge in edges.values()),
                       "supported": sum(edge["status"] in {"single_source", "corroborated"} for edge in edges.values()),
                       "source_reference_supported": sum(edge["status"] == "source_reference_supported" for edge in edges.values()),
                       "source_supported": sum(edge["status"] == "source_supported" for edge in edges.values())}}


def write_graphml(graph: dict[str, Any], path: Path) -> None:
    """Retain an interoperable, directed export with parallel claim edges."""
    output = nx.MultiDiGraph()
    for node in graph["nodes"]:
        output.add_node(node["id"], label=node["label"], entity_type=node["type"])
    for edge in graph["edges"]:
        output.add_edge(edge["source"], edge["target"], key=edge["id"],
                        candidate_id=edge["id"], relation=edge["relation"], status=edge["status"],
                        support_count=edge["support_count"], contradiction_count=edge["contradiction_count"],
                        claim_text=edge.get("claim_text") or "", certainty=edge.get("certainty") or "",
                        qualifiers=json.dumps(edge.get("qualifiers") or {}, ensure_ascii=False),
                        claim_text_source_verified=bool(edge.get("claim_text_source_verified")),
                        paper=edge.get("paper") or "", quote=edge.get("quote") or "")
    nx.write_graphml(output, path)


def reasoning_paths(graph: dict[str, Any], limit: int = 40, max_hops: int = 5) -> list[list[dict[str, Any]]]:
    """Use only forward corroborated edges; never traverse synthetic reverse links."""
    nodes = {node["id"]: node for node in graph["nodes"]}
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    incoming: set[str] = set()
    for edge in graph["edges"]:
        if edge["status"] == "corroborated":
            outgoing[edge["source"]].append(edge)
            incoming.add(edge["target"])
    # A soil node may begin a partial path when no upstream relation exists, but
    # should not create a duplicate suffix when agriculture or climate reaches it.
    sources = [node["id"] for node in graph["nodes"]
               if node["type"] in {"ClimateFactor", "SoilCondition", "AgriculturalPractice"}
               and node["id"] not in incoming]
    paths: list[list[dict[str, Any]]] = []

    def visit(node_id: str, visited: set[str], path: list[dict[str, Any]]) -> None:
        if len(paths) >= limit or len(path) >= max_hops:
            return
        for edge in outgoing[node_id]:
            if edge["target"] in visited:
                continue
            next_path = [*path, edge]
            if nodes.get(edge["target"], {}).get("type") == "HumanHealthOutcome":
                paths.append(next_path)
            else:
                visit(edge["target"], {*visited, edge["target"]}, next_path)

    for source in sources:
        visit(source, {source}, [])
        if len(paths) >= limit:
            break
    return paths
