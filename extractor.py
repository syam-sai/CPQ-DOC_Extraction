# extractor.py
#
# Runs the full nested, active-only CPQ extraction against a connected
# `sf` (simple_salesforce.Salesforce) object. Auth-method agnostic —
# doesn't care how `sf` was obtained.

from collections import defaultdict

EXCLUDED_FIELDS = {
    "OwnerId", "CreatedById", "CreatedDate",
    "LastModifiedById", "LastModifiedDate", "SystemModstamp",
    "IsDeleted", "LastViewedDate", "LastReferencedDate"
}

PRODUCT_ACTIVE_FIELD = "IsActive"
PRODUCT_RULE_ACTIVE_FIELD = "SBQQ__Active__c"
PRICE_RULE_ACTIVE_FIELD = "SBQQ__Active__c"


def get_queryable_fields(sf, object_name):
    describe = getattr(sf, object_name).describe()
    return [
        f["name"] for f in describe["fields"]
        if f["name"] not in EXCLUDED_FIELDS and f.get("queryable", True)
    ]


def query_table(sf, object_name, where_clause=None):
    fields = get_queryable_fields(sf, object_name)
    query = f"SELECT {','.join(fields)} FROM {object_name}"
    if where_clause:
        query += f" WHERE {where_clause}"
    results = sf.query_all(query)
    records = results["records"]
    for r in records:
        r.pop("attributes", None)
    return records


def group_by_fk(records, fk_field, valid_parent_ids=None):
    grouped = defaultdict(list)
    for r in records:
        parent_id = r.get(fk_field)
        if parent_id and (valid_parent_ids is None or parent_id in valid_parent_ids):
            grouped[parent_id].append(r)
    return grouped


def extract_all(sf, progress_callback=None):
    """Returns (data, counts). progress_callback(msg) is called with status strings."""

    def step(msg):
        if progress_callback:
            progress_callback(msg)

    step("Pulling active products...")
    products = query_table(sf, "Product2", where_clause=f"{PRODUCT_ACTIVE_FIELD} = true")
    product_ids = set(p["Id"] for p in products)

    step("Pulling product child objects...")
    pricebook_entries_raw = query_table(sf, "PricebookEntry")
    block_prices_raw = query_table(sf, "SBQQ__BlockPrice__c")
    costs_raw = query_table(sf, "SBQQ__Cost__c")
    dimensions_raw = query_table(sf, "SBQQ__Dimension__c")
    discount_schedules_raw = query_table(sf, "SBQQ__DiscountSchedule__c")
    price_schedules_raw = query_table(sf, "SBQQ__PriceSchedule__c")
    features_raw = query_table(sf, "SBQQ__ProductFeature__c")
    options_raw = query_table(sf, "SBQQ__ProductOption__c")
    constraints_raw = query_table(sf, "SBQQ__OptionConstraint__c")
    config_attrs_raw = query_table(sf, "SBQQ__ConfigurationAttribute__c")
    config_rules_raw = query_table(sf, "SBQQ__ConfigurationRule__c")
    product_scoped_price_rules_raw = query_table(
        sf, "SBQQ__PriceRule__c", where_clause=f"{PRICE_RULE_ACTIVE_FIELD} = true"
    )

    pricebook_by_product = group_by_fk(pricebook_entries_raw, "Product2Id", product_ids)
    block_by_product = group_by_fk(block_prices_raw, "SBQQ__Product__c", product_ids)
    cost_by_product = group_by_fk(costs_raw, "SBQQ__Product__c", product_ids)
    dim_by_product = group_by_fk(dimensions_raw, "SBQQ__Product__c", product_ids)
    discount_by_product = group_by_fk(discount_schedules_raw, "SBQQ__Product__c", product_ids)
    price_sched_by_product = group_by_fk(price_schedules_raw, "SBQQ__Product__c", product_ids)
    features_by_product = group_by_fk(features_raw, "SBQQ__ConfiguredSKU__c", product_ids)
    options_by_bundle = group_by_fk(options_raw, "SBQQ__ConfiguredSKU__c", product_ids)
    constraints_by_bundle = group_by_fk(constraints_raw, "SBQQ__ConfiguredSKU__c", product_ids)
    config_attrs_by_product = group_by_fk(config_attrs_raw, "SBQQ__Product__c", product_ids)
    config_rules_by_product = group_by_fk(config_rules_raw, "SBQQ__Product__c", product_ids)
    price_rules_by_product = group_by_fk(product_scoped_price_rules_raw, "SBQQ__Product__c", product_ids)

    features_by_id = {f["Id"]: f for f in features_raw}

    step("Nesting product structure...")
    nested_products = []
    for product in products:
        pid = product["Id"]
        this_products_options = options_by_bundle.get(pid, [])

        options_by_feature_id = defaultdict(list)
        direct_options = []
        for opt in this_products_options:
            feature_id = opt.get("SBQQ__Feature__c")
            if feature_id and feature_id in features_by_id:
                options_by_feature_id[feature_id].append(opt)
            else:
                direct_options.append(opt)

        product_features = []
        for feature in features_by_product.get(pid, []):
            product_features.append({
                **feature,
                "options": options_by_feature_id.get(feature["Id"], [])
            })

        nested_products.append({
            **product,
            "pricebook_entries": pricebook_by_product.get(pid, []),
            "pricing": {
                "block_prices": block_by_product.get(pid, []),
                "costs": cost_by_product.get(pid, []),
                "dimensions": dim_by_product.get(pid, []),
                "discount_schedules": discount_by_product.get(pid, []),
                "price_schedules": price_sched_by_product.get(pid, []),
            },
            "features": product_features,
            "direct_options": direct_options,
            "option_constraints": constraints_by_bundle.get(pid, []),
            "configuration_attributes": config_attrs_by_product.get(pid, []),
            "configuration_rules": config_rules_by_product.get(pid, []),
            "price_rules": price_rules_by_product.get(pid, []),
        })

    step("Pulling active product rules...")
    product_rules_raw = query_table(sf, "SBQQ__ProductRule__c", where_clause=f"{PRODUCT_RULE_ACTIVE_FIELD} = true")
    product_rule_ids = set(r["Id"] for r in product_rules_raw)

    error_conditions_raw = query_table(sf, "SBQQ__ErrorCondition__c")
    product_actions_raw = query_table(sf, "SBQQ__ProductAction__c")
    pr_lookup_queries_raw = query_table(sf, "SBQQ__LookupQuery__c")

    error_cond_by_rule = group_by_fk(error_conditions_raw, "SBQQ__Rule__c", product_rule_ids)
    product_action_by_rule = group_by_fk(product_actions_raw, "SBQQ__Rule__c", product_rule_ids)
    config_rule_by_product_rule = group_by_fk(config_rules_raw, "SBQQ__ProductRule__c", product_rule_ids)
    pr_lookup_by_rule = group_by_fk(pr_lookup_queries_raw, "SBQQ__ProductRule__c", product_rule_ids)

    nested_product_rules = []
    for rule in product_rules_raw:
        rid = rule["Id"]
        nested_product_rules.append({
            **rule,
            "error_conditions": error_cond_by_rule.get(rid, []),
            "product_actions": product_action_by_rule.get(rid, []),
            "configuration_rules": config_rule_by_product_rule.get(rid, []),
            "lookup_queries": pr_lookup_by_rule.get(rid, []),
        })

    step("Pulling active price rules...")
    price_rules_raw = query_table(sf, "SBQQ__PriceRule__c", where_clause=f"{PRICE_RULE_ACTIVE_FIELD} = true")
    price_rule_ids = set(r["Id"] for r in price_rules_raw)

    price_conditions_raw = query_table(sf, "SBQQ__PriceCondition__c")
    price_actions_raw = query_table(sf, "SBQQ__PriceAction__c")
    prc_lookup_queries_raw = query_table(sf, "SBQQ__LookupQuery__c")

    price_cond_by_rule = group_by_fk(price_conditions_raw, "SBQQ__Rule__c", price_rule_ids)
    price_action_by_rule = group_by_fk(price_actions_raw, "SBQQ__Rule__c", price_rule_ids)
    prc_lookup_by_rule = group_by_fk(prc_lookup_queries_raw, "SBQQ__PriceRule2__c", price_rule_ids)

    nested_price_rules = []
    for rule in price_rules_raw:
        rid = rule["Id"]
        nested_price_rules.append({
            **rule,
            "price_conditions": price_cond_by_rule.get(rid, []),
            "price_actions": price_action_by_rule.get(rid, []),
            "lookup_queries": prc_lookup_by_rule.get(rid, []),
        })

    step("Pulling summary variables...")
    summary_variables = query_table(sf, "SBQQ__SummaryVariable__c")

    data = {
        "Product": nested_products,
        "ProductRules": nested_product_rules,
        "PriceRules": nested_price_rules,
        "SummaryVariables": summary_variables,
    }

    counts = {
        "Product": len(nested_products),
        "Features": sum(len(p["features"]) for p in nested_products),
        "Options": sum(
            len(f["options"]) for p in nested_products for f in p["features"]
        ) + sum(len(p["direct_options"]) for p in nested_products),
        "ProductRules": len(nested_product_rules),
        "PriceRules": len(nested_price_rules),
        "SummaryVariables": len(summary_variables),
    }

    return data, counts
