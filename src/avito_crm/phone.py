from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from avito_crm.errors import InvalidListingError

PHONE_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\+?7|8)?(?:[\s()\-.]*\d){10,11}(?!\d)")


def normalize_phone(value: str) -> str | None:
    """Normalize a Russian phone number to +7XXXXXXXXXX."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        return None
    if digits[1] not in {"3", "4", "5", "6", "7", "8", "9"}:
        return None
    return "+" + digits


def extract_phones(text: str) -> list[str]:
    found: list[str] = []
    for match in PHONE_CANDIDATE_RE.finditer(text or ""):
        normalized = normalize_phone(match.group(0))
        if normalized and normalized not in found:
            found.append(normalized)
    if not found:
        normalized = normalize_phone(text or "")
        if normalized:
            found.append(normalized)
    return found


def mask_phone(phone: str) -> str:
    normalized = normalize_phone(phone)
    if not normalized:
        return "***"
    return f"{normalized[:3]}***{normalized[-4:]}"


def canonical_avito_url(value: str) -> str:
    candidate = (value or "").strip()
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not (
        host == "avito.ru" or host.endswith(".avito.ru")
    ):
        raise InvalidListingError("Разрешены только http(s)-ссылки на avito.ru")
    if not parsed.path or parsed.path == "/":
        raise InvalidListingError("Ссылка не похожа на страницу объявления Avito")
    netloc = host
    if parsed.port and parsed.port not in {80, 443}:
        netloc = f"{host}:{parsed.port}"
    return urlunsplit(("https", netloc, parsed.path.rstrip("/"), "", ""))
