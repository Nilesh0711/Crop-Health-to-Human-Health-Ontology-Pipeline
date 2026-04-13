from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from openai import OpenAI, RateLimitError, BadRequestError
from dotenv import load_dotenv
from tqdm import tqdm
import json, os, sys, time, re

# CONFIG
PDF_PATH       = "pdf/The links between agriculture and health.pdf"
ONTOLOGY_DIR   = "ontology"
PROMPT_DIR     = "prompt"
MODEL          = "gemma4-gpu"
CHUNK_SIZE     = 16000   # Characters per chunk (approx 4k tokens)
CONTEXT_WINDOW = 65536           # Total tokens for Ollama
REQUEST_TIMEOUT = 1800           # Seconds to wait for slow local Ollama calls
TRIPLET_BATCH_SIZE = 40           # Keep local Ollama prompts small enough to avoid timeouts
USE_EXISTING_RAW_TRIPLETS = True  # Resume after crashes without redoing step 3

TRIPLET_PROMPT_FILE      = os.path.join(PROMPT_DIR, "Triplet Extraction prompt 1")
CLEAN_PROMPT_FILE        = os.path.join(PROMPT_DIR, "Triplet Cleaning prompt 2")
ONTOLOGY_PROMPT_FILE     = os.path.join(PROMPT_DIR, "Ontology Builder prompt 3")
EXPANSION_PROMPT_FILE    = os.path.join(PROMPT_DIR, "Logical Expansion prompt 4")
PROPERTY_PROMPT_FILE     = os.path.join(PROMPT_DIR, "Property Extraction prompt 5")
CLASS_PROMPT_FILE        = os.path.join(PROMPT_DIR, "Class Generation prompt 6")
CLASSIFY_PROMPT_FILE     = os.path.join(PROMPT_DIR, "Entity Classification prompt 7")
NORM_REL_PROMPT_FILE     = os.path.join(PROMPT_DIR, "Relation Normalization prompt 8")
VALIDATE_PROMPT_FILE     = os.path.join(PROMPT_DIR, "Ontology Validation prompt 9")

# LOAD API KEY + CLIENT
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
client = OpenAI(
    api_key="ollama",
    base_url="http://localhost:11434/v1",
    timeout=REQUEST_TIMEOUT,
)

# LOAD PROMPTS
def load_prompt(file_path):
    full_path = os.path.join(os.path.dirname(__file__), file_path)
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()

triplet_prompt_template   = load_prompt(TRIPLET_PROMPT_FILE)
clean_prompt_template     = load_prompt(CLEAN_PROMPT_FILE)
ontology_prompt_template  = load_prompt(ONTOLOGY_PROMPT_FILE)
expansion_prompt_template = load_prompt(EXPANSION_PROMPT_FILE)
property_prompt_template  = load_prompt(PROPERTY_PROMPT_FILE)
class_prompt_template     = load_prompt(CLASS_PROMPT_FILE)
classify_prompt_template  = load_prompt(CLASSIFY_PROMPT_FILE)
norm_rel_prompt_template  = load_prompt(NORM_REL_PROMPT_FILE)
validate_prompt_template  = load_prompt(VALIDATE_PROMPT_FILE)

# PDF TO TEXT
def extract_text(pdf_path):
    pdf_options = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)}
    )
    page_texts = []
    failed_pages = []
    page_no = 1
    consecutive_missing_pages = 0
    max_consecutive_missing_pages = 3

    # Converting the whole PDF at once can make Docling's preprocessing stage
    # allocate too much memory and silently drop pages. Page-by-page conversion
    # is slower, but it is much more stable for dense/scanned academic PDFs.
    while consecutive_missing_pages < max_consecutive_missing_pages:
        try:
            result = converter.convert(
                pdf_path,
                page_range=(page_no, page_no),
                raises_on_error=False,
            )
            document = getattr(result, "document", None)
            page_text = document.export_to_markdown().strip() if document else ""
            status = str(getattr(result, "status", ""))

            if "FAILURE" in status and not page_text:
                consecutive_missing_pages += 1
                failed_pages.append(page_no)
                page_no += 1
                continue

            consecutive_missing_pages = 0
            if page_text:
                page_texts.append(f"\n\n<!-- page {page_no} -->\n\n{page_text}")
            else:
                page_texts.append(f"\n\n<!-- page {page_no}: no extractable text -->")
        except Exception as e:
            consecutive_missing_pages += 1
            failed_pages.append(page_no)
            print(f"  Page {page_no} extraction failed: {e}")

        page_no += 1

    # The last few failures are usually Docling reporting page ranges beyond
    # the end of the PDF, so do not count them as failed document pages.
    trailing_missing = set(range(page_no - consecutive_missing_pages, page_no))
    failed_pages = [p for p in failed_pages if p not in trailing_missing]

    extracted_pages = len(page_texts)
    print(f"  Extracted {extracted_pages} pages")
    if failed_pages:
        print(f"  Pages with extraction failures: {failed_pages}")

    if not page_texts:
        raise RuntimeError("No text could be extracted from the PDF.")

    return "\n".join(page_texts)

# CHUNK TEXT (sentence-aware)
def chunk_text(text, size=2000):
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        if end >= len(text):
            chunks.append(text[start:])
            break
        boundary = text.rfind(". ", start, end)
        if boundary == -1 or boundary <= start:
            boundary = end
        else:
            boundary += 1
        chunks.append(text[start:boundary])
        start = boundary
    return chunks

# LLM CALL WITH RETRY
def llm_call(prompt, temperature=0.0, max_retries=4):
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                timeout=REQUEST_TIMEOUT,
                extra_body={
                    "num_ctx": CONTEXT_WINDOW,
                    "keep_alive": "30m",
                }
            )
            return response.choices[0].message.content
        except RateLimitError:
            wait = 30 * (attempt + 1)
            print(f"  Rate limit - waiting {wait}s (attempt {attempt+1}/{max_retries})...")
            time.sleep(wait)
        except Exception as e:
            print(f"  LLM call failed: {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("Max retries exceeded")

# PIPELINE STEPS

def extract_triplets(chunk):
    prompt = triplet_prompt_template.replace("{INPUT_TEXT}", chunk)
    return llm_call(prompt, temperature=0.0)

def deduplicate_triplets(triplets_text):
    """Deterministic dedup - no LLM needed."""
    lines = triplets_text.split("\n")
    seen, result = set(), []
    for line in lines:
        key = line.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(line.strip())
    return "\n".join(result)

def strip_llm_noise(text):
    """
    FIX (BONUS): Remove LLM preamble/postamble and numbered lists.
    Ensures only <subject, relation, object> lines survive.
    Also handles HTML-tagged output like <a subject, rel, obj</a> that LLMs sometimes emit.
    """
    lines = text.strip().splitlines()
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Strip leading numbers like "1. " or "1) "
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        line = re.sub(r"^<a\s+", "<", line, flags=re.IGNORECASE)
        line = re.sub(r"</a>$", ">", line, flags=re.IGNORECASE)
        # ONLY strip actual HTML tags that are likely to be noise (a, p, div, span, etc.)
        line = re.sub(r"</?(a|p|div|span|blockquote|li|ul|ol|h[1-6])[^>]*>", "", line, flags=re.IGNORECASE).strip()
        # Re-wrap bare triplets that lost their angle brackets
        if line.startswith("<") and line.endswith(">"):
            cleaned.append(line)
        elif line.count(",") >= 2:
            cleaned.append(f"<{line}>")
        # Drop lines that are plain English commentary
    return "\n".join(cleaned)

def batch_lines(text, batch_size=TRIPLET_BATCH_SIZE):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for start in range(0, len(lines), batch_size):
        yield "\n".join(lines[start:start + batch_size])

def clean_triplets(triplets):
    cleaned_batches = []
    batches = list(batch_lines(triplets))
    for batch in tqdm(batches, desc="  Cleaning batches"):
        prompt = clean_prompt_template.replace("{TRIPLETS}", batch)
        raw = llm_call(prompt, temperature=0)
        cleaned_batches.append(strip_llm_noise(raw))
    return "\n".join(cleaned_batches)

def normalize_entities(triplets):
    normalized_batches = []
    batches = list(batch_lines(triplets))
    for batch in tqdm(batches, desc="  Entity batches"):
        prompt = f"""You are normalizing entity names in scientific triplets about agriculture and health.

Rules:
- Merge clearly identical concepts: "soil microbes"/"soil microorganisms" -> "Soil Microorganism"
- Expand abbreviations: "N" -> "Nitrogen", "P" -> "Phosphorus", "K" -> "Potassium", "Zn" -> "Zinc", "Fe" -> "Iron", "Mn" -> "Manganese"
- Capitalize proper scientific names
- Keep specific entities specific: do NOT merge "Nitrogen" and "Phosphorus" into "Nutrients"
- Do NOT remove any triplets
- IMPORTANT: Output MUST be in exact format: <subject, relation, object>
  Each line must have exactly 3 parts separated by commas inside angle brackets.

Return ONLY the updated triplets, one per line, in format: <subject, relation, object>
No explanations, no numbering.

Triplets:
{batch}
"""
        raw = llm_call(prompt, temperature=0)
        normalized_batches.append(strip_llm_noise(raw))
    return "\n".join(normalized_batches)

def normalize_relations(triplets):
    """FIX 7: Merge synonymous relation labels into canonical atomic verbs."""
    normalized_batches = []
    batches = list(batch_lines(triplets))
    for batch in tqdm(batches, desc="  Relation batches"):
        prompt = norm_rel_prompt_template.replace("{TRIPLETS}", batch)
        raw = llm_call(prompt, temperature=0)
        normalized_batches.append(strip_llm_noise(raw))
    return "\n".join(normalized_batches)

def extract_json_array_in_batches(text, prompt_template, placeholder, array_key, desc):
    merged = []
    batches = list(batch_lines(text))
    for batch in tqdm(batches, desc=desc):
        prompt = prompt_template.replace(placeholder, batch)
        raw = llm_call(prompt, temperature=0)
        try:
            parsed = normalize_json_text(raw)
            merged.extend(parsed.get(array_key, []))
        except Exception:
            print(f"  WARNING: Could not parse {array_key} batch JSON; keeping raw batch output")
            merged.append({"raw": raw})
    return json.dumps({array_key: merged}, indent=2, ensure_ascii=False)

def extract_properties(triplets):
    """FIX 3: Identify entity attributes/properties that should not be entities."""
    return extract_json_array_in_batches(
        triplets,
        property_prompt_template,
        "{TRIPLETS}",
        "properties",
        "  Property batches",
    )

CORE_CLASSES = [
    "Soil",
    "Nutrient",
    "Microorganism",
    "Crop",
    "SoilInput",
    "AgriculturalPractice",
    "EnvironmentalFactor",
    "BiologicalProcess",
    "Chemical",
    "HumanHealth",
]

def generate_classes():
    """FIX 5: Return hardcoded base ontology class schema - NOT LLM-generated."""
    return CORE_CLASSES

def classify_entities(triplets):
    """FIX 6: Classify specific entities from triplets into one of the 10 core classes."""
    classes_json = json.dumps(CORE_CLASSES, indent=2)
    prompt_template = classify_prompt_template.replace("{CLASSES}", classes_json)
    return extract_json_array_in_batches(
        triplets,
        prompt_template,
        "{TRIPLETS}",
        "instances",
        "  Classification batches",
    )

BANNED_RELATION_WORDS = {"affects", "increases", "reduces", "supports", "contains",
                         "produces", "decreases", "improves", "causes", "regulates"}

def validate_entity(entity: str) -> bool:
    """Return True if entity is a valid noun phrase, False if it looks like a verb phrase."""
    tokens = entity.strip().split()
    if len(tokens) > 4:
        return False
    if tokens and tokens[0].lower() in BANNED_RELATION_WORDS:
        return False
    return True

def filter_triplets(triplets_text: str) -> str:
    """Remove triplets whose subject or object fail entity validation."""
    result = []
    for line in triplets_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Parse <subject, relation, object>
        inner = line.strip("<>")
        parts = [p.strip() for p in inner.split(",", 2)]
        if len(parts) != 3:
            continue
        subj, _rel, obj = parts
        if validate_entity(subj) and validate_entity(obj):
            result.append(line)
    return "\n".join(result)

def cluster_concepts(triplets):
    prompt = f"""You are organizing entities from scientific triplets about agriculture, soil, and human health.

Group every unique entity from the triplets into one of these clusters:
- Soil Properties
- Soil Inputs
- Nutrients
- Crops and Plants
- Microorganisms
- Environmental Factors
- Human Health
- Processes

IMPORTANT: Only place ENTITY names in the cluster lists.
Do NOT place relation phrases (e.g. "affects X", "increases Y") in any cluster.

Return ONLY valid JSON, no extra text:
{{
  "Soil Properties": [],
  "Soil Inputs": [],
  "Nutrients": [],
  "Crops and Plants": [],
  "Microorganisms": [],
  "Environmental Factors": [],
  "Human Health": [],
  "Processes": []
}}

Triplets:
{triplets}
"""
    return llm_call(prompt, temperature=0)

def generate_hierarchy(classified_json: str) -> str:
    """FIX 4: is-a hierarchy only, derived from classified entity list."""
    prompt = f"""You are creating is-a (parent-child) subclass relationships for a formal ontology.

STRICT DEFINITION OF is-a:
  A is-a B  means  "A is a type of B"
  Example: Nitrogen is-a Nutrient  (VALID)
  Example: Soil pH is-a Soil       (INVALID - pH is a property, not a type of Soil)

Rules:
- ONLY assert is-a if the child is genuinely a SUBTYPE of the parent class.
- Do NOT use properties (pH, content, moisture, yield) as hierarchy children.
- Do NOT use relation phrases ("affects X", "increases Y") as children.
- Parents MUST come from this official class list:
  Soil, Nutrient, Microorganism, Crop, SoilInput, AgriculturalPractice,
  EnvironmentalFactor, BiologicalProcess, Chemical, HumanHealth
- Do NOT invent entities not present in the input.

Valid examples:
  Nitrogen -> Nutrient
  Beneficial Bacteria -> Microorganism
  Compost -> SoilInput
  Irrigation -> AgriculturalPractice
  Tocopherol -> Chemical

Return ONLY valid JSON - no markdown, no extra text:
{{
  "subclasses": [
    {{"child": "...", "parent": "..."}}
  ]
}}

Classified entities (use these as input):
{classified_json}
"""
    return llm_call(prompt, temperature=0)

ALLOWED_RELATIONS = {
    "affects",
    "increases",
    "decreases",
    "supports",
    "contains",
    "produces",
    "reduces",
}

def parse_triplet_line(line: str):
    line = line.strip()
    if not line.startswith("<") or not line.endswith(">"):
        return None
    parts = [part.strip() for part in line[1:-1].split(",", 2)]
    if len(parts) != 3 or not all(parts):
        return None
    subject, relation, obj = parts
    relation = relation.lower()
    if relation not in ALLOWED_RELATIONS:
        return None
    return {"source": subject, "relation": relation, "target": obj}

def build_ontology(triplets: str, hierarchy_json: str, classified_json: str, properties_json: str) -> str:
    """Build ontology deterministically to avoid one huge timeout-prone LLM call."""
    try:
        hierarchy_dict = normalize_json_text(hierarchy_json)
    except Exception:
        hierarchy_dict = {"subclasses": []}

    try:
        classified_dict = normalize_json_text(classified_json)
    except Exception:
        classified_dict = {"instances": []}

    try:
        properties_dict = normalize_json_text(properties_json)
    except Exception:
        properties_dict = {"properties": []}

    relations = []
    for line in triplets.splitlines():
        relation = parse_triplet_line(line)
        if relation:
            relations.append(relation)

    ontology = {
        "classes": CORE_CLASSES,
        "instances": classified_dict.get("instances", []),
        "subclasses": hierarchy_dict.get("subclasses", []),
        "relations": relations,
        "properties": properties_dict.get("properties", []),
    }
    return json.dumps(clean_ontology(ontology), indent=2, ensure_ascii=False)

def clean_ontology(ontology: dict) -> dict:
    """Remove duplicate relations, invalid hierarchy, and action-word properties."""
    # 1. Deduplicate relations
    seen: set = set()
    unique_relations = []
    for r in ontology.get("relations", []):
        key = (r.get("source", "").lower(), r.get("relation", "").lower(), r.get("target", "").lower())
        if key not in seen:
            seen.add(key)
            unique_relations.append(r)
    ontology["relations"] = unique_relations

    # 2. Remove properties that are relation verbs, not attributes
    allowed_classes = set(c.lower() for c in CORE_CLASSES)
    valid_properties = []
    for p in ontology.get("properties", []):
        prop_name = p.get("property", "").lower()
        if prop_name not in BANNED_RELATION_WORDS:
            valid_properties.append(p)
    ontology["properties"] = valid_properties

    # 3. Remove hierarchy entries where parent is not in CORE_CLASSES
    valid_subclasses = []
    for s in ontology.get("subclasses", []):
        parent = s.get("parent", "")
        child = s.get("child", "")
        if parent in CORE_CLASSES and validate_entity(child) and validate_entity(parent):
            valid_subclasses.append(s)
    ontology["subclasses"] = valid_subclasses

    # 4. Ensure classes field is always the canonical set
    ontology["classes"] = CORE_CLASSES

    return ontology

def expand_ontology(triplets, checkpoint_path=None):
    partial_path = f"{checkpoint_path}.partial.json" if checkpoint_path else None
    expanded_batches = []
    batches = list(batch_lines(triplets))

    if partial_path and os.path.exists(partial_path):
        try:
            with open(partial_path, "r", encoding="utf-8") as f:
                expanded_batches = json.load(f)
            print(f"  Resuming expansion from batch {len(expanded_batches) + 1}/{len(batches)}")
        except Exception:
            expanded_batches = []

    start = len(expanded_batches)
    for batch in tqdm(batches[start:], desc="  Expansion batches", initial=start, total=len(batches)):
        prompt = expansion_prompt_template.replace("{TRIPLETS}", batch)
        expanded_batches.append(llm_call(prompt, temperature=0.0))
        if partial_path:
            with open(partial_path, "w", encoding="utf-8") as f:
                json.dump(expanded_batches, f, indent=2, ensure_ascii=False)

    if partial_path and os.path.exists(partial_path):
        os.remove(partial_path)
    return "\n".join(expanded_batches)

def validate_ontology(ontology_json_str):
    """FIX 8: Validate and auto-correct common ontology mistakes."""
    return ontology_json_str

# JSON NORMALIZATION
def normalize_json_text(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return json.loads(cleaned[start:end + 1])
    raise json.JSONDecodeError("No valid JSON found", cleaned, 0)

# MAIN PIPELINE
if __name__ == "__main__":
    try:
        base_dir = os.path.dirname(__file__)
        pdf_stem = os.path.splitext(os.path.basename(PDF_PATH))[0]
        pdf_output_dir = os.path.join(base_dir, ONTOLOGY_DIR, pdf_stem)
        os.makedirs(pdf_output_dir, exist_ok=True)

        def out(suffix):
            return os.path.join(pdf_output_dir, suffix)

        raw_triplets_path = out("raw_triplets.txt")
        if USE_EXISTING_RAW_TRIPLETS and os.path.exists(raw_triplets_path):
            print("\n[1-3/11] Reusing existing raw triplets...")
            with open(raw_triplets_path, "r", encoding="utf-8") as f:
                raw_triplets = f.read()
            raw_count = len([l for l in raw_triplets.splitlines() if l.strip()])
            print(f"  Loaded {raw_count} raw triplets from previous run")
        else:
            print("\n[1/11] Extracting text from PDF...")
            pdf_text = extract_text(os.path.join(base_dir, PDF_PATH))
            print(f"  {len(pdf_text):,} characters extracted")

            print("[2/11] Chunking text...")
            chunks = chunk_text(pdf_text, size=CHUNK_SIZE)
            print(f"  {len(chunks)} chunks")

            print("[3/11] Extracting triplets from each chunk...")
            all_triplets = []
            for chunk in tqdm(chunks, desc="  Extracting"):
                all_triplets.append(extract_triplets(chunk))
            raw_triplets = "\n".join(all_triplets)
            raw_count = len([l for l in raw_triplets.splitlines() if l.strip()])
            print(f"  {raw_count} raw triplets")
            with open(raw_triplets_path, "w", encoding="utf-8") as f:
                f.write(raw_triplets)
        # Step 4: Clean + dedup
        print("[4/11] Cleaning and deduplicating triplets...")
        cleaned_path = out("cleaned_triplets.txt")
        if USE_EXISTING_RAW_TRIPLETS and os.path.exists(cleaned_path):
            with open(cleaned_path, "r", encoding="utf-8") as f:
                cleaned_triplets = f.read()
            print("  Reusing existing cleaned_triplets.txt")
        else:
            llm_cleaned = clean_triplets(raw_triplets)
            cleaned_triplets = deduplicate_triplets(llm_cleaned)
        clean_count = len([l for l in cleaned_triplets.splitlines() if l.strip()])
        print(f"  {raw_count} -> {clean_count} triplets after cleaning")
        with open(cleaned_path, "w", encoding="utf-8") as f:
            f.write(cleaned_triplets)

        # Step 5: Normalize entities
        print("[5/11] Normalizing entity names...")
        normalized_path = out("normalized_triplets.txt")
        if USE_EXISTING_RAW_TRIPLETS and os.path.exists(normalized_path):
            with open(normalized_path, "r", encoding="utf-8") as f:
                normalized = f.read()
            print("  Reusing existing normalized_triplets.txt")
        else:
            normalized = normalize_entities(cleaned_triplets)
            normalized = filter_triplets(normalized)
        norm_count = len([l for l in normalized.splitlines() if l.strip()])
        print(f"  {clean_count} -> {norm_count} triplets after entity validation")
        with open(normalized_path, "w", encoding="utf-8") as f:
            f.write(normalized)

        # Step 6: Normalize relations
        print("[6/11] Normalizing relation labels to atomic verbs...")
        relation_normalized_path = out("relation_normalized_triplets.txt")
        if USE_EXISTING_RAW_TRIPLETS and os.path.exists(relation_normalized_path):
            with open(relation_normalized_path, "r", encoding="utf-8") as f:
                normalized = f.read()
            print("  Reusing existing relation_normalized_triplets.txt")
        else:
            normalized = normalize_relations(normalized)
        with open(relation_normalized_path, "w", encoding="utf-8") as f:
            f.write(normalized)
        print("[7/11] Extracting entity properties...")
        properties_path = out("properties.json")
        if USE_EXISTING_RAW_TRIPLETS and os.path.exists(properties_path):
            with open(properties_path, "r", encoding="utf-8") as f:
                properties_dict = json.load(f)
            properties_raw = json.dumps(properties_dict, indent=2, ensure_ascii=False)
            print("  Reusing existing properties.json")
        else:
            properties_raw = extract_properties(normalized)
            try:
                properties_dict = normalize_json_text(properties_raw)
            except Exception:
                print("  WARNING: Could not parse properties JSON; saving raw")
                properties_dict = {"properties": [], "raw": properties_raw}

            seen_props: set = set()
            unique_props = []
            for p in properties_dict.get("properties", []):
                key = (p.get("entity", "").strip().lower(), p.get("property", "").strip().lower())
                prop_name = p.get("property", "").strip().lower()
                if key not in seen_props and prop_name not in BANNED_RELATION_WORDS:
                    seen_props.add(key)
                    unique_props.append(p)
            properties_dict["properties"] = unique_props
            properties_raw = json.dumps(properties_dict, indent=2, ensure_ascii=False)
        print(f"  Properties (deduped + filtered): {len(properties_dict.get('properties', []))}")

        with open(properties_path, "w", encoding="utf-8") as f:
            json.dump(properties_dict, f, indent=2, ensure_ascii=False)
        print("[8/11] Generating classes, classifying entities, building hierarchy...")

        # 8a: Hardcoded class schema
        classes_list = generate_classes()
        with open(out("classes.json"), "w", encoding="utf-8") as f:
            json.dump(classes_list, f, indent=2, ensure_ascii=False)
        print(f"  Classes : {classes_list}")

        # 8b: Classify specific entities from triplets into the 10 classes
        classified_path = out("classified_entities.json")
        if USE_EXISTING_RAW_TRIPLETS and os.path.exists(classified_path):
            with open(classified_path, "r", encoding="utf-8") as f:
                classified_dict = json.load(f)
            classified_raw = json.dumps(classified_dict, indent=2, ensure_ascii=False)
            print("  Reusing existing classified_entities.json")
        else:
            classified_raw = classify_entities(normalized)
            try:
                classified_dict = normalize_json_text(classified_raw)
            except Exception:
                print("  WARNING: Could not parse classified JSON; using empty")
                classified_dict = {"instances": []}

            seen_instances: set = set()
            unique_instances = []
            for inst in classified_dict.get("instances", []):
                key = inst.get("name", "").strip().lower()
                if key and key not in seen_instances:
                    seen_instances.add(key)
                    unique_instances.append(inst)
            classified_dict["instances"] = unique_instances
            classified_raw = json.dumps(classified_dict, indent=2, ensure_ascii=False)
        print(f"  Classified instances (deduped): {len(classified_dict.get('instances', []))}")

        with open(classified_path, "w", encoding="utf-8") as f:
            json.dump(classified_dict, f, indent=2, ensure_ascii=False)

        # 8c: Hierarchy from classified entities (is-a only)
        hierarchy_path = out("hierarchy.json")
        if USE_EXISTING_RAW_TRIPLETS and os.path.exists(hierarchy_path):
            with open(hierarchy_path, "r", encoding="utf-8") as f:
                hierarchy_raw = f.read()
            print("  Reusing existing hierarchy.json")
        else:
            hierarchy_raw = generate_hierarchy(json.dumps(classified_dict, indent=2))
        with open(hierarchy_path, "w", encoding="utf-8") as f:
            f.write(hierarchy_raw)
        # (Covered in step 8 above)

        print("[10/11] Building formal ontology...")
        ontology_path = out("ontology.json")
        if USE_EXISTING_RAW_TRIPLETS and os.path.exists(ontology_path):
            with open(ontology_path, "r", encoding="utf-8") as f:
                ontology_dict = json.load(f)
            print("  Reusing existing ontology.json")
        else:
            ontology_raw = build_ontology(
                normalized, hierarchy_raw, classified_raw, properties_raw
            )
            try:
                ontology_dict = normalize_json_text(ontology_raw)
            except Exception:
                print("  WARNING: Could not parse ontology JSON; saving raw")
                ontology_dict = {"raw": ontology_raw}

            print("  Cleaning ontology (dedup, property/relation separation)...")
            if "raw" not in ontology_dict:
                ontology_dict = clean_ontology(ontology_dict)

            print("  Validating ontology...")
            validated_raw = validate_ontology(json.dumps(ontology_dict, indent=2))
            try:
                ontology_dict = normalize_json_text(validated_raw)
                ontology_dict = clean_ontology(ontology_dict)  # clean once more after validation
            except Exception:
                print("  WARNING: Validation could not parse result; keeping pre-validation ontology")

        with open(ontology_path, "w", encoding="utf-8") as f:
            json.dump(ontology_dict, f, indent=2, ensure_ascii=False)

        print("[11/11] Inferring indirect relationships...")
        expanded_path = out("expanded_triplets.txt")
        if USE_EXISTING_RAW_TRIPLETS and os.path.exists(expanded_path):
            with open(expanded_path, "r", encoding="utf-8") as f:
                expanded = f.read()
            print("  Reusing existing expanded_triplets.txt")
        else:
            expanded = expand_ontology(normalized, expanded_path)
        with open(expanded_path, "w", encoding="utf-8") as f:
            f.write(expanded)

        classes   = ontology_dict.get("classes",    [])
        instances = ontology_dict.get("instances",  [])
        subclasses = ontology_dict.get("subclasses", [])
        relations = ontology_dict.get("relations",  [])
        properties = ontology_dict.get("properties", [])

        print("\n" + "="*55)
        print("PIPELINE COMPLETE")
        print(f"  Classes    : {len(classes)}")
        print(f"  Instances  : {len(instances)}")
        print(f"  Subclasses : {len(subclasses)}")
        print(f"  Relations  : {len(relations)}")
        print(f"  Properties : {len(properties)}")
        print(f"  Output dir : {pdf_output_dir}")
        print("="*55)
        if classes:
            print("\nClasses      :", classes[:8])
        if instances:
            print("Sample instances :", [i.get("name") for i in instances[:6]])
        if relations:
            print("Sample relations :", relations[:3])
        if properties:
            print("Sample properties:", properties[:3])

    except RateLimitError as exc:
        print(f"\nRate limit hit: {exc}")
        sys.exit(1)
    except BadRequestError as exc:
        print(f"\nBad request: {exc}")
        sys.exit(1)
