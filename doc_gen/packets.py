# doc_gen/packets.py
#
# Builds self-contained, human-readable JSON packets with 100% resolved names.
# No Salesforce IDs, no system fields, no credentials.
# Enforces scale controls (bundle clustering, rule batching, price exclusions).

import os
import json
import re
from collections import defaultdict, Counter
from doc_gen.facts import is_active_product, load_extraction_data, compute_facts

DEFAULT_MAX_BUNDLE_PACKETS = int(os.environ.get("MAX_BUNDLE_PACKETS", "200"))
DEFAULT_MAX_RULE_PACKETS = int(os.environ.get("MAX_RULE_PACKETS", "50"))
DEFAULT_MAX_PACKET_TOKENS = int(os.environ.get("MAX_PACKET_TOKENS", "3500"))
DEFAULT_INCLUDE_PRICES = os.environ.get("LLM_INCLUDE_PRICES", "false").lower() in ("true", "1", "yes")
DEFAULT_INCLUDE_PERSONAL_DATA = os.environ.get(
    "LLM_INCLUDE_PERSONAL_DATA", "false"
).lower() in ("true", "1", "yes")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)

SYSTEM_FIELDS = {
    "Id", "OwnerId", "CreatedById", "CreatedDate", "LastModifiedById", "LastModifiedDate",
    "SystemModstamp", "IsDeleted", "LastViewedDate", "LastReferencedDate", "attributes"
}

# Salesforce 15/18-char IDs (letter + digit). Used to keep IDs out of packets.
SF_ID_RE = re.compile(r"^(?=[a-zA-Z0-9]*[a-zA-Z])(?=[a-zA-Z0-9]*\d)[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")

VALUE_NOT_RESOLVED = "VALUE NOT RESOLVED"
_PLACEHOLDER_VALUES = {
    "specified value",
    "formula or calculated value",
    "value",
}

# Salesforce CPQ operator picklist labels / API variants -> plain English.
OPERATOR_LABELS = {
    "equals": "equals",
    "equal": "equals",
    "eq": "equals",
    "not equals": "does not equal",
    "not equal": "does not equal",
    "notequals": "does not equal",
    "notequal": "does not equal",
    "neq": "does not equal",
    "less than": "is less than",
    "lessthan": "is less than",
    "lt": "is less than",
    "less or equals": "is less than or equal to",
    "less or equal": "is less than or equal to",
    "lessorequals": "is less than or equal to",
    "lessorequal": "is less than or equal to",
    "leq": "is less than or equal to",
    "lte": "is less than or equal to",
    "greater than": "is greater than",
    "greaterthan": "is greater than",
    "gt": "is greater than",
    "greater or equals": "is greater than or equal to",
    "greater or equal": "is greater than or equal to",
    "greaterorequals": "is greater than or equal to",
    "greaterorequal": "is greater than or equal to",
    "geq": "is greater than or equal to",
    "gte": "is greater than or equal to",
    "starts with": "starts with",
    "startswith": "starts with",
    "ends with": "ends with",
    "endswith": "ends with",
    "contains": "contains",
    "does not contain": "does not contain",
    "doesnotcontain": "does not contain",
    "not contains": "does not contain",
    "in": "is in",
    "not in": "is not in",
    "notin": "is not in",
    "includes": "includes",
    "excludes": "excludes",
    "is blank": "is blank",
    "isblank": "is blank",
    "is null": "is blank",
    "isnull": "is blank",
    "is not blank": "is not blank",
    "isnotblank": "is not blank",
    "is not null": "is not blank",
    "isnotnull": "is not blank",
}

ACTION_TYPE_VERBS = {
    "add": "Adds",
    "select": "Adds",
    "un select": "Removes",
    "unselect": "Removes",
    "deselect": "Removes",
    "remove": "Removes",
    "enable": "Enables",
    "disable": "Disables",
    "show": "Shows",
    "hide": "Hides",
}


def as_bool(value):
    """Interprets Salesforce booleans from either JSON booleans or CSV strings."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def looks_like_sf_id(value):
    if value is None:
        return False
    return bool(SF_ID_RE.match(str(value).strip()))


def humanize_api_name(name):
    """Turns Employee_Count__c / SBQQ__ProductName__c into readable labels."""
    if name is None or name == "":
        return None
    text = str(name).strip()
    if looks_like_sf_id(text):
        return None
    if text.startswith("SBQQ__"):
        text = text[6:]
    if text.endswith("__c"):
        text = text[:-3]
    text = text.replace("__", " ").replace("_", " ").strip()
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return text or None


def humanize_formula(formula):
    """Replaces Salesforce API field references in a formula with readable labels."""
    if not isinstance(formula, str):
        return formula
    pattern = re.compile(r"\b(?:SBQQ__)?[A-Za-z][A-Za-z0-9_]*(?:__c|__r)\b")
    return pattern.sub(
        lambda match: humanize_api_name(match.group(0)) or "field",
        formula,
    )


def humanize_operator(operator):
    """Maps every CPQ operator variant to 'is less than or equal to' style wording."""
    if operator is None or operator == "":
        return "equals"
    raw = str(operator).strip()
    key = raw.lower()
    compact = key.replace(" ", "")
    return OPERATOR_LABELS.get(key) or OPERATOR_LABELS.get(compact) or raw


def is_missing_data_value(val):
    """True when a comparison value is absent or a known placeholder — 0 is a real value."""
    if val is None or val == "":
        return True
    if isinstance(val, str) and val.strip().lower() in _PLACEHOLDER_VALUES:
        return True
    return False


def format_code_name(code, name):
    """Resolves a product to 'CODE - Name'. Returns None if only a raw Salesforce ID remains."""
    code_s = "" if code is None else str(code).strip()
    name_s = "" if name is None else str(name).strip()
    if code_s.lower() in ("no sku", "none", "null"):
        code_s = ""
    if looks_like_sf_id(code_s):
        code_s = ""
    if looks_like_sf_id(name_s):
        name_s = ""
    if code_s and name_s:
        return f"{code_s} - {name_s}"
    return name_s or code_s or None


def format_summary_variable(sv_info):
    """Plain-English summary variable: name, aggregate, and filter."""
    name = sv_info.get("name") or "Unnamed Variable"
    fn = sv_info.get("function")
    field = humanize_api_name(sv_info.get("field")) or sv_info.get("field")
    agg = None
    if fn and field:
        agg = f"{fn} of {field}"
    elif fn:
        agg = str(fn)
    elif field:
        agg = str(field)

    filter_field = humanize_api_name(sv_info.get("filter_field")) or sv_info.get("filter_field")
    filter_op = humanize_operator(sv_info.get("operator")) if sv_info.get("operator") else None
    filter_val = sv_info.get("filter_value")
    where = None
    if filter_field and not is_missing_data_value(filter_val):
        where = f"{filter_field} {filter_op or 'equals'} '{filter_val}'"
    elif filter_field:
        where = str(filter_field)

    detail = agg
    if where:
        detail = f"{agg} where {where}" if agg else where
    if detail:
        return f"{name} ({detail})"
    return name


def format_action_description(action_type, target_label, required=False):
    """One standalone sentence fragment per action, never combined with other actions."""
    verb = ACTION_TYPE_VERBS.get(str(action_type or "Add").strip().lower(), str(action_type or "Add").strip().capitalize())
    label = target_label or VALUE_NOT_RESOLVED
    suffix = " (mandatory)" if required and str(action_type or "").strip().lower() == "add" else ""
    return f"{verb} {label}{suffix}"


def clean_dict(d, exclude_keys=None, include_personal_data=DEFAULT_INCLUDE_PERSONAL_DATA):
    """Recursively removes system fields and empty values from dicts."""
    exclude = set(exclude_keys or set()) | SYSTEM_FIELDS
    out = {}
    for k, v in d.items():
        if k in exclude:
            continue
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, dict):
            sub = clean_dict(v, exclude_keys, include_personal_data)
            if sub:
                out[k] = sub
        elif isinstance(v, list):
            sub_list = []
            for item in v:
                if isinstance(item, dict):
                    clean_item = clean_dict(item, exclude_keys, include_personal_data)
                    if clean_item:
                        sub_list.append(clean_item)
                elif item is not None and item != "":
                    sub_list.append(
                        item if include_personal_data or not isinstance(item, str)
                        else EMAIL_RE.sub("[email redacted]", item)
                    )
            if sub_list:
                out[k] = sub_list
        elif isinstance(v, str) and not include_personal_data:
            out[k] = EMAIL_RE.sub("[email redacted]", v)
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

    products = [
        product for product in data.get("Product", [])
        if is_active_product(product)
    ]
    product_rules = data.get("ProductRules", [])
    price_rules = data.get("PriceRules", [])
    summary_variables = data.get("SummaryVariables", [])

    # 1. Build Reference Lookup Maps
    product_map = {}
    for p in products:
        pid = p.get("Id")
        if pid:
            name = p.get("Name") or p.get("ProductCode") or "Unnamed Product"
            code = p.get("ProductCode") or "No SKU"
            product_map[pid] = {
                "name": name,
                "code": code,
                "family": p.get("Family") or "Unassigned",
                "description": p.get("Description"),
                "label": format_code_name(code, name) or name,
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
    option_bundles_by_product = defaultdict(set)
    for p in products:
        bundle_label = format_code_name(p.get("ProductCode"), p.get("Name"))
        for f in p.get("features", []):
            for opt in f.get("options", []):
                opt_id = opt.get("Id")
                tgt_sku = opt.get("SBQQ__OptionalSKU__c")
                if opt_id:
                    prod_info = product_map.get(tgt_sku, {})
                    option_map[opt_id] = {
                        "name": prod_info.get("label") or "Component",
                        "code": prod_info.get("code") or "No SKU",
                    }
                if tgt_sku and bundle_label:
                    option_bundles_by_product[tgt_sku].add(bundle_label)
        for opt in p.get("direct_options", []):
            opt_id = opt.get("Id")
            tgt_sku = opt.get("SBQQ__OptionalSKU__c")
            if opt_id:
                prod_info = product_map.get(tgt_sku, {})
                option_map[opt_id] = {
                    "name": prod_info.get("label") or "Component",
                    "code": prod_info.get("code") or "No SKU",
                }
            if tgt_sku and bundle_label:
                option_bundles_by_product[tgt_sku].add(bundle_label)

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
        r_order = pr.get("SBQQ__EvaluationOrder__c")
        r_active = pr.get("SBQQ__Active__c")
        data_quality = []

        if r_cond_met and str(r_cond_met).lower() == "custom" and r_adv_cond:
            conditions_logic = f"Custom: {r_adv_cond}"
        else:
            conditions_logic = f"Requires {str(r_cond_met).lower()} conditions to be met"
            if r_adv_cond:
                conditions_logic = f"{conditions_logic} ({r_adv_cond})"

        # Resolve conditions
        resolved_conditions = []
        referenced_svs = set()
        for idx, cond in enumerate(pr.get("error_conditions", []), start=1):
            tested_var_id = cond.get("SBQQ__TestedVariable__c")
            filter_var_id = cond.get("SBQQ__FilterVariable__c")
            filter_type = cond.get("SBQQ__FilterType__c")
            tested_object = cond.get("SBQQ__TestedObject__c")
            tested_field = cond.get("SBQQ__TestedField__c")
            tested_attr = cond.get("SBQQ__TestedAttribute__c")
            cond_num = int(cond.get("SBQQ__Index__c") or idx)

            tested_target = None
            if tested_var_id:
                if tested_var_id in sv_map:
                    sv_info = sv_map[tested_var_id]
                    tested_target = f"Summary Variable: {format_summary_variable(sv_info)}"
                    referenced_svs.add(sv_info["name"])
                else:
                    tested_target = VALUE_NOT_RESOLVED
                    data_quality.append(
                        f"Rule '{r_name}' condition {cond_num}: tested summary variable could not be resolved"
                    )
            elif tested_attr:
                tested_target = humanize_api_name(tested_attr) or str(tested_attr)
            elif tested_field:
                tested_target = humanize_api_name(tested_field) or str(tested_field)
            elif tested_object:
                tested_target = str(tested_object)
            else:
                tested_target = VALUE_NOT_RESOLVED
                data_quality.append(
                    f"Rule '{r_name}' condition {cond_num}: tested field/variable could not be resolved"
                )

            ft_low = str(filter_type or "").strip().lower()
            filter_value = cond.get("SBQQ__TestedValue__c")
            if is_missing_data_value(filter_value):
                filter_value = cond.get("SBQQ__FilterValue__c")
            filter_field = cond.get("SBQQ__FilterField__c")
            target_value = None

            if ft_low == "variable" or (filter_var_id and is_missing_data_value(filter_value)):
                if filter_var_id and filter_var_id in sv_map:
                    sv_info = sv_map[filter_var_id]
                    target_value = f"Summary Variable: {format_summary_variable(sv_info)}"
                    referenced_svs.add(sv_info["name"])
                elif filter_var_id:
                    target_value = VALUE_NOT_RESOLVED
                    data_quality.append(
                        f"Rule '{r_name}' condition {cond_num}: filter summary variable could not be resolved"
                    )
            elif ft_low == "field" or (filter_field and is_missing_data_value(filter_value)):
                resolved_ff = humanize_api_name(filter_field) or filter_field
                if resolved_ff and not looks_like_sf_id(resolved_ff):
                    target_value = resolved_ff
                else:
                    target_value = VALUE_NOT_RESOLVED
                    data_quality.append(
                        f"Rule '{r_name}' condition {cond_num}: filter field could not be resolved"
                    )

            if target_value is None:
                if not is_missing_data_value(filter_value):
                    target_value = filter_value
                elif filter_var_id and filter_var_id in sv_map:
                    sv_info = sv_map[filter_var_id]
                    target_value = f"Summary Variable: {format_summary_variable(sv_info)}"
                    referenced_svs.add(sv_info["name"])
                else:
                    target_value = VALUE_NOT_RESOLVED
                    data_quality.append(
                        f"Rule '{r_name}' condition {cond_num}: comparison value could not be resolved "
                        f"(filter type={filter_type or 'blank'})"
                    )

            tested_value = cond.get("SBQQ__TestedValue__c")
            if not is_missing_data_value(tested_value):
                target_value = tested_value

            if looks_like_sf_id(target_value):
                data_quality.append(
                    f"Rule '{r_name}' condition {cond_num}: comparison value is an unresolved Salesforce ID"
                )
                target_value = VALUE_NOT_RESOLVED
            elif isinstance(target_value, str):
                target_value = humanize_formula(target_value)

            resolved_conditions.append({
                "condition_number": cond_num,
                "tested_target": tested_target,
                "comparison_operator": humanize_operator(cond.get("SBQQ__Operator__c")),
                "comparison_operator_raw": cond.get("SBQQ__Operator__c") or "equals",
                "target_value": target_value,
                "tested_object": tested_object,
                "error_condition_type": (
                    cond.get("SBQQ__Type__c") or tested_object or filter_type
                ),
                "filter_type": filter_type,
            })

        # Resolve actions — one discrete description per action, never merged
        resolved_actions = []
        for act in pr.get("product_actions", []):
            act_type = act.get("SBQQ__Type__c") or "Add"
            act_pid = act.get("SBQQ__Product__c")
            prod_info = product_map.get(act_pid, {})
            required = as_bool(act.get("SBQQ__Required__c", False))
            selected = act.get("SBQQ__Selected__c")
            label = prod_info.get("label")
            if not label:
                label = VALUE_NOT_RESOLVED
                data_quality.append(
                    f"Rule '{r_name}' action type '{act_type}': target product could not be resolved to CODE - Name"
                )
            action_row = {
                "action_type": act_type,
                "target_product_name": prod_info.get("name") or label,
                "target_product_sku": prod_info.get("code") or None,
                "target_label": label,
                "required": required,
                "value": act.get("SBQQ__Value__c"),
                "action_description": format_action_description(act_type, label, required),
            }
            if selected is not None and selected != "":
                action_row["selected"] = selected
            resolved_actions.append(action_row)

        # Resolve configuration rules (bundles this rule attaches to)
        applies_to_bundles = []
        for cr in pr.get("configuration_rules", []):
            target_pid = cr.get("SBQQ__Product__c")
            prod_info = product_map.get(target_pid, {})
            b_label = prod_info.get("label")
            if not b_label:
                if target_pid:
                    data_quality.append(
                        f"Rule '{r_name}': configuration-rule product could not be resolved to CODE - Name"
                    )
                continue
            if b_label not in applies_to_bundles:
                applies_to_bundles.append(b_label)

        packet = {
            "packet_type": "product_rule",
            "rule_name": r_name,
            "rule_type": r_type,
            "scope": r_scope,
            "evaluation_event": r_event,
            "evaluation_order": r_order,
            "active": r_active if r_active is not None else True,
            "conditions_met": r_cond_met,
            "custom_condition_expression": r_adv_cond,
            "conditions_logic": conditions_logic,
            "error_message_shown_to_user": r_err_msg,
            "conditions": resolved_conditions,
            "actions": resolved_actions,
            "applies_to_bundles": applies_to_bundles,
            "referenced_summary_variables": list(referenced_svs),
            "data_quality": data_quality,
        }
        cleaned = clean_dict(packet)
        cleaned["data_quality"] = clean_dict({"items": data_quality}).get("items", [])
        cleaned["active"] = as_bool(packet["active"])
        cleaned["conditions_met"] = r_cond_met
        if r_order is not None and r_order != "":
            cleaned["evaluation_order"] = r_order
        pr_packets.append(cleaned)

    # 3. Build Price Rule Packets
    prc_packets = []
    for prc in price_rules:
        r_name = prc.get("Name") or "Unnamed Price Rule"
        target_obj = prc.get("SBQQ__TargetObject__c") or "Configurator"
        event = (
            prc.get("SBQQ__ConfiguratorEvaluationEvent__c")
            or prc.get("SBQQ__EvaluationEvent__c")
            or "On Calculate"
        )
        cond_met = prc.get("SBQQ__ConditionsMet__c") or "All"
        advanced_condition = prc.get("SBQQ__AdvancedCondition__c")
        active = prc.get("SBQQ__Active__c")
        evaluation_order = prc.get("SBQQ__EvaluationOrder__c")
        data_quality = []

        scoped_prod = None
        scoped_pid = prc.get("SBQQ__Product__c")
        if scoped_pid and scoped_pid in product_map:
            scoped_prod = product_map[scoped_pid]["label"]
        elif scoped_pid:
            scoped_prod = VALUE_NOT_RESOLVED
            data_quality.append(
                f"Price rule '{r_name}': scoped product could not be resolved to CODE - Name"
            )

        resolved_conditions = []
        for cond in prc.get("price_conditions", []):
            condition_value = cond.get("SBQQ__Value__c")
            if is_missing_data_value(condition_value) or looks_like_sf_id(condition_value):
                condition_value = VALUE_NOT_RESOLVED
                data_quality.append(
                    f"Price rule '{r_name}': condition value could not be resolved"
                )
            resolved_conditions.append({
                "field": humanize_api_name(cond.get("SBQQ__Field__c"))
                or VALUE_NOT_RESOLVED,
                "operator": humanize_operator(cond.get("SBQQ__Operator__c")),
                "operator_raw": cond.get("SBQQ__Operator__c") or "equals",
                "value": condition_value,
                "lookup_object": cond.get("SBQQ__LookupObject__c"),
            })

        resolved_actions = []
        for act in prc.get("price_actions", []):
            action_value = act.get("SBQQ__Value__c")
            if action_value is not None and looks_like_sf_id(action_value):
                action_value = VALUE_NOT_RESOLVED
                data_quality.append(
                    f"Price rule '{r_name}': action value is an unresolved Salesforce ID"
                )
            elif isinstance(action_value, str):
                action_value = humanize_formula(action_value)
            source_variable = act.get("SBQQ__SourceVariable__c")
            if source_variable and source_variable in sv_map:
                source_variable = format_summary_variable(sv_map[source_variable])
            elif source_variable and looks_like_sf_id(source_variable):
                source_variable = VALUE_NOT_RESOLVED
                data_quality.append(
                    f"Price rule '{r_name}': source variable could not be resolved"
                )
            resolved_actions.append({
                "target_object": humanize_api_name(
                    act.get("SBQQ__TargetObject__c")
                ) or act.get("SBQQ__TargetObject__c") or VALUE_NOT_RESOLVED,
                "target_field": humanize_api_name(act.get("SBQQ__Field__c"))
                or VALUE_NOT_RESOLVED,
                "value": action_value if not is_missing_data_value(action_value)
                else VALUE_NOT_RESOLVED,
                "formula": humanize_formula(act.get("SBQQ__Formula__c")),
                "source_variable": source_variable,
                "lookup_field": humanize_api_name(act.get("SBQQ__LookupField__c")),
            })
            if not any(
                not is_missing_data_value(resolved_actions[-1].get(key))
                for key in ("value", "formula", "source_variable", "lookup_field")
            ):
                data_quality.append(
                    f"Price rule '{r_name}': action formula/value/source is not available"
                )

        lookup_queries = []
        for lookup in prc.get("lookup_queries", []):
            lookup_fields = {}
            for key, value in lookup.items():
                if key in SYSTEM_FIELDS or key.lower().endswith("id"):
                    continue
                if isinstance(value, str) and looks_like_sf_id(value):
                    continue
                if value not in (None, "", [], {}):
                    lookup_fields[key] = humanize_formula(value) if isinstance(value, str) else value
            if lookup_fields:
                lookup_queries.append(lookup_fields)
        if lookup_queries:
            data_quality.append(
                f"Price rule '{r_name}': lookup query details are extracted, but lookup table rows are not available"
            )

        if str(cond_met).lower() == "custom" and advanced_condition:
            conditions_logic = f"Custom: {advanced_condition}"
        else:
            conditions_logic = f"Requires {str(cond_met).lower()} conditions to be met"
            if advanced_condition:
                conditions_logic = f"{conditions_logic} ({advanced_condition})"

        affects_bundles = sorted(option_bundles_by_product.get(scoped_pid, set()))

        packet = {
            "packet_type": "price_rule",
            "rule_name": r_name,
            "target_object": target_obj,
            "evaluation_event": event,
            "evaluation_order": evaluation_order,
            "scope": target_obj,
            "active": as_bool(active) if active is not None else True,
            "conditions_met": cond_met,
            "custom_condition_expression": advanced_condition,
            "conditions_logic": conditions_logic,
            "scoped_to_product": scoped_prod,
            "affects_bundles": affects_bundles,
            "conditions": resolved_conditions,
            "actions": resolved_actions,
            "lookup_queries": lookup_queries,
            "data_quality": data_quality,
        }
        cleaned = clean_dict(packet)
        cleaned["data_quality"] = clean_dict({"items": data_quality}).get("items", [])
        prc_packets.append(cleaned)

    # 4. Build Bundle Packets (with Clustering for Scale)
    bundle_products = [
        p for p in products
        if p.get("direct_options")
        or any(feature.get("options") for feature in p.get("features", []))
    ]

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
                    "component_name": prod_info.get("label") or "Component",
                    "component_sku": prod_info.get("code") or "No SKU",
                    "default_quantity": opt.get("SBQQ__Quantity__c", 1),
                    "min_quantity": opt.get("SBQQ__MinQuantity__c"),
                    "max_quantity": opt.get("SBQQ__MaxQuantity__c"),
                    "quantity_editable": as_bool(opt.get("SBQQ__QuantityEditable__c", False)),
                    "required": as_bool(opt.get("SBQQ__Required__c", False)),
                    "selected_by_default": as_bool(opt.get("SBQQ__Selected__c", False)),
                    "component_type": opt.get("SBQQ__Type__c") or "Component",
                    "shared_by_bundle_count": len(
                        option_bundles_by_product.get(tgt_sku, set())
                    ),
                    "shared_by_multiple_bundles": len(
                        option_bundles_by_product.get(tgt_sku, set())
                    ) > 1,
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
                "component_name": prod_info.get("label") or "Component",
                "component_sku": prod_info.get("code") or "No SKU",
                "default_quantity": opt.get("SBQQ__Quantity__c", 1),
                "min_quantity": opt.get("SBQQ__MinQuantity__c"),
                "max_quantity": opt.get("SBQQ__MaxQuantity__c"),
                "quantity_editable": as_bool(opt.get("SBQQ__QuantityEditable__c", False)),
                "required": as_bool(opt.get("SBQQ__Required__c", False)),
                "selected_by_default": as_bool(opt.get("SBQQ__Selected__c", False)),
                "component_type": opt.get("SBQQ__Type__c") or "Component",
                "shared_by_bundle_count": len(
                    option_bundles_by_product.get(tgt_sku, set())
                ),
                "shared_by_multiple_bundles": len(
                    option_bundles_by_product.get(tgt_sku, set())
                ) > 1,
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
            attribute = {
                "attribute_name": ca.get("Name"),
                "target_field": humanize_api_name(
                    ca.get("SBQQ__TargetField__c")
                ) or VALUE_NOT_RESOLVED,
                "data_type": ca.get("TargetFieldType"),
                "position": ca.get("SBQQ__Position__c"),
                "shown_values": ca.get("SBQQ__ShownValues__c"),
                "picklist_values": ca.get("TargetFieldPicklistValues"),
                "required": as_bool(ca.get("SBQQ__Required__c", False)),
            }
            if not ca.get("TargetFieldType"):
                attribute["data_quality"] = "Field type not available in extraction"
            attrs_list.append(attribute)

        # Related rules
        related_product_ids = set()
        for feature in p.get("features", []):
            related_product_ids.update(
                option.get("SBQQ__OptionalSKU__c")
                for option in feature.get("options", [])
                if option.get("SBQQ__OptionalSKU__c")
            )
        related_product_ids.update(
            option.get("SBQQ__OptionalSKU__c")
            for option in p.get("direct_options", [])
            if option.get("SBQQ__OptionalSKU__c")
        )
        affecting_rules = set(rules_referencing_product.get(pid, []))
        for related_pid in related_product_ids:
            affecting_rules.update(rules_referencing_product.get(related_pid, []))
        affecting_rules = sorted(affecting_rules)

        b_packet = {
            "packet_type": "bundle",
            "bundle_name": format_code_name(sku, pname) or pname,
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
    option_product_ids = {
        option.get("SBQQ__OptionalSKU__c")
        for product in products
        for option in (
            list(product.get("direct_options", []))
            + [
                nested_option
                for feature in product.get("features", [])
                for nested_option in feature.get("options", [])
            ]
        )
        if option.get("SBQQ__OptionalSKU__c")
    }
    standalone_by_fam = defaultdict(list)
    for p in products:
        if (
            not any(feature.get("options") for feature in p.get("features", []))
            and not p.get("direct_options")
            and p.get("Id") not in option_product_ids
        ):
            fam = p.get("Family") or "Unassigned"
            standalone_by_fam[fam].append({
                "name": format_code_name(
                    p.get("ProductCode"), p.get("Name")
                ) or p.get("Name") or p.get("ProductCode"),
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
            "sample_products": [it["name"] for it in items[:10]],
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
