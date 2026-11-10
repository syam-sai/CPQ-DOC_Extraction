"""Deterministic data-quality checks for CPQ extracts."""

import re
from collections import Counter, defaultdict

from doc_gen.facts import is_active_product, load_extraction_data


SF_ID_RE = re.compile(
    r"^(?=[a-zA-Z0-9]*[a-zA-Z])(?=[a-zA-Z0-9]*\d)[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$"
)
PLACEHOLDERS = {"specified value", "formula or calculated value", "value"}
OPPOSITE_ACTIONS = {
    "add": "remove",
    "remove": "add",
    "enable": "disable",
    "disable": "enable",
    "show": "hide",
    "hide": "show",
}


def _option_rows(product):
    rows = list(product.get("direct_options", []))
    for feature in product.get("features", []):
        rows.extend(feature.get("options", []))
    return rows


def _finding(severity, entity, message, rule=None, product=None):
    return {
        "severity": severity,
        "entity": entity,
        "rule": rule,
        "product": product,
        "message": message,
    }


def validate_extraction(data_or_run_dir, facts=None):
    """Returns stable findings derived only from extracted records."""
    data = load_extraction_data(data_or_run_dir)
    all_products = data.get("Product", [])
    products = [product for product in all_products if is_active_product(product)]
    product_rules = data.get("ProductRules", [])
    price_rules = data.get("PriceRules", [])
    findings = []

    products_by_id = {
        p.get("Id"): p for p in products if p.get("Id")
    }
    product_label_by_id = {
        p.get("Id"): (
            f"{p.get('ProductCode')} - {p.get('Name')}"
            if p.get("ProductCode") and p.get("Name")
            else p.get("Name") or p.get("ProductCode") or "Unresolved product"
        )
        for p in all_products if p.get("Id")
    }
    bundle_products = [
        p for p in products if _option_rows(p)
    ]
    bundle_labels = {
        p.get("Id"): product_label_by_id.get(p.get("Id"), "Unresolved bundle")
        for p in bundle_products
    }

    option_rows = [
        (p, option)
        for p in products
        for option in _option_rows(p)
    ]
    component_ids = {
        option.get("SBQQ__OptionalSKU__c")
        for _, option in option_rows
        if option.get("SBQQ__OptionalSKU__c")
    }
    expected_facts = {
        "active_products": len(products),
        "total_bundles": len(bundle_products),
        "total_options": len(option_rows),
        "distinct_option_products": len(component_ids),
    }
    for key, expected in expected_facts.items():
        if facts is not None and facts.get(key) != expected:
            findings.append(_finding(
                "ERROR", "counts",
                f"The {key.replace('_', ' ')} summary count does not match the extracted records.",
            ))

    rule_by_bundle = defaultdict(list)
    for rule in product_rules:
        rule_name = rule.get("Name") or "Unnamed product rule"
        actions = rule.get("product_actions", [])
        linked_bundle_ids = {
            config_rule.get("SBQQ__Product__c")
            for config_rule in rule.get("configuration_rules", [])
            if config_rule.get("SBQQ__Product__c")
        }
        for bundle_id in linked_bundle_ids:
            rule_by_bundle[bundle_id].append(rule_name)

        by_product_action = defaultdict(set)
        for action in actions:
            product_id = action.get("SBQQ__Product__c")
            action_type = str(action.get("SBQQ__Type__c") or "").strip().lower()
            if product_id and action_type:
                by_product_action[product_id].add(action_type)
        for product_id, action_types in by_product_action.items():
            if any(OPPOSITE_ACTIONS.get(kind) in action_types for kind in action_types):
                findings.append(_finding(
                    "ERROR", "product_rule",
                    "The rule applies opposite actions to the same product.",
                    rule=rule_name,
                    product=product_label_by_id.get(product_id, "Unresolved product"),
                ))

        for condition in rule.get("error_conditions", []):
            values = (
                condition.get("SBQQ__TestedValue__c"),
                condition.get("SBQQ__FilterValue__c"),
            )
            for value in values:
                if isinstance(value, str) and value.strip().lower() in PLACEHOLDERS:
                    findings.append(_finding(
                        "ERROR", "product_rule",
                        "A condition contains a placeholder instead of a resolved comparison value.",
                        rule=rule_name,
                    ))
                if isinstance(value, str) and SF_ID_RE.fullmatch(value.strip()):
                    findings.append(_finding(
                        "ERROR", "product_rule",
                        "A condition comparison value is an unresolved Salesforce record reference.",
                        rule=rule_name,
                    ))

            message = str(rule.get("SBQQ__ErrorMessage__c") or "").lower()
            operator = str(condition.get("SBQQ__Operator__c") or "").lower()
            condition_value = condition.get("SBQQ__TestedValue__c")
            if condition_value in (None, ""):
                condition_value = condition.get("SBQQ__FilterValue__c")
            value = str(condition_value if condition_value is not None else "").strip()
            if (
                value
                and ("less or equal" in operator or "lessorequal" in operator)
                and f"less than {value}".lower() in message
                and "equal" not in message
            ):
                findings.append(_finding(
                    "WARNING", "product_rule",
                    "The message says 'less than' while the condition includes equality.",
                    rule=rule_name,
                ))

        if not linked_bundle_ids and not any(
            action.get("SBQQ__Product__c") in products_by_id for action in actions
        ):
            findings.append(_finding(
                "WARNING", "product_rule",
                "The rule has no related active product or bundle in the extracted data.",
                rule=rule_name,
            ))

        normalized_rule_name = re.sub(r"\s+", " ", rule_name).strip().lower()
        if normalized_rule_name.startswith("validation on ") and linked_bundle_ids:
            bundle_names = [
                bundle_labels.get(bundle_id, "")
                for bundle_id in linked_bundle_ids
            ]
            if not any(name.lower() in normalized_rule_name for name in bundle_names):
                findings.append(_finding(
                    "WARNING", "product_rule",
                    "The rule name does not identify the bundle it is attached to.",
                    rule=rule_name,
                ))

    for bundle in bundle_products:
        bundle_id = bundle.get("Id")
        if not rule_by_bundle.get(bundle_id) and not bundle.get("configuration_rules"):
            findings.append(_finding(
                "WARNING", "bundle",
                "The bundle has no linked product rules in the extracted data.",
                product=bundle_labels.get(bundle_id),
            ))

    for rule in price_rules:
        rule_name = rule.get("Name") or "Unnamed price rule"
        product_id = rule.get("SBQQ__Product__c")
        if product_id:
            if product_id not in products_by_id:
                findings.append(_finding(
                    "WARNING", "price_rule",
                    "The rule references an inactive or missing product.",
                    rule=rule_name,
                ))

        for action in rule.get("price_actions", []):
            value = action.get("SBQQ__Value__c")
            formula = action.get("SBQQ__Formula__c")
            source = action.get("SBQQ__SourceVariable__c")
            if all(item in (None, "") for item in (value, formula, source)):
                findings.append(_finding(
                    "ERROR", "price_rule",
                    "A price action has no extracted value, formula, or source variable.",
                    rule=rule_name,
                ))
            if isinstance(value, str) and value.strip().lower() in PLACEHOLDERS:
                findings.append(_finding(
                    "ERROR", "price_rule",
                    "A price action contains a placeholder instead of a resolved value.",
                    rule=rule_name,
                ))

    for product in products:
        product_id = product.get("Id")
        product_name = product_label_by_id.get(product_id)
        if not product.get("pricebook_entries"):
            findings.append(_finding(
                "WARNING", "product",
                "The active product has no extracted pricebook entry.",
                product=product_name,
            ))

    for bundle, option in option_rows:
        target_id = option.get("SBQQ__OptionalSKU__c")
        if target_id and target_id not in products_by_id:
            findings.append(_finding(
                "WARNING", "product_option",
                "The option references an inactive or missing product.",
                product=product_label_by_id.get(bundle.get("Id"), "Unresolved bundle"),
            ))

        searchable_name = " ".join(
            str(product.get(key) or "")
            for key in ("Name", "ProductCode", "Family", "SBQQ__SubscriptionType__c")
        ).lower()
        is_recurring_candidate = (
            "subscription" in searchable_name
            or "usage" in searchable_name
            or bool(product.get("SBQQ__SubscriptionType__c"))
        )
        pricing = product.get("pricing", {})
        if (
            is_recurring_candidate
            and not pricing.get("dimensions")
            and not pricing.get("block_prices")
            and not product.get("SBQQ__SubscriptionPricing__c")
        ):
            findings.append(_finding(
                "WARNING", "product",
                "The subscription or usage product has no extracted recurring or block pricing configuration.",
                product=product_name,
            ))

    # Flag different rule behavior for component products used by multiple bundles.
    bundle_actions = defaultdict(lambda: defaultdict(set))
    for rule in product_rules:
        rule_name = rule.get("Name") or "Unnamed product rule"
        linked_bundle_ids = {
            config_rule.get("SBQQ__Product__c")
            for config_rule in rule.get("configuration_rules", [])
            if config_rule.get("SBQQ__Product__c") in bundle_labels
        }
        for action in rule.get("product_actions", []):
            target_id = action.get("SBQQ__Product__c")
            action_type = str(action.get("SBQQ__Type__c") or "").strip().lower()
            if target_id and action_type:
                for bundle_id in linked_bundle_ids:
                    bundle_actions[bundle_id][target_id].add((action_type, rule_name))

    bundles_by_component = defaultdict(set)
    for bundle, option in option_rows:
        target_id = option.get("SBQQ__OptionalSKU__c")
        if target_id:
            bundles_by_component[target_id].add(bundle.get("Id"))
    for component_id, bundle_ids in bundles_by_component.items():
        if len(bundle_ids) < 2:
            continue
        automatic_add_bundles = {
            bundle_id for bundle_id in bundle_ids
            if any(
                action_type == "add"
                for action_type, _ in bundle_actions[bundle_id].get(component_id, set())
            )
        }
        if automatic_add_bundles and automatic_add_bundles != bundle_ids:
            findings.append(_finding(
                "INFO", "bundle_behavior",
                "The shared component is automatically added for some bundles but not others; confirm the difference is intentional.",
                product=product_label_by_id.get(component_id, "Unresolved component"),
            ))

    duplicate_names = Counter(
        rule.get("Name") or "Unnamed rule"
        for rule in product_rules + price_rules
    )
    for name, count in sorted(duplicate_names.items()):
        if count < 2:
            continue
        findings.append(_finding(
            "WARNING", "rule_name",
            "Multiple rules have the same display name.",
            rule=name,
        ))

    return findings
