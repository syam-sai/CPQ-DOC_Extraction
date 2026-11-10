# doc_gen/llm_client.py
#
# Unified LLM client supporting mock, anthropic, gemini, and bedrock providers.
# Features exponential backoff, rate limit handling, and strict max concurrency.

import os
import time
import json
import random
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

DEFAULT_LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "mock").lower()
DEFAULT_LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "4"))
DEFAULT_MODEL_ANALYSIS = os.environ.get("LLM_MODEL_ANALYSIS", "gemini-2.5-flash")
DEFAULT_MODEL_SYNTHESIS = os.environ.get("LLM_MODEL_SYNTHESIS", "gemini-2.5-pro")
# Gemini 3 enables thinking by default, and thinking tokens count against
# max_tokens. Left unbudgeted, responses get truncated mid-JSON. 0 disables it;
# set -1 to omit the field entirely and keep the provider default.
DEFAULT_THINKING_BUDGET = int(os.environ.get("LLM_THINKING_BUDGET", "0"))


class LLMClient(ABC):
    """Abstract interface for LLM completion."""

    @abstractmethod
    def generate(self, system: str, user: str, max_tokens: int = 2500, model: str = None) -> str:
        pass


class MockLLMClient(LLMClient):
    """
    Offline deterministic LLM provider for CI, testing, and dry-runs.
    Generates realistic, schema-compliant JSON outputs without network calls.
    """

    def generate(self, system: str, user: str, max_tokens: int = 2500, model: str = None) -> str:
        # Check if this is a packet analysis call or a synthesis call
        user_data = {}
        try:
            user_data = json.loads(user)
        except Exception:
            # Extract JSON packet if wrapped in prompt template
            idx_start = user.find("PACKET:\n")
            if idx_start != -1:
                idx_start += len("PACKET:\n")
                idx_end = user.find("\n\nReturn JSON", idx_start)
                json_str = user[idx_start:idx_end].strip() if idx_end != -1 else user[idx_start:].strip()
                try:
                    user_data = json.loads(json_str)
                except Exception:
                    pass

        packet_type = user_data.get("packet_type")

        # 1. Product Rule Analysis
        if packet_type == "product_rule":
            name = user_data.get("rule_name", "Product Rule")
            rtype = str(user_data.get("rule_type", "Validation")).lower()
            kind_map = {
                "validation": "validation",
                "selection": "selection",
                "alert": "alert",
                "filter": "filter",
            }
            kind = kind_map.get(rtype, "validation")
            err_msg = user_data.get("error_message_shown_to_user")
            bundles = user_data.get("applies_to_bundles", [])
            conditions = user_data.get("conditions", [])
            actions = user_data.get("actions", [])

            related_items = list(bundles)
            for a in actions:
                t_prod = a.get("target_product_name")
                if t_prod and t_prod not in related_items:
                    related_items.append(t_prod)
            for sv in user_data.get("referenced_summary_variables", []):
                if sv not in related_items:
                    related_items.append(sv)

            # Build human-readable conditions description
            cond_clauses = []
            for c in conditions:
                target = c.get("tested_target", "").replace("__c", "").replace("_", " ")
                op = c.get("comparison_operator", "equals")
                val = c.get("target_value", "")
                cond_clauses.append(f"{target} {op} '{val}'")

            if cond_clauses:
                when_it_applies = f"Applies when {' and '.join(cond_clauses)}"
                if bundles:
                    when_it_applies += f" while configuring {', '.join(bundles)}."
                else:
                    when_it_applies += "."
            elif bundles:
                when_it_applies = f"Applies when sales representatives configure {', '.join(bundles)}."
            else:
                when_it_applies = f"Evaluates during the quote configuration event ({user_data.get('evaluation_event', 'Always')})."

            # Build human-readable action description
            if kind == "validation":
                what_it_does = f"Blocks quote progression and displays message: '{err_msg}'" if err_msg else "Validates inputs and prevents quote save if configuration rules are violated."
                one_line_summary = f"Prevents invalid configuration selections on {', '.join(bundles) if bundles else name}."
            elif kind == "selection":
                act_descriptions = []
                for a in actions:
                    act_t = a.get("action_type", "Update")
                    prod_n = a.get("target_product_name", "component")
                    mand = " as mandatory" if a.get("required") else ""
                    act_descriptions.append(f"{act_t.lower()}s '{prod_n}'{mand}")
                what_it_does = f"Automatically {', and '.join(act_descriptions)} based on option selections." if act_descriptions else "Automatically updates selectable component options in the bundle."
                one_line_summary = f"Automated component selection rule for {', '.join(bundles) if bundles else name}."
            elif kind == "alert":
                what_it_does = f"Displays alert warning to sales representatives: '{err_msg}'" if err_msg else "Displays configuration alerts to guide reps."
                one_line_summary = f"Provides guided sales alerts during {', '.join(bundles) if bundles else name} configuration."
            else:
                what_it_does = "Filters available bundle options to match customer criteria."
                one_line_summary = f"Filters catalog options for {', '.join(bundles) if bundles else name}."

            cond_count = len(conditions)
            complexity = "low" if cond_count <= 1 else ("medium" if cond_count <= 3 else "high")

            resp = {
                "name": name,
                "kind": kind,
                "one_line_summary": one_line_summary,
                "when_it_applies": when_it_applies,
                "what_it_does": what_it_does,
                "message_shown_to_user": err_msg,
                "related_items": related_items,
                "inferred_purpose": None,
                "complexity": complexity,
                "complexity_reason": f"Evaluates {cond_count} condition(s) with {len(actions)} automated action(s).",
                "questions_to_confirm": [],
                "data_gaps": []
            }
            return json.dumps(resp, indent=2)

        # 2. Price Rule Analysis
        elif packet_type == "price_rule":
            name = user_data.get("rule_name", "Price Rule")
            conditions = user_data.get("conditions", [])
            actions = user_data.get("actions", [])

            cond_clauses = [f"{str(c.get('field', '')).replace('SBQQ__', '').replace('__c', '').replace('_', ' ')} {c.get('operator')} '{c.get('value')}'" for c in conditions if c.get("field")]
            if cond_clauses:
                when_it_applies = f"Evaluated when {', '.join(cond_clauses)} during the quote calculation cycle ({user_data.get('evaluation_event', 'On Calculate')})."
            else:
                when_it_applies = f"Triggered automatically during the quote calculation cycle ({user_data.get('evaluation_event', 'On Calculate')})."

            act_clauses = []
            for a in actions:
                f_name = str(a.get("target_field", "pricing")).replace("SBQQ__", "").replace("__c", "").replace("_", " ")
                t_obj = a.get("target_object", "Quote Line")
                act_clauses.append(f"calculates {f_name} on {t_obj}")

            summary = (
                f"The extracted price rule has {len(conditions)} condition(s) "
                f"and {len(actions)} action(s)."
            )
            what_it_does = (
                "; ".join(act_clauses)
                if act_clauses else "Not available in extracted data."
            )

            related_items = [str(c.get("value")) for c in conditions if c.get("field") == "Product Code" and c.get("value")]
            if user_data.get("scoped_to_product"):
                related_items.append(user_data.get("scoped_to_product"))

            resp = {
                "name": name,
                "kind": "price",
                "one_line_summary": summary,
                "when_it_applies": when_it_applies,
                "what_it_does": what_it_does,
                "message_shown_to_user": None,
                "related_items": list(set(related_items)),
                "inferred_purpose": None,
                "complexity": "low" if len(conditions) <= 1 else "medium",
                "complexity_reason": f"Evaluates {len(conditions)} condition(s) and executes {len(actions)} pricing action(s).",
                "questions_to_confirm": [],
                "data_gaps": []
            }
            return json.dumps(resp, indent=2)

        # 3. Bundle Analysis
        elif packet_type == "bundle":
            name = user_data.get("bundle_name", "Product Bundle")
            features = user_data.get("features", [])
            choices = []
            related_items = []

            for f in features:
                f_name = f.get("feature_name", "Components")
                opt_names = [o.get("component_name") for o in f.get("options", []) if o.get("component_name")]
                choices.append({
                    "feature": f_name,
                    "what_customer_chooses": f"Selects components and options for {f_name}.",
                    "option_names": opt_names
                })
                related_items.extend(opt_names)

            ungrouped = [o.get("component_name") for o in user_data.get("ungrouped_options", []) if o.get("component_name")]
            related_items.extend(ungrouped)

            total_opts = sum(len(c["option_names"]) for c in choices) + len(ungrouped)
            complexity = "low" if total_opts <= 4 else ("medium" if total_opts <= 10 else "high")
            sku = user_data.get("sku", "")

            resp = {
                "name": name,
                "one_line_summary": f"The extracted bundle has {len(features)} feature section(s) and {total_opts} option record(s).",
                "how_it_is_sold": f"The extract records this product as a bundle with {total_opts} option record(s).",
                "choices_offered": choices,
                "ungrouped_options": ungrouped,
                "rules_that_affect_it": [r.replace("Product Rule: ", "").replace("Price Rule: ", "") for r in user_data.get("affecting_rules", [])],
                "pricing_notes": "Not available in extracted data.",
                "complexity": complexity,
                "complexity_reason": f"Contains {len(features)} feature section(s) with {total_opts} selectable option(s).",
                "questions_to_confirm": [],
                "data_gaps": []
            }
            return json.dumps(resp, indent=2)

        # 4. Standalone Products Summary
        elif packet_type == "standalone_summary":
            fam = user_data.get("family", "Catalog")
            count = user_data.get("product_count", 0)
            resp = {
                "family": fam,
                "summary": f"The extract groups {count} products in the {fam} family.",
                "typical_use": "Not available in extracted data.",
                "governance_note": "Not available in extracted data."
            }
            return json.dumps(resp, indent=2)

        # 5. Synthesis Call (Executive Summary or Section Summary)
        else:
            u_low = user.lower()
            if "executive summary" in u_low or "executive" in u_low:
                resp = {
                    "paragraphs": [
                    "This document summarizes the extracted Salesforce CPQ catalog containing {{active_products}} active products and {{total_bundles}} configurable bundles.",
                    "The extract records {{total_options}} option records across {{total_features}} feature records, plus {{product_rules_total}} product rules and {{price_rules_total}} price rules.",
                    "The deterministic checks recorded {{observations_count}} observations for review."
                    ]
                }
                return json.dumps(resp, indent=2)
            elif "bundle" in u_low:
                resp = {
                    "paragraphs": [
                        "The extract contains {{total_bundles}} configurable bundles and {{standalone_products}} products that are neither bundles nor bundle options.",
                        "Those records contain {{total_features}} feature sections and {{total_options}} option records."
                    ]
                }
                return json.dumps(resp, indent=2)
            elif "rule" in u_low:
                resp = {
                    "paragraphs": [
                        "The extract contains {{product_rules_total}} product rules and {{price_rules_total}} price rules.",
                        "Each rule's extracted conditions and effects are listed in the rule details."
                    ]
                }
                return json.dumps(resp, indent=2)
            elif "pricing" in u_low or "price" in u_low:
                resp = {
                    "paragraphs": [
                        "The extract contains {{total_pricebook_entries}} pricebook entries across {{total_pricebooks}} pricebooks.",
                        "It also contains {{total_block_prices}} block-price records and {{total_dimensions}} pricing-dimension records."
                    ]
                }
                return json.dumps(resp, indent=2)
            else:
                resp = {
                    "paragraphs": [
                        "The configuration setup establishes consistent guidelines for product selection and quotation.",
                        "Sales representatives are guided through structured feature choices, ensuring all quotes align with corporate pricing and packaging standards."
                    ]
                }
                return json.dumps(resp, indent=2)


class AnthropicClient(LLMClient):
    """Anthropic Claude API client."""

    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY or LLM_API_KEY must be set to use Anthropic.")

    def generate(self, system: str, user: str, max_tokens: int = 2500, model: str = None) -> str:
        import requests
        model_name = model or DEFAULT_MODEL_ANALYSIS
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model_name,
            "system": system,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "user", "content": user}
            ],
            "temperature": 0.1,
        }

        retries = 3
        backoff = 2
        for attempt in range(retries):
            try:
                resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("content", [])
                    if content and "text" in content[0]:
                        return content[0]["text"]
                    return ""
                elif resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(backoff + random.uniform(0.1, 1.0))
                    backoff *= 2
                else:
                    raise RuntimeError(f"Anthropic API error ({resp.status_code}): {resp.text}")
            except Exception as e:
                if attempt == retries - 1:
                    raise
                time.sleep(backoff)
                backoff *= 2


class GeminiClient(LLMClient):
    """Google Gemini API client using standard REST interface."""

    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("LLM_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY or LLM_API_KEY must be set to use Gemini.")

    def generate(self, system: str, user: str, max_tokens: int = 2500, model: str = None) -> str:
        import requests
        model_name = model or DEFAULT_MODEL_ANALYSIS
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}
        generation_config = {
            "temperature": 0.1,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        }
        thinking_budget = int(os.environ.get("LLM_THINKING_BUDGET", DEFAULT_THINKING_BUDGET))
        if thinking_budget >= 0:
            generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}
        payload = {
            "systemInstruction": {
                "parts": [{"text": system}]
            },
            "contents": [
                {"parts": [{"text": user}]}
            ],
            "generationConfig": generation_config
        }

        retries = 3
        backoff = 2
        for attempt in range(retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", "")
                    return ""
                elif resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(backoff + random.uniform(0.1, 1.0))
                    backoff *= 2
                else:
                    raise RuntimeError(f"Gemini API error ({resp.status_code}): {resp.text}")
            except Exception as e:
                if attempt == retries - 1:
                    raise
                time.sleep(backoff)
                backoff *= 2


class BedrockClient(LLMClient):
    """Stub for future AWS Bedrock integration."""

    def generate(self, system: str, user: str, max_tokens: int = 2500, model: str = None) -> str:
        raise NotImplementedError("Bedrock provider is reserved for AWS deployment (Phase 3). Use 'mock', 'gemini', or 'anthropic' for local execution.")


def get_llm_client(provider_name: str = None) -> LLMClient:
    """Factory function returning the configured LLM client."""
    provider = (provider_name or os.environ.get("LLM_PROVIDER") or "mock").lower()
    if provider == "mock":
        return MockLLMClient()
    elif provider == "gemini":
        return GeminiClient()
    elif provider == "anthropic":
        return AnthropicClient()
    elif provider == "bedrock":
        return BedrockClient()
    else:
        raise ValueError(f"Unknown LLM provider: '{provider}'. Available options: mock, gemini, anthropic, bedrock.")
