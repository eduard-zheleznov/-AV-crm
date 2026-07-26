from avito_crm.avito import AvitoBrowser, RevealRoundResult
from avito_crm.models import PhoneResult


class DummyOcr:
    pass


def test_second_round_reloads_after_ambiguous_first_round(settings, monkeypatch):
    browser = AvitoBrowser(settings, DummyOcr())
    browser.page = object()
    calls: list[object] = []
    outcomes = iter(
        (
            RevealRoundResult(None, False, True),
            RevealRoundResult(PhoneResult("+79991234567", "test"), False, True),
        )
    )

    monkeypatch.setattr(browser, "_maybe_take_long_break", lambda: None)
    monkeypatch.setattr(
        browser,
        "_goto_with_retries",
        lambda _page, url: calls.append(("goto", url)),
    )
    monkeypatch.setattr(browser, "_wait_delay", lambda *_args: None)
    monkeypatch.setattr(browser, "_recover_manual_action", lambda *_args: False)
    monkeypatch.setattr(browser, "_inactive_listing_reason", lambda _page: "")
    monkeypatch.setattr(browser, "_phone_from_visible_controls", lambda _page: None)
    monkeypatch.setattr(browser, "_sleep_range", lambda *_args: None)
    monkeypatch.setattr(
        browser,
        "_reveal_round",
        lambda _page, _url, _attempts, *, round_number: (
            calls.append(("round", round_number)) or next(outcomes)
        ),
    )

    result = browser.reveal_phone(
        "https://www.avito.ru/moskva/item_123456789",
        "2",
    )

    assert result.phone == "+79991234567"
    assert [call[0] for call in calls] == ["goto", "round", "goto", "round"]
