from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)

from avito_crm.config import Settings
from avito_crm.errors import ManualActionRequired, PhoneNotFoundError
from avito_crm.models import PhoneResult
from avito_crm.ocr import PhoneOcr
from avito_crm.phone import canonical_avito_url, extract_phones

LOGGER = logging.getLogger(__name__)

CHALLENGE_PATTERNS = (
    "доступ временно ограничен",
    "доступ ограничен",
    "проблема с ip",
    "подтвердите, что вы не робот",
    "пройдите проверку",
    "captcha",
    "слишком много запросов",
    "необычная активность",
)

AUTH_PATTERNS = (
    "телефон или почта",
    "нет аккаунта на авито?",
)

TEMP_NUMBER_ERROR_PATTERNS = (
    "произошла ошибка, поэтому мы не можем показать телефон",
    "не можем показать телефон",
    "попробуйте перезагрузить страницу",
)

PHONE_BUTTON_RE = re.compile(
    r"(?:показать\s+(?:номер(?:\s+телефона)?|телефон)|позвонить)", re.IGNORECASE
)


@dataclass(slots=True)
class RevealRoundResult:
    phone: PhoneResult | None
    explicit_phone_error: bool
    button_found: bool


class AvitoBrowser:
    """Visible, persistent and intentionally sequential Avito browser session."""

    def __init__(self, settings: Settings, ocr: PhoneOcr) -> None:
        self.settings = settings
        self.ocr = ocr
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.processed_in_session = 0
        self.next_long_break_at = 0.0

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
        self._schedule_next_long_break()
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
            self._maybe_take_long_break()
            self._goto_with_retries(page, canonical_url)
            self._wait_delay(0.45, 0.75)
            self._recover_manual_action(page, canonical_url)

            existing = self._phone_from_visible_controls(page)
            if existing:
                return self._complete_listing(existing)

            first_round = self._reveal_round(
                page,
                canonical_url,
                self.settings.avito_phone_first_round_attempts,
                round_number=1,
            )
            if first_round.phone:
                return self._complete_listing(first_round.phone)

            last_round = first_round
            if (
                first_round.explicit_phone_error
                and self.settings.avito_phone_second_round_attempts > 0
            ):
                LOGGER.info(
                    "Avito показал временную ошибку телефона; начинаем второй круг после "
                    "повторной загрузки страницы"
                )
                self._sleep_range(
                    self.settings.avito_phone_retry_min,
                    self.settings.avito_phone_retry_max,
                )
                self._goto_with_retries(page, canonical_url)
                self._wait_delay(0.45, 0.75)
                self._recover_manual_action(page, canonical_url)
                second_round = self._reveal_round(
                    page,
                    canonical_url,
                    self.settings.avito_phone_second_round_attempts,
                    round_number=2,
                )
                last_round = second_round
                if second_round.phone:
                    return self._complete_listing(second_round.phone)

            explicit_error = first_round.explicit_phone_error or last_round.explicit_phone_error
            button_found = first_round.button_found or last_round.button_found
            suffix = "phone-temporary-error" if explicit_error else "phone-failed"
            self._save_diagnostic(page, canonical_url, suffix)
            if not button_found:
                raise PhoneNotFoundError("Кнопка «Показать телефон» не найдена")
            if explicit_error:
                raise PhoneNotFoundError(
                    "Avito не показал временный номер после двух ограниченных кругов"
                )
            raise PhoneNotFoundError(
                "После клика не появились ни «Временный номер», ни явная ошибка Avito"
            )
        except (PhoneNotFoundError, ManualActionRequired):
            raise
        except Exception as exc:
            self._save_diagnostic(page, canonical_url, "browser-error")
            raise PhoneNotFoundError(f"Ошибка браузера при открытии номера: {exc}") from exc

    def _reveal_round(
        self, page: Page, url: str, attempts: int, *, round_number: int
    ) -> RevealRoundResult:
        explicit_error_seen = self._has_temp_number_error(page)
        button_found = False
        for attempt in range(1, attempts + 1):
            self._recover_manual_action(page, url)
            existing = self._phone_from_visible_controls(page)
            if existing:
                return RevealRoundResult(existing, explicit_error_seen, button_found)
            if self._has_temp_number_label(page):
                return RevealRoundResult(
                    self._read_open_phone(page, None, url), explicit_error_seen, button_found
                )

            if attempt > 1:
                self._sleep_range(
                    self.settings.avito_phone_retry_min,
                    self.settings.avito_phone_retry_max,
                )

            button, candidate_found = self._click_phone_button(page)
            button_found = button_found or candidate_found
            if button is None:
                LOGGER.warning(
                    "Круг %s, попытка %s/%s: кликабельная кнопка телефона не найдена",
                    round_number,
                    attempt,
                    attempts,
                )
                if not explicit_error_seen:
                    break
                continue

            LOGGER.info(
                "Круг %s: кнопка телефона нажата, попытка %s/%s",
                round_number,
                attempt,
                attempts,
            )
            result, label_ready, error_seen, page_reloaded = self._wait_for_phone_result(page, url)
            explicit_error_seen = explicit_error_seen or error_seen
            if page_reloaded:
                LOGGER.info(
                    "Страница перезагружена после ручной проверки; заново ищем кнопку телефона"
                )
                continue
            if result:
                return RevealRoundResult(result, explicit_error_seen, button_found)
            if label_ready:
                return RevealRoundResult(
                    self._read_open_phone(page, button, url),
                    explicit_error_seen,
                    button_found,
                )
            if not explicit_error_seen:
                LOGGER.info(
                    "Повторы остановлены: явная ошибка Avito «не можем показать телефон» "
                    "не обнаружена"
                )
                break
        return RevealRoundResult(None, explicit_error_seen, button_found)

    def _wait_for_phone_result(
        self, page: Page, url: str
    ) -> tuple[PhoneResult | None, bool, bool, bool]:
        max_wait = self._random_between(
            self.settings.avito_temp_number_wait_min,
            self.settings.avito_temp_number_wait_max,
        )
        LOGGER.info("Ждём «Временный номер» до %.1f сек.", max_wait)
        deadline = time.monotonic() + max_wait
        error_seen = False
        while time.monotonic() < deadline:
            if self._recover_manual_action(page, url):
                return None, False, error_seen, True
            direct = self._phone_from_visible_controls(page)
            if direct:
                return direct, False, error_seen, False
            if self._has_temp_number_label(page):
                self._sleep_range(
                    self.settings.avito_after_label_min,
                    self.settings.avito_after_label_max,
                )
                return None, True, error_seen, False
            error_seen = error_seen or self._has_temp_number_error(page)
            time.sleep(0.3)
        return None, False, error_seen, False

    def _read_open_phone(self, page: Page, button: Locator | None, url: str) -> PhoneResult:
        direct = self._phone_from_visible_controls(page)
        if direct:
            return direct
        return self._ocr_phone_area(page, button, url)

    def _goto_with_retries(self, page: Page, url: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.settings.avito_page_timeout * 1000,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt >= 3:
                    break
                LOGGER.warning(
                    "Ошибка навигации, повтор %s/3 через короткую паузу: %s",
                    attempt,
                    exc.__class__.__name__,
                )
                self._sleep_range(1.0, 3.0)
        raise PhoneNotFoundError(f"Страница не загрузилась после 3 попыток: {last_error}")

    def _click_phone_button(self, page: Page) -> tuple[Locator | None, bool]:
        """Click the first working phone control and fall back from stale candidates."""
        candidates = [
            (
                "show-number-exact",
                page.locator('[data-marker="seller-contact-bar/button-show-number"]'),
            ),
            ("show-number", page.locator('button[data-marker*="show-number"]')),
            ("button-role", page.get_by_role("button", name=PHONE_BUTTON_RE)),
            ("link-role", page.get_by_role("link", name=PHONE_BUTTON_RE)),
            ("button-text", page.locator("button", has_text=PHONE_BUTTON_RE)),
            ("link-text", page.locator("a", has_text=PHONE_BUTTON_RE)),
            # Avito also uses header phone buttons whose marker does not say
            # "show-number". Keep this broad selector after semantic controls.
            ("phone-marker", page.locator('button[data-marker*="phone"]')),
            ("visible-text", page.get_by_text(PHONE_BUTTON_RE)),
        ]
        candidate_found = False
        click_timeout_ms = max(
            1000,
            min(8000, int(self.settings.avito_page_timeout * 1000)),
        )
        for selector_name, locator in candidates:
            try:
                count = min(locator.count(), 8)
                for index in range(count):
                    candidate = locator.nth(index)
                    if not candidate.is_visible():
                        continue
                    candidate_found = True
                    try:
                        candidate.scroll_into_view_if_needed(timeout=3000)
                        self._wait_delay(0.15, 0.35)
                        candidate.click(timeout=click_timeout_ms)
                        return candidate, True
                    except Exception as exc:
                        LOGGER.warning(
                            "Не удалось нажать вариант кнопки телефона %s[%s]: %s; "
                            "пробуем следующий",
                            selector_name,
                            index,
                            exc.__class__.__name__,
                        )
            except Exception as exc:
                LOGGER.debug(
                    "Не удалось проверить селектор кнопки телефона %s: %s",
                    selector_name,
                    exc.__class__.__name__,
                )
                continue
        return None, candidate_found

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

    def _ocr_phone_area(self, page: Page, button: Locator | None, url: str) -> PhoneResult:
        candidates: list[Locator] = [
            page.locator('a[href^="tel:"]'),
            page.locator('[data-marker*="phone"]'),
            page.locator('[class*="phone"]'),
        ]
        if button is not None:
            candidates.extend([button, button.locator("xpath=.."), button.locator("xpath=../..")])
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
        try:
            viewport_png = page.screenshot(full_page=False, type="png", animations="disabled")
            return self.ocr.read_viewport_png(viewport_png)
        except PhoneNotFoundError as exc:
            errors.append(str(exc))
        self._save_diagnostic(page, url, "ocr-failed")
        detail = errors[-1] if errors else "подходящая область номера не найдена"
        raise PhoneNotFoundError(f"Не удалось распознать номер: {detail}")

    def _recover_manual_action(self, page: Page, url: str) -> bool:
        """Wait for the operator, then reload the listing to discard stale DOM state."""
        recovered = False
        for _reload_number in range(1, 4):
            if not self._wait_for_manual_action(page, url):
                return recovered
            recovered = True
            LOGGER.info(
                "После ручной проверки заново загружаем это же объявление, "
                "чтобы обновить элементы страницы"
            )
            self._sleep_range(1.5, 3.0)
            self._goto_with_retries(page, url)
            self._wait_delay(0.45, 0.75)

        if self._manual_action_reason(page):
            self._save_diagnostic(page, url, "manual-repeated")
            raise ManualActionRequired(
                "Avito повторно запросил ручную проверку после 3 перезагрузок; "
                "остановитесь и повторите запуск позже"
            )
        return recovered

    def _wait_for_manual_action(self, page: Page, url: str) -> bool:
        reason = self._manual_action_reason(page)
        if not reason:
            return False
        self._save_diagnostic(page, url, "manual-required")
        if self.settings.avito_headless:
            raise ManualActionRequired(
                f"Avito запросил {reason}; запустите в видимом режиме и завершите действие вручную"
            )
        LOGGER.warning(
            "Avito запросил %s. Завершите действие в открытом браузере; ожидание до %.0f сек.",
            reason,
            self.settings.avito_manual_timeout,
        )
        deadline = time.monotonic() + self.settings.avito_manual_timeout
        while time.monotonic() < deadline:
            time.sleep(3)
            if not self._manual_action_reason(page):
                LOGGER.info("Ручное действие завершено")
                return True
        raise ManualActionRequired(
            f"Ручное действие Avito ({reason}) не завершено за отведённое время"
        )

    @staticmethod
    def _page_visible_text(page: Page) -> str:
        try:
            return (page.title() + "\n" + page.locator("body").inner_text(timeout=3000)).lower()
        except Exception:
            return ""

    def _manual_action_reason(self, page: Page) -> str:
        content = self._page_visible_text(page)
        if any(pattern in content for pattern in CHALLENGE_PATTERNS):
            return "ручную проверку"
        if any(pattern in content for pattern in AUTH_PATTERNS):
            return "авторизацию"
        return ""

    @staticmethod
    def _has_visible_text(page: Page, text: str) -> bool:
        try:
            locator = page.get_by_text(text, exact=False)
            for index in range(min(locator.count(), 10)):
                if locator.nth(index).is_visible():
                    return True
        except Exception:
            return False
        return False

    def _has_temp_number_label(self, page: Page) -> bool:
        return self._has_visible_text(page, "Временный номер")

    def _has_temp_number_error(self, page: Page) -> bool:
        return any(self._has_visible_text(page, text) for text in TEMP_NUMBER_ERROR_PATTERNS)

    def _wait_delay(self, low_factor: float = 1.0, high_factor: float = 1.0) -> None:
        low = self.settings.avito_min_delay * low_factor
        high = self.settings.avito_max_delay * high_factor
        delay = random.SystemRandom().uniform(low, max(low, high))
        LOGGER.debug("Пауза %.1f сек.", delay)
        time.sleep(delay)

    def _sleep_range(self, minimum: float, maximum: float) -> None:
        delay = self._random_between(minimum, maximum)
        LOGGER.debug("Пауза %.1f сек.", delay)
        time.sleep(delay)

    @staticmethod
    def _random_between(minimum: float, maximum: float) -> float:
        return random.SystemRandom().uniform(minimum, max(minimum, maximum))

    def _schedule_next_long_break(self) -> None:
        interval = self._random_between(
            self.settings.avito_long_break_interval_min,
            self.settings.avito_long_break_interval_max,
        )
        self.next_long_break_at = time.monotonic() + interval

    def _maybe_take_long_break(self) -> None:
        if self.next_long_break_at <= 0 or time.monotonic() < self.next_long_break_at:
            return
        duration = self._random_between(
            self.settings.avito_long_break_duration_min,
            self.settings.avito_long_break_duration_max,
        )
        LOGGER.info("Плановый длинный перерыв: %.1f мин.", duration / 60)
        time.sleep(duration)
        self._schedule_next_long_break()

    def _complete_listing(self, result: PhoneResult) -> PhoneResult:
        self.processed_in_session += 1
        self._sleep_range(0.7, 1.8)
        return result

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
