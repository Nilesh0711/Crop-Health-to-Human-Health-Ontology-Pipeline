import os, sys, re
import networkx as nx

# ============================================================
# CONFIG
# ============================================================
PDF_NAMES = [
    "Crop health and its global impacts on the components of food security",
    "Exploring the link between soil health and crop productivity",
    "Integrated Soil and Crop Management in Organic Agriculture",
    "The links between agriculture and health",
    "agronomy-12-02947",
]
ONTOLOGY_DIR = "ontology"

# ============================================================
# CONTROLLED VOCABULARIES
# ============================================================
ENTITY_TYPES = [
    "Crop", "PlantDisease", "Nutrient", "SoilCondition",
    "ClimateFactor", "HumanDisease", "AgriculturalPractice"
]

RELATIONS = [
    "affects", "causes", "reduces", "increases",
    "has_nutrient", "leads_to", "associated_with", "deficiency_causes"
]

# Expanded rules — every valid (subject_type, relation, object_type) combination
VALID_RULES = {
    # Disease-Crop
    ("PlantDisease", "affects", "Crop"),
    ("PlantDisease", "reduces", "Crop"),
    ("PlantDisease", "reduces", "Nutrient"),
    ("PlantDisease", "increases", "Nutrient"),
    # Crop-Nutrient
    ("Crop", "has_nutrient", "Nutrient"),
    ("Crop", "increases", "Nutrient"),
    ("Crop", "reduces", "Nutrient"),
    # Soil
    ("SoilCondition", "causes", "PlantDisease"),
    ("SoilCondition", "affects", "PlantDisease"),
    ("SoilCondition", "increases", "PlantDisease"),
    ("SoilCondition", "reduces", "PlantDisease"),
    ("SoilCondition", "affects", "Crop"),
    ("SoilCondition", "affects", "Nutrient"),
    ("SoilCondition", "increases", "Nutrient"),
    ("SoilCondition", "reduces", "Nutrient"),
    # Nutrient-HumanDisease (the critical bridge)
    ("Nutrient", "deficiency_causes", "HumanDisease"),
    ("Nutrient", "causes", "HumanDisease"),
    ("Nutrient", "leads_to", "HumanDisease"),
    ("Nutrient", "reduces", "HumanDisease"),
    ("Nutrient", "affects", "HumanDisease"),
    ("Nutrient", "associated_with", "HumanDisease"),
    # Nutrient-Nutrient
    ("Nutrient", "affects", "Nutrient"),
    ("Nutrient", "increases", "Nutrient"),
    ("Nutrient", "reduces", "Nutrient"),
    # Climate
    ("ClimateFactor", "causes", "SoilCondition"),
    ("ClimateFactor", "affects", "SoilCondition"),
    ("ClimateFactor", "associated_with", "PlantDisease"),
    ("ClimateFactor", "causes", "PlantDisease"),
    ("ClimateFactor", "affects", "Crop"),
    ("ClimateFactor", "reduces", "Crop"),
    ("ClimateFactor", "affects", "Nutrient"),
    # Agricultural Practice
    ("AgriculturalPractice", "affects", "SoilCondition"),
    ("AgriculturalPractice", "causes", "SoilCondition"),
    ("AgriculturalPractice", "increases", "SoilCondition"),
    ("AgriculturalPractice", "reduces", "SoilCondition"),
    ("AgriculturalPractice", "affects", "Crop"),
    ("AgriculturalPractice", "increases", "Crop"),
    ("AgriculturalPractice", "reduces", "Crop"),
    ("AgriculturalPractice", "affects", "Nutrient"),
    ("AgriculturalPractice", "increases", "Nutrient"),
    ("AgriculturalPractice", "reduces", "Nutrient"),
    ("AgriculturalPractice", "reduces", "PlantDisease"),
    ("AgriculturalPractice", "increases", "PlantDisease"),
    ("AgriculturalPractice", "affects", "PlantDisease"),
    # Cross-domain
    ("PlantDisease", "causes", "HumanDisease"),
    ("PlantDisease", "leads_to", "HumanDisease"),
    ("Crop", "reduces", "HumanDisease"),
    ("Crop", "affects", "HumanDisease"),
    ("Crop", "associated_with", "HumanDisease"),
    ("AgriculturalPractice", "affects", "HumanDisease"),
    ("AgriculturalPractice", "reduces", "HumanDisease"),
    ("AgriculturalPractice", "increases", "HumanDisease"),
    ("SoilCondition", "leads_to", "HumanDisease"),
    ("ClimateFactor", "leads_to", "HumanDisease"),
}

# Entity normalization — merge synonymous concepts into canonical forms
ENTITY_NORMALIZATION = {
    "nutritional quality": "nutrient",
    "nutrient concentration": "nutrient",
    "nutrition": "nutrient",
    "iron content": "iron",
    "nitrogen-fixation": "nitrogen",
    "soil-borne pests": "soilborne plant disease",
    "soil-borne plant disease": "soilborne plant disease",
    "soilborne plant diseases": "soilborne plant disease",
    "antioxidant activity": "antioxidant",
    "antioxidant capacity": "antioxidant",
    "antioxidant compounds": "antioxidant",
    "phytochemicals with antioxidant activity": "phytochemicals",
    "phenolic concentrations": "phenolic compounds",
    "phenolic levels": "phenolic compounds",
    "phenolics": "phenolic compounds",
    "mineral micronutrients": "micronutrient",
    "micronutrient uptake": "micronutrient",
    "cu concentration": "copper",
    "cu-fungicides": "copper fungicide",
    "chlormequat levels": "chlormequat",
    "negative environmental impacts": "environmental degradation",
    "false smut disease spread": "false smut disease",
    "chlamydospores germination": "false smut disease",
    "sclerotia production": "fungal disease",
    "foliar disease severity": "foliar disease",
    "cadmium concentration": "cadmium",
    "human disease": "human disease (general)",
    "human health risk": "human disease (general)",
    "chronic diseases": "chronic disease",
}

# ============================================================
# REVERSE RELATION MAP
# ============================================================
REVERSE_REL_MAP = {
    "affects": "affected_by",
    "causes": "caused_by",
    "reduces": "reduced_by",
    "increases": "increased_by",
    "has_nutrient": "nutrient_of",
    "leads_to": "led_to_by",
    "associated_with": "associated_with",
    "deficiency_causes": "caused_by_deficiency_in",
}

# ============================================================
# CORE FUNCTIONS
# ============================================================

def parse_tuple_line(line: str):
    """Parse a single 6-tuple line into a dict. Returns None if invalid."""
    line = line.strip()
    if not line.startswith("<") or not line.endswith(">"):
        return None
    parts = [part.strip() for part in line[1:-1].split(",")]
    if len(parts) != 6 or not all(parts[:5]):
        return None
    subject, relation, obj, subject_type, object_type, confidence = parts
    relation = relation.lower().strip()
    subject_type = subject_type.strip()
    object_type = object_type.strip()
    if relation not in set(RELATIONS):
        return None
    if subject_type not in ENTITY_TYPES or object_type not in ENTITY_TYPES:
        return None
    try:
        conf_float = float(confidence)
    except ValueError:
        conf_float = 0.5
    return {
        "subject": subject, "relation": relation, "object": obj,
        "subject_type": subject_type, "object_type": object_type,
        "confidence": conf_float,
    }


def deduplicate(tuples_text: str) -> str:
    """Remove exact-duplicate lines (case-insensitive)."""
    seen, result = set(), []
    for line in tuples_text.splitlines():
        key = line.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(line.strip())
    return "\n".join(result)


def validate_and_filter(tuples_text: str) -> str:
    """Keep only tuples that pass VALID_RULES and confidence >= 0.7."""
    result = []
    rejected = 0
    for line in tuples_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = parse_tuple_line(line)
        if not parsed:
            rejected += 1
            continue
        if parsed["confidence"] < 0.7:
            rejected += 1
            continue
        rule_key = (parsed["subject_type"], parsed["relation"], parsed["object_type"])
        if rule_key not in VALID_RULES:
            rejected += 1
            continue
        result.append(line)
    print(f"  Validated: {len(result)} accepted, {rejected} rejected")
    return "\n".join(result)


def normalize_node_key(name: str) -> str:
    """Normalize entity name using the ENTITY_NORMALIZATION dictionary."""
    key = name.strip().lower()
    return ENTITY_NORMALIZATION.get(key, key)


def add_edge_to_graph(G: nx.DiGraph, subj: str, rel: str, obj: str, subj_type: str, obj_type: str):
    """Add a forward edge and its semantic reverse to the graph."""
    subj_key = normalize_node_key(subj)
    obj_key = normalize_node_key(obj)

    if subj_key not in G:
        G.add_node(subj_key, label=subj, type=subj_type)
    if obj_key not in G:
        G.add_node(obj_key, label=obj, type=obj_type)

    G.add_edge(subj_key, obj_key, relation=rel)
    rev_rel = REVERSE_REL_MAP.get(rel, rel + "_by")
    G.add_edge(obj_key, subj_key, relation=rev_rel)


def build_graph(tuples_text: str, G: nx.DiGraph = None) -> nx.DiGraph:
    """Construct a NetworkX DiGraph from validated 6-tuples."""
    if G is None:
        G = nx.DiGraph()
    for line in tuples_text.splitlines():
        parsed = parse_tuple_line(line.strip())
        if not parsed:
            continue
        add_edge_to_graph(
            G, parsed["subject"], parsed["relation"], parsed["object"],
            parsed["subject_type"], parsed["object_type"]
        )
    return G


def format_path(G: nx.DiGraph, path: list) -> str:
    """Format a graph path as a human-readable chain."""
    parts = []
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        rel = G.edges[u, v].get("relation", "?")
        label_u = G.nodes[u].get("label", u)
        label_v = G.nodes[v].get("label", v)
        type_u = G.nodes[u].get("type", "?")
        type_v = G.nodes[v].get("type", "?")
        if i == 0:
            parts.append(f"{label_u} [{type_u}]")
        parts.append(f" -({rel})-> ")
        parts.append(f"{label_v} [{type_v}]")
    return "".join(parts)


def reason_graph(G: nx.DiGraph):
    """Multi-stage reasoning: find paths from source domains to HumanDisease."""
    print("  Running Reasoning Layer (Path Traversal)...")

    # Debug: node type distribution
    type_counts = {}
    for _, d in G.nodes(data=True):
        t = d.get("type", "Unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"  Node type distribution: {type_counts}")
    print(f"  Graph connected (undirected): {nx.is_connected(G.to_undirected())}")

    source_types = {"ClimateFactor", "SoilCondition", "AgriculturalPractice"}
    target_types = {"HumanDisease"}

    source_nodes = [n for n, d in G.nodes(data=True) if d.get("type") in source_types]
    target_nodes = [n for n, d in G.nodes(data=True) if d.get("type") in target_types]
    nutrient_nodes = [n for n, d in G.nodes(data=True) if d.get("type") == "Nutrient"]

    print(f"  Source nodes (Climate/Soil/Practice): {len(source_nodes)}")
    print(f"  Target nodes (HumanDisease): {len(target_nodes)}")
    print(f"  Intermediate nodes (Nutrient): {len(nutrient_nodes)}")

    paths_found = []

    # Stage 1: Direct full paths (source -> ... -> HumanDisease)
    for src in source_nodes:
        for tgt in target_nodes:
            try:
                for path in nx.all_simple_paths(G, source=src, target=tgt, cutoff=6):
                    paths_found.append(format_path(G, path))
            except nx.NetworkXNoPath:
                pass

    # Stage 2: Partial path stitching via Nutrient bridge
    if not paths_found:
        print("  No direct paths found. Trying partial path extension...")
        partial_to_nutrient = []
        for src in source_nodes:
            for nut in nutrient_nodes:
                try:
                    for path in nx.all_simple_paths(G, source=src, target=nut, cutoff=4):
                        partial_to_nutrient.append(path)
                except nx.NetworkXNoPath:
                    pass

        nutrient_to_disease = []
        for nut in nutrient_nodes:
            for tgt in target_nodes:
                try:
                    for path in nx.all_simple_paths(G, source=nut, target=tgt, cutoff=3):
                        nutrient_to_disease.append(path)
                except nx.NetworkXNoPath:
                    pass

        for p1 in partial_to_nutrient:
            for p2 in nutrient_to_disease:
                if p1[-1] == p2[0]:
                    full_path = p1 + p2[1:]
                    paths_found.append(format_path(G, full_path))

        print(f"  Partial paths to Nutrient: {len(partial_to_nutrient)}")
        print(f"  Partial paths Nutrient to Disease: {len(nutrient_to_disease)}")

    return list(set(paths_found))


# ============================================================
# MAIN PIPELINE
# ============================================================
if __name__ == "__main__":
    base_dir = os.path.dirname(__file__)

    for pdf_name in PDF_NAMES:
        print(f"\n{'='*60}")
        print(f"Processing: {pdf_name}")
        print(f"{'='*60}")

        pdf_output_dir = os.path.join(base_dir, ONTOLOGY_DIR, pdf_name)
        os.makedirs(pdf_output_dir, exist_ok=True)

        def out(suffix):
            return os.path.join(pdf_output_dir, suffix)

        # Step 1: Load raw tuples (must be pre-populated from ChatGPT)
        raw_triplets_path = out("raw_triplets.txt")
        if not os.path.exists(raw_triplets_path):
            print(f"  ERROR: raw_triplets.txt not found at:")
            print(f"    {raw_triplets_path}")
            print(f"  Please extract tuples using ChatGPT and save them to the above file.")
            print(f"  Skipping this PDF.\n")
            continue

        print("[1/4] Loading raw tuples...")
        with open(raw_triplets_path, "r", encoding="utf-8") as f:
            raw_tuples = f.read()
        raw_count = len([l for l in raw_tuples.splitlines() if l.strip()])
        print(f"  Loaded {raw_count} raw tuples")

        # Step 2: Deduplicate + Validate
        print("[2/4] Deduplicating and validating...")
        deduped = deduplicate(raw_tuples)
        dedup_count = len([l for l in deduped.splitlines() if l.strip()])
        print(f"  {raw_count} -> {dedup_count} after deduplication")

        validated = validate_and_filter(deduped)
        valid_count = len([l for l in validated.splitlines() if l.strip()])

        validated_path = out("validated_triplets.txt")
        with open(validated_path, "w", encoding="utf-8") as f:
            f.write(validated)

        # Step 3: Build Knowledge Graph
        print("[3/4] Building Knowledge Graph...")

        # Update Master Graph
        master_graph_path = os.path.join(base_dir, ONTOLOGY_DIR, "master_knowledge_graph.graphml")
        if os.path.exists(master_graph_path):
            print("  Loading existing Master Graph...")
            G_master = nx.read_graphml(master_graph_path)
        else:
            print("  Creating new Master Graph...")
            G_master = nx.DiGraph()

        G_master = build_graph(validated, G_master)
        nx.write_graphml(G_master, master_graph_path)
        print(f"  Master Graph: {G_master.number_of_nodes()} nodes, {G_master.number_of_edges()} edges")

        # Build local graph for this PDF
        graph_path = out("knowledge_graph.graphml")
        G_local = build_graph(validated)
        nx.write_graphml(G_local, graph_path)
        print(f"  Local Graph: {G_local.number_of_nodes()} nodes, {G_local.number_of_edges()} edges")

        # Step 4: Multi-hop Reasoning
        print("[4/4] Inferring Multi-hop Relationships...")
        inferred_paths = reason_graph(G_master)
        reasoning_path = out("reasoning_output.txt")
        with open(reasoning_path, "w", encoding="utf-8") as f:
            f.write("\n".join(inferred_paths))
        print(f"  Found {len(inferred_paths)} reasoning paths (via Master Graph)")

        # Summary
        print(f"\n{'─'*55}")
        print(f"PIPELINE COMPLETE for: {pdf_name}")
        print(f"  Raw Tuples:     {raw_count}")
        print(f"  After Validate: {valid_count}")
        print(f"  Master Nodes:   {G_master.number_of_nodes()}")
        print(f"  Master Edges:   {G_master.number_of_edges()}")
        print(f"  Paths Found:    {len(inferred_paths)}")
        print(f"{'─'*55}")

        if inferred_paths:
            print("\nSample Reasoning Chains:")
            for p in inferred_paths[:10]:
                print(f"  {p}")
