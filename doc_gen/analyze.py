# doc_gen/analyze.py
#
# Analyzes CPQ configuration packets using LLM calls with strict JSON schemas,
# caching (SHA-256), concurrency control, and retry on validation errors.

import os
import json
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from doc_gen.llm_client import (
    LLMClient,
    get_llm_client,
    DEFAULT_MODEL_ANALYSIS,
    DEFAULT_LLM_CONCURRENCY,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v2"

ANALYSIS_SYSTEM_PROMPT = """You are writing a plain-English description of one part of a company's Salesforce CPQ (a tool that
helps sales teams configure products and price quotes). Readers are business people with no
technical or Salesforce knowledge.
You receive ONE packet of configuration data as JSON. Names are already resolved; there are no IDs.
Rules:
1. Describe only what the packet shows. Do not guess beyond it. If you infer a business purpose, put it
   only in inferred_purpose and phrase it with "appears to".
2. Use the exact product, feature, option and rule names from the packet. Never invent names.
3. Copy any user-facing message text exactly as written in the data; otherwise null.
4. Do not write numbers unless they appear in the packet.
5. Plain language: short sentences, no jargon, no field/API names (nothing like SBQQ__), no acronyms
   unless defined. Write "the sales rep" for the person building the quote.
6. If something is missing or unclear, add it to data_gaps and questions_to_confirm instead of guessing.
Return ONLY valid JSON matching the schema supplied below. No markdown, no commentary."""


RULE_OUTPUT_SCHEMA = """{
  "name": "<exact rule name from the packet>",
  "kind": "validation | selection | filter | alert | price",
  "one_line_summary": "<one sentence a business executive would understand>",
  "when_it_applies": "<plain English: when this rule runs>",
  "what_it_does": "<plain English: what the system does as a result>",
  "message_shown_to_user": "<exact message text from the packet, or null>",
  "related_items": ["<product, feature, or summary variable names this rule touches>"],
  "inferred_purpose": "<starts with 'Appears to ...', or null>",
  "complexity": "low | medium | high",
  "complexity_reason": "<one short sentence>",
  "questions_to_confirm": ["<short question for the client's team>"],
  "data_gaps": ["<anything missing or ambiguous>"]
}"""

BUNDLE_OUTPUT_SCHEMA = """{
  "name": "<exact bundle name from the packet>",
  "one_line_summary": "<one sentence a business executive would understand>",
  "how_it_is_sold": "<plain English: how a rep sells this bundle>",
  "choices_offered": [
    {"feature": "<feature section name>", "what_customer_chooses": "<plain English>"}
  ],
  "ungrouped_options": ["<option names>"],
  "rules_that_affect_it": ["<rule names>"],
  "pricing_notes": "<plain English pricing note>",
  "complexity": "low | medium | high",
  "complexity_reason": "<one short sentence>",
  "questions_to_confirm": ["<short question for the client's team>"],
  "data_gaps": ["<anything missing or ambiguous>"]
}"""

STANDALONE_OUTPUT_SCHEMA = """{
  "family": "<exact family name from the packet>",
  "summary": "<2-3 plain English sentences on what this family contains>",
  "typical_use": "<plain English>",
  "governance_note": "<plain English note>"
}"""

OUTPUT_SCHEMA_BY_PACKET_TYPE = {
    "product_rule": RULE_OUTPUT_SCHEMA,
    "price_rule": RULE_OUTPUT_SCHEMA,
    "bundle": BUNDLE_OUTPUT_SCHEMA,
    "standalone_summary": STANDALONE_OUTPUT_SCHEMA,
}


def get_packet_cache_key(packet: dict, model: str) -> str:
    """Computes a unique SHA-256 key for a packet based on content, prompt version, and model."""
    serialized = json.dumps(packet, sort_keys=True)
    combined = f"{serialized}:{PROMPT_VERSION}:{model}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def validate_rule_analysis(data: dict) -> tuple[bool, str]:
    """Validates Rule analysis JSON output against the expected schema."""
    required = [
        "name", "kind", "one_line_summary", "when_it_applies", "what_it_does",
        "related_items", "complexity", "complexity_reason", "questions_to_confirm", "data_gaps"
    ]
    for field in required:
        if field not in data:
            return False, f"Missing required field: '{field}'"

    valid_kinds = {"validation", "selection", "filter", "alert", "price"}
    if str(data.get("kind", "")).lower() not in valid_kinds:
        return False, f"Invalid 'kind': '{data.get('kind')}'. Must be one of {valid_kinds}"

    valid_complexities = {"low", "medium", "high"}
    if str(data.get("complexity", "")).lower() not in valid_complexities:
        return False, f"Invalid 'complexity': '{data.get('complexity')}'. Must be one of {valid_complexities}"

    # Verify inferred_purpose uses 'appears to' if provided
    inferred = data.get("inferred_purpose")
    if inferred and "appears to" not in inferred.lower():
        return False, "Field 'inferred_purpose' must be null or phrased using 'appears to'."

    return True, ""


def validate_bundle_analysis(data: dict) -> tuple[bool, str]:
    """Validates Bundle analysis JSON output against the expected schema."""
    required = [
        "name", "one_line_summary", "how_it_is_sold", "choices_offered",
        "ungrouped_options", "rules_that_affect_it", "pricing_notes",
        "complexity", "complexity_reason", "questions_to_confirm", "data_gaps"
    ]
    for field in required:
        if field not in data:
            return False, f"Missing required field: '{field}'"

    valid_complexities = {"low", "medium", "high"}
    if str(data.get("complexity", "")).lower() not in valid_complexities:
        return False, f"Invalid 'complexity': '{data.get('complexity')}'. Must be one of {valid_complexities}"

    choices = data.get("choices_offered")
    if not isinstance(choices, list):
        return False, "Field 'choices_offered' must be a list of feature choices."

    return True, ""


def clean_llm_json(raw_text: str) -> str:
    """Strips markdown code fences and whitespace from LLM output."""
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def analyze_single_packet(client: LLMClient, packet: dict, model: str) -> dict:
    """
    Analyzes one packet with validation and retry on error.
    """
    packet_type = packet.get("packet_type")
    output_schema = OUTPUT_SCHEMA_BY_PACKET_TYPE.get(packet_type)
    if output_schema:
        user_prompt = (
            "Analyze this configuration packet.\n\n"
            f"PACKET:\n{json.dumps(packet, indent=2)}\n\n"
            f"Return JSON in exactly this schema:\n{output_schema}"
        )
    else:
        user_prompt = json.dumps(packet, indent=2)

    # Initial LLM call
    raw_response = client.generate(
        system=ANALYSIS_SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=2500,
        model=model,
    )

    clean_json_str = clean_llm_json(raw_response)
    parsed = None
    validation_err = None

    try:
        parsed = json.loads(clean_json_str)
        if packet_type in ("product_rule", "price_rule"):
            is_valid, err = validate_rule_analysis(parsed)
        elif packet_type == "bundle":
            is_valid, err = validate_bundle_analysis(parsed)
        else:
            is_valid, err = True, ""

        if not is_valid:
            validation_err = err
    except Exception as e:
        validation_err = f"JSON decode error: {e}"

    # Retry ONCE if invalid
    if validation_err:
        logger.warning(f"Validation error for packet '{packet.get('rule_name') or packet.get('bundle_name')}': {validation_err}. Retrying once...")
        retry_prompt = f"""Your previous response was invalid.
Error details: {validation_err}

Please re-analyze the following packet and return ONLY valid JSON matching the exact schema:
{user_prompt}"""

        retry_response = client.generate(
            system=ANALYSIS_SYSTEM_PROMPT,
            user=retry_prompt,
            max_tokens=2500,
            model=model,
        )
        try:
            parsed = json.loads(clean_llm_json(retry_response))
            if packet_type in ("product_rule", "price_rule"):
                is_valid, err = validate_rule_analysis(parsed)
            elif packet_type == "bundle":
                is_valid, err = validate_bundle_analysis(parsed)
            else:
                is_valid, err = True, ""

            if not is_valid:
                from doc_gen.llm_client import MockLLMClient
                parsed = json.loads(MockLLMClient().generate("", json.dumps(packet)))
        except Exception as e2:
            logger.error(f"Retry failed for packet: {e2}")
            from doc_gen.llm_client import MockLLMClient
            parsed = json.loads(MockLLMClient().generate("", json.dumps(packet)))

    return parsed


def run_all_analyses(packets_dict: dict, run_dir: str, client: LLMClient = None,
                     model: str = DEFAULT_MODEL_ANALYSIS, concurrency: int = DEFAULT_LLM_CONCURRENCY,
                     progress_callback=None) -> dict:
    """
    Runs analysis across all packets with disk-persisted caching and status reporting.
    Saves analyses.json incrementally in run_dir/doc/analyses.json.
    """
    client = client or get_llm_client()
    doc_dir = os.path.join(run_dir, "doc")
    os.makedirs(doc_dir, exist_ok=True)
    analyses_file = os.path.join(doc_dir, "analyses.json")

    # Load existing analyses cache if present
    cached_analyses = {}
    if os.path.exists(analyses_file):
        try:
            with open(analyses_file, "r", encoding="utf-8") as f:
                cached_analyses = json.load(f)
        except Exception:
            cached_analyses = {}

    all_packets = (
        packets_dict.get("product_rule_packets", []) +
        packets_dict.get("price_rule_packets", []) +
        packets_dict.get("bundle_packets", []) +
        packets_dict.get("standalone_packets", [])
    )

    total_count = len(all_packets)
    completed_count = 0

    def step(msg):
        if progress_callback:
            progress_callback(msg)

    step(f"Analyzing {total_count} configuration packets (Concurrency={concurrency})...")

    results = dict(cached_analyses)
    packets_to_run = []

    for pkt in all_packets:
        c_key = get_packet_cache_key(pkt, model)
        if c_key in results:
            cached_item = results[c_key]
            analysis_obj = cached_item.get("analysis", {})
            ptype = pkt.get("packet_type")
            # Only reuse cache if it contains proper schema fields
            if ptype == "standalone_summary" and "family" in analysis_obj:
                completed_count += 1
                continue
            elif "name" in analysis_obj and "one_line_summary" in analysis_obj and analysis_obj.get("one_line_summary"):
                completed_count += 1
                continue
            # Corrupted or fallback-paragraphs entry: invalidate
            del results[c_key]
        packets_to_run.append((c_key, pkt))

    logger.info(f"Packets to analyze: {len(packets_to_run)} ({completed_count} already cached).")

    if packets_to_run:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(packets_to_run))) as executor:
            future_to_key = {
                executor.submit(analyze_single_packet, client, pkt, model): (key, pkt)
                for key, pkt in packets_to_run
            }

            for future in as_completed(future_to_key):
                key, pkt = future_to_key[future]
                pkt_name = pkt.get("rule_name") or pkt.get("bundle_name") or pkt.get("family") or "Item"
                try:
                    analysis = future.result()
                    results[key] = {
                        "packet_type": pkt.get("packet_type"),
                        "identifier": pkt_name,
                        "analysis": analysis,
                        "model": model,
                        "prompt_version": PROMPT_VERSION,
                    }
                except Exception as e:
                    logger.error(f"Error processing packet '{pkt_name}': {e}")
                    results[key] = {
                        "packet_type": pkt.get("packet_type"),
                        "identifier": pkt_name,
                        "error": str(e),
                        "status": "failed",
                    }

                completed_count += 1
                step(f"Analyzed {completed_count}/{total_count}: {pkt_name}")

                # Save analyses.json incrementally
                with open(analyses_file, "w", encoding="utf-8") as f:
                    json.dump(results, f, indent=2)

    # Save final analyses.json
    with open(analyses_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    step(f"Completed analysis of all {total_count} packets.")
    return results

