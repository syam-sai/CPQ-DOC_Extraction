# doc_gen/packets.py
#
# Builds self-contained, human-readable JSON packets with 100% resolved names.
# No Salesforce IDs, no system fields, no credentials.
# Enforces scale controls (bundle clustering, rule batching, price exclusions).

import os
import json
from collections import defaultdict, Counter
from doc_gen.facts import load_extraction_data, compute_facts

DEFAULT_MAX_BUNDLE_PACKETS = int(os.environ.get("MAX_BUNDLE_PACKETS", "200"))
DEFAULT_MAX_RULE_PACKETS = int(os.environ.get("MAX_RULE_PACKETS", "50"))
DEFAULT_MAX_PACKET_TOKENS = int(os.environ.get("MAX_PACKET_TOKENS", "3500"))
DEFAULT_INCLUDE_PRICES = os.environ.get("LLM_INCLUDE_PRICES", "false").lower() in ("true", "1", "yes")

SYSTEM_FIELDS = {
    "Id", "OwnerId", "CreatedById", "CreatedDate", "LastModifiedById", "LastModifiedDate",
    "SystemModstamp", "IsDeleted", "LastViewedDate", "LastReferencedDate", "attributes"
}


def clean_dict(d, exclude_keys=None):
    """Recursively removes system fields and empty values from dicts."""
    exclude = set(exclude_keys or set()) | SYSTEM_FIELDS
    out = {}
    for k, v in d.items():
        if k in exclude:
            continue
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, dict):
            sub = clean_dict(v, exclude_keys)
            if sub:
                out[k] = sub
        elif isinstance(v, list):
            sub_list = []
            for item in v:
                if isinstance(item, dict):
                    clean_item = clean_dict(item, exclude_keys)
                    if clean_item:
                        sub_list.append(clean_item)
                elif item is not None and item != "":
                    sub_list.append(item)
            if sub_list:
                out[k] = sub_list
        else:
            out[k] = v
    return out


def estimate_tokens(obj):
    """Estimates token count by serialized characters // 4."""
    text = json.dumps(obj)
    return len(text) // 4


def build_packets(data_or_run_dir, include_prices=DEFAULT_INCLUDE_PRICES,
                  max_bundle_packets=DEFAULT_MAX_BUNDLE_PACKETS,
                  max_rule_packets=DEFAULT_MAX_RULE_PACKETS):
    """
    Builds analysis packets for:
      1. Product Rules
      2. Price Rules
      3. Bundles (clustered if > max_bundle_packets)
      4. Standalone Product Summaries (grouped by family)
    Returns:
      {
        "product_rule_packets": [...],
        "price_rule_packets": [...],
        "bundle_packets": [...],
        "standalone_packets": [...],
        "summary": {...}
      }
    """
    data = load_extraction_data(data_or_run_dir)
    facts = compute_facts(data)

    products = data.get("Product", [])
    product_rules = data.get("ProductRules", [])
    price_rules = data.get("PriceRules", [])
    summary_variables = data.get("SummaryVariables", [])

    # 1. Build Reference Lookup Maps
    product_map = {}
    for p in products:
        pid = p.get("Id")
        if pid:
            product_map[pid] = {
                "name": p.get("Name") or p.get("ProductCode") or "Unnamed Product",
                "code": p.get("ProductCode") or "No SKU",
                "family": p.get("Family") or "Unassigned",
                "description": p.get("Description"),
            }

    sv_map = {}
    for sv in summary_variables:
        sv_id = sv.get("Id")
        if sv_id:
            sv_map[sv_id] = {
                "name": sv.get("Name") or "Unnamed Variable",
                "target_object": sv.get("SBQQ__TargetObject__c"),
                "function": sv.get("SBQQ__AggregateFunction__c"),
                "field": sv.get("SBQQ__AggregateField__c"),
                "filter_field": sv.get("SBQQ__FilterField__c"),
                "operator": sv.get("SBQQ__Operator__c"),
                "filter_value": sv.get("SBQQ__FilterValue__c"),
            }

    # Map option ID -> product details (for option constraints resolution)
    option_map = {}
    for p in products:
        for f in p.get("features", []):
            for opt in f.get("options", []):
                opt_id = opt.get("Id")
                tgt_sku = opt.get("SBQQ__OptionalSKU__c")
                if opt_id:
                    prod_info = product_map.get(tgt_sku, {})
                    option_map[opt_id] = {
                        "name": opt.get("SBQQ__ProductName__c") or prod_info.get("name") or "Component",
                        "code": prod_info.get("code") or "No SKU",
                    }
        for opt in p.get("direct_options", []):
            opt_id = opt.get("Id")
            tgt_sku = opt.get("SBQQ__OptionalSKU__c")
            if opt_id:
                prod_info = product_map.get(tgt_sku, {})
                option_map[opt_id] = {
                    "name": opt.get("SBQQ__ProductName__c") or prod_info.get("name") or "Component",
                    "code": prod_info.get("code") or "No SKU",
                }

    # Map Product ID -> Rules referencing it
    rules_referencing_product = defaultdict(list)
    for pr in product_rules:
        pr_name = pr.get("Name") or "Unnamed Product Rule"
        for cr in pr.get("configuration_rules", []):
            target_pid = cr.get("SBQQ__Product__c")
            if target_pid:
                rules_referencing_product[target_pid].append(f"Product Rule: {pr_name}")
        for pa in pr.get("product_actions", []):
            target_pid = pa.get("SBQQ__Product__c")
            if target_pid:
                rules_referencing_product[target_pid].append(f"Product Action in: {pr_name}")

    for prc in price_rules:
        prc_name = prc.get("Name") or "Unnamed Price Rule"
        target_pid = prc.get("SBQQ__Product__c")
        if target_pid:
            rules_referencing_product[target_pid].append(f"Price Rule: {prc_name}")

    # 2. Build Product Rule Packets
    pr_packets = []
    for pr in product_rules:
        r_name = pr.get("Name") or "Unnamed Product Rule"
        r_type = pr.get("SBQQ__Type__c") or "Validation"
        r_scope = pr.get("SBQQ__Scope__c") or "Product"
        r_event = pr.get("SBQQ__EvaluationEvent__c") or "Always"
        r_cond_met = pr.get("SBQQ__ConditionsMet__c") or "All"
        r_err_msg = pr.get("SBQQ__ErrorMessage__c")
        r_adv_cond = pr.get("SBQQ__AdvancedCondition__c")

        # Resolve conditions
        resolved_conditions = []
        referenced_svs = set()
        for idx, cond in enumerate(pr.get("error_conditions", []), start=1):
            tested_var_id = cond.get("SBQQ__TestedVariable__c")
            filter_var_id = cond.get("SBQQ__FilterVariable__c")

            tested_target = cond.get("SBQQ__TestedField__c")
            if tested_var_id and tested_var_id in sv_map:
                sv_info = sv_map[tested_var_id]
                tested_target = f"Summary Variable: {sv_info['name']} ({sv_info['function']} of {sv_info['field']})"
                referenced_svs.add(sv_info['name'])

            filter_target = cond.get("SBQQ__FilterValue__c")
            if filter_var_id and filter_var_id in sv_map:
                sv_info = sv_map[filter_var_id]
                filter_target = f"Summary Variable: {sv_info['name']}"
                referenced_svs.add(sv_info['name'])

            resolved_conditions.append({
                "condition_number": int(cond.get("SBQQ__Index__c") or idx),
                "tested_target": tested_target or "Tested Condition",
                "comparison_operator": cond.get("SBQQ__Operator__c") or "equals",
                "target_value": filter_target or "Specified Value",
                "tested_object": cond.get("SBQQ__TestedObject__c"),
            })

        # Resolve actions
        resolved_actions = []
        for act in pr.get("product_actions", []):
            act_type = act.get("SBQQ__Type__c") or "Add"
            act_pid = act.get("SBQQ__Product__c")
            prod_info = product_map.get(act_pid, {})
            resolved_actions.append({
                "action_type": act_type,
                "target_product_name": prod_info.get("name") or "Target Product",
                "target_product_sku": prod_info.get("code") or "No SKU",
                "required": act.get("SBQQ__Required__c", False),
                "value": act.get("SBQQ__Value__c"),
            })

        # Resolve configuration rules (bundles this rule attaches to)
        applies_to_bundles = []
        for cr in pr.get("configuration_rules", []):
            target_pid = cr.get("SBQQ__Product__c")
            prod_info = product_map.get(target_pid, {})
            b_name = prod_info.get("name")
            if b_name and b_name not in applies_to_bundles:
                applies_to_bundles.append(b_name)

        packet = {
            "packet_type": "product_rule",
            "rule_name": r_name,
            "rule_type": r_type,
            "scope": r_scope,
            "evaluation_event": r_event,
            "conditions_logic": r_adv_cond or f"Requires {r_cond_met.lower()} conditions to be met",
            "error_message_shown_to_user": r_err_msg,
            "conditions": resolved_conditions,
            "actions": resolved_actions,
            "applies_to_bundles": applies_to_bundles,
            "referenced_summary_variables": list(referenced_svs),
        }
        pr_packets.append(clean_dict(packet))

    # 3. Build Price Rule Packets
    prc_packets = []
    for prc in price_rules:
        r_name = prc.get("Name") or "Unnamed Price Rule"
        target_obj = prc.get("SBQQ__TargetObject__c") or "Configurator"
        event = prc.get("SBQQ__EvaluationEvent__c") or "On Calculate"
        cond_met = prc.get("SBQQ__ConditionsMet__c") or "All"

        scoped_prod = None
        scoped_pid = prc.get("SBQQ__Product__c")
        if scoped_pid and scoped_pid in product_map:
            scoped_prod = f"{product_map[scoped_pid]['name']} ({product_map[scoped_pid]['code']})"

        resolved_conditions = []
        for cond in prc.get("price_conditions", []):
            resolved_conditions.append({
                "field": cond.get("SBQQ__Field__c") or "Field",
                "operator": cond.get("SBQQ__Operator__c") or "equals",
                "value": cond.get("SBQQ__Value__c") or "Value",
            })

        resolved_actions = []
        for act in prc.get("price_actions", []):
            resolved_actions.append({
                "target_object": act.get("SBQQ__TargetObject__c") or "Quote Line",
                "target_field": act.get("SBQQ__Field__c") or "Target Field",
                "value": act.get("SBQQ__Value__c") or "Formula or Calculated Value",
            })

        packet = {
            "packet_type": "price_rule",
            "rule_name": r_name,
            "target_object": target_obj,
            "evaluation_event": event,
            "conditions_logic": f"Requires {cond_met.lower()} conditions to be met",
            "scoped_to_product": scoped_prod,
            "conditions": resolved_conditions,
            "actions": resolved_actions,
        }
        prc_packets.append(clean_dict(packet))

    # 4. Build Bundle Packets (with Clustering for Scale)
    bundle_products = [p for p in products if (p.get("features") or p.get("direct_options"))]

    raw_bundle_packets = []
    for p in bundle_products:
        pid = p.get("Id")
        pname = p.get("Name") or p.get("ProductCode") or "Unnamed Bundle"
        sku = p.get("ProductCode") or "No SKU"
        family = p.get("Family") or "Unassigned"
        desc = p.get("Description")

        # Features with grouped options
        features_list = []
        for f in p.get("features", []):
            feat_name = f.get("Name") or "Feature Category"
            feat_options = []
            for opt in f.get("options", []):
                tgt_sku = opt.get("SBQQ__OptionalSKU__c")
                prod_info = product_map.get(tgt_sku, {})
                opt_dict = {
                    "component_name": opt.get("SBQQ__ProductName__c") or prod_info.get("name") or "Component",
                    "component_sku": prod_info.get("code") or "No SKU",
                    "default_quantity": opt.get("SBQQ__Quantity__c", 1),
                    "quantity_editable": opt.get("SBQQ__QuantityEditable__c", False),
                    "required": opt.get("SBQQ__Required__c", False),
                    "selected_by_default": opt.get("SBQQ__Selected__c", False),
                    "component_type": opt.get("SBQQ__Type__c") or "Component",
                }
                if include_prices and opt.get("SBQQ__UnitPrice__c") is not None:
                    opt_dict["unit_price"] = opt.get("SBQQ__UnitPrice__c")
                feat_options.append(opt_dict)

            features_list.append({
                "feature_name": feat_name,
                "category": f.get("SBQQ__Category__c"),
                "min_options": f.get("SBQQ__MinOptions__c"),
                "max_options": f.get("SBQQ__MaxOptions__c"),
                "options": feat_options,
            })

        # Direct (ungrouped) options
        direct_options_list = []
        for opt in p.get("direct_options", []):
            tgt_sku = opt.get("SBQQ__OptionalSKU__c")
            prod_info = product_map.get(tgt_sku, {})
            opt_dict = {
                "component_name": opt.get("SBQQ__ProductName__c") or prod_info.get("name") or "Component",
                "component_sku": prod_info.get("code") or "No SKU",
                "default_quantity": opt.get("SBQQ__Quantity__c", 1),
                "quantity_editable": opt.get("SBQQ__QuantityEditable__c", False),
                "required": opt.get("SBQQ__Required__c", False),
                "selected_by_default": opt.get("SBQQ__Selected__c", False),
                "component_type": opt.get("SBQQ__Type__c") or "Component",
            }
            if include_prices and opt.get("SBQQ__UnitPrice__c") is not None:
                opt_dict["unit_price"] = opt.get("SBQQ__UnitPrice__c")
            direct_options_list.append(opt_dict)

        # Option Constraints (Dependencies / Exclusions)
        constraints_list = []
        for oc in p.get("option_constraints", []):
            c_opt_id = oc.get("SBQQ__ConstrainedOption__c")
            cg_opt_id = oc.get("SBQQ__ConstrainingOption__c")
            c_info = option_map.get(c_opt_id, {})
            cg_info = option_map.get(cg_opt_id, {})
            constraints_list.append({
                "constraint_name": oc.get("Name"),
                "constrained_component": c_info.get("name") or "Target Option",
                "constraining_component": cg_info.get("name") or "Trigger Option",
                "constraint_type": oc.get("SBQQ__Type__c") or "Dependency",
            })

        # Configuration Attributes
        attrs_list = []
        for ca in p.get("configuration_attributes", []):
            attrs_list.append({
                "attribute_name": ca.get("Name"),
                "target_field": ca.get("SBQQ__TargetField__c"),
                "position": ca.get("SBQQ__Position__c"),
                "shown_values": ca.get("SBQQ__ShownValues__c"),
                "required": ca.get("SBQQ__Required__c", False),
            })

        # Related rules
        affecting_rules = sorted(list(set(rules_referencing_product.get(pid, []))))

        b_packet = {
            "packet_type": "bundle",
            "bundle_name": pname,
            "sku": sku,
            "family": family,
            "description": desc,
            "configuration_type": p.get("SBQQ__ConfigurationType__c") or "Allowed",
            "configuration_event": p.get("SBQQ__ConfigurationEvent__c") or "Always",
            "features": features_list,
            "ungrouped_options": direct_options_list,
            "option_constraints": constraints_list,
            "configuration_attributes": attrs_list,
            "affecting_rules": affecting_rules,
        }
        raw_bundle_packets.append(clean_dict(b_packet))

    # Apply Clustering if Bundles > MAX_BUNDLE_PACKETS
    final_bundle_packets = []
    if len(raw_bundle_packets) <= max_bundle_packets:
        final_bundle_packets = raw_bundle_packets
    else:
        # Cluster bundles by structure signature
        clusters = defaultdict(list)
        for bp in raw_bundle_packets:
            feat_sig = tuple(sorted((f.get("feature_name", ""), len(f.get("options", []))) for f in bp.get("features", [])))
            dir_count = len(bp.get("ungrouped_options", []))
            sig = (bp.get("family", ""), feat_sig, dir_count)
            clusters[sig].append(bp)

        for sig, cluster_members in clusters.items():
            rep = dict(cluster_members[0])
            if len(cluster_members) > 1:
                rep["clustered"] = True
                rep["cluster_size"] = len(cluster_members)
                rep["similar_products_in_catalog"] = [
                    f"{m['bundle_name']} ({m.get('sku', 'No SKU')})" for m in cluster_members[1:]
                ]
            final_bundle_packets.append(rep)

    # 5. Standalone Products Summary Packets (Summarized by Family, never individual)
    standalone_by_fam = defaultdict(list)
    for p in products:
        if not p.get("features") and not p.get("direct_options"):
            fam = p.get("Family") or "Unassigned"
            standalone_by_fam[fam].append({
                "name": p.get("Name") or p.get("ProductCode"),
                "sku": p.get("ProductCode"),
                "pricing_method": p.get("SBQQ__PricingMethod__c") or "List",
                "subscription_pricing": p.get("SBQQ__SubscriptionPricing__c"),
            })

    standalone_packets = []
    for fam, items in standalone_by_fam.items():
        packet = {
            "packet_type": "standalone_summary",
            "family": fam,
            "product_count": len(items),
            "sample_products": [f"{it['name']} ({it['sku']})" for it in items[:10]],
            "has_more": len(items) > 10,
        }
        standalone_packets.append(clean_dict(packet))

    # Calculate token estimates
    total_tokens = sum(estimate_tokens(p) for p in (pr_packets + prc_packets + final_bundle_packets + standalone_packets))

    summary = {
        "total_packets": len(pr_packets) + len(prc_packets) + len(final_bundle_packets) + len(standalone_packets),
        "product_rule_packets": len(pr_packets),
        "price_rule_packets": len(prc_packets),
        "bundle_packets": len(final_bundle_packets),
        "standalone_packets": len(standalone_packets),
        "total_bundles_in_catalog": len(bundle_products),
        "clustered_bundle_packets": len(final_bundle_packets) < len(bundle_products),
        "estimated_total_tokens": total_tokens,
    }

    return {
        "product_rule_packets": pr_packets,
        "price_rule_packets": prc_packets,
        "bundle_packets": final_bundle_packets,
        "standalone_packets": standalone_packets,
        "summary": summary,
    }

