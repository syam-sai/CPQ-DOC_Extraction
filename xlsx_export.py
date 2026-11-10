# xlsx_export.py
#
# Converts the nested extraction JSON into a 23-tab Excel workbook:
#   4 main sheets (Products, Product Rules, Price Rules, Summary Variables)
#   17 child-detail sheets, each carrying explicit Parent_*_Id columns
#
# Rollup counts on the main sheets are real COUNTIF formulas pointing at
# the child sheets when Products <= 5,000 (configurable). Above that threshold,
# pre-computed counts are written to prevent Excel performance degradation.

import os
import json
import re
from collections import Counter
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

FONT_NAME = "Arial"
MAX_CELL_CHARS = 32767
MAX_EXCEL_ROWS = 1048576
DEFAULT_COUNTIF_MAX_PRODUCTS = int(os.environ.get("COUNTIF_MAX_PRODUCTS", "5000"))


def flat_row(record, exclude):
    """Returns a shallow copy of record with nested/list keys removed."""
    return {k: v for k, v in record.items() if k not in exclude}


def sanitize_value(val, sheet_name="", row_idx=0, col_name="", truncation_log=None):
    """Sanitizes cell values: handles dict/list, strips illegal XML chars, truncates text > 32,767."""
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        val = json.dumps(val, default=str)
    elif not isinstance(val, (str, int, float, bool)):
        val = str(val)

    if isinstance(val, str):
        val = ILLEGAL_CHARACTERS_RE.sub("", val)
        if len(val) > MAX_CELL_CHARS:
            if truncation_log is not None:
                truncation_log.append({
                    "sheet": sheet_name,
                    "row": row_idx,
                    "column": col_name,
                    "original_length": len(val),
                    "truncated_length": MAX_CELL_CHARS,
                })
            val = val[:MAX_CELL_CHARS]
    return val


def get_populated_columns(rows, id_columns=None):
    """Returns columns that have at least one non-empty value across all rows in the sheet."""
    id_columns = id_columns or []
    if not rows:
        return []

    active_cols = set()
    for r in rows:
        for k, v in r.items():
            if v is not None and v != "":
                active_cols.add(k)

    id_cols_present = [k for k in id_columns if k in active_cols]
    remaining = sorted(k for k in active_cols if k not in id_columns)
    priority = [k for k in ("Id", "Name") if k in remaining]
    others = [k for k in remaining if k not in priority]
    return id_cols_present + priority + others


def write_sheet(wb, sheet_name, rows, id_columns=None, truncation_log=None):
    """
    Writes rows (list of dicts) to a new sheet.
    Drops columns that are empty across all rows.
    Returns (worksheet, ordered_column_list).
    """
    clean_title = sheet_name[:31]  # Excel's 31-char tab name limit
    ws = wb.create_sheet(clean_title)
    id_columns = id_columns or []

    if not rows:
        ws.append(["No records extracted"])
        ws["A1"].font = Font(name=FONT_NAME, italic=True)
        return ws, []

    if len(rows) + 1 > MAX_EXCEL_ROWS:
        raise ValueError(
            f"Sheet '{sheet_name}' exceeds Excel row limit of {MAX_EXCEL_ROWS:,} rows "
            f"({len(rows) + 1:,} rows). Split data or filter active records."
        )

    columns = get_populated_columns(rows, id_columns)
    if not columns:
        ws.append(["No populated records"])
        ws["A1"].font = Font(name=FONT_NAME, italic=True)
        return ws, []

    ws.append(columns)
    for cell in ws[1]:
        cell.font = Font(bold=True, name=FONT_NAME)
    ws.freeze_panes = "A2"

    for r_idx, r in enumerate(rows, start=2):
        row_vals = [
            sanitize_value(r.get(c, ""), clean_title, r_idx, c, truncation_log)
            for c in columns
        ]
        ws.append(row_vals)

    # Set default column width for readability based on column name length
    for i, col_name in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(str(col_name)) + 2, 12), 40)

    return ws, columns


def add_rollup_column(ws, base_columns, rows, header, target_ws_name,
                       target_columns, target_id_field, source_id_field="Id",
                       use_formula=True, target_rows=None):
    """
    Appends one rollup column to ws. If use_formula=True, writes COUNTIF formulas.
    If use_formula=False, writes computed values and appends '(computed)' to header.
    """
    if not target_columns or target_id_field not in target_columns or source_id_field not in base_columns:
        return  # nothing to link

    new_col_index = len(base_columns) + 1
    while ws.cell(row=1, column=new_col_index).value not in (None, ""):
        new_col_index += 1

    col_letter = get_column_letter(new_col_index)
    source_col_letter = get_column_letter(base_columns.index(source_id_field) + 1)
    target_col_letter = get_column_letter(target_columns.index(target_id_field) + 1)

    display_header = header if use_formula else f"{header} (computed)"
    ws.cell(row=1, column=new_col_index, value=display_header).font = Font(bold=True, name=FONT_NAME)

    if use_formula:
        for r in range(2, len(rows) + 2):
            formula = f"=COUNTIF('{target_ws_name}'!{target_col_letter}:{target_col_letter},{source_col_letter}{r})"
            ws.cell(row=r, column=new_col_index, value=formula)
    else:
        counts = Counter()
        if target_rows:
            for tr in target_rows:
                tid = tr.get(target_id_field)
                if tid:
                    counts[tid] += 1
        for r_idx, r in enumerate(rows, start=2):
            sid = r.get(source_id_field)
            ws.cell(row=r_idx, column=new_col_index, value=counts.get(sid, 0))

    ws.column_dimensions[col_letter].width = max(len(display_header) + 2, 14)


def build_workbook(data, truncation_log=None, countif_max_products=DEFAULT_COUNTIF_MAX_PRODUCTS):
    wb = Workbook()
    wb.remove(wb.active)

    # ---------------- Normalize all child rows with explicit Parent_*_Id ----------------
    features_rows, options_rows, constraints_rows = [], [], []
    config_attrs_rows = []
    config_rules_by_id = {}  # dual-parent object — dedupe by Id
    pricebook_rows, block_price_rows, cost_rows = [], [], []
    dimension_rows, discount_sched_rows, price_sched_rows = [], [], []

    product_rows = []

    for p in data.get("Product", []):
        pid = p.get("Id")

        product_rows.append(flat_row(p, exclude=[
            "pricebook_entries", "pricing", "features", "direct_options",
            "option_constraints", "configuration_attributes",
            "configuration_rules", "price_rules"
        ]))

        for pbe in p.get("pricebook_entries", []):
            pricebook_rows.append({"Parent_Product_Id": pid, **pbe})

        pricing = p.get("pricing", {})
        for bp in pricing.get("block_prices", []):
            block_price_rows.append({"Parent_Product_Id": pid, **bp})
        for c in pricing.get("costs", []):
            cost_rows.append({"Parent_Product_Id": pid, **c})
        for d in pricing.get("dimensions", []):
            dimension_rows.append({"Parent_Product_Id": pid, **d})
        for ds in pricing.get("discount_schedules", []):
            discount_sched_rows.append({"Parent_Product_Id": pid, **ds})
        for ps in pricing.get("price_schedules", []):
            price_sched_rows.append({"Parent_Product_Id": pid, **ps})

        for feat in p.get("features", []):
            fid = feat.get("Id")
            features_rows.append({"Parent_Product_Id": pid, **flat_row(feat, exclude=["options"])})
            for opt in feat.get("options", []):
                options_rows.append({"Parent_Product_Id": pid, "Parent_Feature_Id": fid, **opt})

        for opt in p.get("direct_options", []):
            options_rows.append({"Parent_Product_Id": pid, "Parent_Feature_Id": None, **opt})

        for oc in p.get("option_constraints", []):
            constraints_rows.append({"Parent_Product_Id": pid, **oc})

        for ca in p.get("configuration_attributes", []):
            config_attrs_rows.append({"Parent_Product_Id": pid, **ca})

        for cr in p.get("configuration_rules", []):
            crid = cr.get("Id")
            merged = config_rules_by_id.get(crid, {})
            merged.update({"Parent_Product_Id": pid, **cr})
            config_rules_by_id[crid] = merged

    # ---------------- Product Rules ----------------
    product_rule_rows, error_condition_rows, product_action_rows, pr_lookup_rows = [], [], [], []

    for pr in data.get("ProductRules", []):
        rid = pr.get("Id")
        product_rule_rows.append(flat_row(pr, exclude=[
            "error_conditions", "product_actions", "configuration_rules", "lookup_queries"
        ]))
        for ec in pr.get("error_conditions", []):
            error_condition_rows.append({"Parent_Rule_Id": rid, **ec})
        for pa in pr.get("product_actions", []):
            product_action_rows.append({"Parent_Rule_Id": rid, **pa})
        for cr in pr.get("configuration_rules", []):
            crid = cr.get("Id")
            merged = config_rules_by_id.get(crid, {})
            merged.update({"Parent_Rule_Id": rid, **cr})
            config_rules_by_id[crid] = merged
        for lq in pr.get("lookup_queries", []):
            pr_lookup_rows.append({"Parent_Rule_Id": rid, **lq})

    # ---------------- Price Rules ----------------
    price_rule_rows, price_condition_rows, price_action_rows, prc_lookup_rows = [], [], [], []

    for pr in data.get("PriceRules", []):
        rid = pr.get("Id")
        price_rule_rows.append({
            "Parent_Product_Id": pr.get("SBQQ__Product__c"),  # product-scoped link, if any
            **flat_row(pr, exclude=["price_conditions", "price_actions", "lookup_queries"])
        })
        for pc in pr.get("price_conditions", []):
            price_condition_rows.append({"Parent_Rule_Id": rid, **pc})
        for pa in pr.get("price_actions", []):
            price_action_rows.append({"Parent_Rule_Id": rid, **pa})
        for lq in pr.get("lookup_queries", []):
            prc_lookup_rows.append({"Parent_Rule_Id": rid, **lq})

    summary_var_rows = data.get("SummaryVariables", [])
    config_rules_final = list(config_rules_by_id.values())

    # Determine whether to use COUNTIF formula or computed values based on product count
    use_formulas = len(product_rows) <= countif_max_products

    # ---------------- Write child sheets first (main sheets need their column layouts) ----------------
    child = {}

    def add_child(name, rows, id_cols):
        ws, cols = write_sheet(wb, name, rows, id_columns=id_cols, truncation_log=truncation_log)
        child[name] = (ws, cols, rows)

    add_child("Features", features_rows, ["Parent_Product_Id"])
    add_child("Options", options_rows, ["Parent_Product_Id", "Parent_Feature_Id"])
    add_child("Option Constraints", constraints_rows, ["Parent_Product_Id"])
    add_child("Configuration Attributes", config_attrs_rows, ["Parent_Product_Id"])
    add_child("Configuration Rules", config_rules_final, ["Parent_Product_Id", "Parent_Rule_Id"])
    add_child("Pricebook Entries", pricebook_rows, ["Parent_Product_Id"])
    add_child("Block Prices", block_price_rows, ["Parent_Product_Id"])
    add_child("Costs", cost_rows, ["Parent_Product_Id"])
    add_child("Dimensions", dimension_rows, ["Parent_Product_Id"])
    add_child("Discount Schedules", discount_sched_rows, ["Parent_Product_Id"])
    add_child("Price Schedules", price_sched_rows, ["Parent_Product_Id"])
    add_child("Error Conditions", error_condition_rows, ["Parent_Rule_Id"])
    add_child("Product Actions", product_action_rows, ["Parent_Rule_Id"])
    add_child("Product Rule Lookup Queries", pr_lookup_rows, ["Parent_Rule_Id"])
    add_child("Price Conditions", price_condition_rows, ["Parent_Rule_Id"])
    add_child("Price Actions", price_action_rows, ["Parent_Rule_Id"])
    add_child("Price Rule Lookup Queries", prc_lookup_rows, ["Parent_Rule_Id"])

    # ---------------- Main sheets with rollup formulas ----------------
    products_ws, products_cols = write_sheet(wb, "Products", product_rows, id_columns=[], truncation_log=truncation_log)
    for header, target_name, target_id_field in [
        ("Feature Count", "Features", "Parent_Product_Id"),
        ("Option Count", "Options", "Parent_Product_Id"),
        ("Configuration Rule Count", "Configuration Rules", "Parent_Product_Id"),
        ("Pricebook Entry Count", "Pricebook Entries", "Parent_Product_Id"),
    ]:
        if target_name in child:
            t_ws, t_cols, t_rows = child[target_name]
            add_rollup_column(
                products_ws, products_cols, product_rows, header, target_name,
                t_cols, target_id_field, use_formula=use_formulas, target_rows=t_rows
            )

    product_rules_ws, product_rules_cols = write_sheet(wb, "Product Rules", product_rule_rows, id_columns=[], truncation_log=truncation_log)
    for header, target_name, target_id_field in [
        ("Error Condition Count", "Error Conditions", "Parent_Rule_Id"),
        ("Product Action Count", "Product Actions", "Parent_Rule_Id"),
        ("Configuration Rule Count", "Configuration Rules", "Parent_Rule_Id"),
        ("Lookup Query Count", "Product Rule Lookup Queries", "Parent_Rule_Id"),
    ]:
        t_ws, t_cols, t_rows = child[target_name]
        add_rollup_column(
            product_rules_ws, product_rules_cols, product_rule_rows, header, target_name,
            t_cols, target_id_field, use_formula=True, target_rows=t_rows
        )

    price_rules_ws, price_rules_cols = write_sheet(wb, "Price Rules", price_rule_rows, id_columns=["Parent_Product_Id"], truncation_log=truncation_log)
    for header, target_name, target_id_field in [
        ("Price Condition Count", "Price Conditions", "Parent_Rule_Id"),
        ("Price Action Count", "Price Actions", "Parent_Rule_Id"),
        ("Lookup Query Count", "Price Rule Lookup Queries", "Parent_Rule_Id"),
    ]:
        t_ws, t_cols, t_rows = child[target_name]
        add_rollup_column(
            price_rules_ws, price_rules_cols, price_rule_rows, header, target_name,
            t_cols, target_id_field, use_formula=True, target_rows=t_rows
        )

    # Scoped price rules rollup onto Products
    add_rollup_column(
        products_ws, products_cols, product_rows, "Price Rule Count (scoped)",
        "Price Rules", price_rules_cols, "Parent_Product_Id",
        use_formula=use_formulas, target_rows=price_rule_rows
    )

    write_sheet(wb, "Summary Variables", summary_var_rows, id_columns=[], truncation_log=truncation_log)
    quality_rows = [
        {
            "Severity": finding.get("severity"),
            "Entity": finding.get("entity"),
            "Rule": finding.get("rule"),
            "Product": finding.get("product"),
            "Message": finding.get("message"),
        }
        for finding in data.get("data_quality", [])
    ]
    write_sheet(
        wb,
        "Data Quality",
        quality_rows or [{"Message": "No data-quality findings were recorded."}],
        id_columns=[],
        truncation_log=truncation_log,
    )
    metadata_rows = [
        {"Field": key, "Value": value}
        for key, value in data.get("metadata", {}).items()
    ] or [{"Field": "metadata", "Value": "Not available"}]
    write_sheet(
        wb, "Extraction Metadata", metadata_rows, id_columns=[],
        truncation_log=truncation_log
    )

    # Put the 4 main sheets first, in sensible reading order
    main_order = ["Products", "Product Rules", "Price Rules", "Summary Variables"]
    by_title = {ws.title: ws for ws in wb.worksheets}
    ordered = [by_title[name] for name in main_order if name in by_title]
    ordered += [ws for ws in wb.worksheets if ws.title not in main_order]
    wb._sheets = ordered

    return wb


def save_workbook(data, filepath, truncation_log=None, countif_max_products=DEFAULT_COUNTIF_MAX_PRODUCTS):
    """Builds and saves the 23-tab workbook to filepath."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    wb = build_workbook(data, truncation_log=truncation_log, countif_max_products=countif_max_products)
    wb.save(filepath)
    return filepath