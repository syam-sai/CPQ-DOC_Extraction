# doc_gen/synthesize.py
#
# Synthesizes section summaries and executive summary from packet analyses and facts.
# Enforces placeholder substitution (e.g. {{active_products}}) so code drives all metrics.

import os
import json
import re
from doc_gen.llm_client import LLMClient, get_llm_client, DEFAULT_MODEL_SYNTHESIS
from doc_gen.analyze import clean_llm_json

INCLUDE_PERSONAL_DATA = os.environ.get(
    "LLM_INCLUDE_PERSONAL_DATA", "false"
).lower() in {"1", "true", "yes"}
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def _sanitize_prompt_value(value):
    if INCLUDE_PERSONAL_DATA:
        return value
    if isinstance(value, str):
        return EMAIL_RE.sub("[email redacted]", value)
    if isinstance(value, list):
        return [_sanitize_prompt_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_prompt_value(item) for key, item in value.items()}
    return value


SYNTHESIS_SYSTEM_PROMPT = """Write for business readers in plain, active English.
Use ONLY facts supplied in the prompt. Do not invent intent, behavior, tiers, percentages,
or product capabilities. If a fact is missing, say "Not available in extracted data".
Do not use generic filler, unsupported business claims, or technical API names. Explain
abbreviations on first use. Use only the metric placeholders explicitly listed by the user
prompt; never write a metric number directly. Keep each paragraph focused on one idea.
Return only valid JSON with a "paragraphs" array of strings."""


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


def _parse_paragraph_response(raw_response, placeholder_map):
    parsed = json.loads(clean_llm_json(raw_response))
    if not isinstance(parsed, dict) or set(parsed) != {"paragraphs"}:
        raise ValueError("Response must be an object containing only 'paragraphs'.")
    paragraphs = parsed["paragraphs"]
    if (
        not isinstance(paragraphs, list)
        or not paragraphs
        or any(not isinstance(paragraph, str) or not paragraph.strip() for paragraph in paragraphs)
    ):
        raise ValueError("'paragraphs' must be a non-empty array of non-empty strings.")
    resolved = [
        substitute_placeholders(paragraph, placeholder_map)
        for paragraph in paragraphs
    ]
    unresolved = [
        paragraph for paragraph in resolved
        if re.search(r"\{\{[^{}]+\}\}", paragraph)
    ]
    if unresolved:
        raise ValueError("Response contains an unknown metric placeholder.")
    return resolved


def _generate_paragraphs(client, user_prompt, placeholder_map, model, section, max_tokens):
    response = client.generate(
        SYNTHESIS_SYSTEM_PROMPT,
        user_prompt,
        max_tokens=max_tokens,
        model=model,
    )
    for attempt in range(2):
        try:
            return _parse_paragraph_response(response, placeholder_map)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            if attempt == 1:
                raise ValueError(
                    f"{section} synthesis returned invalid structured JSON after one retry: {exc}"
                ) from exc
            response = client.generate(
                SYNTHESIS_SYSTEM_PROMPT,
                (
                    f"{user_prompt}\n\nYour previous response failed schema validation: "
                    f"{exc}. Return only an object with a non-empty 'paragraphs' "
                    "array of non-empty strings, and use only listed placeholders."
                ),
                max_tokens=max_tokens,
                model=model,
            )
    raise RuntimeError(f"{section} synthesis produced no response.")


def synthesize_all(facts: dict, analyses_dict: dict, run_dir: str,
                   client: LLMClient = None, model: str = DEFAULT_MODEL_SYNTHESIS,
                   progress_callback=None, packets_result: dict = None) -> dict:
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
- Observations: {json.dumps(_sanitize_prompt_value([obs.get('description') for obs in facts.get('observations', [])]))}
- Data quality findings: {json.dumps(_sanitize_prompt_value(facts.get('data_quality', [])))}

Return JSON: {{"paragraphs": ["paragraph 1", "paragraph 2", "paragraph 3"]}}"""

    exec_paragraphs = _generate_paragraphs(
        client, exec_user_prompt, placeholder_map, model,
        "Executive summary", max_tokens=1500,
    )

    # 2. Synthesize Bundling & Configuration Section Narrative
    step("Synthesizing Bundles narrative...")
    bundles_user_prompt = f"""Write an introduction to the Bundles and Configuration section (2-3 paragraphs).
Explain how products are packaged into configurable bundles versus sold standalone.
Placeholders available: {{{{total_bundles}}}}, {{{{standalone_products}}}}, {{{{total_features}}}}, {{{{total_options}}}}.
Sample bundle structures analyzed: {[b.get('name') for b in bundle_analyses[:8]]}
Return JSON: {{"paragraphs": ["..."]}}"""

    bundle_paragraphs = _generate_paragraphs(
        client, bundles_user_prompt, placeholder_map, model,
        "Bundle overview", max_tokens=1000,
    )

    # 3. Synthesize Business Rules Section Narrative
    step("Synthesizing Rules narrative...")
    rules_user_prompt = f"""Write an introduction to the Product Rules and Price Rules section (2 paragraphs).
Describe only the rule types represented in the supplied counts. Do not infer business intent.
Placeholders available: {{{{product_rules_total}}}}, {{{{validation_rules_count}}}}, {{{{selection_rules_count}}}}, {{{{alert_rules_count}}}}, {{{{price_rules_total}}}}.
Rule types present: {json.dumps(facts.get('product_rules', {}).get('by_type', {}))}
Return JSON: {{"paragraphs": ["..."]}}"""

    rule_paragraphs = _generate_paragraphs(
        client, rules_user_prompt, placeholder_map, model,
        "Rule overview", max_tokens=1000,
    )

    # 4. Synthesize Pricing Section Narrative
    step("Synthesizing Pricing narrative...")
    pricing_user_prompt = f"""Write an overview of Pricing Models in the catalog (2 paragraphs).
Report only extracted pricing records:
- Block Pricing: {{{{total_block_prices}}}} block price tier(s) for tiered pricing
- Multi-dimensional pricing: {{{{total_dimensions}}}} recurring service dimension(s)
- Pricebooks: {{{{total_pricebook_entries}}}} active pricebook entry items across {{{{total_pricebooks}}}} pricebook(s)
Return JSON: {{"paragraphs": ["..."]}}"""

    pricing_paragraphs = _generate_paragraphs(
        client, pricing_user_prompt, placeholder_map, model,
        "Pricing overview", max_tokens=1000,
    )

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
        "data_quality": facts.get("data_quality", []),
        "rule_details": {
            "product_rules": (packets_result or {}).get("product_rule_packets", []),
            "price_rules": (packets_result or {}).get("price_rule_packets", []),
        },
        "bundle_details": (packets_result or {}).get("bundle_packets", []),
    }

    with open(sections_file, "w", encoding="utf-8") as f:
        json.dump(sections, f, indent=2)

    step("Sections synthesis complete.")
    return sections
