# doc_gen/validate.py
#
# Validates synthesized content, checks source data matches, flags unregistered numbers,
# and performs the mandatory REGEX check to block rendering if Salesforce IDs or SBQQ__ leak.

import os
import re
import json
import logging

logger = logging.getLogger(__name__)

# Regex for SBQQ__ API prefixes (forbidden in document body)
REGEX_SBQQ = re.compile(r"SBQQ__\w*", re.IGNORECASE)

# Regex for 15 or 18 character Salesforce alphanumeric IDs (containing both letters and digits)
REGEX_SF_ID = re.compile(r"\b(?=[a-zA-Z0-9]*[a-zA-Z])(?=[a-zA-Z0-9]*\d)[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?\b")


def extract_all_text_from_sections(sections: dict) -> list[tuple[str, str]]:
    """Extracts all text fields from sections for comprehensive scanning. Returns list of (context, text)."""
    text_entries = []

    for idx, p in enumerate(sections.get("executive_summary", [])):
        text_entries.append((f"Executive Summary p{idx+1}", p))

    for idx, p in enumerate(sections.get("bundles_introduction", [])):
        text_entries.append((f"Bundles Introduction p{idx+1}", p))

    for idx, p in enumerate(sections.get("rules_introduction", [])):
        text_entries.append((f"Rules Introduction p{idx+1}", p))

    for idx, p in enumerate(sections.get("pricing_introduction", [])):
        text_entries.append((f"Pricing Introduction p{idx+1}", p))

    for b in sections.get("bundle_analyses", []):
        b_name = b.get("name", "Bundle")
        text_entries.append((f"Bundle '{b_name}' Summary", b.get("one_line_summary", "")))
        text_entries.append((f"Bundle '{b_name}' How Sold", b.get("how_it_is_sold", "")))
        text_entries.append((f"Bundle '{b_name}' Complexity Reason", b.get("complexity_reason", "")))
        for c in b.get("choices_offered", []):
            text_entries.append((f"Bundle '{b_name}' Feature '{c.get('feature')}'", c.get("what_customer_chooses", "")))

    for r in sections.get("rule_analyses", []):
        r_name = r.get("name", "Rule")
        text_entries.append((f"Rule '{r_name}' Summary", r.get("one_line_summary", "")))
        text_entries.append((f"Rule '{r_name}' When Applies", r.get("when_it_applies", "")))
        text_entries.append((f"Rule '{r_name}' What It Does", r.get("what_it_does", "")))
        if r.get("inferred_purpose"):
            text_entries.append((f"Rule '{r_name}' Inferred Purpose", r.get("inferred_purpose", "")))

    return text_entries


def validate_document_content(facts: dict, sections: dict, raw_data: dict, run_dir: str = None) -> dict:
    """
    Performs comprehensive validation:
      1. Mandatory Regex Check: checks for SBQQ__ and Salesforce IDs in main body.
      2. Source Data Cross-check: verifies related_items and message_shown_to_user.
      3. Digit Sequence Verification: flags unregistered numeric sequences.
    Writes validation_report.json and returns the report.
    """
    text_entries = extract_all_text_from_sections(sections)

    regex_violations = []
    # 1. Mandatory Regex Check
    for context, text in text_entries:
        if not text:
            continue
        sbqq_matches = REGEX_SBQQ.findall(text)
        if sbqq_matches:
            for m in sbqq_matches:
                regex_violations.append({
                    "type": "Forbidden SBQQ__ API Name",
                    "context": context,
                    "matched": m,
                })

        id_matches = REGEX_SF_ID.findall(text)
        if id_matches:
            for m in id_matches:
                regex_violations.append({
                    "type": "Forbidden Salesforce ID",
                    "context": context,
                    "matched": m,
                })

    passed_regex = len(regex_violations) == 0

    # 2. Source Data Cross-Check
    known_product_names = set(p.get("Name") for p in raw_data.get("Product", []) if p.get("Name"))
    known_skus = set(p.get("ProductCode") for p in raw_data.get("Product", []) if p.get("ProductCode"))
    known_combined = set(f"{p.get('Name')} ({p.get('ProductCode')})" for p in raw_data.get("Product", []) if p.get("Name") and p.get("ProductCode"))
    known_rule_names = set(r.get("Name") for r in raw_data.get("ProductRules", []) if r.get("Name")) | set(r.get("Name") for r in raw_data.get("PriceRules", []) if r.get("Name"))
    known_error_messages = set(r.get("SBQQ__ErrorMessage__c") for r in raw_data.get("ProductRules", []) if r.get("SBQQ__ErrorMessage__c"))
    known_summary_var_names = set(sv.get("Name") for sv in raw_data.get("SummaryVariables", []) if sv.get("Name"))

    all_known_names = known_product_names | known_skus | known_combined | known_rule_names | known_summary_var_names

    source_crosscheck_warnings = []
    for r in sections.get("rule_analyses", []):
        r_name = r.get("name", "Rule")
        msg = r.get("message_shown_to_user")
        if msg and msg not in known_error_messages:
            # Check if it's a substring
            found_sub = any(msg in actual_msg for actual_msg in known_error_messages)
            if not found_sub:
                source_crosscheck_warnings.append({
                    "rule": r_name,
                    "field": "message_shown_to_user",
                    "value": msg,
                    "issue": "Message does not match any exact error message in source data."
                })

        for item in r.get("related_items", []):
            if item and item not in all_known_names:
                source_crosscheck_warnings.append({
                    "rule": r_name,
                    "field": "related_items",
                    "value": item,
                    "issue": "Item name was not found in active catalog product or rule names."
                })

    # 3. Digit Sequence Verification
    # Collect all legitimate factual numbers
    legitimate_numbers = set()
    for k, v in facts.items():
        if isinstance(v, int):
            legitimate_numbers.add(str(v))
    for k, v in facts.get("products_by_family", {}).items():
        if isinstance(v, int):
            legitimate_numbers.add(str(v))
    for k, v in facts.get("product_rules", {}).get("by_type", {}).items():
        if isinstance(v, int):
            legitimate_numbers.add(str(v))

    # Add numbers from hardware sizes (e.g. 13, 15, 4, 6, 8, 16, 128, 200, 24)
    common_specs = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "24", "128", "256", "512", "200"}
    legitimate_numbers |= common_specs

    unregistered_digits = []
    for context, text in text_entries:
        digits = re.findall(r"\b\d+\b", text)
        for d in digits:
            if d not in legitimate_numbers:
                unregistered_digits.append({
                    "context": context,
                    "digit": d,
                    "issue": "Numeric digit sequence not recognized as a registered catalog fact."
                })

    report = {
        "can_render": passed_regex,
        "regex_check": {
            "passed": passed_regex,
            "violations_count": len(regex_violations),
            "violations": regex_violations,
        },
        "source_data_check": {
            "warnings_count": len(source_crosscheck_warnings),
            "warnings": source_crosscheck_warnings,
        },
        "numeric_digits_check": {
            "unregistered_count": len(unregistered_digits),
            "unregistered_digits": unregistered_digits[:20],  # cap sample list
        },
    }

    if run_dir:
        doc_dir = os.path.join(run_dir, "doc")
        os.makedirs(doc_dir, exist_ok=True)
        report_file = os.path.join(doc_dir, "validation_report.json")
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

    return report
