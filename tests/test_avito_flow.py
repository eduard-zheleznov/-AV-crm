from avito_crm.avito import AvitoBrowser


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
