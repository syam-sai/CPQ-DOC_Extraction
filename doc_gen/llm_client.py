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
        try:
            user_data = json.loads(user)
        except Exception:
            user_data = {}

        packet_type = user_data.get("packet_type")

        # 1. Product Rule Analysis
        if packet_type == "product_rule":
            name = user_data.get("rule_name", "Product Rule")
            rtype = user_data.get("rule_type", "Validation").lower()
            kind_map = {
                "validation": "validation",
                "selection": "selection",
                "alert": "alert",
                "filter": "filter",
            }
            kind = kind_map.get(rtype, "validation")
            err_msg = user_data.get("error_message_shown_to_user")

            related_items = []
            for b in user_data.get("applies_to_bundles", []):
                related_items.append(b)
            for a in user_data.get("actions", []):
                t_prod = a.get("target_product_name")
                if t_prod and t_prod not in related_items:
                    related_items.append(t_prod)
            for sv in user_data.get("referenced_summary_variables", []):
                if sv not in related_items:
                    related_items.append(sv)

            cond_count = len(user_data.get("conditions", []))
            complexity = "low" if cond_count <= 1 else ("medium" if cond_count <= 3 else "high")

            resp = {
                "name": name,
                "kind": kind,
                "one_line_summary": f"Business {kind} rule enforcing configuration standards during quote creation.",
                "when_it_applies": f"Applies when a sales representative configures {' or '.join(user_data.get('applies_to_bundles', ['products']))}.",
                "what_it_does": f"Evaluates {cond_count} criteria and {('displays an alert message' if kind in ('validation', 'alert') else 'automatically updates selectable options')}.",
                "message_shown_to_user": err_msg,
                "related_items": related_items,
                "inferred_purpose": f"Appears to ensure quote configurations remain technically feasible and compliant with standard sales packages.",
                "complexity": complexity,
                "complexity_reason": f"Evaluates {cond_count} condition(s) with {len(user_data.get('actions', []))} automated action(s).",
                "questions_to_confirm": [
                    f"Should this rule apply globally or remain restricted to {', '.join(user_data.get('applies_to_bundles', ['selected bundles']))}?",
                    "Are there exception scenarios where sales representatives should be allowed to bypass this constraint?"
                ],
                "data_gaps": []
            }
            return json.dumps(resp, indent=2)

        # 2. Price Rule Analysis
        elif packet_type == "price_rule":
            name = user_data.get("rule_name", "Price Rule")
            resp = {
                "name": name,
                "kind": "price",
                "one_line_summary": "Automated pricing rule that calculates dynamic discounts or overrides standard pricing.",
                "when_it_applies": f"Triggered during the quote calculation cycle ({user_data.get('evaluation_event', 'On Calculate')}).",
                "what_it_does": "Calculates pricing adjustments across targeted quote line items based on configured criteria.",
                "message_shown_to_user": None,
                "related_items": [user_data.get("scoped_to_product")] if user_data.get("scoped_to_product") else [],
                "inferred_purpose": "Appears to automate pricing rules to ensure approved pricing models are applied consistently.",
                "complexity": "low" if len(user_data.get("conditions", [])) <= 1 else "medium",
                "complexity_reason": "Automated pricing calculation rule.",
                "questions_to_confirm": [
                    "Is this pricing logic intended to operate automatically or should sales reps have override authority?"
                ],
                "data_gaps": [
                    "No explicit conditions configured; relies on system calculation events."
                ] if not user_data.get("conditions") else []
            }
            return json.dumps(resp, indent=2)

        # 3. Bundle Analysis
        elif packet_type == "bundle":
            name = user_data.get("bundle_name", "Product Bundle")
            features = user_data.get("features", [])
            choices = []
            related_items = []

            for f in features:
                f_name = f.get("feature_name", "Options")
                opt_names = [o.get("component_name") for o in f.get("options", []) if o.get("component_name")]
                choices.append({
                    "feature": f_name,
                    "what_customer_chooses": f"Selects components for {f_name.lower()}.",
                    "option_names": opt_names
                })
                related_items.extend(opt_names)

            ungrouped = [o.get("component_name") for o in user_data.get("ungrouped_options", []) if o.get("component_name")]
            related_items.extend(ungrouped)

            total_opts = sum(len(c["option_names"]) for c in choices) + len(ungrouped)
            complexity = "low" if total_opts <= 4 else ("medium" if total_opts <= 10 else "high")

            resp = {
                "name": name,
                "one_line_summary": f"Configurable {user_data.get('family', 'catalog')} product bundle offering {total_opts} customizable components.",
                "how_it_is_sold": f"Sold as a base product with {len(features)} customizable feature section(s).",
                "choices_offered": choices,
                "ungrouped_options": ungrouped,
                "rules_that_affect_it": user_data.get("affecting_rules", []),
                "pricing_notes": "Standard package pricing applies based on the selected component quantities.",
                "complexity": complexity,
                "complexity_reason": f"Contains {len(features)} feature section(s) with {total_opts} selectable option(s).",
                "questions_to_confirm": [
                    f"Are all {total_opts} component options actively stocked and sold today?",
                    "Should default quantities be pre-selected to accelerate the quotation flow?"
                ],
                "data_gaps": []
            }
            return json.dumps(resp, indent=2)

        # 4. Standalone Products Summary
        elif packet_type == "standalone_summary":
            fam = user_data.get("family", "Catalog")
            count = user_data.get("product_count", 0)
            resp = {
                "family": fam,
                "summary": f"The {fam} family contains {count} standalone offerings sold directly without component configuration.",
                "typical_use": "Direct quotation and standard order placement.",
                "governance_note": "Requires active price book entries for quotation."
            }
            return json.dumps(resp, indent=2)

        # 5. Synthesis Call (Executive Summary or Section Summary)
        else:
            # Check if this is the Executive Summary synthesis
            if "Executive Summary" in user or "executive" in user.lower():
                resp = {
                    "paragraphs": [
                        "This document provides an executive summary of your Salesforce CPQ configuration. The catalog comprises {{active_products}} active products, designed to streamline how your sales representatives quote complex products and services.",
                        "Within the catalog, {{total_bundles}} products are structured as configurable bundles containing {{total_features}} feature categories and {{total_options}} selectable components. An additional {{standalone_products}} products are offered as standalone items.",
                        "Configuration integrity is protected by {{product_rules_total}} automated product rules and {{price_rules_total}} price rules. These rules prevent incompatible component selections and ensure pricing accuracy across every quote."
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

