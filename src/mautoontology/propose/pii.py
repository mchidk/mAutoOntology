"""PII classification from column names and value shapes.

Classes follow common privacy practice:
* ``direct_identifier`` - identifies a person on its own (national id, passport, card number);
* ``contact`` - reaches a person (email, phone, street address);
* ``sensitive`` - special-category or secret data (health, religion, salary, password, IP);
* ``quasi_identifier`` - identifies in combination (name, birth date, postal code, gender).

Names are tokenised first (``CustomerEmail`` -> ``customer_email``), so camelCase
and PascalCase columns are classified like snake_case ones. Value shapes are a
second line of defence: profiling drops any value domain that looks like emails,
phone numbers or personal names, even when the column name gave no hint.
Columns classified as PII never get sample values collected or written.
"""

from __future__ import annotations

import re

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("direct_identifier", re.compile(r"(^|_)(ssn|cpr|social_security|national_id|nin|passport|tax_id|tin|driver_?licen[cs]e|iban|credit_?card|card_?number|cc_?num|pan)(_|$)")),
    ("sensitive", re.compile(r"(^|_)(password|passwd|pwd|secret|token|api_?key|salary|wage|income|health|diagnosis|religion|ethnicity|race|sexual|ip_?address|ip_?addr|ipv4|ipv6|biometric|medical)(_|$)")),
    ("contact", re.compile(r"(^|_)(e_?mail|email_?address|mail_?address|phone|phone_?number|mobile|cell|fax|telephone|tel|street|address_?line\d?|addr\d?|home_?address)(_|$)")),
    ("quasi_identifier", re.compile(r"(^|_)(name|first_?name|last_?name|full_?name|surname|given_?name|maiden_?name|user_?name|username|contact_?name|customer_?name|birth_?date|date_?of_?birth|dob|birthday|age|gender|sex|postal_?code|zip_?code|zip|postcode|latitude|longitude|lat|lon|lng)(_|$)")),
]
#: Name tokens that look personal but describe things, not people.
_NOT_PERSONAL = re.compile(r"(^|_)(product|category|company|brand|file|table|column|schema|database|host|domain|event|campaign|city|country|region|store|shop|item|sku|plan|tier|segment|status|type|display)_name$")

_EMAIL_VALUE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)
_PHONE_VALUE = re.compile(r"^\+?[\d\s().-]{7,}$")
_PERSON_NAME_VALUE = re.compile(r"^[A-Z][a-z]+(?:[ '-][A-Z][a-z]+)+$")


def tokenize(column: str) -> str:
    """``CustomerEmail`` / ``customerEmail`` / ``customer-email`` -> ``customer_email``."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", column)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


#: Substrings safe to match inside concatenated lowercase names (``customeremail``).
_COMPACT = [
    ("direct_identifier", ("passport", "creditcard", "cardnumber", "socialsecurity", "nationalid", "driverslicense")),
    ("sensitive", ("password", "passwd", "ipaddress", "salary", "apikey", "diagnosis")),
    ("contact", ("email", "phone", "mobilenumber", "streetaddress", "homeaddress")),
    ("quasi_identifier", ("firstname", "lastname", "fullname", "surname", "username", "birthdate", "dateofbirth", "birthday")),
]
_NOT_PERSONAL_PREFIXES = ("product", "category", "company", "brand", "file", "table", "column", "schema", "database", "host",
                          "domain", "event", "campaign", "city", "country", "region", "store", "shop", "item", "sku", "plan",
                          "tier", "segment", "status", "type", "display")


def classify_name(column: str) -> str:
    """Return the PII class implied by a column name, or ``none``."""
    name = tokenize(column)
    if _NOT_PERSONAL.search(name):
        return "none"
    for pii_class, pattern in _RULES:
        if pattern.search(name):
            return pii_class
    compact = name.replace("_", "")  # catalogs often lowercase camelCase: CustomerEmail -> customeremail
    for pii_class, needles in _COMPACT:
        if any(needle in compact for needle in needles):
            return pii_class
    if compact.endswith("name") and len(compact) > 4 and not compact.startswith(_NOT_PERSONAL_PREFIXES):
        return "quasi_identifier"
    return "none"


def classify_values(values: list[object]) -> str:
    """Detect PII from value shapes (emails, phone numbers, personal names)."""
    texts = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not texts:
        return "none"
    threshold = max(1, len(texts) // 2)
    if sum(bool(_EMAIL_VALUE.match(t)) for t in texts) >= threshold:
        return "contact"
    if sum(bool(_PHONE_VALUE.match(t)) and sum(ch.isdigit() for ch in t) >= 7 for t in texts) >= threshold:
        return "contact"
    if len(texts) >= 3 and sum(bool(_PERSON_NAME_VALUE.match(t)) for t in texts) >= max(3, threshold):
        return "quasi_identifier"
    return "none"
