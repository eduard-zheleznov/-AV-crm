from dataclasses import replace

import pytest
from playwright.sync_api import Error

from avito_crm.avito import (
    AvitoBrowser,
    _wait_until_profile_window_closes,
    launch_avito_context,
)


class FakeChromium:
    def __init__(self):
        self.calls = []
        self.context = object()

    def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        return self.context


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


def test_profile_manager_and_worker_share_profile_without_requiring_login(settings):
    configured = replace(settings, avito_headless=True)
    playwright = FakePlaywright()

    context = launch_avito_context(playwright, configured, force_visible=True)
    launch_avito_context(playwright, configured)

    assert context is playwright.chromium.context
    assert [call["user_data_dir"] for call in playwright.chromium.calls] == [
        str(configured.browser_profile_dir),
        str(configured.browser_profile_dir),
    ]
    assert [call["headless"] for call in playwright.chromium.calls] == [False, True]
    assert all(
        "username" not in call and "password" not in call for call in playwright.chromium.calls
    )


def test_profile_window_wait_survives_a_closed_tab():
    class ClosingPage:
        def wait_for_timeout(self, _milliseconds):
            raise Error("tab closed")

    class LastPage:
        def __init__(self):
            self.waits = 0

        def wait_for_timeout(self, _milliseconds):
            self.waits += 1

    class ClosingContext:
        def __init__(self):
            self.last_page = LastPage()
            self.states = iter([[ClosingPage()], [self.last_page], []])

        @property
        def pages(self):
            return next(self.states)

    context = ClosingContext()

    _wait_until_profile_window_closes(context)

    assert context.last_page.waits == 1


class FakeButton:
    def __init__(self):
        self.clicks = 0

    def scroll_into_view_if_needed(self):
        return None

    def click(self, **_kwargs):
        self.clicks += 1


def _browser_for_round(settings, monkeypatch, *, explicit_error):
    browser = AvitoBrowser(settings, ocr=object())
    button = FakeButton()
    reloads = []
    monkeypatch.setattr(browser, "_wait_for_manual_action", lambda *_args: None)
    monkeypatch.setattr(browser, "_phone_from_visible_controls", lambda *_args: None)
    monkeypatch.setattr(browser, "_has_temp_number_label", lambda *_args: False)
    monkeypatch.setattr(browser, "_has_temp_number_error", lambda *_args: False)

    def click_phone_button(*_args):
        button.click()
        return button, True

    monkeypatch.setattr(browser, "_click_phone_button", click_phone_button)
    monkeypatch.setattr(browser, "_wait_delay", lambda *_args: None)
    monkeypatch.setattr(browser, "_sleep_range", lambda *_args: None)
    monkeypatch.setattr(browser, "_goto_with_retries", lambda _page, url: reloads.append(url))
    monkeypatch.setattr(
        browser,
        "_wait_for_phone_result",
        lambda *_args: (None, False, explicit_error, False),
    )
    return browser, button, reloads


def test_phone_click_gets_one_reload_retry_without_explicit_avito_error(settings, monkeypatch):
    browser, button, reloads = _browser_for_round(settings, monkeypatch, explicit_error=False)

    result = browser._reveal_round(object(), "https://www.avito.ru/x", 6, round_number=1)

    assert result.phone is None
    assert result.explicit_phone_error is False
    assert button.clicks == 2
    assert reloads == ["https://www.avito.ru/x"]


def test_phone_click_uses_bounded_retries_for_explicit_avito_error(settings, monkeypatch):
    browser, button, reloads = _browser_for_round(settings, monkeypatch, explicit_error=True)

    result = browser._reveal_round(object(), "https://www.avito.ru/x", 6, round_number=1)

    assert result.phone is None
    assert result.explicit_phone_error is True
    assert button.clicks == 6
    assert reloads == []


def test_ip_restriction_is_treated_as_manual_action(settings, monkeypatch):
    browser = AvitoBrowser(settings, ocr=object())
    monkeypatch.setattr(
        browser,
        "_page_visible_text",
        lambda *_args: "Доступ ограничен: проблема с IP".lower(),
    )

    assert browser._manual_action_reason(object()) == "ручную проверку"


def test_manual_action_reloads_same_listing_before_continuing(settings, monkeypatch):
    browser = AvitoBrowser(settings, ocr=object())
    answers = iter([True, False])
    navigations = []
    url = "https://www.avito.ru/x"
    monkeypatch.setattr(browser, "_wait_for_manual_action", lambda *_args: next(answers))
    monkeypatch.setattr(
        browser, "_goto_with_retries", lambda _page, value: navigations.append(value)
    )
    monkeypatch.setattr(browser, "_wait_delay", lambda *_args: None)
    monkeypatch.setattr(browser, "_sleep_range", lambda *_args: None)

    recovered = browser._recover_manual_action(object(), url)

    assert recovered is True
    assert navigations == [url]


class FakeNotifier:
    enabled = True

    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    def _record(self, name, **kwargs):
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.events.append((name, kwargs))

    def send_captcha_detected(self, **kwargs):
        self._record("detected", **kwargs)

    def send_captcha_reminder(self, **kwargs):
        self._record("reminder", **kwargs)

    def send_captcha_resolved(self, **kwargs):
        self._record("resolved", **kwargs)

    def send_captcha_timeout(self, **kwargs):
        self._record("timeout", **kwargs)

    def send_captcha_stopped(self, **kwargs):
        self._record("stopped", **kwargs)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_captcha_wait_sends_cascade_and_continues_automatically(settings, monkeypatch):
    configured = replace(
        settings,
        avito_manual_timeout=120,
        telegram_reminder_minutes=(0.05, 0.1),
        telegram_backup_chat_ids=("10002",),
    )
    notifier = FakeNotifier()
    clock = FakeClock()
    browser = AvitoBrowser(configured, ocr=object(), notifier=notifier)
    monkeypatch.setattr("avito_crm.avito.time.monotonic", clock.monotonic)
    monkeypatch.setattr("avito_crm.avito.time.sleep", clock.sleep)
    monkeypatch.setattr(browser, "_save_diagnostic", lambda *_args: None)
    monkeypatch.setattr(
        browser,
        "_manual_action_reason",
        lambda _page: "ручную проверку" if clock.now < 7 else "",
    )

    assert browser._wait_for_manual_action(object(), "https://www.avito.ru/x") is True
    assert [name for name, _kwargs in notifier.events] == [
        "detected",
        "reminder",
        "reminder",
        "resolved",
    ]
    assert notifier.events[1][1]["escalate"] is False
    assert notifier.events[2][1]["escalate"] is True
    assert notifier.events[3][1]["include_backup"] is True


def test_telegram_failure_does_not_interrupt_captcha_wait(settings, monkeypatch):
    configured = replace(settings, avito_manual_timeout=120)
    notifier = FakeNotifier(fail=True)
    clock = FakeClock()
    browser = AvitoBrowser(configured, ocr=object(), notifier=notifier)
    monkeypatch.setattr("avito_crm.avito.time.monotonic", clock.monotonic)
    monkeypatch.setattr("avito_crm.avito.time.sleep", clock.sleep)
    monkeypatch.setattr(browser, "_save_diagnostic", lambda *_args: None)
    monkeypatch.setattr(
        browser,
        "_manual_action_reason",
        lambda _page: "ручную проверку" if clock.now < 1 else "",
    )

    assert browser._wait_for_manual_action(object(), "https://www.avito.ru/x") is True


def test_stop_request_interrupts_long_captcha_wait(settings, monkeypatch):
    configured = replace(settings, avito_manual_timeout=120)
    notifier = FakeNotifier()
    clock = FakeClock()
    browser = AvitoBrowser(configured, ocr=object(), notifier=notifier)
    monkeypatch.setattr("avito_crm.avito.time.monotonic", clock.monotonic)

    def sleep_and_request_stop(seconds):
        clock.sleep(seconds)
        (configured.data_dir / "STOP").write_text("test", encoding="utf-8")

    monkeypatch.setattr("avito_crm.avito.time.sleep", sleep_and_request_stop)
    monkeypatch.setattr(browser, "_save_diagnostic", lambda *_args: None)
    monkeypatch.setattr(browser, "_manual_action_reason", lambda _page: "ручную проверку")

    from avito_crm.errors import ManualActionRequired

    with pytest.raises(ManualActionRequired, match="остановлено оператором"):
        browser._wait_for_manual_action(object(), "https://www.avito.ru/x")

    assert [name for name, _kwargs in notifier.events] == ["detected", "stopped"]


class FakeCandidate:
    def __init__(self, *, fails=False):
        self.fails = fails
        self.clicks = 0

    def is_visible(self):
        return True

    def scroll_into_view_if_needed(self, **_kwargs):
        return None

    def click(self, **_kwargs):
        self.clicks += 1
        if self.fails:
            raise RuntimeError("stale candidate")


class FakeLocatorGroup:
    def __init__(self, candidate=None):
        self.candidate = candidate

    def count(self):
        return int(self.candidate is not None)

    def nth(self, _index):
        return self.candidate


class FakePage:
    def __init__(self, failed_candidate, working_candidate):
        self.failed_candidate = failed_candidate
        self.working_candidate = working_candidate

    def locator(self, selector, **_kwargs):
        if selector == '[data-marker="seller-contact-bar/button-show-number"]':
            return FakeLocatorGroup(self.failed_candidate)
        if selector == 'button[data-marker*="show-number"]':
            return FakeLocatorGroup(self.working_candidate)
        return FakeLocatorGroup()

    def get_by_role(self, *_args, **_kwargs):
        return FakeLocatorGroup()

    def get_by_text(self, *_args, **_kwargs):
        return FakeLocatorGroup()


def test_phone_click_falls_back_after_stale_candidate(settings, monkeypatch):
    browser = AvitoBrowser(settings, ocr=object())
    failed = FakeCandidate(fails=True)
    working = FakeCandidate()
    page = FakePage(failed, working)
    monkeypatch.setattr(browser, "_wait_delay", lambda *_args: None)

    clicked, candidate_found = browser._click_phone_button(page)

    assert clicked is working
    assert candidate_found is True
    assert failed.clicks == 1
    assert working.clicks == 1
