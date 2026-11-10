# doc_gen/render_docx.py
#
# Professional Word document (.docx) generator for non-technical client presentation.
# Styled with executive typography, palettes, structured tables, and TOC fields.
# Strictly enforces regex validation: blocks rendering if SBQQ__ or Salesforce IDs are present.

import os
from datetime import datetime
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn

# Color Palette: Deep Navy, Slate Blue, Charcoal, Soft Grey
COLOR_PRIMARY = RGBColor(27, 54, 93)      # #1B365D Deep Navy
COLOR_SECONDARY = RGBColor(74, 119, 122)  # #4A777A Slate Teal
COLOR_TEXT = RGBColor(51, 51, 51)         # #333333 Charcoal Body
COLOR_MUTED = RGBColor(102, 102, 102)     # #666666 Muted Text
COLOR_ALERT = RGBColor(180, 40, 40)       # #B42828 Warning / Needs Review

HEX_PRIMARY = "1B365D"
HEX_SECONDARY = "4A777A"
HEX_LIGHT_BG = "F4F6F9"
HEX_BORDER = "D1D5DB"
HEX_ALERT_BG = "FFF4F4"


def _rule_order_key(rule):
    value = rule.get("evaluation_order")
    try:
        order = float(value)
    except (TypeError, ValueError):
        order = float("inf")
    return order, rule.get("rule_name", "")


def set_cell_margins(cell, top=100, bottom=100, left=150, right=150):
    """Sets internal padding for a table cell in twentieths of a point (dxa)."""
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = OxmlElement('w:tcMar')
    for m, val in (('top', top), ('bottom', bottom), ('left', left), ('right', right)):
        node = OxmlElement(f'w:{m}')
        node.set(qn('w:w'), str(val))
        node.set(qn('w:type'), 'dxa')
        tcMar.append(node)
    tcPr.append(tcMar)


def set_cell_background(cell, hex_color):
    """Sets cell shading background color."""
    shading_xml = f'<w:shd {nsdecls("w")} w:fill="{hex_color}"/>'
    cell._tc.get_or_add_tcPr().append(parse_xml(shading_xml))


def set_table_borders(table, color="D1D5DB", sz="4", val="single"):
    """Applies subtle border formatting to a table."""
    tblPr = table._tbl.tblPr
    borders_xml = f"""
    <w:tblBorders {nsdecls("w")}>
      <w:top w:val="{val}" w:sz="{sz}" w:space="0" w:color="{color}"/>
      <w:bottom w:val="{val}" w:sz="{sz}" w:space="0" w:color="{color}"/>
      <w:insideH w:val="{val}" w:sz="{sz}" w:space="0" w:color="{color}"/>
      <w:insideV w:val="none"/>
      <w:left w:val="none"/>
      <w:right w:val="none"/>
    </w:tblBorders>
    """
    tblPr.append(parse_xml(borders_xml))


def add_callout(doc, text: str, title: str = None, is_alert: bool = False):
    """Creates an executive callout block with a left-accent border."""
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    cell = tbl.cell(0, 0)
    cell.width = Inches(6.5)

    bg_color = HEX_ALERT_BG if is_alert else HEX_LIGHT_BG
    accent_color = "B42828" if is_alert else HEX_PRIMARY

    set_cell_background(cell, bg_color)
    set_cell_margins(cell, top=140, bottom=140, left=200, right=200)

    # Accent border on left
    borders_xml = f"""
    <w:tcBorders {nsdecls("w")}>
      <w:left w:val="single" w:sz="24" w:space="0" w:color="{accent_color}"/>
      <w:top w:val="none"/>
      <w:right w:val="none"/>
      <w:bottom w:val="none"/>
    </w:tcBorders>
    """
    cell._tc.get_or_add_tcPr().append(parse_xml(borders_xml))

    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(2)

    if title:
        run_title = p.add_run(f"{title}\n")
        run_title.bold = True
        run_title.font.size = Pt(10.5)
        run_title.font.name = "Arial"
        run_title.font.color.rgb = COLOR_ALERT if is_alert else COLOR_PRIMARY

    run_text = p.add_run(text)
    run_text.font.size = Pt(10)
    run_text.font.name = "Arial"
    run_text.font.color.rgb = COLOR_TEXT

    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def render_document(facts: dict, sections: dict, validation_report: dict,
                    output_filepath: str, client_name: str = "Client",
                    include_migration_notes: bool = False):
    """
    Builds the executive client-shareable Word document.
    Enforces regex check: raises ValueError if validation_report['can_render'] is False.
    """
    if not validation_report.get("can_render", False):
        violations = validation_report.get("regex_check", {}).get("violations", [])
        raise ValueError(
            f"Document rendering blocked! Found {len(violations)} forbidden Salesforce ID or SBQQ__ token(s) in main body: {violations[:3]}"
        )

    os.makedirs(os.path.dirname(os.path.abspath(output_filepath)), exist_ok=True)
    doc = Document()

    # Page Margins
    for section in doc.sections:
        section.top_margin = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)

    # Base Styles
    style_normal = doc.styles["Normal"]
    style_normal.font.name = "Arial"
    style_normal.font.size = Pt(10.5)
    style_normal.font.color.rgb = COLOR_TEXT

    # -------------------------------------------------------------
    # TITLE PAGE
    # -------------------------------------------------------------
    title_p = doc.add_paragraph()
    title_p.paragraph_format.space_before = Pt(72)
    title_p.paragraph_format.space_after = Pt(8)
    has_data_errors = (
        facts.get("data_quality_errors", 0) > 0
        or any(
            str(finding.get("severity", "")).upper() == "ERROR"
            for finding in sections.get("data_quality", [])
        )
    )
    badge = "[ DRAFT - DATA ISSUES ]" if has_data_errors else "[ DRAFT - FOR DISCUSSION PURPOSES ]"
    r_badge = title_p.add_run(f"{badge}\n\n")
    r_badge.font.size = Pt(11)
    r_badge.font.bold = True
    r_badge.font.color.rgb = COLOR_ALERT

    r_title = title_p.add_run("Salesforce CPQ Configuration & Product Catalog Guide")
    r_title.font.size = Pt(26)
    r_title.font.bold = True
    r_title.font.color.rgb = COLOR_PRIMARY

    sub_p = doc.add_paragraph()
    sub_p.paragraph_format.space_before = Pt(4)
    sub_p.paragraph_format.space_after = Pt(40)
    r_sub = sub_p.add_run("Executive Commercial Architecture, Bundle Structures, Product Rules, and Price Rules Reference")
    r_sub.font.size = Pt(13)
    r_sub.font.color.rgb = COLOR_SECONDARY

    meta_tbl = doc.add_table(rows=3, cols=2)
    meta_tbl.autofit = False
    meta_tbl.columns[0].width = Inches(1.8)
    meta_tbl.columns[1].width = Inches(4.7)
    set_table_borders(meta_tbl, color="FFFFFF")

    labels = [
        ("Prepared For:", client_name),
        ("Date:", datetime.now().strftime("%B %d, %Y")),
        ("Catalog Scope:", f"{facts.get('active_products', 0):,} Active Products ({facts.get('total_bundles', 0)} Bundles)")
    ]
    for row_idx, (lbl, val) in enumerate(labels):
        c0 = meta_tbl.cell(row_idx, 0)
        c1 = meta_tbl.cell(row_idx, 1)
        p0 = c0.paragraphs[0]
        p1 = c1.paragraphs[0]
        p0.paragraph_format.space_after = Pt(4)
        p1.paragraph_format.space_after = Pt(4)
        r0 = p0.add_run(lbl)
        r0.bold = True
        r0.font.color.rgb = COLOR_MUTED
        r1 = p1.add_run(val)
        r1.font.color.rgb = COLOR_TEXT

    doc.add_page_break()

    # -------------------------------------------------------------
    # SECTION 1: EXECUTIVE SUMMARY
    # -------------------------------------------------------------
    h1 = doc.add_heading("1. Executive Summary", level=1)
    h1.runs[0].font.color.rgb = COLOR_PRIMARY
    h1.paragraph_format.space_after = Pt(8)

    for p_text in sections.get("executive_summary", []):
        p = doc.add_paragraph(p_text)
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.line_spacing = 1.15

    # -------------------------------------------------------------
    # SECTION 2: HOW TO READ THIS DOCUMENT
    # -------------------------------------------------------------
    doc.add_heading("2. How to Read This Document", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    intro_p = doc.add_paragraph(
        "This document describes your commercial setup in plain business terms. "
        "The table below defines the fundamental concepts used throughout your quote configuration system:"
    )
    intro_p.paragraph_format.space_after = Pt(8)

    glossary_items = [
        ("CPQ", "Configure, Price, Quote: the Salesforce product and pricing configuration system covered by this document."),
        ("SKU", "Stock Keeping Unit: a code used to identify a product."),
        ("Product", "A commercial good or service that sales representatives can add to an estimate or contract."),
        ("Bundle", "A core package product (like a workstation or enterprise platform) that includes customizable component options."),
        ("Feature", "A category tab within a bundle (such as 'Processor', 'Storage', or 'Support Tier') that organizes component choices."),
        ("Option", "A specific selectable component item offered inside a feature section of a product bundle."),
        ("Product Rule", "An automated business constraint that validates selections, automatically adds required companion products, or displays alert warnings."),
        ("Price Rule", "An automated formula rule that calculates discounts, dynamic markups, or price overrides during the quotation cycle."),
        ("Summary Variable", "A background counter that aggregates totals (e.g. summing total storage licenses) so rules can evaluate catalog thresholds."),
        ("Block Price", "A tiered pricing bracket where pricing is calculated based on quantity ranges rather than a flat per-unit cost."),
        ("Price Book", "The commercial catalog price list that assigns standard currency rates to individual products.")
    ]

    g_tbl = doc.add_table(rows=len(glossary_items) + 1, cols=2)
    g_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    g_tbl.autofit = False
    g_tbl.columns[0].width = Inches(1.8)
    g_tbl.columns[1].width = Inches(4.7)
    set_table_borders(g_tbl)

    # Header
    for c, title in zip(g_tbl.rows[0].cells, ["Concept", "Plain-English Definition"]):
        set_cell_background(c, HEX_LIGHT_BG)
        set_cell_margins(c, 100, 100, 150, 150)
        p = c.paragraphs[0]
        r = p.add_run(title)
        r.bold = True
        r.font.color.rgb = COLOR_PRIMARY

    for idx, (term, defn) in enumerate(glossary_items, start=1):
        row = g_tbl.rows[idx]
        set_cell_margins(row.cells[0], 80, 80, 150, 150)
        set_cell_margins(row.cells[1], 80, 80, 150, 150)
        p0 = row.cells[0].paragraphs[0]
        r0 = p0.add_run(term)
        r0.bold = True
        p1 = row.cells[1].paragraphs[0]
        p1.add_run(defn)

    doc.add_paragraph().paragraph_format.space_after = Pt(8)

    # -------------------------------------------------------------
    # SECTION 3: YOUR CATALOG AT A GLANCE
    # -------------------------------------------------------------
    doc.add_heading("3. Your Catalog at a Glance", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    doc.add_paragraph(
        "The following figures summarize the scale and operational dimensions of your active commercial catalog:"
    ).paragraph_format.space_after = Pt(8)

    # Summary Metrics Table
    summary_metrics = [
        ("Active Products", f"{facts.get('active_products', 0):,}", "Commercial products available for sale"),
        (
            "Inactive Products",
            (
                f"{facts['inactive_products_count']:,}"
                if isinstance(facts.get("inactive_products_count"), int)
                else "Not available"
            ),
            "Included in the extract" if facts.get("metadata", {}).get("include_inactive_records")
            else "Excluded from the extract",
        ),
        ("Configurable Bundles", f"{facts.get('total_bundles', 0):,}", "Products structured with component selections"),
        ("Standalone Products", f"{facts.get('standalone_products', 0):,}", "Products that are neither bundles nor bundle options"),
        ("Feature Categories", f"{facts.get('total_features', 0):,}", "Organizational sections across all bundles"),
        ("Selectable Component Options", f"{facts.get('total_options', 0):,}", "Total configurable choices available to reps"),
        ("Distinct Products Used as Options", f"{facts.get('distinct_option_products', 0):,}", "Unique products referenced by option records"),
        (
            "Products Also Used as Options",
            f"{facts.get('option_products_also_sold_alone', 0):,}",
            "Active products that are options and may also be sold independently",
        ),
        ("Active Product Rules", f"{facts.get('product_rules', {}).get('total', 0):,}", "Automated configuration validation rules"),
        ("Active Price Rules", f"{facts.get('price_rules', {}).get('total', 0):,}", "Dynamic pricing calculation rules"),
        ("Active Pricebook Entries", f"{facts.get('total_pricebook_entries', 0):,}", f"Priced items across {facts.get('total_pricebooks', 1)} pricebook(s)")
    ]

    m_tbl = doc.add_table(rows=len(summary_metrics) + 1, cols=3)
    m_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    m_tbl.autofit = False
    m_tbl.columns[0].width = Inches(2.2)
    m_tbl.columns[1].width = Inches(1.1)
    m_tbl.columns[2].width = Inches(3.2)
    set_table_borders(m_tbl)

    for c, h in zip(m_tbl.rows[0].cells, ["Dimension", "Count", "Commercial Context"]):
        set_cell_background(c, HEX_LIGHT_BG)
        set_cell_margins(c, 100, 100, 150, 150)
        r = c.paragraphs[0].add_run(h)
        r.bold = True
        r.font.color.rgb = COLOR_PRIMARY

    for idx, (dim, cnt, ctx) in enumerate(summary_metrics, start=1):
        row = m_tbl.rows[idx]
        set_cell_margins(row.cells[0], 80, 80, 150, 150)
        set_cell_margins(row.cells[1], 80, 80, 150, 150)
        set_cell_margins(row.cells[2], 80, 80, 150, 150)
        row.cells[0].paragraphs[0].add_run(dim).bold = True
        p1 = row.cells[1].paragraphs[0]
        p1.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p1.add_run(cnt)
        row.cells[2].paragraphs[0].add_run(ctx)

    doc.add_paragraph().paragraph_format.space_after = Pt(8)

    # Product Family Breakdown
    fam_data = facts.get("products_by_family", {})
    tot_p = facts.get("active_products", 1) or 1
    if len(fam_data) > 1:
        doc.add_heading("Product Catalog by Family", level=2)
        doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_SECONDARY

        f_tbl = doc.add_table(rows=len(fam_data) + 1, cols=3)
        f_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
        f_tbl.autofit = False
        f_tbl.columns[0].width = Inches(2.8)
        f_tbl.columns[1].width = Inches(1.4)
        f_tbl.columns[2].width = Inches(2.3)
        set_table_borders(f_tbl)

        for c, h in zip(f_tbl.rows[0].cells, ["Product Family", "Number of Products", "Catalog Share"]):
            set_cell_background(c, HEX_LIGHT_BG)
            set_cell_margins(c, 100, 100, 150, 150)
            r = c.paragraphs[0].add_run(h)
            r.bold = True
            r.font.color.rgb = COLOR_PRIMARY

        for idx, (fam, count) in enumerate(fam_data.items(), start=1):
            row = f_tbl.rows[idx]
            set_cell_margins(row.cells[0], 80, 80, 150, 150)
            set_cell_margins(row.cells[1], 80, 80, 150, 150)
            set_cell_margins(row.cells[2], 80, 80, 150, 150)
            row.cells[0].paragraphs[0].add_run(fam).bold = True
            p1 = row.cells[1].paragraphs[0]
            p1.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p1.add_run(f"{count:,}")
            pct = (count / tot_p) * 100
            row.cells[2].paragraphs[0].add_run(f"{pct:.1f}%")

    doc.add_paragraph().paragraph_format.space_after = Pt(12)

    changes = sections.get("changes")
    if changes:
        doc.add_heading("What Changed Since Previous Extract", level=2)
        for category in ("products", "rules", "prices"):
            details = changes.get(category, {})
            counts = changes.get("totals", {}).get(category, {})
            doc.add_paragraph(
                f"{category.capitalize()}: {counts.get('added', 0)} added, "
                f"{counts.get('removed', 0)} removed, "
                f"{counts.get('modified', 0)} modified."
            )
            for action in ("added", "removed", "modified"):
                labels = details.get(action, [])
                if labels:
                    doc.add_paragraph(
                        f"{action.capitalize()}: {', '.join(labels)}",
                        style="List Bullet",
                    )

    # -------------------------------------------------------------
    # SECTION 4: HOW PRODUCTS ARE BUNDLED AND CONFIGURED
    # -------------------------------------------------------------
    doc.add_heading("4. How Products Are Bundled and Configured", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    for p_text in sections.get("bundles_introduction", []):
        doc.add_paragraph(p_text).paragraph_format.space_after = Pt(6)

    # Detailed Bundle Profiles
    doc.add_heading("Configurable Product Packages", level=2)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_SECONDARY

    bundle_details_by_name = {
        packet.get("bundle_name"): packet
        for packet in sections.get("bundle_details", [])
        if packet.get("bundle_name")
    }
    for b in sections.get("bundle_analyses", []):
        b_name = b.get("name", "Product Bundle")
        bundle_detail = bundle_details_by_name.get(b_name)
        doc.add_heading(b_name, level=3)
        doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

        doc.add_paragraph(b.get("one_line_summary", "")).paragraph_format.space_after = Pt(4)
        doc.add_paragraph(f"Commercial Packaging: {b.get('how_it_is_sold', '')}").paragraph_format.space_after = Pt(4)

        if bundle_detail:
            feature_count = len(bundle_detail.get("features", []))
            option_count = sum(
                len(feature.get("options", []))
                for feature in bundle_detail.get("features", [])
            ) + len(bundle_detail.get("ungrouped_options", []))
            doc.add_paragraph(
                f"Feature categories in this bundle: {feature_count}; option records: {option_count}."
            )
            option_rows = []
            for feature in bundle_detail.get("features", []):
                feature_name = feature.get("feature_name") or "Not available in extracted data"
                for option in feature.get("options", []):
                    option_rows.append((feature_name, option))
            for option in bundle_detail.get("ungrouped_options", []):
                option_rows.append(("Direct option", option))

            if option_rows:
                b_tbl = doc.add_table(rows=len(option_rows) + 1, cols=3)
                b_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                b_tbl.autofit = False
                b_tbl.columns[0].width = Inches(1.7)
                b_tbl.columns[1].width = Inches(2.6)
                b_tbl.columns[2].width = Inches(2.2)
                set_table_borders(b_tbl)
                for cell, heading in zip(
                    b_tbl.rows[0].cells,
                    ["Feature", "Option (CODE - Name)", "Required / Default / Quantity"],
                ):
                    set_cell_background(cell, HEX_LIGHT_BG)
                    set_cell_margins(cell, 80, 80, 120, 120)
                    cell.paragraphs[0].add_run(heading).bold = True
                for row_index, (feature_name, option) in enumerate(option_rows, start=1):
                    component_name = option.get("component_name") or "Not available in extracted data"
                    code = option.get("component_sku")
                    label = (
                        f"{code} - {component_name}"
                        if code and code != "No SKU" and not component_name.startswith(f"{code} - ")
                        else component_name
                    )
                    quantity = option.get("default_quantity", "Not available in extracted data")
                    bounds = []
                    if option.get("min_quantity") is not None:
                        bounds.append(f"min {option['min_quantity']}")
                    if option.get("max_quantity") is not None:
                        bounds.append(f"max {option['max_quantity']}")
                    quantity_text = f"default {quantity}"
                    if bounds:
                        quantity_text += f" ({', '.join(bounds)})"
                    quantity_text += (
                        "; editable" if option.get("quantity_editable") else "; fixed"
                    )
                    required_text = "Required" if option.get("required") else "Optional"
                    selected_text = (
                        "selected by default"
                        if option.get("selected_by_default")
                        else "not selected by default"
                    )
                    shared_count = option.get("shared_by_bundle_count", 0)
                    if shared_count > 1:
                        selected_text += f"; shared by {shared_count} bundles"
                    row = b_tbl.rows[row_index]
                    for cell in row.cells:
                        set_cell_margins(cell, 60, 60, 120, 120)
                    row.cells[0].text = feature_name
                    row.cells[1].text = label
                    row.cells[2].text = f"{required_text}; {selected_text}; {quantity_text}"

            attributes = bundle_detail.get("configuration_attributes", [])
            if attributes:
                doc.add_paragraph("Configuration Attributes").runs[0].bold = True
                for attribute in attributes:
                    attr_name = attribute.get("attribute_name") or "Unnamed attribute"
                    field_name = attribute.get("target_field") or "Not available in extracted data"
                    data_type = attribute.get("data_type") or "type not available"
                    values = attribute.get("picklist_values") or attribute.get("shown_values")
                    value_text = (
                        ", ".join(str(value) for value in values)
                        if isinstance(values, list) else values
                    )
                    text = f"{attr_name}: {field_name} ({data_type})"
                    if value_text:
                        text += f"; values: {value_text}"
                    doc.add_paragraph(text, style="List Bullet")

            if bundle_detail.get("affecting_rules"):
                doc.add_paragraph(
                    "Rules affecting this bundle: "
                    + ", ".join(bundle_detail["affecting_rules"])
                )
        else:
            choices = b.get("choices_offered", [])
            if choices:
                b_tbl = doc.add_table(rows=len(choices) + 1, cols=3)
                b_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                b_tbl.autofit = False
                b_tbl.columns[0].width = Inches(1.8)
                b_tbl.columns[1].width = Inches(2.2)
                b_tbl.columns[2].width = Inches(2.5)
                set_table_borders(b_tbl)

                for c, h in zip(b_tbl.rows[0].cells, ["Feature Section", "Customer Decision", "Available Options"]):
                    set_cell_background(c, HEX_LIGHT_BG)
                    set_cell_margins(c, 80, 80, 120, 120)
                    r = c.paragraphs[0].add_run(h)
                    r.bold = True
                    r.font.size = Pt(9.5)
                    r.font.color.rgb = COLOR_PRIMARY

                for c_idx, c_info in enumerate(choices, start=1):
                    row = b_tbl.rows[c_idx]
                    set_cell_margins(row.cells[0], 60, 60, 120, 120)
                    set_cell_margins(row.cells[1], 60, 60, 120, 120)
                    set_cell_margins(row.cells[2], 60, 60, 120, 120)

                    p0 = row.cells[0].paragraphs[0]
                    p0.add_run(c_info.get("feature", "Category")).bold = True
                    p0.runs[0].font.size = Pt(9)

                    p1 = row.cells[1].paragraphs[0]
                    p1.add_run(c_info.get("what_customer_chooses", ""))
                    p1.runs[0].font.size = Pt(9)

                    p2 = row.cells[2].paragraphs[0]
                    opts = c_info.get("option_names", [])
                    p2.add_run(", ".join(opts) if opts else "Standard options")
                    p2.runs[0].font.size = Pt(9)

                doc.add_paragraph().paragraph_format.space_after = Pt(4)

        if not bundle_detail and b.get("ungrouped_options"):
            doc.add_paragraph(f"Direct Add-on Options: {', '.join(b['ungrouped_options'])}").paragraph_format.space_after = Pt(4)

        if not bundle_detail and b.get("rules_that_affect_it"):
            doc.add_paragraph(f"Automated Rules Affecting This Bundle: {', '.join(b['rules_that_affect_it'])}").paragraph_format.space_after = Pt(4)

        doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # -------------------------------------------------------------
    # SECTION 5: PRODUCT RULES AND PRICE RULES IN PLAIN ENGLISH
    # -------------------------------------------------------------
    doc.add_heading("5. Product Rules and Price Rules in Plain English", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    for p_text in sections.get("rules_introduction", []):
        doc.add_paragraph(p_text).paragraph_format.space_after = Pt(6)

    product_rule_analyses = {
        rule.get("name"): rule
        for rule in sections.get("product_rule_analyses", [])
        if rule.get("name")
    }
    product_rule_details = sections.get("rule_details", {}).get("product_rules", [])

    doc.add_heading("Product Rules", level=2)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_SECONDARY
    if not product_rule_details:
        product_rule_details = [
            {"rule_name": rule.get("name", "Product Rule")}
            for rule in sections.get("product_rule_analyses", [])
        ]

    product_rule_details = sorted(
        product_rule_details,
        key=_rule_order_key,
    )
    for detail in product_rule_details:
        r_name = detail.get("rule_name", "Product Rule")
        r_kind = str(detail.get("rule_type") or "Rule").capitalize()
        doc.add_heading(f"{r_name} ({r_kind})", level=3)
        doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

        metadata = (
            f"Active: {detail.get('active', 'Not available')}; "
            f"scope: {detail.get('scope', 'Not available')}; "
            f"evaluation event: {detail.get('evaluation_event', 'Not available')}; "
            f"evaluation order: {detail.get('evaluation_order', 'Not available')}; "
            f"conditions met: {detail.get('conditions_met', 'Not available')}."
        )
        if detail.get("custom_condition_expression"):
            metadata += f" Custom expression: {detail['custom_condition_expression']}."
        doc.add_paragraph(metadata).paragraph_format.space_after = Pt(3)
        condition_types = sorted({
            str(condition.get("error_condition_type"))
            for condition in detail.get("conditions", [])
            if condition.get("error_condition_type")
        })
        doc.add_paragraph(
            "Error condition type: "
            + (", ".join(condition_types) if condition_types else "Not available in extracted data")
        ).paragraph_format.space_after = Pt(3)

        conditions = detail.get("conditions", [])
        trigger_parts = [
            f"{condition.get('tested_target') or 'Not available in extracted data'} "
            f"{condition.get('comparison_operator') or 'equals'} "
            f"{condition.get('target_value') if condition.get('target_value') is not None else 'VALUE NOT RESOLVED'}"
            for condition in conditions
        ]
        trigger = " and ".join(trigger_parts) if trigger_parts else "No condition was extracted."
        doc.add_paragraph(f"Trigger: {trigger}").paragraph_format.space_after = Pt(3)

        actions = detail.get("actions", [])
        if actions:
            doc.add_paragraph("Effects:").runs[0].bold = True
            for action in actions:
                description = action.get("action_description") or "Effect not available in extracted data"
                doc.add_paragraph(description, style="List Bullet")
        else:
            doc.add_paragraph("Effects: Not available in extracted data.")

        message = detail.get("error_message_shown_to_user")
        if message:
            add_callout(doc, f'"{message}"', title="Message shown to the sales rep:")
        if conditions:
            example = f"Example: If {trigger}, the rule evaluates and applies the listed effect(s)."
        else:
            example = "Example: Not available in extracted data because no trigger condition was extracted."
        doc.add_paragraph(example).paragraph_format.space_after = Pt(3)

        for finding in detail.get("data_quality", []):
            doc.add_paragraph(f"Data quality: {finding}", style="List Bullet")

    # -------------------------------------------------------------
    # SECTION 6: HOW PRICING WORKS
    # -------------------------------------------------------------
    doc.add_heading("6. How Pricing Works", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    for p_text in sections.get("pricing_introduction", []):
        doc.add_paragraph(p_text).paragraph_format.space_after = Pt(6)

    product_prices = sections.get("product_prices", [])
    if product_prices:
        doc.add_heading("Extracted Product Prices", level=2)
        price_table = doc.add_table(rows=len(product_prices) + 1, cols=3)
        price_table.alignment = WD_TABLE_ALIGNMENT.CENTER
        price_table.autofit = False
        price_table.columns[0].width = Inches(3.5)
        price_table.columns[1].width = Inches(1.3)
        price_table.columns[2].width = Inches(1.6)
        set_table_borders(price_table)
        for cell, label in zip(
            price_table.rows[0].cells, ["Product (CODE - Name)", "Currency", "Unit Price"]
        ):
            set_cell_background(cell, HEX_LIGHT_BG)
            cell.paragraphs[0].add_run(label).bold = True
        for row_idx, entry in enumerate(product_prices, start=1):
            price_table.cell(row_idx, 0).text = entry.get("product") or "Unnamed product"
            price_table.cell(row_idx, 1).text = entry.get("currency") or "Currency not available"
            amount = entry.get("unit_price")
            price_table.cell(row_idx, 2).text = (
                "Not available in extracted data" if amount is None else str(amount)
            )
    else:
        doc.add_paragraph("Product prices: Not available in extracted data.")

    pricing_models = [
        ("Standard List Pricing", f"{facts.get('total_pricebook_entries', 0):,} pricebook entries were extracted."),
        ("Block Pricing", f"{facts.get('total_block_prices', 0):,} block-price records were extracted."),
        ("Pricing Dimensions", f"{facts.get('total_dimensions', 0):,} pricing-dimension records were extracted.")
    ]

    p_tbl = doc.add_table(rows=len(pricing_models) + 1, cols=2)
    p_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    p_tbl.autofit = False
    p_tbl.columns[0].width = Inches(2.2)
    p_tbl.columns[1].width = Inches(4.3)
    set_table_borders(p_tbl)

    for c, h in zip(p_tbl.rows[0].cells, ["Pricing Structure", "Catalog Application"]):
        set_cell_background(c, HEX_LIGHT_BG)
        set_cell_margins(c, 80, 80, 120, 120)
        c.paragraphs[0].add_run(h).bold = True

    for idx, (m_name, m_desc) in enumerate(pricing_models, start=1):
        row = p_tbl.rows[idx]
        set_cell_margins(row.cells[0], 60, 60, 120, 120)
        set_cell_margins(row.cells[1], 60, 60, 120, 120)
        row.cells[0].paragraphs[0].add_run(m_name).bold = True
        row.cells[1].paragraphs[0].add_run(m_desc)

    doc.add_paragraph().paragraph_format.space_after = Pt(10)

    # Detailed Automated Dynamic Pricing Rules
    price_rule_analyses = {
        rule.get("name"): rule
        for rule in sections.get("price_rule_analyses", [])
        if rule.get("name")
    }
    price_rule_details = sections.get("rule_details", {}).get("price_rules", [])

    if price_rule_details or price_rule_analyses:
        doc.add_heading("Automated Dynamic Pricing Rules", level=2)
        doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_SECONDARY
        doc.add_paragraph(
            "The following details are taken from the extracted price-rule conditions, actions, and lookup-query definitions."
        ).paragraph_format.space_after = Pt(6)

        if not price_rule_details:
            price_rule_details = [
                {"rule_name": rule.get("name", "Pricing Rule")}
                for rule in price_rule_analyses.values()
            ]
        price_rule_details = sorted(
            price_rule_details,
            key=_rule_order_key,
        )
        for detail in price_rule_details:
            pr_name = detail.get("rule_name", "Pricing Rule")
            doc.add_heading(f"{pr_name}", level=3)
            doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

            doc.add_paragraph(
                f"Active: {detail.get('active', 'Not available')}; "
                f"scope: {detail.get('scope', 'Not available')}; "
                f"evaluation event: {detail.get('evaluation_event', 'Not available')}; "
                f"evaluation order: {detail.get('evaluation_order', 'Not available')}; "
                f"conditions met: {detail.get('conditions_met', 'Not available')}."
            ).paragraph_format.space_after = Pt(3)
            if detail.get("custom_condition_expression"):
                doc.add_paragraph(
                    f"Custom condition expression: {detail['custom_condition_expression']}"
                ).paragraph_format.space_after = Pt(3)
            if detail.get("scoped_to_product"):
                doc.add_paragraph(
                    f"Product scope: {detail['scoped_to_product']}"
                ).paragraph_format.space_after = Pt(3)
            if detail.get("affects_bundles"):
                doc.add_paragraph(
                    f"Bundles containing scoped products: {', '.join(detail['affects_bundles'])}"
                ).paragraph_format.space_after = Pt(3)

            conditions = detail.get("conditions", [])
            doc.add_paragraph("Conditions:").runs[0].bold = True
            if conditions:
                for condition in conditions:
                    doc.add_paragraph(
                        f"{condition.get('field') or 'Not available in extracted data'} "
                        f"{condition.get('operator') or 'equals'} "
                        f"{condition.get('value') if condition.get('value') is not None else 'VALUE NOT RESOLVED'}",
                        style="List Bullet",
                    )
            else:
                doc.add_paragraph("Not available in extracted data.", style="List Bullet")

            actions = detail.get("actions", [])
            doc.add_paragraph("Price actions:").runs[0].bold = True
            if actions:
                for action in actions:
                    pieces = [
                        f"Target: {action.get('target_object') or 'Not available'} / "
                        f"{action.get('target_field') or 'Not available'}"
                    ]
                    pieces.append(
                        "Value: "
                        + (
                            str(action["value"])
                            if action.get("value") is not None
                            else "Not available in extracted data"
                        )
                    )
                    if action.get("formula"):
                        pieces.append(f"Formula: {action['formula']}")
                    if action.get("source_variable"):
                        pieces.append(f"Source: {action['source_variable']}")
                    if action.get("lookup_field"):
                        pieces.append(f"Lookup field: {action['lookup_field']}")
                    doc.add_paragraph("; ".join(pieces), style="List Bullet")
            else:
                doc.add_paragraph("Not available in extracted data.", style="List Bullet")

            for lookup in detail.get("lookup_queries", []):
                summary = "; ".join(
                    f"{key.replace('SBQQ__', '').replace('__c', '').replace('_', ' ')}: {value}"
                    for key, value in lookup.items()
                )
                doc.add_paragraph(f"Lookup query: {summary}", style="List Bullet")
            if any(
                "lookup table rows are not available" in message.lower()
                for message in detail.get("data_quality", [])
            ):
                doc.add_paragraph("Lookup table rows: Not available in extracted data.")
            if not conditions and not actions and not detail.get("lookup_queries"):
                doc.add_paragraph("Formula, values, tiers, and lookup data: Not available in extracted data.")
            for finding in detail.get("data_quality", []):
                doc.add_paragraph(f"Data quality: {finding}", style="List Bullet")

            doc.add_paragraph().paragraph_format.space_after = Pt(8)

    doc.add_paragraph().paragraph_format.space_after = Pt(12)

    # -------------------------------------------------------------
    # SECTION 7: THINGS WORTH A CLOSER LOOK
    # -------------------------------------------------------------
    doc.add_heading("7. Things Worth a Closer Look", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    doc.add_paragraph(
        "Our deterministic extraction analysis highlighted the following configuration observations. "
        "These represent setup anomalies or optimization opportunities that should be confirmed with your commercial team:"
    ).paragraph_format.space_after = Pt(6)

    quality_findings = sections.get("data_quality", [])
    observations = sections.get("observations", [])
    if not quality_findings and not observations:
        doc.add_paragraph(
            "No findings were produced. Checks run: count reconciliation, contradictory rule actions, "
            "placeholder and ID detection, product/pricebook references, bundle-rule links, rule naming, "
            "boundary-message consistency, and subscription pricing configuration."
        ).paragraph_format.space_after = Pt(6)
    for finding in quality_findings:
        severity = str(finding.get("severity") or "INFO").upper()
        title = f"{severity}: {finding.get('entity') or 'Data quality'}"
        context = finding.get("rule") or finding.get("product")
        message = finding.get("message") or "Finding details not available."
        if context:
            message = f"{context}: {message}"
        add_callout(
            doc,
            message,
            title=title,
            is_alert=severity in {"ERROR", "WARNING"},
        )
    for observation in observations:
        title = f"{observation.get('category')}: {observation.get('id')}"
        body_text = observation.get("description", "")
        names = observation.get("affected_names", [])
        if names:
            body_text += f"\n\nAffected Items: {', '.join(names[:15])}"
            if len(names) > 15:
                body_text += f" (and {len(names) - 15} more)"
        add_callout(doc, body_text, title=title, is_alert=True)

    # -------------------------------------------------------------
    # SECTION 8: QUESTIONS TO CONFIRM WITH YOUR TEAM
    # -------------------------------------------------------------
    doc.add_heading("8. Questions to Confirm with Your Team", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    doc.add_paragraph(
        "Based on our analysis of your product rules, bundles, and pricing logic, we recommend reviewing the following questions with sales and operations stakeholders:"
    ).paragraph_format.space_after = Pt(6)

    all_questions = []
    for finding in quality_findings:
        severity = str(finding.get("severity") or "").upper()
        if severity not in {"ERROR", "WARNING", "INFO"}:
            continue
        subject = finding.get("rule") or finding.get("product") or finding.get("entity") or "This setup"
        reason = finding.get("message") or "the extracted data contains an ambiguity"
        all_questions.append((
            finding.get("entity") or "Data quality",
            (
                f"Can your team confirm whether {subject} should be configured this way? "
                f"This is being asked because {reason[0].lower() + reason[1:]}"
            ),
        ))

    if not all_questions:
        for rule in sections.get("rule_analyses", []):
            rule_type = "Price Rule" if rule.get("kind") == "price" else "Product Rule"
            for question in rule.get("questions_to_confirm", []):
                all_questions.append((f"{rule_type}: {rule.get('name')}", question))
        for bundle in sections.get("bundle_analyses", []):
            for question in bundle.get("questions_to_confirm", []):
                all_questions.append((f"Bundle: {bundle.get('name')}", question))

    # Deduplicate and limit to high-value questions
    seen_q = set()
    unique_questions = []
    for topic, q in all_questions:
        if q not in seen_q:
            seen_q.add(q)
            unique_questions.append((topic, q))

    if unique_questions:
        for idx, (topic, q) in enumerate(unique_questions[:8], start=1):
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(4)
            r_num = p.add_run(f"{idx}. [{topic}] ")
            r_num.bold = True
            r_num.font.color.rgb = COLOR_SECONDARY
            p.add_run(q)
    else:
        doc.add_paragraph("No specific operational questions were raised during automated validation.").paragraph_format.space_after = Pt(4)

    doc.add_paragraph().paragraph_format.space_after = Pt(12)

    # -------------------------------------------------------------
    # OPTIONAL MIGRATION NOTES
    # -------------------------------------------------------------
    if include_migration_notes:
        doc.add_heading("Areas Needing Care During System Changes", level=1)
        doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

        doc.add_paragraph(
            "If your organization plans to update packaging, re-architect product rules, or migrate quoting platforms, "
            "the following areas require rigorous change-management:"
        ).paragraph_format.space_after = Pt(6)

        migration_points = [
            ("Summary Variable Dependencies", "Product rules evaluating summary variables require testing against quote lines with multiple quantities to prevent premature triggering."),
            ("Direct-Option Bundles", "Bundles structured without feature categories (such as flat-option equipment) should be refactored into clear feature tabs to improve quoting usability."),
            ("Unpriced Products", "Catalog items lacking active pricebook entries cannot be quoted directly and should either be assigned prices or retired if obsolete."),
            ("Selection Automation", "Auto-add and auto-remove selection rules should be verified in staging to ensure complementary products are not added multiple times.")
        ]

        for topic, detail in migration_points:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(4)
            p.add_run(f"• {topic}: ").bold = True
            p.add_run(detail)

        doc.add_paragraph().paragraph_format.space_after = Pt(12)

    # -------------------------------------------------------------
    # SCOPE AND LIMITATIONS
    # -------------------------------------------------------------
    doc.add_heading("9. Scope and Limitations", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY
    doc.add_paragraph(
        "This extraction covers the CPQ catalog and records listed in the attached outputs. "
        "It does not inspect all Salesforce automation or custom quoting behavior."
    )
    limitations = [
        "Custom calculator plugin scripts (QCP) and Apex code.",
        "Flows, approval rules, guided selling, and custom actions.",
        "Discount schedules and contracted prices not represented in the extracted records.",
        "Lookup-table rows and external data sources not included in the extraction.",
    ]
    for limitation in limitations:
        doc.add_paragraph(limitation, style="List Bullet")
    doc.add_paragraph(
        "These excluded components may affect quote configuration, validation, and pricing behavior."
    )

    # -------------------------------------------------------------
    # SECTION 10: APPENDIX: TECHNICAL REFERENCE
    # -------------------------------------------------------------
    doc.add_heading("10. Appendix: Technical Reference", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    doc.add_paragraph(
        "This appendix provides pointers to the underlying technical dataset and raw extraction files for Salesforce administrators and architects:"
    ).paragraph_format.space_after = Pt(6)

    metadata = facts.get("metadata") or {}
    metadata_rows = [
        ("Organization ID", "Available in extraction metadata; not displayed here to avoid exposing record identifiers."),
        ("Sandbox", metadata.get("is_sandbox", "Not available in extracted data")),
        ("Instance", metadata.get("instance", "Not available in extracted data")),
        ("Extracted at (UTC)", metadata.get("extraction_timestamp_utc", "Not available in extracted data")),
        ("CPQ package version", metadata.get("cpq_package_version", "Not available in extracted data")),
        ("Extractor version", metadata.get("extractor_version", "Not available in extracted data")),
        ("JSON schema version", metadata.get("json_schema_version", "Not available in extracted data")),
    ]
    metadata_table = doc.add_table(rows=len(metadata_rows), cols=2)
    set_table_borders(metadata_table)
    for idx, (label, value) in enumerate(metadata_rows):
        metadata_table.cell(idx, 0).text = label
        metadata_table.cell(idx, 1).text = str(value)
    app_items = [
        ("Excel Workbook Export", "cpq_extract.xlsx", "Normalized workbook containing the extracted records, data-quality findings, metadata, and parent links."),
        ("Nested JSON Extract", "cpq_extract.json", "Full nested JSON schema representing the active CPQ hierarchy."),
        ("Streaming NDJSON", "cpq_extract.ndjson", "Newline-delimited JSON stream for scalable data loading and ETL pipelines."),
        ("Raw CSV Folder", "raw/", "Individual sObject CSV tables extracted directly via Salesforce Bulk API 2.0 / REST.")
    ]

    a_tbl = doc.add_table(rows=len(app_items) + 1, cols=3)
    a_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    a_tbl.autofit = False
    a_tbl.columns[0].width = Inches(2.2)
    a_tbl.columns[1].width = Inches(1.8)
    a_tbl.columns[2].width = Inches(2.5)
    set_table_borders(a_tbl)

    for c, h in zip(a_tbl.rows[0].cells, ["Artifact", "File / Folder Name", "Technical Purpose"]):
        set_cell_background(c, HEX_LIGHT_BG)
        set_cell_margins(c, 80, 80, 120, 120)
        c.paragraphs[0].add_run(h).bold = True

    for idx, (art, fn, purp) in enumerate(app_items, start=1):
        row = a_tbl.rows[idx]
        set_cell_margins(row.cells[0], 60, 60, 120, 120)
        set_cell_margins(row.cells[1], 60, 60, 120, 120)
        set_cell_margins(row.cells[2], 60, 60, 120, 120)
        row.cells[0].paragraphs[0].add_run(art).bold = True
        row.cells[1].paragraphs[0].add_run(fn)
        row.cells[2].paragraphs[0].add_run(purp)

    if metadata.get("org_id"):
        doc.core_properties.identifier = str(metadata["org_id"])
    doc.save(output_filepath)
    return output_filepath
