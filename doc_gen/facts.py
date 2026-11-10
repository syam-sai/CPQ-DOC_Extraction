# doc_gen/facts.py
#
# Computes deterministic facts and catalog observations from CPQ extraction.
# Rule: Code = facts. The LLM never counts or invents metrics.

import os
import json
from collections import Counter


def is_active_product(product):
    """Treats legacy extracts without IsActive as active, while parsing CSV strings."""
    value = product.get("IsActive")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"false", "0", "no"}


def load_extraction_data(data_or_run_dir):
    """Loads extraction data from a dict, a file path, or a run directory."""
    if isinstance(data_or_run_dir, dict):
        return data_or_run_dir

    if isinstance(data_or_run_dir, str):
        path = os.path.abspath(data_or_run_dir)
        if os.path.isdir(path):
            # Check output/cpq_extract.json
            cand_json = os.path.join(path, "output", "cpq_extract.json")
            if os.path.exists(cand_json):
                path = cand_json
            else:
                root_json = os.path.join(path, "cpq_extract.json")
                if os.path.exists(root_json):
                    path = root_json

        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                with open(path, "r", encoding="utf-16") as f:
                    return json.load(f)

    raise ValueError(f"Could not load CPQ extraction data from: {data_or_run_dir}")


def compute_facts(data_or_run_dir):
    """
    Computes deterministic metrics, breakdowns, and quality observations from CPQ extract.
    Returns a facts dictionary suitable for serialization to facts.json.
    """
    data = load_extraction_data(data_or_run_dir)

    all_products = data.get("Product", [])
    products = [product for product in all_products if is_active_product(product)]
    product_rules = data.get("ProductRules", [])
    price_rules = data.get("PriceRules", [])
    summary_variables = data.get("SummaryVariables", [])
    metadata = data.get("metadata", {})
    inactive_products_included_count = sum(
        not is_active_product(product) for product in all_products
    )
    inactive_products_count = metadata.get("inactive_products_count")
    if metadata.get("include_inactive_records"):
        inactive_products_count = inactive_products_included_count

    active_product_ids = {p.get("Id") for p in products if p.get("Id")}
    product_name_by_id = {
        p.get("Id"): (p.get("Name") or p.get("ProductCode") or p.get("Id"))
        for p in products if p.get("Id")
    }

    # 1. Product & Bundle Classification
    bundles = []
    bundles_with_features = []
    bundles_direct_only = []
    option_product_ids = set()

    features_all = []
    options_all = []
    pricebook_entries_all = []
    discount_schedules_all = []
    block_prices_all = []
    dimensions_all = []
    costs_all = []
    option_constraints_all = []
    config_attributes_all = []
    config_rules_all = []
    pricebooks_set = set()

    families_counter = Counter()

    for p in products:
        pid = p.get("Id")
        pname = p.get("Name") or p.get("ProductCode") or pid
        fam = p.get("Family") or "Unassigned"
        families_counter[fam] += 1

        pbes = p.get("pricebook_entries", [])
        pricebook_entries_all.extend(pbes)
        for pbe in pbes:
            pbid = pbe.get("Pricebook2Id")
            if pbid:
                pricebooks_set.add(pbid)

        pricing = p.get("pricing", {})
        discount_schedules_all.extend(pricing.get("discount_schedules", []))
        block_prices_all.extend(pricing.get("block_prices", []))
        dimensions_all.extend(pricing.get("dimensions", []))
        costs_all.extend(pricing.get("costs", []))

        option_constraints_all.extend(p.get("option_constraints", []))
        config_attributes_all.extend(p.get("configuration_attributes", []))
        config_rules_all.extend(p.get("configuration_rules", []))

        p_features = p.get("features", [])
        direct_opts = p.get("direct_options", [])

        features_all.extend(p_features)
        options_all.extend(direct_opts)
        for feat in p_features:
            options_all.extend(feat.get("options", []))
            option_product_ids.update(
                option.get("SBQQ__OptionalSKU__c")
                for option in feat.get("options", [])
                if option.get("SBQQ__OptionalSKU__c")
            )
        option_product_ids.update(
            option.get("SBQQ__OptionalSKU__c")
            for option in direct_opts
            if option.get("SBQQ__OptionalSKU__c")
        )

        total_opts_count = sum(len(f.get("options", [])) for f in p_features) + len(direct_opts)

        if total_opts_count > 0:
            bundles.append(p)
            if p_features:
                bundles_with_features.append(p)
            else:
                bundles_direct_only.append(p)

    bundle_ids = {p.get("Id") for p in bundles if p.get("Id")}
    standalone_products = [
        p for p in products
        if p.get("Id") not in bundle_ids and p.get("Id") not in option_product_ids
    ]
    option_products_sold_alone = [
        p for p in products
        if p.get("Id") in option_product_ids and p.get("Id") not in bundle_ids
    ]

    # 2. Product Rules Classification
    pr_by_type = Counter()
    pr_by_scope = Counter()
    pr_by_event = Counter()
    error_conditions_all = []
    product_actions_all = []

    for pr in product_rules:
        pr_type = pr.get("SBQQ__Type__c") or "Unspecified"
        pr_scope = pr.get("SBQQ__Scope__c") or "Product"
        pr_event = pr.get("SBQQ__EvaluationEvent__c") or "Always"

        pr_by_type[pr_type] += 1
        pr_by_scope[pr_scope] += 1
        pr_by_event[pr_event] += 1

        error_conditions_all.extend(pr.get("error_conditions", []))
        product_actions_all.extend(pr.get("product_actions", []))

    # 3. Price Rules Classification
    price_conditions_all = []
    price_actions_all = []
    price_rules_by_target = Counter()
    price_rules_by_event = Counter()

    for prc in price_rules:
        target = prc.get("SBQQ__TargetObject__c") or "Unspecified"
        event = prc.get("SBQQ__EvaluationEvent__c") or "Unspecified"
        price_rules_by_target[target] += 1
        price_rules_by_event[event] += 1

        price_conditions_all.extend(prc.get("price_conditions", []))
        price_actions_all.extend(prc.get("price_actions", []))

    # 4. Deterministic Observations
    observations = []

    # Obs 1: Bundles with options but no defined features
    if bundles_direct_only:
        names = [b.get("Name") or b.get("ProductCode") or b.get("Id") for b in bundles_direct_only]
        observations.append({
            "id": "OBS-BUNDLE-NO-FEATURES",
            "category": "Bundle Architecture",
            "description": f"{len(names)} bundle(s) have selectable component options but no category features defined. Options appear as a flat list.",
            "affected_names": names,
        })

    # Obs 2: Features with no options
    empty_features = []
    for f in features_all:
        if not f.get("options"):
            fname = f.get("Name") or f.get("Id")
            parent_sku = f.get("SBQQ__ConfiguredSKU__c")
            parent_name = product_name_by_id.get(parent_sku, parent_sku)
            empty_features.append(f"{fname} (under {parent_name})")
    if empty_features:
        observations.append({
            "id": "OBS-FEATURE-NO-OPTIONS",
            "category": "Bundle Architecture",
            "description": f"{len(empty_features)} feature section(s) have no options configured under them.",
            "affected_names": empty_features,
        })

    # Obs 3: Rules with no conditions
    rules_no_conditions = []
    for pr in product_rules:
        if not pr.get("error_conditions"):
            rules_no_conditions.append(f"Product Rule: {pr.get('Name') or pr.get('Id')}")
    for prc in price_rules:
        if not prc.get("price_conditions"):
            rules_no_conditions.append(f"Price Rule: {prc.get('Name') or prc.get('Id')}")
    if rules_no_conditions:
        observations.append({
            "id": "OBS-RULE-NO-CONDITIONS",
            "category": "Rule Configuration",
            "description": f"{len(rules_no_conditions)} rule(s) have no conditions specified, meaning they may fire unconditionally or rely solely on lookup queries.",
            "affected_names": rules_no_conditions,
        })

    # Obs 4: Rules with no actions or error message where expected
    rules_missing_action_or_msg = []
    for pr in product_rules:
        rtype = (pr.get("SBQQ__Type__c") or "").lower()
        if rtype in ("validation", "alert") and not pr.get("SBQQ__ErrorMessage__c"):
            rules_missing_action_or_msg.append(f"{pr.get('Name')} (Validation/Alert rule without error message)")
        elif rtype == "selection" and not pr.get("product_actions"):
            rules_missing_action_or_msg.append(f"{pr.get('Name')} (Selection rule without product actions)")

    for prc in price_rules:
        if not prc.get("price_actions"):
            rules_missing_action_or_msg.append(f"{prc.get('Name')} (Price Rule without price actions)")

    if rules_missing_action_or_msg:
        observations.append({
            "id": "OBS-RULE-NO-ACTION-OR-MSG",
            "category": "Rule Configuration",
            "description": f"{len(rules_missing_action_or_msg)} rule(s) are missing expected action steps or user-facing notification messages.",
            "affected_names": rules_missing_action_or_msg,
        })

    # Obs 5: Products with no pricebook entry
    products_no_pbe = []
    for p in products:
        if not p.get("pricebook_entries"):
            products_no_pbe.append(p.get("Name") or p.get("ProductCode") or p.get("Id"))
    if products_no_pbe:
        observations.append({
            "id": "OBS-PRODUCT-NO-PRICEBOOK",
            "category": "Pricing Integrity",
            "description": f"{len(products_no_pbe)} active product(s) have no price book entry and cannot be quoted directly without an active price entry.",
            "affected_names": products_no_pbe,
        })

    # Obs 6: Options pointing to inactive or missing products
    options_inactive_target = []
    for opt in options_all:
        target_sku_id = opt.get("SBQQ__OptionalSKU__c")
        if target_sku_id and target_sku_id not in active_product_ids:
            opt_name = opt.get("SBQQ__ProductName__c") or target_sku_id
            options_inactive_target.append(f"Option {opt_name} (target {target_sku_id} not active)")

    if options_inactive_target:
        observations.append({
            "id": "OBS-OPTION-INACTIVE-TARGET",
            "category": "Catalog Integrity",
            "description": f"{len(options_inactive_target)} product option(s) point to product records that are not currently active in the catalog.",
            "affected_names": options_inactive_target,
        })

    # 5. Compile Facts Dictionary
    facts = {
        "metadata": metadata,
        "active_products": len(products),
        "total_products_extracted": len(all_products),
        "inactive_products_count": inactive_products_count,
        "inactive_products_included": inactive_products_included_count,
        "inactive_products_excluded_count": (
            inactive_products_count
            if not metadata.get("include_inactive_records")
            else 0
        ),
        "total_bundles": len(bundles),
        "standalone_products": len(standalone_products),
        "distinct_option_products": len(option_product_ids),
        "option_products_also_sold_alone": len(option_products_sold_alone),
        "option_products_also_sold_alone_names": [
            p.get("Name") or p.get("ProductCode")
            for p in option_products_sold_alone
        ],
        "feature_categories_by_bundle": {
            p.get("ProductCode") or p.get("Name") or p.get("Id"):
                len(p.get("features", []))
            for p in bundles
        },
        "total_features": len(features_all),
        "total_options": len(options_all),
        "total_pricebooks": max(len(pricebooks_set), 1 if pricebook_entries_all else 0),
        "total_pricebook_entries": len(pricebook_entries_all),
        "total_discount_schedules": len(discount_schedules_all),
        "total_block_prices": len(block_prices_all),
        "total_dimensions": len(dimensions_all),
        "total_costs": len(costs_all),
        "total_option_constraints": len(option_constraints_all),
        "total_configuration_attributes": len(config_attributes_all),
        "total_configuration_rules": len(config_rules_all),
        "products_by_family": dict(families_counter.most_common()),
        "product_rules": {
            "total": len(product_rules),
            "by_type": dict(pr_by_type),
            "by_scope": dict(pr_by_scope),
            "by_event": dict(pr_by_event),
            "total_conditions": len(error_conditions_all),
            "total_actions": len(product_actions_all),
        },
        "price_rules": {
            "total": len(price_rules),
            "by_target": dict(price_rules_by_target),
            "by_event": dict(price_rules_by_event),
            "total_conditions": len(price_conditions_all),
            "total_actions": len(price_actions_all),
        },
        "summary_variables": {
            "total": len(summary_variables),
            "names": [sv.get("Name") for sv in summary_variables if sv.get("Name")],
        },
        "observations": observations,
        "observations_count": len(observations),
    }

    return facts
