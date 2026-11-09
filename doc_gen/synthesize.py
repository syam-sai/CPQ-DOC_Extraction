# doc_gen/synthesize.py
#
# Synthesizes section summaries and executive summary from packet analyses and facts.
# Enforces placeholder substitution (e.g. {{active_products}}) so code drives all metrics.

import os
import json
import logging
from doc_gen.llm_client import LLMClient, get_llm_client, DEFAULT_MODEL_SYNTHESIS
from doc_gen.analyze import clean_llm_json

logger = logging.getLogger(__name__)

SYNTHESIS_SYSTEM_PROMPT = """You are writing a section of a high-level, client-shareable document about a company's Salesforce CPQ setup.
Readers are senior business executives with no technical or Salesforce knowledge.
Rules:
1. Write in clear, professional business English. Short, impactful paragraphs.
2. No technical jargon, no field names, no acronyms unless explained.
3. NEVER write numbers directly. Instead, you MUST use the exact placeholder tags provided (e.g. {{active_products}}, {{total_bundles}}). The code will inject the verified numbers.
4. Lead with what the setup enables for sales representatives and the quoting process, then scale, then areas deserving leadership attention.
Return ONLY valid JSON with a "paragraphs" list of strings. No markdown formatting, no commentary."""


def build_placeholder_map(facts: dict) -> dict:
    """Builds the dictionary of allowed metric placeholders and their computed values."""
    pr_by_type = facts.get("product_rules", {}).get("by_type", {})
    return {
        "active_products": facts.get("active_products", 0),
        "total_bundles": facts.get("total_bundles", 0),
        "standalone_products": facts.get("standalone_products", 0),
        "total_features": facts.get("total_features", 0),
        "total_options": facts.get("total_options", 0),
        "total_pricebooks": facts.get("total_pricebooks", 1),
        "total_pricebook_entries": facts.get("total_pricebook_entries", 0),
        "total_block_prices": facts.get("total_block_prices", 0),
        "total_dimensions": facts.get("total_dimensions", 0),
        "total_costs": facts.get("total_costs", 0),
        "product_rules_total": facts.get("product_rules", {}).get("total", 0),
        "validation_rules_count": pr_by_type.get("Validation", 0),
        "selection_rules_count": pr_by_type.get("Selection", 0),
        "alert_rules_count": pr_by_type.get("Alert", 0),
        "price_rules_total": facts.get("price_rules", {}).get("total", 0),
        "summary_variables_total": facts.get("summary_variables", {}).get("total", 0),
        "observations_count": facts.get("observations_count", 0),
    }


def substitute_placeholders(text: str, placeholder_map: dict) -> str:
    """Replaces {{placeholder}} tokens with verified factual values."""
    out = text
    for key, val in placeholder_map.items():
        token = f"{{{{{key}}}}}"
        out = out.replace(token, f"{val:,}" if isinstance(val, int) else str(val))
    return out


def synthesize_all(facts: dict, analyses_dict: dict, run_dir: str,
                   client: LLMClient = None, model: str = DEFAULT_MODEL_SYNTHESIS,
                   progress_callback=None) -> dict:
    """
    Synthesizes executive summary and high-level section narratives.
    Produces run_dir/doc/sections.json.
    """
    client = client or get_llm_client()
    doc_dir = os.path.join(run_dir, "doc")
    os.makedirs(doc_dir, exist_ok=True)
    sections_file = os.path.join(doc_dir, "sections.json")

    placeholder_map = build_placeholder_map(facts)

    def step(msg):
        if progress_callback:
            progress_callback(msg)

    step("Synthesizing executive summary and section narratives...")

    # Group analyses by type
    product_rule_analyses = []
    price_rule_analyses = []
    bundle_analyses = []
    standalone_analyses = []

    for item in analyses_dict.values():
        ptype = item.get("packet_type")
        analysis = item.get("analysis", {})
        if ptype == "product_rule":
            product_rule_analyses.append(analysis)
        elif ptype == "price_rule":
            price_rule_analyses.append(analysis)
        elif ptype == "bundle":
            bundle_analyses.append(analysis)
        elif ptype == "standalone_summary":
            standalone_analyses.append(analysis)

    rule_analyses = product_rule_analyses + price_rule_analyses

    # 1. Synthesize Executive Summary
    step("Synthesizing Executive Summary...")
    exec_user_prompt = f"""Write an Executive Summary (maximum 350 words) for a client presentation.
Available metric placeholders you MUST use:
- {{{{active_products}}}}: total active products in catalog
- {{{{total_bundles}}}}: total configured bundle packages
- {{{{standalone_products}}}}: standalone non-configured products
- {{{{total_features}}}}: total feature category sections
- {{{{total_options}}}}: total selectable component options
- {{{{product_rules_total}}}}: total active product configuration rules
- {{{{price_rules_total}}}}: total pricing rules
- {{{{observations_count}}}}: catalog observations requiring team review

Key context:
- Catalog breakdown by family: {json.dumps(facts.get('products_by_family', {}))}
- Observations: {[obs.get('description') for obs in facts.get('observations', [])]}

Return JSON: {{"paragraphs": ["paragraph 1", "paragraph 2", "paragraph 3"]}}"""

    raw_exec = client.generate(SYNTHESIS_SYSTEM_PROMPT, exec_user_prompt, max_tokens=1500, model=model)
    try:
        exec_json = json.loads(clean_llm_json(raw_exec))
        exec_paragraphs = [
            substitute_placeholders(p, placeholder_map) for p in exec_json.get("paragraphs", [])
        ]
    except Exception as e:
        logger.warning(f"Executive summary JSON parse error: {e}. Using fallback structure.")
        exec_paragraphs = [
            substitute_placeholders(
                "This document provides a comprehensive, non-technical overview of your Salesforce CPQ setup. "
                "The active commercial catalog encompasses {{active_products}} products, including {{total_bundles}} "
                "configured product bundles and {{standalone_products}} standalone items sold directly without component configuration.",
                placeholder_map
            ),
            substitute_placeholders(
                "Configured bundles offer a combined {{total_options}} component options structured across {{total_features}} "
                "distinct feature categories. Sales quotation accuracy is maintained by {{product_rules_total}} product rules "
                "and {{price_rules_total}} pricing rule, preventing invalid configurations and standardizing pricing across teams.",
                placeholder_map
            ),
            substitute_placeholders(
                "Our analysis identified {{observations_count}} catalog configuration item(s) deserving closer team review, "
                "such as products without active pricebook entries and bundles structured with flat component lists.",
                placeholder_map
            ),
        ]

    # 2. Synthesize Bundling & Configuration Section Narrative
    step("Synthesizing Bundles narrative...")
    bundles_user_prompt = f"""Write an introduction to the Bundles and Configuration section (2-3 paragraphs).
Explain how products are packaged into configurable bundles versus sold standalone.
Placeholders available: {{{{total_bundles}}}}, {{{{standalone_products}}}}, {{{{total_features}}}}, {{{{total_options}}}}.
Sample bundle structures analyzed: {[b.get('name') for b in bundle_analyses[:8]]}
Return JSON: {{"paragraphs": ["..."]}}"""

    raw_bundles = client.generate(SYNTHESIS_SYSTEM_PROMPT, bundles_user_prompt, max_tokens=1000, model=model)
    try:
        b_json = json.loads(clean_llm_json(raw_bundles))
        bundle_paragraphs = [
            substitute_placeholders(p, placeholder_map) for p in b_json.get("paragraphs", [])
        ]
    except Exception:
        bundle_paragraphs = [
            substitute_placeholders(
                "The product catalog leverages guided selling principles to simplify quoting. Products are organized into "
                "{{total_bundles}} configurable bundle offerings and {{standalone_products}} standalone items.",
                placeholder_map
            ),
            substitute_placeholders(
                "Bundles structure complex products into {{total_features}} clear feature sections containing {{total_options}} "
                "selectable components. Sales representatives select appropriate hardware, software, and services while "
                "automated validation logic ensures component compatibility.",
                placeholder_map
            )
        ]

    # 3. Synthesize Business Rules Section Narrative
    step("Synthesizing Rules narrative...")
    rules_user_prompt = f"""Write an introduction to the Product Rules and Price Rules section (2 paragraphs).
Explain how validation, auto-addition, and alert rules safeguard quote quality.
Placeholders available: {{{{product_rules_total}}}}, {{{{validation_rules_count}}}}, {{{{selection_rules_count}}}}, {{{{alert_rules_count}}}}, {{{{price_rules_total}}}}.
Rule types present: {json.dumps(facts.get('product_rules', {}).get('by_type', {}))}
Return JSON: {{"paragraphs": ["..."]}}"""

    raw_rules = client.generate(SYNTHESIS_SYSTEM_PROMPT, rules_user_prompt, max_tokens=1000, model=model)
    try:
        r_json = json.loads(clean_llm_json(raw_rules))
        rule_paragraphs = [
            substitute_placeholders(p, placeholder_map) for p in r_json.get("paragraphs", [])
        ]
    except Exception:
        rule_paragraphs = [
            substitute_placeholders(
                "Product rules and price rules automate quotation governance, ensuring quotes conform to technical prerequisites "
                "and corporate sales policies without requiring manual engineering oversight.",
                placeholder_map
            ),
            substitute_placeholders(
                "Your setup contains {{product_rules_total}} product rules and {{price_rules_total}} price rules. "
                "Product rules automatically validate selections and add mandatory prerequisites, while price rules dynamically "
                "calculate list prices and enforce approved package discounting.",
                placeholder_map
            )
        ]

    # 4. Synthesize Pricing Section Narrative
    step("Synthesizing Pricing narrative...")
    pricing_user_prompt = f"""Write an overview of Pricing Models in the catalog (2 paragraphs).
Explain that pricing follows standard list pricing alongside specialized models:
- Block Pricing: {{{{total_block_prices}}}} block price tier(s) for tiered pricing
- Multi-dimensional pricing: {{{{total_dimensions}}}} recurring service dimension(s)
- Pricebooks: {{{{total_pricebook_entries}}}} active pricebook entry items across {{{{total_pricebooks}}}} pricebook(s)
Return JSON: {{"paragraphs": ["..."]}}"""

    raw_pricing = client.generate(SYNTHESIS_SYSTEM_PROMPT, pricing_user_prompt, max_tokens=1000, model=model)
    try:
        p_json = json.loads(clean_llm_json(raw_pricing))
        pricing_paragraphs = [
            substitute_placeholders(p, placeholder_map) for p in p_json.get("paragraphs", [])
        ]
    except Exception:
        pricing_paragraphs = [
            substitute_placeholders(
                "The commercial catalog establishes transparent pricing logic across all products. Standard pricing is driven by "
                "{{total_pricebook_entries}} price book entries across {{total_pricebooks}} active price book(s).",
                placeholder_map
            ),
            substitute_placeholders(
                "To support modern commercial models, specialized pricing structures are active: {{total_block_prices}} block price "
                "schedule(s) for quantity-bracket pricing, and {{total_dimensions}} multi-dimensional recurring dimension(s) for service agreements.",
                placeholder_map
            )
        ]

    sections = {
        "executive_summary": exec_paragraphs,
        "bundles_introduction": bundle_paragraphs,
        "rules_introduction": rule_paragraphs,
        "pricing_introduction": pricing_paragraphs,
        "bundle_analyses": bundle_analyses,
        "product_rule_analyses": product_rule_analyses,
        "price_rule_analyses": price_rule_analyses,
        "rule_analyses": rule_analyses,
        "standalone_analyses": standalone_analyses,
        "observations": facts.get("observations", []),
    }

    with open(sections_file, "w", encoding="utf-8") as f:
        json.dump(sections, f, indent=2)

    step("Sections synthesis complete.")
    return sections

