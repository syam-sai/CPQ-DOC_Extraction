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
    r_badge = title_p.add_run("[ DRAFT - FOR DISCUSSION PURPOSES ]\n\n")
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
        ("Configurable Bundles", f"{facts.get('total_bundles', 0):,}", "Products structured with component selections"),
        ("Standalone Products", f"{facts.get('standalone_products', 0):,}", "Items sold directly without sub-components"),
        ("Feature Categories", f"{facts.get('total_features', 0):,}", "Organizational sections across all bundles"),
        ("Selectable Component Options", f"{facts.get('total_options', 0):,}", "Total configurable choices available to reps"),
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
    doc.add_heading("Product Catalog by Family", level=2)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_SECONDARY

    fam_data = facts.get("products_by_family", {})
    tot_p = facts.get("active_products", 1) or 1

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

    for b in sections.get("bundle_analyses", []):
        b_name = b.get("name", "Product Bundle")
        doc.add_heading(b_name, level=3)
        doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

        doc.add_paragraph(b.get("one_line_summary", "")).paragraph_format.space_after = Pt(4)
        doc.add_paragraph(f"Commercial Packaging: {b.get('how_it_is_sold', '')}").paragraph_format.space_after = Pt(4)

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

        if b.get("ungrouped_options"):
            doc.add_paragraph(f"Direct Add-on Options: {', '.join(b['ungrouped_options'])}").paragraph_format.space_after = Pt(4)

        if b.get("rules_that_affect_it"):
            doc.add_paragraph(f"Automated Rules Affecting This Bundle: {', '.join(b['rules_that_affect_it'])}").paragraph_format.space_after = Pt(4)

        doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # -------------------------------------------------------------
    # SECTION 5: PRODUCT RULES AND PRICE RULES IN PLAIN ENGLISH
    # -------------------------------------------------------------
    doc.add_heading("5. Product Rules and Price Rules in Plain English", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    for p_text in sections.get("rules_introduction", []):
        doc.add_paragraph(p_text).paragraph_format.space_after = Pt(6)

    prod_rules = sections.get("product_rule_analyses")
    if not prod_rules:
        prod_rules = [r for r in sections.get("rule_analyses", []) if r.get("kind") != "price"]

    price_rules = sections.get("price_rule_analyses")
    if not price_rules:
        price_rules = [r for r in sections.get("rule_analyses", []) if r.get("kind") == "price"]

    # --- Part A: Product Rules ---
    doc.add_heading("Product Rules", level=2)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_SECONDARY
    doc.add_paragraph(
        "Product rules safeguard configuration integrity by validating sales selections, "
        "preventing incompatible combinations, and automatically adding or removing required components:"
    ).paragraph_format.space_after = Pt(6)

    for r in prod_rules:
        r_name = r.get("name", "Product Rule")
        r_kind = (r.get("kind") or "Validation").capitalize()
        doc.add_heading(f"{r_name} ({r_kind})", level=3)
        doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

        doc.add_paragraph(f"Overview: {r.get('one_line_summary', '')}").paragraph_format.space_after = Pt(3)
        doc.add_paragraph(f"When it triggers: {r.get('when_it_applies', '')}").paragraph_format.space_after = Pt(3)
        doc.add_paragraph(f"System behavior: {r.get('what_it_does', '')}").paragraph_format.space_after = Pt(3)

        msg = r.get("message_shown_to_user")
        if msg:
            add_callout(doc, f'"{msg}"', title="Notification Displayed to Sales Representative:")

        if r.get("inferred_purpose"):
            doc.add_paragraph(f"Business Intent: {r['inferred_purpose']}").paragraph_format.space_after = Pt(3)

        if r.get("related_items"):
            doc.add_paragraph(f"Related Catalog Items: {', '.join(r['related_items'])}").paragraph_format.space_after = Pt(3)

        doc.add_paragraph().paragraph_format.space_after = Pt(8)

    # --- Part B: Price Rules ---
    if price_rules:
        doc.add_heading("Price Rules", level=2)
        doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_SECONDARY
        doc.add_paragraph(
            "Price rules automate quotation math by dynamically calculating list prices "
            "and applying approved package discounts during quote calculation cycles:"
        ).paragraph_format.space_after = Pt(6)

        for pr in price_rules:
            pr_name = pr.get("name", "Price Rule")
            doc.add_heading(f"{pr_name}", level=3)
            doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

            doc.add_paragraph(f"Commercial Impact: {pr.get('one_line_summary', '')}").paragraph_format.space_after = Pt(3)
            doc.add_paragraph(f"Calculation Trigger: {pr.get('when_it_applies', '')}").paragraph_format.space_after = Pt(3)
            doc.add_paragraph(f"Pricing Formula & Behavior: {pr.get('what_it_does', '')}").paragraph_format.space_after = Pt(3)

            if pr.get("inferred_purpose"):
                doc.add_paragraph(f"Business Intent: {pr['inferred_purpose']}").paragraph_format.space_after = Pt(3)

            if pr.get("related_items"):
                doc.add_paragraph(f"Catalog Products Affected: {', '.join(pr['related_items'])}").paragraph_format.space_after = Pt(3)

            doc.add_paragraph().paragraph_format.space_after = Pt(8)

    # -------------------------------------------------------------
    # SECTION 6: HOW PRICING WORKS
    # -------------------------------------------------------------
    doc.add_heading("6. How Pricing Works", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    for p_text in sections.get("pricing_introduction", []):
        doc.add_paragraph(p_text).paragraph_format.space_after = Pt(6)

    pricing_models = [
        ("Standard List Pricing", f"{facts.get('total_pricebook_entries', 0):,} items priced via standard catalog price books."),
        ("Tiered Block Pricing", f"{facts.get('total_block_prices', 0):,} products configured with quantity bracket tiers (e.g. hourly packages or user volume brackets)."),
        ("Recurring Service Dimensions", f"{facts.get('total_dimensions', 0):,} services configured with multi-year recurring billing schedules.")
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
    price_rules = sections.get("price_rule_analyses", [])
    if not price_rules:
        price_rules = [r for r in sections.get("rule_analyses", []) if r.get("kind") == "price"]

    if price_rules:
        doc.add_heading("Automated Dynamic Pricing Rules", level=2)
        doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_SECONDARY
        doc.add_paragraph(
            "In addition to standard price book rates, your catalog enforces dynamic pricing through automated price rules. "
            "These rules eliminate manual calculation errors by evaluating quote lines in real time during the quotation cycle:"
        ).paragraph_format.space_after = Pt(6)

        for pr in price_rules:
            pr_name = pr.get("name", "Pricing Rule")
            doc.add_heading(f"{pr_name}", level=3)
            doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

            doc.add_paragraph(f"Commercial Impact: {pr.get('one_line_summary', '')}").paragraph_format.space_after = Pt(3)
            doc.add_paragraph(f"Calculation Trigger: {pr.get('when_it_applies', '')}").paragraph_format.space_after = Pt(3)
            doc.add_paragraph(f"Pricing Behavior & Formula: {pr.get('what_it_does', '')}").paragraph_format.space_after = Pt(3)

            if pr.get("inferred_purpose"):
                doc.add_paragraph(f"Business Intent: {pr['inferred_purpose']}").paragraph_format.space_after = Pt(3)

            if pr.get("related_items"):
                doc.add_paragraph(f"Catalog Products Affected: {', '.join(pr['related_items'])}").paragraph_format.space_after = Pt(3)

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

    observations = sections.get("observations", [])
    if not observations:
        doc.add_paragraph("No significant configuration anomalies or data gaps were detected in this extraction.").paragraph_format.space_after = Pt(6)
    else:
        for obs in observations:
            title = f"{obs.get('category')}: {obs.get('id')}"
            desc = obs.get("description", "")
            names = obs.get("affected_names", [])
            body_text = desc
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
    for r in sections.get("rule_analyses", []):
        r_type = "Price Rule" if r.get("kind") == "price" else "Product Rule"
        for q in r.get("questions_to_confirm", []):
            all_questions.append((f"{r_type}: {r.get('name')}", q))

    for b in sections.get("bundle_analyses", []):
        for q in b.get("questions_to_confirm", []):
            all_questions.append((f"Bundle: {b.get('name')}", q))

    # Deduplicate and limit to high-value questions
    seen_q = set()
    unique_questions = []
    for topic, q in all_questions:
        if q not in seen_q:
            seen_q.add(q)
            unique_questions.append((topic, q))

    if unique_questions:
        for idx, (topic, q) in enumerate(unique_questions[:15], start=1):
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
    # SECTION 9: APPENDIX: TECHNICAL REFERENCE
    # -------------------------------------------------------------
    doc.add_heading("9. Appendix: Technical Reference", level=1)
    doc.paragraphs[-1].runs[0].font.color.rgb = COLOR_PRIMARY

    doc.add_paragraph(
        "This appendix provides pointers to the underlying technical dataset and raw extraction files for Salesforce administrators and architects:"
    ).paragraph_format.space_after = Pt(6)

    app_items = [
        ("Excel Workbook Export", "cpq_extract.xlsx", "Comprehensive 21-tab normalized workbook containing raw records, Parent link IDs, and rollup formulas."),
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

    doc.save(output_filepath)
    return output_filepath

