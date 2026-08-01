import io

import pytest
from PIL import Image

from avito_crm.errors import PhoneNotFoundError
from avito_crm.ocr import PhoneOcr


def test_ocr_uses_consensus_without_real_tesseract(monkeypatch):
    ocr = PhoneOcr(min_agreement=2)
    monkeypatch.setattr(
        ocr.pytesseract,
        "image_to_string",
        lambda *_args, **_kwargs: "+7 (999) 123-45-67",
    )
    image = Image.new("RGB", (200, 50), "white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    result = ocr.read_png(buffer.getvalue())

    assert result.phone == "+79991234567"
    assert result.source == "ocr"


def test_viewport_fallback_uses_proven_crops_and_psm6(monkeypatch):
    ocr = PhoneOcr(min_agreement=2)
    configs = []

    def fake_ocr(*_args, **kwargs):
        configs.append(kwargs["config"])
        return "8 999 123-45-67"

    monkeypatch.setattr(ocr.pytesseract, "image_to_string", fake_ocr)
    image = Image.new("RGB", (1280, 720), "white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    result = ocr.read_viewport_png(buffer.getvalue())

    assert result.phone == "+79991234567"
    assert result.source == "ocr-viewport-crop"
    assert configs and all("--psm 6" in config for config in configs)


def test_avito_screen_ocr_scans_proven_regions_and_requires_formatting(monkeypatch):
    ocr = PhoneOcr(min_agreement=2)
    monkeypatch.setattr(
        ocr.pytesseract,
        "image_to_string",
        lambda *_args, **_kwargs: "Временный номер: 8 999 123-45-67",
    )
    image = Image.new("RGB", (1280, 720), "white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    result = ocr.read_avito_screen_png(buffer.getvalue())

    assert result.phone == "+79991234567"
    assert result.source == "ocr-avito-inline-right"


def test_avito_screen_ocr_rejects_unformatted_digit_sequences(monkeypatch):
    ocr = PhoneOcr(min_agreement=2)
    monkeypatch.setattr(
        ocr.pytesseract,
        "image_to_string",
        lambda *_args, **_kwargs: "цена 1000, объявление 8066051247, ошибка 74010003462",
    )
    image = Image.new("RGB", (1280, 720), "white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    with pytest.raises(PhoneNotFoundError, match="форматированный"):
        ocr.read_avito_screen_png(buffer.getvalue())
