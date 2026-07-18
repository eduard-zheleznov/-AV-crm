from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, sync_playwright

from avito_crm.config import Settings
from avito_crm.errors import ManualActionRequired, PhoneNotFoundError
from avito_crm.models import PhoneResult
from avito_crm.ocr import PhoneOcr
from avito_crm.phone import canonical_avito_url, extract_phones

LOGGER = logging.getLogger(__name__)

CHALLENGE_PATTERNS = (
    "доступ временно ограничен",
    "подтвердите, что вы не робот",
    "пройдите проверку",
    "captcha",
    "слишком много запросов",
    "необычная активность",
)

PHONE_BUTTON_RE = re.compile(r"показать\s+(?:номер\s+)?телефон", re.IGNORECASE)


class AvitoBrowser:
    """Visible, persistent and intentionally sequential Avito browser session."""

    def __init__(self, settings: Settings, ocr: PhoneOcr) -> None:
        self.settings = settings
        self.ocr = ocr
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.processed_in_session = 0

    def __enter__(self) -> AvitoBrowser:
        self.playwright = sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.browser_profile_dir),
                headless=self.settings.avito_headless,
                locale="ru-RU",
                viewport={"width": 1440, "height": 900},
                accept_downloads=False,
            )
        except Exception:
            self.playwright.stop()
            self.playwright = None
            raise
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(self.settings.avito_page_timeout * 1000)
        return self

    def __exit__(self, *_args: object) -> None:
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()
        self.page = None
        self.context = None
        self.playwright = None

    @property
    def session_limit_reached(self) -> bool:
        return self.processed_in_session >= self.settings.avito_max_per_session

    def reveal_phone(self, url: str, row_id: str = "") -> PhoneResult:
        if self.page is None:
            raise RuntimeError("AvitoBrowser должен использоваться как context manager")
        canonical_url = canonical_avito_url(url)
        page = self.page
        LOGGER.info("Открываем объявление, строка=%s", row_id or "-")
        try:
            page.goto(
                canonical_url,
                wait_until="domcontentloaded",
                timeout=self.settings.avito_page_timeout * 1000,
            )
            self._wait_delay(0.45, 0.75)
            self._wait_for_manual_action(page, canonical_url)

            existing = self._phone_from_visible_controls(page)
            if existing:
                self.processed_in_session += 1
                return existing

            button = self._find_phone_button(page)
            if button is None:
                self._save_diagnostic(page, canonical_url, "button-not-found")
                raise PhoneNotFoundError("Кнопка «Показать телефон» не найдена")

            button.scroll_into_view_if_needed()
            self._wait_delay(0.15, 0.35)
            button.click(timeout=self.settings.avito_page_timeout * 1000)
            self._wait_delay(0.25, 0.5)
            self._wait_for_manual_action(page, canonical_url)

            direct = self._phone_from_visible_controls(page)
            if direct:
                self.processed_in_session += 1
                return direct

            result = self._ocr_phone_area(page, button, canonical_url)
            self.processed_in_session += 1
            return result
        except (PhoneNotFoundError, ManualActionRequired):
            raise
        except Exception as exc:
            self._save_diagnostic(page, canonical_url, "browser-error")
            raise PhoneNotFoundError(f"Ошибка браузера при открытии номера: {exc}") from exc

    def _find_phone_button(self, page: Page) -> Locator | None:
        candidates = [
            page.get_by_role("button", name=PHONE_BUTTON_RE),
            page.get_by_role("link", name=PHONE_BUTTON_RE),
            page.locator("button", has_text=PHONE_BUTTON_RE),
            page.locator("a", has_text=PHONE_BUTTON_RE),
            page.get_by_text(PHONE_BUTTON_RE),
        ]
        for locator in candidates:
            try:
                count = min(locator.count(), 8)
                for index in range(count):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                continue
        return None

    def _phone_from_visible_controls(self, page: Page) -> PhoneResult | None:
        candidates = [
            page.locator('a[href^="tel:"]'),
            page.locator('[data-marker*="phone"]'),
            page.locator('[class*="phone"]'),
            page.locator('[aria-label*="телефон" i]'),
        ]
        for locator in candidates:
            try:
                for index in range(min(locator.count(), 30)):
                    candidate = locator.nth(index)
                    if not candidate.is_visible():
                        continue
                    texts = [
                        candidate.get_attribute("href") or "",
                        candidate.get_attribute("aria-label") or "",
                        candidate.inner_text(timeout=1500),
                    ]
                    for text in texts:
                        phones = extract_phones(text)
                        if phones:
                            return PhoneResult(phones[0], "dom", 1.0, text[:120])
            except Exception:
                continue
        return None

    def _ocr_phone_area(self, page: Page, button: Locator, url: str) -> PhoneResult:
        candidates: list[Locator] = [
            page.locator('a[href^="tel:"]'),
            page.locator('[data-marker*="phone"]'),
            page.locator('[class*="phone"]'),
            button,
            button.locator("xpath=.."),
            button.locator("xpath=../.."),
        ]
        errors: list[str] = []
        seen_boxes: set[tuple[int, int, int, int]] = set()
        for locator in candidates:
            try:
                for index in range(min(locator.count(), 20)):
                    candidate = locator.nth(index)
                    if not candidate.is_visible():
                        continue
                    box = candidate.bounding_box()
                    if not box or box["width"] < 35 or box["height"] < 15:
                        continue
                    if box["width"] > 1000 or box["height"] > 450:
                        continue
                    key = tuple(round(box[name]) for name in ("x", "y", "width", "height"))
                    if key in seen_boxes:
                        continue
                    seen_boxes.add(key)
                    png = candidate.screenshot(type="png", animations="disabled")
                    try:
                        return self.ocr.read_png(png)
                    except PhoneNotFoundError as exc:
                        artifact = self._artifact_path(url, f"ocr-{len(seen_boxes)}-failed")
                        artifact.parent.mkdir(parents=True, exist_ok=True)
                        artifact.write_bytes(png)
                        errors.append(str(exc))
            except Exception as exc:
                errors.append(str(exc))
        self._save_diagnostic(page, url, "ocr-failed")
        detail = errors[-1] if errors else "подходящая область номера не найдена"
        raise PhoneNotFoundError(f"Не удалось распознать номер: {detail}")

    def _wait_for_manual_action(self, page: Page, url: str) -> None:
        if not self._has_challenge(page):
            return
        self._save_diagnostic(page, url, "manual-required")
        if self.settings.avito_headless:
            raise ManualActionRequired(
                "Avito запросил проверку; запустите в видимом режиме и пройдите её вручную"
            )
        LOGGER.warning(
            "Avito запросил ручную проверку. Завершите её в открытом браузере; "
            "ожидание до %.0f сек.",
            self.settings.avito_manual_timeout,
        )
        deadline = time.monotonic() + self.settings.avito_manual_timeout
        while time.monotonic() < deadline:
            time.sleep(3)
            if not self._has_challenge(page):
                LOGGER.info("Ручная проверка завершена, продолжаем")
                return
        raise ManualActionRequired("Ручная проверка Avito не завершена за отведённое время")

    @staticmethod
    def _has_challenge(page: Page) -> bool:
        try:
            content = (page.title() + "\n" + page.locator("body").inner_text(timeout=3000)).lower()
        except Exception:
            return False
        return any(pattern in content for pattern in CHALLENGE_PATTERNS)

    def _wait_delay(self, low_factor: float = 1.0, high_factor: float = 1.0) -> None:
        low = self.settings.avito_min_delay * low_factor
        high = self.settings.avito_max_delay * high_factor
        delay = random.SystemRandom().uniform(low, max(low, high))
        LOGGER.debug("Пауза %.1f сек.", delay)
        time.sleep(delay)

    def _artifact_path(self, url: str, suffix: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.settings.screenshot_dir / f"{stamp}-{digest}-{suffix}.png"

    def _save_diagnostic(self, page: Page, url: str, suffix: str) -> Path | None:
        path = self._artifact_path(url, suffix)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=False, animations="disabled")
            return path
        except Exception as exc:
            LOGGER.debug("Не удалось сохранить диагностический скриншот: %s", exc)
            return None
