import pytest

from avito_crm.errors import InvalidListingError
from avito_crm.phone import canonical_avito_url, extract_phones, mask_phone, normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8 (999) 123-45-67", "+79991234567"),
        ("+7 999 123 45 67", "+79991234567"),
        ("9991234567", "+79991234567"),
        ("123", None),
        ("+1 202 555 0100", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_extract_phone_from_noisy_ocr_text():
    assert extract_phones("Тел. 8-999-123-45-67, звоните") == ["+79991234567"]


def test_mask_phone():
    assert mask_phone("+79991234567") == "+79***4567"


def test_canonical_avito_url_drops_tracking():
    value = canonical_avito_url("http://www.avito.ru/moskva/item_123456789?utm_source=test#x")
    assert value == "https://www.avito.ru/moskva/item_123456789"


@pytest.mark.parametrize("url", ["https://example.com/x", "file:///tmp/x", "https://avito.ru/"])
def test_canonical_avito_url_rejects_other_targets(url):
    with pytest.raises(InvalidListingError):
        canonical_avito_url(url)
