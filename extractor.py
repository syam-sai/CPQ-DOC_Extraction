# extractor.py
#
# Runs the full nested, active-only CPQ extraction against a connected
# `sf` (simple_salesforce.Salesforce) object.
# Auth-method agnostic — doesn't care how `sf` was obtained.
#
# Optimizations:
#   - Cached describe() results per object
#   - Server-side filtering via semi-joins (where supported)
#   - Try/except around each object query with extract_log.json recording
#   - Field splitting for long queries / QUERY_TOO_COMPLICATED
#   - Single pull for shared tables (LookupQuery, PriceRule)
#   - Raw CSV caching & resumption support
#   - Bulk API 2.0 support with automatic REST fallback

import os
import csv
import json
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

EXCLUDED_FIELDS = {
    "OwnerId", "CreatedById", "CreatedDate",
    "LastModifiedById", "LastModifiedDate", "SystemModstamp",
    "IsDeleted", "LastViewedDate", "LastReferencedDate"
}

EXCLUDED_FIELD_TYPES = {"base64", "address", "location"}

PRODUCT_ACTIVE_FIELD = "IsActive"
PRODUCT_RULE_ACTIVE_FIELD = "SBQQ__Active__c"
PRICE_RULE_ACTIVE_FIELD = "SBQQ__Active__c"

_describe_cache = {}


def get_object_describe(sf, object_name):
    """Caches describe() per object to prevent duplicate API calls."""
    if object_name not in _describe_cache:
        _describe_cache[object_name] = getattr(sf, object_name).describe()
    return _describe_cache[object_name]


def clear_describe_cache():
    _describe_cache.clear()


def get_queryable_fields(sf, object_name):
    """Retrieves all queryable fields, omitting system and unsupported binary/compound fields."""
    describe = get_object_describe(sf, object_name)
    fields = []
    for f in describe.get("fields", []):
        name = f["name"]
        if name in EXCLUDED_FIELDS:
            continue
        if not f.get("queryable", True):
            continue
        if f.get("type") in EXCLUDED_FIELD_TYPES:
            continue
        fields.append(name)
    return fields


def write_records_to_csv(records, csv_path, fieldnames=None):
    """Writes a list of dicts to CSV."""
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    if not records:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            if fieldnames:
                writer = csv.writer(f)
                writer.writerow(fieldnames)
        return
    if not fieldnames:
        fieldnames = list(records[0].keys())
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow(r)


def read_records_from_csv(csv_path):
    """Reads records from CSV into a list of dicts."""
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
        return []
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        records = []
        for row in reader:
            # Clean empty strings vs None
            records.append(dict(row))
        return records


def _run_soql_chunks(sf, object_name, fields, where_clause=None):
    """Splits fields into chunks if query exceeds limits or hits QUERY_TOO_COMPLICATED."""
    where_str = f" WHERE {where_clause}" if where_clause else ""
    full_query = f"SELECT {','.join(fields)} FROM {object_name}{where_str}"

    # If query is within normal length limits, attempt full query first
    if len(full_query) < 80000:
        try:
            results = sf.query_all(full_query)
            records = results.get("records", [])
            for r in records:
                r.pop("attributes", None)
            return records
        except Exception as e:
            err_msg = str(e)
            if "QUERY_TOO_COMPLICATED" not in err_msg and "MALFORMED_QUERY" not in err_msg:
                raise

    # Split fields into chunks of ~40 fields, ensuring 'Id' is always present
    chunk_size = 40
    non_id_fields = [f for f in fields if f != "Id"]
    field_chunks = [
        ["Id"] + non_id_fields[i:i + chunk_size]
        for i in range(0, len(non_id_fields), chunk_size)
    ]
    if not field_chunks:
        field_chunks = [["Id"]]

    merged = {}
    for chunk in field_chunks:
        chunk_query = f"SELECT {','.join(chunk)} FROM {object_name}{where_str}"
        res = sf.query_all(chunk_query)
        for r in res.get("records", []):
            r.pop("attributes", None)
            rid = r.get("Id")
            if rid not in merged:
                merged[rid] = r
            else:
                merged[rid].update(r)

    return list(merged.values())


def query_table(sf, object_name, where_clause=None, raw_path=None, use_bulk=False, extract_log=None):
    """
    Safely queries an object with caching, semi-join server filtering,
    Bulk 2.0 with REST fallback, and error handling.
    """
    # 1. Check if raw CSV already exists (resumption support)
    if raw_path and os.path.exists(raw_path) and os.path.getsize(raw_path) > 0:
        try:
            records = read_records_from_csv(raw_path)
            if extract_log is not None:
                extract_log[object_name] = {
                    "status": "success",
                    "count": len(records),
                    "mode": "cache_resumed",
                    "error": None,
                }
            return records
        except Exception as e:
            logger.warning(f"Could not read existing raw CSV {raw_path}: {e}. Re-querying.")

    # 2. Try Bulk API 2.0 if requested and raw_path provided
    if use_bulk and raw_path and hasattr(sf, "bulk2"):
        try:
            fields = get_queryable_fields(sf, object_name)
            where_str = f" WHERE {where_clause}" if where_clause else ""
            query = f"SELECT {','.join(fields)} FROM {object_name}{where_str}"
            bulk_handler = getattr(sf.bulk2, object_name)
            bulk_handler.download(query, raw_path)
            records = read_records_from_csv(raw_path)
            if extract_log is not None:
                extract_log[object_name] = {
                    "status": "success",
                    "count": len(records),
                    "mode": "bulk2",
                    "error": None,
                }
            return records
        except Exception as bulk_err:
            logger.warning(f"Bulk 2.0 query for {object_name} failed: {bulk_err}. Falling back to REST.")

    # 3. REST query with try/except and semi-join fallback
    try:
        fields = get_queryable_fields(sf, object_name)
        try:
            records = _run_soql_chunks(sf, object_name, fields, where_clause=where_clause)
        except Exception as q_err:
            # If semi-join was rejected, fall back to querying without where_clause
            if where_clause and ("MALFORMED_QUERY" in str(q_err) or "Semi join subselects" in str(q_err)):
                logger.warning(f"Semi-join on {object_name} failed: {q_err}. Falling back to unjoined query.")
                records = _run_soql_chunks(sf, object_name, fields, where_clause=None)
            else:
                raise

        if raw_path:
            write_records_to_csv(records, raw_path, fieldnames=fields)

        if extract_log is not None:
            extract_log[object_name] = {
                "status": "success",
                "count": len(records),
                "mode": "rest",
                "error": None,
            }
        return records

    except Exception as e:
        logger.error(f"Failed to extract {object_name}: {e}")
        if extract_log is not None:
            extract_log[object_name] = {
                "status": "failed",
                "count": 0,
                "mode": "error",
                "error": str(e),
            }
        return []


def group_by_fk(records, fk_field, valid_parent_ids=None):
    """Groups child records by parent foreign key, retaining only active parents."""
    grouped = defaultdict(list)
    for r in records:
        parent_id = r.get(fk_field)
        if parent_id and (valid_parent_ids is None or parent_id in valid_parent_ids):
            grouped[parent_id].append(r)
    return grouped


def extract_all(sf, progress_callback=None, run_dir=None, use_bulk=False, extract_log=None):
    """
    Extracts all active CPQ configuration.
    Returns (data, counts, extract_log).
    """
    if extract_log is None:
        extract_log = {}

    def step(msg):
        if progress_callback:
            progress_callback(msg)

    def raw_file(obj_name):
        if not run_dir:
            return None
        return os.path.join(run_dir, "raw", f"{obj_name}.csv")

    # 1. Active Products
    step("Pulling active products...")
    products = query_table(
        sf, "Product2",
        where_clause=f"{PRODUCT_ACTIVE_FIELD} = true",
        raw_path=raw_file("Product2"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    product_ids = set(p["Id"] for p in products)

    # 2. Product Child Objects with Server-Side Semi-Joins
    step("Pulling product child objects...")
    pricebook_entries_raw = query_table(
        sf, "PricebookEntry",
        where_clause="Product2Id IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("PricebookEntry"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    block_prices_raw = query_table(
        sf, "SBQQ__BlockPrice__c",
        where_clause="SBQQ__Product__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("SBQQ__BlockPrice__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    costs_raw = query_table(
        sf, "SBQQ__Cost__c",
        where_clause="SBQQ__Product__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("SBQQ__Cost__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    dimensions_raw = query_table(
        sf, "SBQQ__Dimension__c",
        where_clause="SBQQ__Product__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("SBQQ__Dimension__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    discount_schedules_raw = query_table(
        sf, "SBQQ__DiscountSchedule__c",
        where_clause="SBQQ__Product__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("SBQQ__DiscountSchedule__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    price_schedules_raw = query_table(
        sf, "SBQQ__PriceSchedule__c",
        where_clause="SBQQ__Product__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("SBQQ__PriceSchedule__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    features_raw = query_table(
        sf, "SBQQ__ProductFeature__c",
        where_clause="SBQQ__ConfiguredSKU__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("SBQQ__ProductFeature__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    options_raw = query_table(
        sf, "SBQQ__ProductOption__c",
        where_clause="SBQQ__ConfiguredSKU__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("SBQQ__ProductOption__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    constraints_raw = query_table(
        sf, "SBQQ__OptionConstraint__c",
        where_clause="SBQQ__ConfiguredSKU__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("SBQQ__OptionConstraint__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    config_attrs_raw = query_table(
        sf, "SBQQ__ConfigurationAttribute__c",
        where_clause="SBQQ__Product__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        raw_path=raw_file("SBQQ__ConfigurationAttribute__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )

    # Configuration Rules (Dual Parent: Product OR ProductRule)
    cr_prod = query_table(
        sf, "SBQQ__ConfigurationRule__c",
        where_clause="SBQQ__Product__c IN (SELECT Id FROM Product2 WHERE IsActive = true)",
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    cr_rule = query_table(
        sf, "SBQQ__ConfigurationRule__c",
        where_clause="SBQQ__ProductRule__c IN (SELECT Id FROM SBQQ__ProductRule__c WHERE SBQQ__Active__c = true)",
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    # Dedupe by Id
    config_rules_merged = {r["Id"]: r for r in cr_prod}
    for r in cr_rule:
        config_rules_merged[r["Id"]] = r
    config_rules_raw = list(config_rules_merged.values())
    if raw_file("SBQQ__ConfigurationRule__c"):
        write_records_to_csv(config_rules_raw, raw_file("SBQQ__ConfigurationRule__c"))

    # Active Price Rules (Pulled ONCE and reused for product-scoped rules and main price rules)
    price_rules_raw = query_table(
        sf, "SBQQ__PriceRule__c",
        where_clause=f"{PRICE_RULE_ACTIVE_FIELD} = true",
        raw_path=raw_file("SBQQ__PriceRule__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    price_rule_ids = set(r["Id"] for r in price_rules_raw)

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
    price_rules_by_product = group_by_fk(price_rules_raw, "SBQQ__Product__c", product_ids)

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

    # 3. Product Rules and Children
    step("Pulling active product rules...")
    product_rules_raw = query_table(
        sf, "SBQQ__ProductRule__c",
        where_clause=f"{PRODUCT_RULE_ACTIVE_FIELD} = true",
        raw_path=raw_file("SBQQ__ProductRule__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    product_rule_ids = set(r["Id"] for r in product_rules_raw)

    error_conditions_raw = query_table(
        sf, "SBQQ__ErrorCondition__c",
        where_clause="SBQQ__Rule__c IN (SELECT Id FROM SBQQ__ProductRule__c WHERE SBQQ__Active__c = true)",
        raw_path=raw_file("SBQQ__ErrorCondition__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    product_actions_raw = query_table(
        sf, "SBQQ__ProductAction__c",
        where_clause="SBQQ__Rule__c IN (SELECT Id FROM SBQQ__ProductRule__c WHERE SBQQ__Active__c = true)",
        raw_path=raw_file("SBQQ__ProductAction__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )

    # Lookup Queries (Shared: ProductRule OR PriceRule2) - pulled once!
    lq_pr = query_table(
        sf, "SBQQ__LookupQuery__c",
        where_clause="SBQQ__ProductRule__c IN (SELECT Id FROM SBQQ__ProductRule__c WHERE SBQQ__Active__c = true)",
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    lq_prc = query_table(
        sf, "SBQQ__LookupQuery__c",
        where_clause="SBQQ__PriceRule2__c IN (SELECT Id FROM SBQQ__PriceRule__c WHERE SBQQ__Active__c = true)",
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    lookup_queries_merged = {lq["Id"]: lq for lq in lq_pr}
    for lq in lq_prc:
        lookup_queries_merged[lq["Id"]] = lq
    all_lookup_queries = list(lookup_queries_merged.values())
    if raw_file("SBQQ__LookupQuery__c"):
        write_records_to_csv(all_lookup_queries, raw_file("SBQQ__LookupQuery__c"))

    error_cond_by_rule = group_by_fk(error_conditions_raw, "SBQQ__Rule__c", product_rule_ids)
    product_action_by_rule = group_by_fk(product_actions_raw, "SBQQ__Rule__c", product_rule_ids)
    config_rule_by_product_rule = group_by_fk(config_rules_raw, "SBQQ__ProductRule__c", product_rule_ids)
    pr_lookup_by_rule = group_by_fk(all_lookup_queries, "SBQQ__ProductRule__c", product_rule_ids)

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

    # 4. Price Rules and Children
    step("Pulling price rule child objects...")
    price_conditions_raw = query_table(
        sf, "SBQQ__PriceCondition__c",
        where_clause="SBQQ__Rule__c IN (SELECT Id FROM SBQQ__PriceRule__c WHERE SBQQ__Active__c = true)",
        raw_path=raw_file("SBQQ__PriceCondition__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )
    price_actions_raw = query_table(
        sf, "SBQQ__PriceAction__c",
        where_clause="SBQQ__Rule__c IN (SELECT Id FROM SBQQ__PriceRule__c WHERE SBQQ__Active__c = true)",
        raw_path=raw_file("SBQQ__PriceAction__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )

    price_cond_by_rule = group_by_fk(price_conditions_raw, "SBQQ__Rule__c", price_rule_ids)
    price_action_by_rule = group_by_fk(price_actions_raw, "SBQQ__Rule__c", price_rule_ids)
    prc_lookup_by_rule = group_by_fk(all_lookup_queries, "SBQQ__PriceRule2__c", price_rule_ids)

    nested_price_rules = []
    for rule in price_rules_raw:
        rid = rule["Id"]
        nested_price_rules.append({
            **rule,
            "price_conditions": price_cond_by_rule.get(rid, []),
            "price_actions": price_action_by_rule.get(rid, []),
            "lookup_queries": prc_lookup_by_rule.get(rid, []),
        })

    # 5. Summary Variables
    step("Pulling summary variables...")
    summary_variables = query_table(
        sf, "SBQQ__SummaryVariable__c",
        raw_path=raw_file("SBQQ__SummaryVariable__c"),
        use_bulk=use_bulk,
        extract_log=extract_log,
    )

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

    if run_dir:
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "extract_log.json"), "w", encoding="utf-8") as f:
            json.dump(extract_log, f, indent=2)

    return data, counts
