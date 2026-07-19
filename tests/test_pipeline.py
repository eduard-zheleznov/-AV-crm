from contextlib import nullcontext

import pytest

from avito_crm.errors import (
    BrowserOperationError,
    InactiveListingError,
    PhoneButtonUnavailableError,
    PhoneNotFoundError,
)
from avito_crm.models import ItemStatus, PhoneResult, QueueItem, QueuePatch
from avito_crm.pipeline import Pipeline
from avito_crm.queue import QueueColumns, QueueSource
from avito_crm.state import StateStore


class FakeQueue(QueueSource):
    def __init__(self, settings):
        super().__init__(QueueColumns.from_settings(settings), 3)
        self.item = QueueItem(
            row_id="2",
            url="https://www.avito.ru/moskva/item_123456789",
            values={settings.phone_column: "+79991234567"},
        )
        self.patches: list[QueuePatch] = []

    def list_actionable(self, *, include_manual=False):
        return [self.item]

    def update(self, item, patch):
        self.patches.append(patch)
        item.status = str(patch.status)
        item.attempts = patch.attempts
        item.values[self.columns.phone] = patch.phone


def test_dry_run_reuses_captured_phone_without_browser_or_crm(tmp_path, settings):
    source = FakeQueue(settings)
    progress = []
    with StateStore(tmp_path / "state.sqlite3") as state:
        summary = Pipeline(
            settings,
            source,
            state,
            source_name="test",
            mode="full",
            live=False,
        ).run(
            1,
            run_id="remote-command-1",
            progress=lambda current, row_id: progress.append(
                (current.run_id, current.captured, row_id)
            ),
        )

    assert summary.run_id == "remote-command-1"
    assert summary.captured == 1
    assert summary.created == 0
    assert source.patches[-1].status == ItemStatus.CAPTURED
    assert source.patches[-1].phone == "+79991234567"
    assert progress[-1] == ("remote-command-1", 1, "")


class RoundQueue(QueueSource):
    def __init__(self, settings, count=1):
        super().__init__(QueueColumns.from_settings(settings), settings.max_attempts)
        self.items = [
            QueueItem(
                row_id=str(index + 2),
                url=f"https://www.avito.ru/moskva/item_{123456789 + index}",
                values={settings.phone_column: ""},
            )
            for index in range(count)
        ]
        self.patches: list[tuple[str, QueuePatch]] = []

    def list_actionable(self, *, include_manual=False):
        return [
            item
            for item in self.items
            if self._is_actionable(item.status, item.attempts, include_manual)
        ]

    def update(self, item, patch):
        self.patches.append((item.row_id, patch))
        item.status = str(patch.status)
        item.attempts = patch.attempts
        item.values[self.columns.status] = str(patch.status)
        item.values[self.columns.phone] = patch.phone


class FakeOcr:
    def __init__(self, *_args):
        pass

    def check_available(self):
        return None


class SequencedBrowser:
    def __init__(self, outcomes):
        self.outcomes = {row_id: iter(values) for row_id, values in outcomes.items()}
        self.calls = []
        self.session_limit_reached = False

    def reveal_phone(self, _url, row_id):
        self.calls.append(row_id)
        outcome = next(self.outcomes[row_id])
        if isinstance(outcome, Exception):
            raise outcome
        return PhoneResult(outcome, "test")


def _run_with_browser(tmp_path, settings, monkeypatch, source, browser):
    monkeypatch.setattr("avito_crm.pipeline.PhoneOcr", FakeOcr)
    monkeypatch.setattr(
        "avito_crm.pipeline.AvitoBrowser",
        lambda *_args, **_kwargs: nullcontext(browser),
    )
    with StateStore(tmp_path / "state.sqlite3") as state:
        return Pipeline(
            settings,
            source,
            state,
            source_name="test",
            mode="full",
            live=False,
        ).run(10)


@pytest.mark.parametrize(
    ("error", "status", "counter"),
    [
        (InactiveListingError("снято"), ItemStatus.INACTIVE, "inactive"),
        (
            PhoneButtonUnavailableError("нет кнопки"),
            ItemStatus.UNAVAILABLE,
            "unavailable",
        ),
    ],
)
def test_normal_listing_outcomes_are_terminal_not_technical_errors(
    tmp_path, settings, monkeypatch, error, status, counter
):
    source = RoundQueue(settings)
    browser = SequencedBrowser({"2": [error]})

    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert source.items[0].status == status
    assert getattr(summary, counter) == 1
    assert summary.errors == 0
    assert summary.rounds == 1


def test_phone_failures_retry_in_top_to_bottom_rounds_and_can_recover(
    tmp_path, settings, monkeypatch
):
    source = RoundQueue(settings, count=2)
    browser = SequencedBrowser(
        {
            "2": [PhoneNotFoundError("не открылся"), "+79991234567"],
            "3": ["+79997654321"],
        }
    )

    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == ["2", "3", "2"]
    assert summary.rounds == 2
    assert summary.retries == 1
    assert summary.captured == 2
    assert summary.errors == 0


def test_phone_failure_becomes_normal_terminal_outcome_after_all_attempts(
    tmp_path, settings, monkeypatch
):
    source = RoundQueue(settings)
    browser = SequencedBrowser(
        {"2": [PhoneNotFoundError("не открылся") for _ in range(settings.max_attempts)]}
    )

    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == ["2", "2", "2"]
    assert source.items[0].status == ItemStatus.NO_PHONE
    assert summary.phone_failed == 1
    assert summary.retries == settings.max_attempts - 1
    assert summary.errors == 0


def test_only_unresolved_technical_failure_counts_as_error(
    tmp_path, settings, monkeypatch
):
    source = RoundQueue(settings)
    browser = SequencedBrowser(
        {"2": [BrowserOperationError("сеть") for _ in range(settings.max_attempts)]}
    )

    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert source.items[0].status == ItemStatus.ERROR
    assert summary.errors == 1
    assert summary.retries == settings.max_attempts - 1


def test_recovered_technical_failure_is_removed_from_final_error_count(
    tmp_path, settings, monkeypatch
):
    source = RoundQueue(settings)
    browser = SequencedBrowser(
        {"2": [BrowserOperationError("сеть"), "+79991234567"]}
    )

    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == ["2", "2"]
    assert source.items[0].status == ItemStatus.CAPTURED
    assert summary.captured == 1
    assert summary.retries == 1
    assert summary.errors == 0
