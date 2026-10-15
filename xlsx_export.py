# xlsx_export.py
#
# Converts the nested extraction JSON into a 21-tab Excel workbook:
#   4 main sheets (Products, Product Rules, Price Rules, Summary Variables)
#   17 child-detail sheets, each carrying explicit Parent_*_Id columns
#
# Rollup counts on the main sheets are real COUNTIF formulas pointing at
# the child sheets — not hardcoded numbers — so the workbook stays correct
# if someone edits a row by hand later. Formulas evaluate automatically
# when the file is opened in real Excel/Google Sheets/LibreOffice; they
# don't need to be "pre-calculated" for that to work.

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

FONT_NAME = "Arial"


def flat_row(record, exclude):
    """Returns a shallow copy of record with nested/list keys removed."""
    return {k: v for k, v in record.items() if k not in exclude}


def write_sheet(wb, sheet_name, rows, id_columns=None):
    """
    Writes rows (list of dicts) to a new sheet. id_columns, if given, are
    forced to appear first (in that order) — used for the normalized
    Parent_*_Id link columns so their position is predictable for formulas.
    Returns (worksheet, ordered_column_list).
    """
    ws = wb.create_sheet(sheet_name[:31])  # Excel's 31-char tab name limit
    id_columns = id_columns or []

    if not rows:
        ws.append(["No records extracted"])
        ws["A1"].font = Font(name=FONT_NAME, italic=True)
        return ws, []

    all_keys = set()
    for r in rows:
        all_keys.update(r.keys())

    remaining = sorted(k for k in all_keys if k not in id_columns)
    priority = [k for k in ("Id", "Name") if k in remaining]
    others = [k for k in remaining if k not in priority]
    columns = id_columns + priority + others

    ws.append(columns)
    for cell in ws[1]:
        cell.font = Font(bold=True, name=FONT_NAME)
    ws.freeze_panes = "A2"

    for r in rows:
        ws.append([r.get(c, "") for c in columns])

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name=FONT_NAME)

    # reasonable default column width so sheets are readable on open
    for i, col_name in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(str(col_name)) + 2, 12), 40)

    return ws, columns


def add_rollup_column(ws, base_columns, row_count, header, target_ws_name,
                       target_columns, target_id_field, source_id_field="Id"):
    """
    Appends one COUNTIF rollup column to ws, counting rows in a target
    sheet whose target_id_field matches this row's source_id_field (Id).
    No-ops safely if the target sheet has no data (empty columns).
    """
    if not target_columns or target_id_field not in target_columns or source_id_field not in base_columns:
        return  # nothing to link — leave the rollup out rather than write a broken formula

    new_col_index = len(base_columns) + 1
    # account for rollups already added in this call sequence
    while ws.cell(row=1, column=new_col_index).value not in (None, ""):
        new_col_index += 1

    col_letter = get_column_letter(new_col_index)
    source_col_letter = get_column_letter(base_columns.index(source_id_field) + 1)
    target_col_letter = get_column_letter(target_columns.index(target_id_field) + 1)

    ws.cell(row=1, column=new_col_index, value=header).font = Font(bold=True, name=FONT_NAME)
    for r in range(2, row_count + 2):
        formula = f"=COUNTIF('{target_ws_name}'!{target_col_letter}:{target_col_letter},{source_col_letter}{r})"
        ws.cell(row=r, column=new_col_index, value=formula).font = Font(name=FONT_NAME)

    ws.column_dimensions[col_letter].width = max(len(header) + 2, 14)


def build_workbook(data):
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

    # ---------------- Write child sheets first (main sheets need their column layouts) ----------------
    child = {}

    def add_child(name, rows, id_cols):
        ws, cols = write_sheet(wb, name, rows, id_columns=id_cols)
        child[name] = (ws, cols)

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
    products_ws, products_cols = write_sheet(wb, "Products", product_rows, id_columns=[])
    n = len(product_rows)
    for header, target_name, target_id_field in [
        ("Feature Count", "Features", "Parent_Product_Id"),
        ("Option Count", "Options", "Parent_Product_Id"),
        ("Configuration Rule Count", "Configuration Rules", "Parent_Product_Id"),
        ("Price Rule Count (scoped)", "Price Rules", "Parent_Product_Id"),
        ("Pricebook Entry Count", "Pricebook Entries", "Parent_Product_Id"),
    ]:
        # "Price Rules" isn't in `child` (it's a main sheet, written after this) —
        # handled separately below once that sheet exists.
        if target_name in child:
            t_ws, t_cols = child[target_name]
            add_rollup_column(products_ws, products_cols, n, header, target_name, t_cols, target_id_field)

    product_rules_ws, product_rules_cols = write_sheet(wb, "Product Rules", product_rule_rows, id_columns=[])
    n = len(product_rule_rows)
    for header, target_name, target_id_field in [
        ("Error Condition Count", "Error Conditions", "Parent_Rule_Id"),
        ("Product Action Count", "Product Actions", "Parent_Rule_Id"),
        ("Configuration Rule Count", "Configuration Rules", "Parent_Rule_Id"),
        ("Lookup Query Count", "Product Rule Lookup Queries", "Parent_Rule_Id"),
    ]:
        t_ws, t_cols = child[target_name]
        add_rollup_column(product_rules_ws, product_rules_cols, n, header, target_name, t_cols, target_id_field)

    price_rules_ws, price_rules_cols = write_sheet(wb, "Price Rules", price_rule_rows, id_columns=["Parent_Product_Id"])
    n = len(price_rule_rows)
    for header, target_name, target_id_field in [
        ("Price Condition Count", "Price Conditions", "Parent_Rule_Id"),
        ("Price Action Count", "Price Actions", "Parent_Rule_Id"),
        ("Lookup Query Count", "Price Rule Lookup Queries", "Parent_Rule_Id"),
    ]:
        t_ws, t_cols = child[target_name]
        add_rollup_column(price_rules_ws, price_rules_cols, n, header, target_name, t_cols, target_id_field)

    # Now that "Price Rules" exists, go back and add its rollup onto Products
    if "Price Rule Count (scoped)" not in [c.value for c in products_ws[1]]:
        n = len(product_rows)
        add_rollup_column(products_ws, products_cols, n, "Price Rule Count (scoped)",
                           "Price Rules", price_rules_cols, "Parent_Product_Id")

    write_sheet(wb, "Summary Variables", summary_var_rows, id_columns=[])

    # Put the 4 main sheets first, in a sensible reading order
    main_order = ["Products", "Product Rules", "Price Rules", "Summary Variables"]
    by_title = {ws.title: ws for ws in wb.worksheets}
    ordered = [by_title[name] for name in main_order if name in by_title]
    ordered += [ws for ws in wb.worksheets if ws.title not in main_order]
    wb._sheets = ordered

    return wb


def save_workbook(data, filepath):
    wb = build_workbook(data)
    wb.save(filepath)
    return filepath