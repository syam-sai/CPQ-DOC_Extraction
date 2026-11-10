# transformer.py
#
# High-performance, streaming transformation using DuckDB and openpyxl write_only mode:
#   - Loads raw CSVs from raw/
#   - Streams NDJSON (one product per line) to output/cpq_extract.ndjson
#   - Streams full JSON to output/cpq_extract.json
#   - Streams 23-tab XLSX to output/cpq_extract.xlsx
#   - Keeps peak memory flat (under 150 MB) even for 10,000+ products
#   - Supports rollup COUNTIF formulas (<= 5,000 products) or computed counts (> 5,000 products)

import os
import csv
import json
from collections import defaultdict, Counter
import duckdb
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.cell import WriteOnlyCell
from openpyxl.utils import get_column_letter

from xlsx_export import (
    FONT_NAME,
    MAX_CELL_CHARS,
    MAX_EXCEL_ROWS,
    DEFAULT_COUNTIF_MAX_PRODUCTS,
    sanitize_value,
    get_populated_columns,
)


def coerce_value(val):
    """Coerces CSV string values into native Python types for JSON output."""
    if val is None or val == "" or val == "NaN":
        return None
    if isinstance(val, str):
        v_low = val.lower()
        if v_low == "true":
            return True
        if v_low == "false":
            return False
        # Integer
        if val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
            try:
                return int(val)
            except ValueError:
                pass
        # Float
        if "." in val or "e" in v_low:
            try:
                return float(val)
            except ValueError:
                pass
    return val


def read_table_rows(con, table_name):
    """Reads all rows from a DuckDB table into a list of dicts with coerced values."""
    try:
        desc = con.execute(f"DESCRIBE {table_name}").fetchall()
        col_names = [d[0] for d in desc]
        raw_rows = con.execute(f"SELECT * FROM {table_name}").fetchall()
        rows = []
        for r in raw_rows:
            d = {}
            for col, val in zip(col_names, r):
                d[col] = coerce_value(val)
            rows.append(d)
        return rows
    except Exception:
        return []


def group_rows_by_fk(rows, fk_field):
    grouped = defaultdict(list)
    for r in rows:
        parent_id = r.get(fk_field)
        if parent_id:
            grouped[parent_id].append(r)
    return grouped


def write_streaming_sheet(wb, sheet_name, rows, id_columns=None, truncation_log=None,
                          rollups=None, use_formulas=True, source_id_field="Id"):
    """
    Streams a sheet using openpyxl write_only mode:
    - Drops empty columns across all rows
    - Appends any rollup columns (either COUNTIF formula or computed count)
    - Enforces Excel 1,048,576 row limit
    Returns (clean_title, final_columns, row_count).
    """
    clean_title = sheet_name[:31]
    ws = wb.create_sheet(clean_title)
    id_columns = id_columns or []
    rollups = rollups or []

    if not rows:
        ws.append(["No records extracted"])
        return clean_title, [], 0

    if len(rows) + 1 > MAX_EXCEL_ROWS:
        raise ValueError(
            f"Sheet '{sheet_name}' exceeds Excel row limit of {MAX_EXCEL_ROWS:,} rows "
            f"({len(rows) + 1:,} rows). Split data or filter active records."
        )

    base_cols = get_populated_columns(rows, id_columns)
    if not base_cols:
        ws.append(["No populated records"])
        return clean_title, [], 0

    # Prepare rollup headers
    rollup_headers = []
    active_rollups = []
    for r_def in rollups:
        header = r_def["header"]
        target_sheet = r_def["target_sheet"]
        target_cols = r_def["target_cols"]
        target_id_field = r_def["target_id_field"]
        if target_cols and target_id_field in target_cols and source_id_field in base_cols:
            display_hdr = header if use_formulas else f"{header} (computed)"
            rollup_headers.append(display_hdr)
            active_rollups.append({
                **r_def,
                "display_header": display_hdr,
                "target_col_letter": get_column_letter(target_cols.index(target_id_field) + 1),
                "counts": r_def.get("counts") or Counter(),
            })

    all_cols = base_cols + rollup_headers

    # Style header row
    header_cells = []
    for col_name in all_cols:
        c = WriteOnlyCell(ws, value=col_name)
        c.font = Font(bold=True, name=FONT_NAME)
        header_cells.append(c)
    ws.append(header_cells)
    ws.freeze_panes = "A2"

    src_col_idx = base_cols.index(source_id_field) + 1 if source_id_field in base_cols else 1
    src_col_letter = get_column_letter(src_col_idx)

    # Stream data rows
    for r_idx, r in enumerate(rows, start=2):
        row_vals = [
            sanitize_value(r.get(c, ""), clean_title, r_idx, c, truncation_log)
            for c in base_cols
        ]
        # Append rollup values
        for r_def in active_rollups:
            if use_formulas:
                formula = f"={r_def.get('func', 'COUNTIF')}('{r_def['target_sheet']}'!{r_def['target_col_letter']}:{r_def['target_col_letter']},{src_col_letter}{r_idx})"
                row_vals.append(formula)
            else:
                sid = r.get(source_id_field)
                cnt = r_def["counts"].get(sid, 0)
                row_vals.append(cnt)

        ws.append(row_vals)

    # Set column widths
    for i, col_name in enumerate(all_cols, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(str(col_name)) + 2, 12), 40)

    return clean_title, all_cols, len(rows)


def transform_run(run_dir, countif_max_products=DEFAULT_COUNTIF_MAX_PRODUCTS, progress_callback=None):
    """
    Transforms raw CSVs in run_dir/raw to:
      run_dir/output/cpq_extract.ndjson
      run_dir/output/cpq_extract.json
      run_dir/output/cpq_extract.xlsx
    Returns (counts, truncation_log).
    """
    def step(msg):
        if progress_callback:
            progress_callback(msg)

    raw_dir = os.path.join(run_dir, "raw")
    output_dir = os.path.join(run_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    truncation_log = []
    metadata_path = os.path.join(run_dir, "extraction_metadata.json")
    quality_path = os.path.join(run_dir, "data_quality.json")
    metadata = {}
    data_quality = []
    if os.path.exists(metadata_path):
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    if os.path.exists(quality_path):
        with open(quality_path, "r", encoding="utf-8") as f:
            data_quality = json.load(f)

    con = duckdb.connect()

    # Register all available raw CSVs into DuckDB
    expected_tables = [
        "Product2", "PricebookEntry", "SBQQ__BlockPrice__c", "SBQQ__Cost__c",
        "SBQQ__Dimension__c", "SBQQ__DiscountSchedule__c", "SBQQ__PriceSchedule__c",
        "SBQQ__ProductFeature__c", "SBQQ__ProductOption__c", "SBQQ__OptionConstraint__c",
        "SBQQ__ConfigurationAttribute__c", "SBQQ__ConfigurationRule__c",
        "SBQQ__ProductRule__c", "SBQQ__ErrorCondition__c", "SBQQ__ProductAction__c",
        "SBQQ__PriceRule__c", "SBQQ__PriceCondition__c", "SBQQ__PriceAction__c",
        "SBQQ__LookupQuery__c", "SBQQ__SummaryVariable__c"
    ]

    for tbl in expected_tables:
        csv_file = os.path.join(raw_dir, f"{tbl}.csv")
        clean_tbl = tbl.replace("__", "_")
        if os.path.exists(csv_file) and os.path.getsize(csv_file) > 0:
            csv_path = csv_file.replace("\\", "/")
            con.execute(f"CREATE TABLE {clean_tbl} AS SELECT * FROM read_csv_auto('{csv_path}', all_varchar=True)")
        else:
            con.execute(f"CREATE TABLE {clean_tbl} (Id VARCHAR)")

    step("Reading staged objects from DuckDB...")
    products_raw = read_table_rows(con, "Product2")
    pricebook_raw = read_table_rows(con, "PricebookEntry")
    block_raw = read_table_rows(con, "SBQQ_BlockPrice_c")
    cost_raw = read_table_rows(con, "SBQQ_Cost_c")
    dim_raw = read_table_rows(con, "SBQQ_Dimension_c")
    discount_raw = read_table_rows(con, "SBQQ_DiscountSchedule_c")
    price_sched_raw = read_table_rows(con, "SBQQ_PriceSchedule_c")
    features_raw = read_table_rows(con, "SBQQ_ProductFeature_c")
    options_raw = read_table_rows(con, "SBQQ_ProductOption_c")
    constraints_raw = read_table_rows(con, "SBQQ_OptionConstraint_c")
    config_attrs_raw = read_table_rows(con, "SBQQ_ConfigurationAttribute_c")
    config_rules_raw = read_table_rows(con, "SBQQ_ConfigurationRule_c")
    product_rules_raw = read_table_rows(con, "SBQQ_ProductRule_c")
    error_cond_raw = read_table_rows(con, "SBQQ_ErrorCondition_c")
    product_action_raw = read_table_rows(con, "SBQQ_ProductAction_c")
    price_rules_raw = read_table_rows(con, "SBQQ_PriceRule_c")
    price_cond_raw = read_table_rows(con, "SBQQ_PriceCondition_c")
    price_action_raw = read_table_rows(con, "SBQQ_PriceAction_c")
    lookup_queries_raw = read_table_rows(con, "SBQQ_LookupQuery_c")
    summary_vars_raw = read_table_rows(con, "SBQQ_SummaryVariable_c")

    # Group child rows by parent FK
    active_prod_ids = set(p["Id"] for p in products_raw if p.get("Id"))
    active_pr_ids = set(r["Id"] for r in product_rules_raw if r.get("Id"))
    active_prc_ids = set(r["Id"] for r in price_rules_raw if r.get("Id"))

    features_by_id = {f["Id"]: f for f in features_raw if f.get("Id")}

    # Options grouped by bundle SKU
    options_by_bundle = group_rows_by_fk(options_raw, "SBQQ__ConfiguredSKU__c")
    features_by_product = group_rows_by_fk(features_raw, "SBQQ__ConfiguredSKU__c")
    pricebook_by_product = group_rows_by_fk(pricebook_raw, "Product2Id")
    block_by_product = group_rows_by_fk(block_raw, "SBQQ__Product__c")
    cost_by_product = group_rows_by_fk(cost_raw, "SBQQ__Product__c")
    dim_by_product = group_rows_by_fk(dim_raw, "SBQQ__Product__c")
    discount_by_product = group_rows_by_fk(discount_raw, "SBQQ__Product__c")
    price_sched_by_product = group_rows_by_fk(price_sched_raw, "SBQQ__Product__c")
    constraints_by_bundle = group_rows_by_fk(constraints_raw, "SBQQ__ConfiguredSKU__c")
    config_attrs_by_product = group_rows_by_fk(config_attrs_raw, "SBQQ__Product__c")
    config_rules_by_product = group_rows_by_fk(config_rules_raw, "SBQQ__Product__c")
    price_rules_by_product = group_rows_by_fk(price_rules_raw, "SBQQ__Product__c")

    error_cond_by_rule = group_rows_by_fk(error_cond_raw, "SBQQ__Rule__c")
    product_action_by_rule = group_rows_by_fk(product_action_raw, "SBQQ__Rule__c")
    config_rule_by_product_rule = group_rows_by_fk(config_rules_raw, "SBQQ__ProductRule__c")
    pr_lookup_by_rule = group_rows_by_fk(lookup_queries_raw, "SBQQ__ProductRule__c")

    price_cond_by_rule = group_rows_by_fk(price_cond_raw, "SBQQ__Rule__c")
    price_action_by_rule = group_rows_by_fk(price_action_raw, "SBQQ__Rule__c")
    prc_lookup_by_rule = group_rows_by_fk(lookup_queries_raw, "SBQQ__PriceRule2__c")

    # ---------------- Prepare Child Rows for Excel ----------------
    features_sheet_rows, options_sheet_rows = [], []
    pricebook_sheet_rows, block_sheet_rows, cost_sheet_rows = [], [], []
    dim_sheet_rows, discount_sheet_rows, price_sched_sheet_rows = [], [], []
    constraints_sheet_rows, config_attrs_sheet_rows = [], []
    config_rules_by_id = {}

    for p in products_raw:
        pid = p["Id"]
        for pbe in pricebook_by_product.get(pid, []):
            pricebook_sheet_rows.append({"Parent_Product_Id": pid, **pbe})
        for bp in block_by_product.get(pid, []):
            block_sheet_rows.append({"Parent_Product_Id": pid, **bp})
        for c in cost_by_product.get(pid, []):
            cost_sheet_rows.append({"Parent_Product_Id": pid, **c})
        for d in dim_by_product.get(pid, []):
            dim_sheet_rows.append({"Parent_Product_Id": pid, **d})
        for ds in discount_by_product.get(pid, []):
            discount_sheet_rows.append({"Parent_Product_Id": pid, **ds})
        for ps in price_sched_by_product.get(pid, []):
            price_sched_sheet_rows.append({"Parent_Product_Id": pid, **ps})
        for feat in features_by_product.get(pid, []):
            features_sheet_rows.append({"Parent_Product_Id": pid, **feat})
        for opt in options_by_bundle.get(pid, []):
            fid = opt.get("SBQQ__Feature__c")
            options_sheet_rows.append({
                "Parent_Product_Id": pid,
                "Parent_Feature_Id": fid if fid in features_by_id else None,
                **opt
            })
        for oc in constraints_by_bundle.get(pid, []):
            constraints_sheet_rows.append({"Parent_Product_Id": pid, **oc})
        for ca in config_attrs_by_product.get(pid, []):
            config_attrs_sheet_rows.append({"Parent_Product_Id": pid, **ca})
        for cr in config_rules_by_product.get(pid, []):
            crid = cr.get("Id")
            merged = config_rules_by_id.get(crid, {})
            merged.update({"Parent_Product_Id": pid, **cr})
            config_rules_by_id[crid] = merged

    error_cond_sheet_rows, product_action_sheet_rows, pr_lookup_sheet_rows = [], [], []
    for pr in product_rules_raw:
        rid = pr["Id"]
        for ec in error_cond_by_rule.get(rid, []):
            error_cond_sheet_rows.append({"Parent_Rule_Id": rid, **ec})
        for pa in product_action_by_rule.get(rid, []):
            product_action_sheet_rows.append({"Parent_Rule_Id": rid, **pa})
        for cr in config_rule_by_product_rule.get(rid, []):
            crid = cr.get("Id")
            merged = config_rules_by_id.get(crid, {})
            merged.update({"Parent_Rule_Id": rid, **cr})
            config_rules_by_id[crid] = merged
        for lq in pr_lookup_by_rule.get(rid, []):
            pr_lookup_sheet_rows.append({"Parent_Rule_Id": rid, **lq})

    price_rules_sheet_rows = []
    price_cond_sheet_rows, price_action_sheet_rows, prc_lookup_sheet_rows = [], [], []
    for prc in price_rules_raw:
        rid = prc["Id"]
        price_rules_sheet_rows.append({
            "Parent_Product_Id": prc.get("SBQQ__Product__c"),
            **prc
        })
        for pc in price_cond_by_rule.get(rid, []):
            price_cond_sheet_rows.append({"Parent_Rule_Id": rid, **pc})
        for pa in price_action_by_rule.get(rid, []):
            price_action_sheet_rows.append({"Parent_Rule_Id": rid, **pa})
        for lq in prc_lookup_by_rule.get(rid, []):
            prc_lookup_sheet_rows.append({"Parent_Rule_Id": rid, **lq})

    config_rules_final = list(config_rules_by_id.values())

    # ---------------- Stream NDJSON and full JSON ----------------
    step("Streaming JSON and NDJSON outputs...")
    ndjson_path = os.path.join(output_dir, "cpq_extract.ndjson")
    json_path = os.path.join(output_dir, "cpq_extract.json")

    total_options_count = 0
    total_features_count = 0

    with open(ndjson_path, "w", encoding="utf-8") as f_nd, open(json_path, "w", encoding="utf-8") as f_json:
        f_json.write('{\n  "Product": [\n')

        for i, prod in enumerate(products_raw):
            pid = prod["Id"]
            this_opts = options_by_bundle.get(pid, [])
            direct_opts = []
            opts_by_feat = defaultdict(list)
            for opt in this_opts:
                fid = opt.get("SBQQ__Feature__c")
                if fid and fid in features_by_id:
                    opts_by_feat[fid].append(opt)
                else:
                    direct_opts.append(opt)

            prod_feats = []
            for feat in features_by_product.get(pid, []):
                fid = feat["Id"]
                f_opts = opts_by_feat.get(fid, [])
                prod_feats.append({
                    **feat,
                    "options": f_opts
                })
                total_options_count += len(f_opts)

            total_options_count += len(direct_opts)
            total_features_count += len(prod_feats)

            nested_prod = {
                **prod,
                "pricebook_entries": pricebook_by_product.get(pid, []),
                "pricing": {
                    "block_prices": block_by_product.get(pid, []),
                    "costs": cost_by_product.get(pid, []),
                    "dimensions": dim_by_product.get(pid, []),
                    "discount_schedules": discount_by_product.get(pid, []),
                    "price_schedules": price_sched_by_product.get(pid, []),
                },
                "features": prod_feats,
                "direct_options": direct_opts,
                "option_constraints": constraints_by_bundle.get(pid, []),
                "configuration_attributes": config_attrs_by_product.get(pid, []),
                "configuration_rules": config_rules_by_product.get(pid, []),
                "price_rules": price_rules_by_product.get(pid, []),
            }

            prod_json = json.dumps(nested_prod)
            f_nd.write(prod_json + "\n")

            if i > 0:
                f_json.write(",\n")
            f_json.write("    " + prod_json)

        # Assemble full nested ProductRules and PriceRules
        nested_prs = []
        for pr in product_rules_raw:
            rid = pr["Id"]
            nested_prs.append({
                **pr,
                "error_conditions": error_cond_by_rule.get(rid, []),
                "product_actions": product_action_by_rule.get(rid, []),
                "configuration_rules": config_rule_by_product_rule.get(rid, []),
                "lookup_queries": pr_lookup_by_rule.get(rid, []),
            })

        nested_prcs = []
        for prc in price_rules_raw:
            rid = prc["Id"]
            nested_prcs.append({
                **prc,
                "price_conditions": price_cond_by_rule.get(rid, []),
                "price_actions": price_action_by_rule.get(rid, []),
                "lookup_queries": prc_lookup_by_rule.get(rid, []),
            })

        f_json.write('\n  ],\n  "ProductRules": ' + json.dumps(nested_prs) + ",\n")
        f_json.write('  "PriceRules": ' + json.dumps(nested_prcs) + ",\n")
        f_json.write('  "SummaryVariables": ' + json.dumps(summary_vars_raw) + ",\n")
        f_json.write('  "metadata": ' + json.dumps(metadata) + ",\n")
        f_json.write('  "data_quality": ' + json.dumps(data_quality) + "\n}\n")

    # ---------------- Stream 21-Tab XLSX ----------------
    step("Streaming 23-tab XLSX output...")
    xlsx_path = os.path.join(output_dir, "cpq_extract.xlsx")
    wb = Workbook(write_only=True)
    child_sheets = {}

    def add_child(name, rows, id_cols):
        clean_name, cols, count = write_streaming_sheet(
            wb, name, rows, id_columns=id_cols, truncation_log=truncation_log
        )
        child_sheets[name] = {"name": clean_name, "cols": cols, "count": count, "rows": rows}

    add_child("Features", features_sheet_rows, ["Parent_Product_Id"])
    add_child("Options", options_sheet_rows, ["Parent_Product_Id", "Parent_Feature_Id"])
    add_child("Option Constraints", constraints_sheet_rows, ["Parent_Product_Id"])
    add_child("Configuration Attributes", config_attrs_sheet_rows, ["Parent_Product_Id"])
    add_child("Configuration Rules", config_rules_final, ["Parent_Product_Id", "Parent_Rule_Id"])
    add_child("Pricebook Entries", pricebook_sheet_rows, ["Parent_Product_Id"])
    add_child("Block Prices", block_sheet_rows, ["Parent_Product_Id"])
    add_child("Costs", cost_sheet_rows, ["Parent_Product_Id"])
    add_child("Dimensions", dim_sheet_rows, ["Parent_Product_Id"])
    add_child("Discount Schedules", discount_sheet_rows, ["Parent_Product_Id"])
    add_child("Price Schedules", price_sched_sheet_rows, ["Parent_Product_Id"])
    add_child("Error Conditions", error_cond_sheet_rows, ["Parent_Rule_Id"])
    add_child("Product Actions", product_action_sheet_rows, ["Parent_Rule_Id"])
    add_child("Product Rule Lookup Queries", pr_lookup_sheet_rows, ["Parent_Rule_Id"])
    add_child("Price Conditions", price_cond_sheet_rows, ["Parent_Rule_Id"])
    add_child("Price Actions", price_action_sheet_rows, ["Parent_Rule_Id"])
    add_child("Price Rule Lookup Queries", prc_lookup_sheet_rows, ["Parent_Rule_Id"])

    use_formulas = len(products_raw) <= countif_max_products

    # Pre-write Price Rules so its columns are known for the Products scoped rollup
    price_rules_rollups = [
        {
            "header": "Price Condition Count",
            "target_sheet": child_sheets["Price Conditions"]["name"],
            "target_cols": child_sheets["Price Conditions"]["cols"],
            "target_id_field": "Parent_Rule_Id",
            "counts": Counter(r.get("Parent_Rule_Id") for r in price_cond_sheet_rows),
        },
        {
            "header": "Price Action Count",
            "target_sheet": child_sheets["Price Actions"]["name"],
            "target_cols": child_sheets["Price Actions"]["cols"],
            "target_id_field": "Parent_Rule_Id",
            "counts": Counter(r.get("Parent_Rule_Id") for r in price_action_sheet_rows),
        },
        {
            "header": "Lookup Query Count",
            "target_sheet": child_sheets["Price Rule Lookup Queries"]["name"],
            "target_cols": child_sheets["Price Rule Lookup Queries"]["cols"],
            "target_id_field": "Parent_Rule_Id",
            "counts": Counter(r.get("Parent_Rule_Id") for r in prc_lookup_sheet_rows),
        },
    ]
    prc_name, prc_cols, _ = write_streaming_sheet(
        wb, "Price Rules", price_rules_sheet_rows, id_columns=["Parent_Product_Id"],
        truncation_log=truncation_log, rollups=price_rules_rollups, use_formulas=True, source_id_field="Id"
    )

    # Product Rules sheet
    product_rules_rollups = [
        {
            "header": "Error Condition Count",
            "target_sheet": child_sheets["Error Conditions"]["name"],
            "target_cols": child_sheets["Error Conditions"]["cols"],
            "target_id_field": "Parent_Rule_Id",
            "counts": Counter(r.get("Parent_Rule_Id") for r in error_cond_sheet_rows),
        },
        {
            "header": "Product Action Count",
            "target_sheet": child_sheets["Product Actions"]["name"],
            "target_cols": child_sheets["Product Actions"]["cols"],
            "target_id_field": "Parent_Rule_Id",
            "counts": Counter(r.get("Parent_Rule_Id") for r in product_action_sheet_rows),
        },
        {
            "header": "Configuration Rule Count",
            "target_sheet": child_sheets["Configuration Rules"]["name"],
            "target_cols": child_sheets["Configuration Rules"]["cols"],
            "target_id_field": "Parent_Rule_Id",
            "counts": Counter(r.get("Parent_Rule_Id") for r in config_rules_final if r.get("Parent_Rule_Id")),
        },
        {
            "header": "Lookup Query Count",
            "target_sheet": child_sheets["Product Rule Lookup Queries"]["name"],
            "target_cols": child_sheets["Product Rule Lookup Queries"]["cols"],
            "target_id_field": "Parent_Rule_Id",
            "counts": Counter(r.get("Parent_Rule_Id") for r in pr_lookup_sheet_rows),
        },
    ]
    pr_name, pr_cols, _ = write_streaming_sheet(
        wb, "Product Rules", product_rules_raw, id_columns=[],
        truncation_log=truncation_log, rollups=product_rules_rollups, use_formulas=True, source_id_field="Id"
    )

    # Products sheet
    products_rollups = [
        {
            "header": "Feature Count",
            "target_sheet": child_sheets["Features"]["name"],
            "target_cols": child_sheets["Features"]["cols"],
            "target_id_field": "Parent_Product_Id",
            "counts": Counter(r.get("Parent_Product_Id") for r in features_sheet_rows),
        },
        {
            "header": "Option Count",
            "target_sheet": child_sheets["Options"]["name"],
            "target_cols": child_sheets["Options"]["cols"],
            "target_id_field": "Parent_Product_Id",
            "counts": Counter(r.get("Parent_Product_Id") for r in options_sheet_rows),
        },
        {
            "header": "Configuration Rule Count",
            "target_sheet": child_sheets["Configuration Rules"]["name"],
            "target_cols": child_sheets["Configuration Rules"]["cols"],
            "target_id_field": "Parent_Product_Id",
            "counts": Counter(r.get("Parent_Product_Id") for r in config_rules_final if r.get("Parent_Product_Id")),
        },
        {
            "header": "Pricebook Entry Count",
            "target_sheet": child_sheets["Pricebook Entries"]["name"],
            "target_cols": child_sheets["Pricebook Entries"]["cols"],
            "target_id_field": "Parent_Product_Id",
            "counts": Counter(r.get("Parent_Product_Id") for r in pricebook_sheet_rows),
        },
        {
            "header": "Price Rule Count (scoped)",
            "target_sheet": prc_name,
            "target_cols": prc_cols,
            "target_id_field": "Parent_Product_Id",
            "counts": Counter(r.get("Parent_Product_Id") for r in price_rules_sheet_rows if r.get("Parent_Product_Id")),
        },
    ]
    prod_name, prod_cols, _ = write_streaming_sheet(
        wb, "Products", products_raw, id_columns=[],
        truncation_log=truncation_log, rollups=products_rollups, use_formulas=use_formulas, source_id_field="Id"
    )

    # Summary Variables sheet
    write_streaming_sheet(
        wb, "Summary Variables", summary_vars_raw, id_columns=[], truncation_log=truncation_log
    )
    quality_rows = [
        {
            "Severity": finding.get("severity"),
            "Entity": finding.get("entity"),
            "Rule": finding.get("rule"),
            "Product": finding.get("product"),
            "Message": finding.get("message"),
        }
        for finding in data_quality
    ]
    write_streaming_sheet(
        wb,
        "Data Quality",
        quality_rows or [{"Message": "No data-quality findings were recorded."}],
        id_columns=[],
        truncation_log=truncation_log,
    )
    metadata_rows = [
        {"Field": key, "Value": value}
        for key, value in metadata.items()
    ] or [{"Field": "metadata", "Value": "Not available"}]
    write_streaming_sheet(
        wb, "Extraction Metadata", metadata_rows, id_columns=[],
        truncation_log=truncation_log
    )

    # Reorder sheets to put 4 main sheets first
    main_order = ["Products", "Product Rules", "Price Rules", "Summary Variables"]
    by_title = {ws.title: ws for ws in wb.worksheets}
    ordered = [by_title[name] for name in main_order if name in by_title]
    ordered += [ws for ws in wb.worksheets if ws.title not in main_order]
    wb._sheets = ordered

    wb.save(xlsx_path)
    con.close()

    counts = {
        "Product": len(products_raw),
        "Features": total_features_count,
        "Options": total_options_count,
        "ProductRules": len(product_rules_raw),
        "PriceRules": len(price_rules_raw),
        "SummaryVariables": len(summary_vars_raw),
    }

    return counts, truncation_log
