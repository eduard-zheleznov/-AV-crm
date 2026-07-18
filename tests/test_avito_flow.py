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
    monkeypatch.setattr(browser, "_wait_for_manual_action", lambda *_args: None)
    monkeypatch.setattr(browser, "_phone_from_visible_controls", lambda *_args: None)
    monkeypatch.setattr(browser, "_has_temp_number_label", lambda *_args: False)
    monkeypatch.setattr(browser, "_has_temp_number_error", lambda *_args: False)
    monkeypatch.setattr(browser, "_find_phone_button", lambda *_args: button)
    monkeypatch.setattr(browser, "_wait_delay", lambda *_args: None)
    monkeypatch.setattr(browser, "_sleep_range", lambda *_args: None)
    monkeypatch.setattr(
        browser,
        "_wait_for_phone_result",
        lambda *_args: (None, False, explicit_error),
    )
    return browser, button


def test_phone_click_is_not_repeated_without_explicit_avito_error(settings, monkeypatch):
    browser, button = _browser_for_round(settings, monkeypatch, explicit_error=False)

    result = browser._reveal_round(object(), "https://www.avito.ru/x", 6, round_number=1)

    assert result.phone is None
    assert result.explicit_phone_error is False
    assert button.clicks == 1


def test_phone_click_uses_bounded_retries_for_explicit_avito_error(settings, monkeypatch):
    browser, button = _browser_for_round(settings, monkeypatch, explicit_error=True)

    result = browser._reveal_round(object(), "https://www.avito.ru/x", 6, round_number=1)

    assert result.phone is None
    assert result.explicit_phone_error is True
    assert button.clicks == 6


def test_ip_restriction_is_treated_as_manual_action(settings, monkeypatch):
    browser = AvitoBrowser(settings, ocr=object())
    monkeypatch.setattr(
        browser,
        "_page_visible_text",
        lambda *_args: "Доступ ограничен: проблема с IP".lower(),
    )

    assert browser._manual_action_reason(object()) == "ручную проверку"
