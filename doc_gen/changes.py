"""Compares two extracted CPQ snapshots without exposing record IDs."""

import re

from doc_gen.facts import load_extraction_data


SF_ID_RE = re.compile(
    r"^(?=[a-zA-Z0-9]*[a-zA-Z])(?=[a-zA-Z0-9]*\d)[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$"
)
SYSTEM_FIELDS = {
    "Id", "OwnerId", "CreatedById", "CreatedDate", "LastModifiedById",
    "LastModifiedDate", "SystemModstamp", "IsDeleted", "LastViewedDate",
    "LastReferencedDate",
}


def _label(record, name_field="Name"):
    code = record.get("ProductCode")
    name = record.get(name_field) or code or "Unnamed record"
    return f"{code} - {name}" if code and name != code else str(name)


def _normalize(value):
    if isinstance(value, dict):
        return {
            key: _normalize(child)
            for key, child in value.items()
            if key not in SYSTEM_FIELDS and not key.lower().endswith("id")
        }
    if isinstance(value, list):
        return [_normalize(child) for child in value]
    if isinstance(value, str) and SF_ID_RE.fullmatch(value.strip()):
        return None
    return value


def _compare_records(current_records, previous_records, key_fn, label_fn):
    current_by_key = {key_fn(record): record for record in current_records if key_fn(record)}
    previous_by_key = {key_fn(record): record for record in previous_records if key_fn(record)}
    current_keys = set(current_by_key)
    previous_keys = set(previous_by_key)
    added = [label_fn(current_by_key[key]) for key in sorted(current_keys - previous_keys)]
    removed = [label_fn(previous_by_key[key]) for key in sorted(previous_keys - current_keys)]
    modified = []
    for key in sorted(current_keys & previous_keys):
        if _normalize(current_by_key[key]) != _normalize(previous_by_key[key]):
            modified.append(label_fn(current_by_key[key]))
    return {"added": added, "removed": removed, "modified": modified}


def build_change_summary(current_data, previous_extract):
    """Returns added, removed, and modified product, rule, and price labels."""
    current = load_extraction_data(current_data)
    previous = load_extraction_data(previous_extract)

    products = _compare_records(
        [
            {key: value for key, value in product.items() if key != "pricebook_entries"}
            for product in current.get("Product", [])
        ],
        [
            {key: value for key, value in product.items() if key != "pricebook_entries"}
            for product in previous.get("Product", [])
        ],
        lambda record: record.get("Id") or record.get("ProductCode") or record.get("Name"),
        _label,
    )

    current_rules = current.get("ProductRules", []) + current.get("PriceRules", [])
    previous_rules = previous.get("ProductRules", []) + previous.get("PriceRules", [])
    rules = _compare_records(
        current_rules,
        previous_rules,
        lambda record: str(
            record.get("Id")
            or "|".join((
                record.get("Name") or "Unnamed rule",
                record.get("SBQQ__Type__c")
                or record.get("SBQQ__TargetObject__c")
                or "unspecified",
            ))
        ),
        lambda record: record.get("Name") or "Unnamed rule",
    )

    def price_rows(data):
        rows = []
        for product in data.get("Product", []):
            product_label = _label(product)
            product_id = product.get("Id")
            for entry in product.get("pricebook_entries", []):
                row = dict(entry)
                row["_product_label"] = product_label
                row["_product_id"] = product_id
                rows.append(row)
        return rows

    def price_key(record):
        return (
            record.get("_product_id") or record.get("_product_label"),
            record.get("Pricebook2Id"),
            record.get("CurrencyIsoCode"),
        )

    def price_label(record):
        currency = record.get("CurrencyIsoCode") or "currency unavailable"
        return f"{record.get('_product_label')} ({currency})"

    prices = _compare_records(
        price_rows(current),
        price_rows(previous),
        price_key,
        price_label,
    )

    return {
        "products": products,
        "rules": rules,
        "prices": prices,
        "totals": {
            category: {
                action: len(details[action])
                for action in ("added", "removed", "modified")
            }
            for category, details in (
                ("products", products),
                ("rules", rules),
                ("prices", prices),
            )
        },
    }
