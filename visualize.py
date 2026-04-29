"""
Visualize the Master Knowledge Graph and generate readable reports.
Run: python visualize.py
"""
import os
import networkx as nx

ONTOLOGY_DIR = "ontology"
base_dir = os.path.dirname(__file__)
master_path = os.path.join(base_dir, ONTOLOGY_DIR, "master_knowledge_graph.graphml")

if not os.path.exists(master_path):
    print("ERROR: master_knowledge_graph.graphml not found. Run pdf_to_antology.py first.")
    exit(1)

G = nx.read_graphml(master_path)

# ============================================================
# 1. GRAPH SUMMARY
# ============================================================
print("=" * 60)
print("MASTER KNOWLEDGE GRAPH SUMMARY")
print("=" * 60)
print(f"  Total Nodes: {G.number_of_nodes()}")
print(f"  Total Edges: {G.number_of_edges()}")
print(f"  Connected:   {nx.is_connected(G.to_undirected())}")

type_counts = {}
for _, d in G.nodes(data=True):
    t = d.get("type", "Unknown")
    type_counts[t] = type_counts.get(t, 0) + 1
print(f"\n  Node Type Distribution:")
for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
    print(f"    {t:25s} {c:4d} nodes")

# ============================================================
# 2. LIST ALL NODES BY TYPE
# ============================================================
report_path = os.path.join(base_dir, ONTOLOGY_DIR, "graph_report.txt")
with open(report_path, "w", encoding="utf-8") as f:
    f.write("=" * 70 + "\n")
    f.write("MASTER KNOWLEDGE GRAPH - FULL REPORT\n")
    f.write("=" * 70 + "\n\n")

    # Nodes by type
    f.write("NODES BY TYPE\n")
    f.write("-" * 70 + "\n")
    for entity_type in sorted(type_counts.keys()):
        nodes = [n for n, d in G.nodes(data=True) if d.get("type") == entity_type]
        f.write(f"\n[{entity_type}] ({len(nodes)} nodes)\n")
        for n in sorted(nodes):
            label = G.nodes[n].get("label", n)
            f.write(f"  - {label}\n")

    # Edge summary
    f.write("\n\nEDGE SUMMARY\n")
    f.write("-" * 70 + "\n")
    rel_counts = {}
    for u, v, d in G.edges(data=True):
        rel = d.get("relation", "unknown")
        rel_counts[rel] = rel_counts.get(rel, 0) + 1
    for rel, cnt in sorted(rel_counts.items(), key=lambda x: -x[1]):
        f.write(f"  {rel:30s} {cnt:4d} edges\n")

    # All edges listed
    f.write("\n\nALL EDGES (Source -> Relation -> Target)\n")
    f.write("-" * 70 + "\n")
    for u, v, d in sorted(G.edges(data=True)):
        rel = d.get("relation", "?")
        label_u = G.nodes[u].get("label", u)
        label_v = G.nodes[v].get("label", v)
        type_u = G.nodes[u].get("type", "?")
        type_v = G.nodes[v].get("type", "?")
        f.write(f"  {label_u} [{type_u}]  --({rel})-->  {label_v} [{type_v}]\n")

print(f"\n  Full report saved to: {report_path}")

# ============================================================
# 3. READABLE REASONING PATHS
# ============================================================
print("\n" + "=" * 60)
print("REASONING PATHS (Source Domain -> Human Disease)")
print("=" * 60)

source_types = {"ClimateFactor", "SoilCondition", "AgriculturalPractice"}
target_types = {"HumanDisease"}

source_nodes = [n for n, d in G.nodes(data=True) if d.get("type") in source_types]
target_nodes = [n for n, d in G.nodes(data=True) if d.get("type") in target_types]

paths_by_target = {}  # Group paths by disease

for src in source_nodes:
    for tgt in target_nodes:
        try:
            for path in nx.all_simple_paths(G, source=src, target=tgt, cutoff=5):
                # Build readable path
                steps = []
                for i in range(len(path) - 1):
                    u, v = path[i], path[i + 1]
                    rel = G.edges[u, v].get("relation", "?")
                    label_u = G.nodes[u].get("label", u)
                    label_v = G.nodes[v].get("label", v)
                    if i == 0:
                        steps.append(label_u)
                    steps.append(f"  --({rel})-->")
                    steps.append(f"  {label_v}")

                disease = G.nodes[tgt].get("label", tgt)
                if disease not in paths_by_target:
                    paths_by_target[disease] = []
                paths_by_target[disease].append("\n    ".join(steps))
        except nx.NetworkXNoPath:
            pass

# Save readable paths report
paths_report_path = os.path.join(base_dir, ONTOLOGY_DIR, "reasoning_paths_report.txt")
with open(paths_report_path, "w", encoding="utf-8") as f:
    f.write("=" * 70 + "\n")
    f.write("MULTI-HOP REASONING PATHS\n")
    f.write(f"Total unique paths: {sum(len(v) for v in paths_by_target.values())}\n")
    f.write("=" * 70 + "\n\n")

    for disease in sorted(paths_by_target.keys()):
        paths = paths_by_target[disease]
        f.write(f"\n{'─' * 60}\n")
        f.write(f"TARGET: {disease} ({len(paths)} paths lead here)\n")
        f.write(f"{'─' * 60}\n")
        # Show max 10 paths per disease to keep it readable
        for i, path_str in enumerate(paths[:10]):
            f.write(f"\n  Path {i+1}:\n    {path_str}\n")
        if len(paths) > 10:
            f.write(f"\n  ... and {len(paths) - 10} more paths\n")

# Print summary to console
print(f"\n  Paths grouped by disease target:")
for disease in sorted(paths_by_target.keys()):
    count = len(paths_by_target[disease])
    print(f"    {disease:35s} -> {count:4d} paths")

total_paths = sum(len(v) for v in paths_by_target.values())
print(f"\n  Total paths: {total_paths}")
print(f"  Readable report saved to: {paths_report_path}")
