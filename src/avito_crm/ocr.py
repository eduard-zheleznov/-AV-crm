from __future__ import annotations

import io
from collections import Counter
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from avito_crm.errors import ConfigurationError, PhoneNotFoundError
from avito_crm.models import PhoneResult
from avito_crm.phone import extract_formatted_phones, extract_phones


class PhoneOcr:
    def __init__(self, tesseract_cmd: str = "", min_agreement: int = 2) -> None:
        try:
            import pytesseract
        except ImportError as exc:
            raise ConfigurationError("Не установлен pytesseract") from exc
        self.pytesseract = pytesseract
        if tesseract_cmd:
            self.pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        self.min_agreement = max(1, min_agreement)

    def check_available(self) -> str:
        try:
            return str(self.pytesseract.get_tesseract_version())
        except Exception as exc:
            raise ConfigurationError(
                "Tesseract OCR не найден. Установите его или задайте TESSERACT_CMD."
            ) from exc

    def read_png(
        self, png: bytes, artifact_path: Path | None = None, *, psm: int = 7
    ) -> PhoneResult:
        try:
            image = Image.open(io.BytesIO(png)).convert("RGB")
        except Exception as exc:
            raise PhoneNotFoundError(f"Не удалось открыть изображение номера: {exc}") from exc
        if artifact_path:
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(artifact_path)

        readings: list[tuple[str, str]] = []
        for variant_name, variant in self._variants(image):
            try:
                text = self.pytesseract.image_to_string(
                    variant,
                    config=f"--psm {psm} -c tessedit_char_whitelist=+0123456789()- ",
                    lang="eng",
                ).strip()
            except Exception as exc:
                raise ConfigurationError(f"Ошибка запуска Tesseract OCR: {exc}") from exc
            for phone in extract_phones(text):
                readings.append((phone, f"{variant_name}: {text}"))

        if not readings:
            raise PhoneNotFoundError("OCR не распознал российский номер телефона")
        counts = Counter(phone for phone, _ in readings)
        phone, votes = counts.most_common(1)[0]
        if votes < self.min_agreement:
            raise PhoneNotFoundError(
                f"OCR дал недостаточно согласованный результат: {votes} вариант(а)"
            )
        raw = next(raw for candidate, raw in readings if candidate == phone)
        return PhoneResult(
            phone=phone,
            source="ocr",
            confidence=min(1.0, votes / max(self.min_agreement + 1, len(readings))),
            raw_text=raw,
        )

    def read_viewport_png(self, png: bytes) -> PhoneResult:
        """Fallback crops proven by the user's existing full-viewport recognizer."""
        try:
            image = Image.open(io.BytesIO(png)).convert("RGB")
        except Exception as exc:
            raise PhoneNotFoundError(f"Не удалось открыть скриншот страницы: {exc}") from exc

        errors: list[str] = []
        for top, bottom in ((0.18, 0.40), (0.25, 0.70)):
            width, height = image.size
            region = image.crop(
                (
                    int(width * 0.15),
                    int(height * top),
                    int(width * 0.85),
                    int(height * bottom),
                )
            )
            if region.width > 900:
                scale = 900 / region.width
                region = region.resize(
                    (900, max(1, int(region.height * scale))), Image.Resampling.LANCZOS
                )
            buffer = io.BytesIO()
            region.save(buffer, format="PNG")
            try:
                result = self.read_png(buffer.getvalue(), psm=6)
                result.source = "ocr-viewport-crop"
                return result
            except PhoneNotFoundError as exc:
                errors.append(str(exc))
        detail = errors[-1] if errors else "области номера пусты"
        raise PhoneNotFoundError(f"OCR proven-crop не распознал номер: {detail}")

    def read_avito_screen_png(self, png: bytes) -> PhoneResult:
        """Recognize only formatted phones in proven inline and modal screen regions."""
        try:
            image = Image.open(io.BytesIO(png)).convert("RGB")
        except Exception as exc:
            raise PhoneNotFoundError(f"Не удалось открыть снимок экрана: {exc}") from exc

        regions = (
            ("inline-right", (0.42, 0.22, 0.76, 0.58)),
            ("legacy-top", (0.15, 0.18, 0.85, 0.40)),
            ("legacy-lower", (0.15, 0.25, 0.85, 0.70)),
        )
        ambiguous = False
        for name, (left_rel, top_rel, right_rel, bottom_rel) in regions:
            width, height = image.size
            region = image.crop(
                (
                    int(width * left_rel),
                    int(height * top_rel),
                    int(width * right_rel),
                    int(height * bottom_rel),
                )
            )
            if region.width > 900:
                scale = 900 / region.width
                region = region.resize(
                    (900, max(1, int(region.height * scale))), Image.Resampling.LANCZOS
                )
            text = self.pytesseract.image_to_string(
                region.convert("L"),
                lang="eng",
                config="--psm 6",
            ).strip()
            phones = extract_formatted_phones(text)
            if len(phones) == 1:
                return PhoneResult(
                    phone=phones[0],
                    source=f"ocr-avito-{name}",
                    confidence=0.8,
                    raw_text=f"{name}: {text}",
                )
            if len(phones) > 1:
                ambiguous = True
        if ambiguous:
            raise PhoneNotFoundError("OCR увидел несколько форматированных номеров в одной области")
        raise PhoneNotFoundError("OCR не нашёл форматированный номер в проверенных областях Avito")

    @staticmethod
    def _variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
        scale = 4 if image.height < 100 else 3
        resized = image.resize(
            (max(1, image.width * scale), max(1, image.height * scale)), Image.Resampling.LANCZOS
        )
        gray = ImageOps.grayscale(resized)
        gray = ImageOps.autocontrast(gray).filter(ImageFilter.SHARPEN)
        variants: list[tuple[str, Image.Image]] = [
            ("gray", ImageEnhance.Contrast(gray).enhance(2.0)),
        ]
        for threshold in (105, 135, 165, 195):
            binary = gray.point(lambda pixel, edge=threshold: 255 if pixel > edge else 0)
            variants.append((f"threshold-{threshold}", binary))
            variants.append((f"invert-{threshold}", ImageOps.invert(binary)))
        return variants
