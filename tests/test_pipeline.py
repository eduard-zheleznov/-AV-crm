from contextlib import nullcontext
from dataclasses import replace

import pytest

from avito_crm.errors import (
    BrowserOperationError,
    CrmError,
    InactiveListingError,
    OperatorStopRequested,
    PhoneButtonUnavailableError,
    PhoneNotFoundError,
)
from avito_crm.models import (
    CrmDestination,
    CrmWriteResult,
    ItemStatus,
    PhoneResult,
    QueueItem,
    QueuePatch,
)
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


class ClosedLocalWindowQueue(FakeQueue):
    def __init__(self, settings):
        super().__init__(settings)
        self.item.values[settings.phone_column] = ""

    def is_local_window_open(self, item, *, now=None):
        return False


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


def test_remote_worker_can_suppress_its_partial_completion_notification(
    tmp_path, settings, monkeypatch
):
    delivered = []

    class FakeNotifier:
        enabled = True

        def __init__(self, _settings):
            pass

        def send_run_completed(self, **kwargs):
            delivered.append(kwargs)

        def close(self):
            pass

    monkeypatch.setattr("avito_crm.pipeline.NotificationRouter", FakeNotifier)
    source = FakeQueue(settings)
    with StateStore(tmp_path / "state.sqlite3") as state:
        Pipeline(
            settings,
            source,
            state,
            source_name="google:test",
            mode="full",
            live=False,
            notify_completion=False,
        ).run(1, run_id="remote-part")

    assert delivered == []


def test_live_run_reports_when_all_rows_are_deferred_by_local_time(tmp_path, settings):
    source = ClosedLocalWindowQueue(settings)
    phases = []
    with StateStore(tmp_path / "state.sqlite3") as state:
        summary = Pipeline(
            settings,
            source,
            state,
            source_name="test",
            mode="capture",
            live=True,
        ).run(1, phase=phases.append)

    assert summary.inspected == 0
    assert summary.time_deferred == 1
    assert summary.stopped_reason.startswith("Отложено по времени")
    assert "10:00–19:45" in summary.stopped_reason
    assert phases[-1] == summary.stopped_reason


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
            if self._is_actionable(
                item.status,
                item.attempts,
                include_manual,
                next_retry_at=str(item.values.get(self.columns.next_retry_at, "") or ""),
            )
        ]

    def update(self, item, patch):
        self.patches.append((item.row_id, patch))
        item.status = str(patch.status)
        item.attempts = patch.attempts
        item.values[self.columns.status] = str(patch.status)
        item.values[self.columns.phone] = patch.phone
        item.values[self.columns.processed_at] = patch.processed_at
        if patch.next_retry_at is not None:
            item.values[self.columns.next_retry_at] = patch.next_retry_at


class WindowClosingQueue(RoundQueue):
    """Open for selection/reveal, then closed immediately before the CRM write."""

    def __init__(self, settings):
        super().__init__(settings)
        self.window_checks = 0

    def list_all(self):
        return self.items

    def is_local_window_open(self, item, *, now=None):
        self.window_checks += 1
        # Initial discovery, eligibility filtering and the pre-row guard are
        # open. The fourth check happens after reveal, before the CRM write.
        return self.window_checks < 4


class WindowClosingBeforeNavigationQueue(RoundQueue):
    """One selected row expires before navigation; another timezone stays open."""

    def __init__(self, settings):
        super().__init__(settings, count=2)
        self.window_checks: dict[str, int] = {}

    def list_all(self):
        return self.items

    def is_local_window_open(self, item, *, now=None):
        checks = self.window_checks.get(item.row_id, 0) + 1
        self.window_checks[item.row_id] = checks
        if item.row_id == "2":
            # The row is open during round selection and closes immediately
            # before the per-row navigation guard.
            return checks < 2
        return True


class FakeOcr:
    def __init__(self, *_args):
        pass

    def check_available(self):
        return None


class SequencedBrowser:
    def __init__(self, outcomes):
        self.outcomes = {row_id: iter(values) for row_id, values in outcomes.items()}
        self.calls = []
        self.click_budgets = []

    def reveal_phone(self, _url, row_id, *, max_clicks=2):
        self.calls.append(row_id)
        self.click_budgets.append(max_clicks)
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


def _make_retry_due(item, columns):
    item.values[columns.next_retry_at] = "2000-01-01T00:00:00+00:00"


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

    first = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)
    _make_retry_due(source.items[0], source.columns)
    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == ["2", "3", "2"]
    assert browser.click_budgets == [2, 2, 1]
    assert summary.rounds == 1
    assert first.retries == 1
    assert summary.captured == 1
    assert summary.errors == 0


def test_stop_during_captcha_restores_row_for_the_next_normal_run(tmp_path, settings, monkeypatch):
    source = RoundQueue(settings)
    browser = SequencedBrowser({"2": [OperatorStopRequested("остановлено")]})

    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert summary.stopped_reason == "Остановлено оператором"
    assert summary.errors == 0
    assert summary.manual_required == 0
    assert source.items[0].status == ItemStatus.PENDING
    assert source.items[0].attempts == 0
    assert source.list_actionable() == [source.items[0]]


def test_unlimited_mode_processes_more_than_twenty_five_rows(tmp_path, settings, monkeypatch):
    source = RoundQueue(settings, count=30)
    browser = SequencedBrowser(
        {item.row_id: [f"+7999{index:07d}"] for index, item in enumerate(source.items, start=1)}
    )

    monkeypatch.setattr("avito_crm.pipeline.PhoneOcr", FakeOcr)
    monkeypatch.setattr(
        "avito_crm.pipeline.AvitoBrowser",
        lambda *_args, **_kwargs: nullcontext(browser),
    )
    with StateStore(tmp_path / "state.sqlite3") as state:
        summary = Pipeline(
            settings,
            source,
            state,
            source_name="test",
            mode="full",
            live=False,
        ).run(0)

    assert len(browser.calls) == 30
    assert summary.processed == 30
    assert summary.captured == 30
    assert summary.stopped_reason == "Очередь обработана: все доступные попытки завершены"


def test_max_inspected_is_a_hard_canary_boundary(tmp_path, settings, monkeypatch):
    source = RoundQueue(settings, count=10)
    browser = SequencedBrowser(
        {item.row_id: [f"+7999{index:07d}"] for index, item in enumerate(source.items, start=1)}
    )

    monkeypatch.setattr("avito_crm.pipeline.PhoneOcr", FakeOcr)
    monkeypatch.setattr(
        "avito_crm.pipeline.AvitoBrowser",
        lambda *_args, **_kwargs: nullcontext(browser),
    )
    with StateStore(tmp_path / "state.sqlite3") as state:
        summary = Pipeline(
            settings,
            source,
            state,
            source_name="test",
            mode="full",
            live=False,
        ).run(10, max_inspected=5)

    assert browser.calls == ["2", "3", "4", "5", "6"]
    assert summary.inspected == 5
    assert summary.captured == 5
    assert summary.stopped_reason == "Достигнут предел просмотренных объявлений"


def test_phone_failure_becomes_normal_terminal_outcome_after_all_attempts(
    tmp_path, settings, monkeypatch
):
    source = RoundQueue(settings)
    browser = SequencedBrowser({"2": [PhoneNotFoundError("не открылся")] * 2})

    first = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)
    _make_retry_due(source.items[0], source.columns)
    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == ["2", "2"]
    assert browser.click_budgets == [2, 1]
    assert source.items[0].status == ItemStatus.NO_PHONE
    assert summary.phone_failed == 1
    assert first.retries == 1
    assert summary.errors == 0


def test_only_unresolved_technical_failure_counts_as_error(tmp_path, settings, monkeypatch):
    source = RoundQueue(settings)
    browser = SequencedBrowser({"2": [BrowserOperationError("сеть")] * 2})

    first = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)
    _make_retry_due(source.items[0], source.columns)
    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert source.items[0].status == ItemStatus.ERROR
    assert summary.errors == 1
    assert first.retries == 1


def test_recovered_technical_failure_is_removed_from_final_error_count(
    tmp_path, settings, monkeypatch
):
    source = RoundQueue(settings)
    browser = SequencedBrowser({"2": [BrowserOperationError("сеть"), "+79991234567"]})

    first = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)
    _make_retry_due(source.items[0], source.columns)
    summary = _run_with_browser(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == ["2", "2"]
    assert source.items[0].status == ItemStatus.CAPTURED
    assert summary.captured == 1
    assert first.retries == 1
    assert summary.errors == 0


class RepeatQueue(QueueSource):
    def __init__(self, settings, *, repeat_failures=0):
        super().__init__(
            QueueColumns.from_settings(settings),
            settings.max_attempts,
            settings.repeat_phone_max_attempts,
        )
        self.item = QueueItem(
            row_id="2",
            url="https://www.avito.ru/moskva/item_123456789",
            status=ItemStatus.CRM_MONITORING,
            attempts=1,
            values={
                settings.phone_column: "+79990000000",
                settings.crm_lead_column: "111",
                settings.crm_create_count_column: "1",
                settings.funnel_stage_column: "Новый лид",
                settings.repeat_crm_lead_column: "",
                settings.repeat_phone_attempts_column: str(repeat_failures),
                settings.status_column: ItemStatus.CRM_MONITORING,
                settings.error_column: "",
                settings.processed_at_column: "",
                settings.next_retry_at_column: "",
            },
        )
        self.patches = []

    def list_all(self):
        return [self.item]

    def list_actionable(self, *, include_manual=False):
        count = int(self.item.values.get(self.columns.crm_create_count, 0) or 0)
        repeat_attempts = int(self.item.values.get(self.columns.repeat_phone_attempts, 0) or 0)
        return (
            [self.item]
            if self._is_actionable(
                self.item.status,
                self.item.attempts,
                include_manual,
                count,
                repeat_attempts,
                str(self.item.values.get(self.columns.next_retry_at, "") or ""),
            )
            else []
        )

    def update(self, item, patch):
        self.patches.append(patch)


class FakeRepeatCrm:
    instances = []
    recovered_repeat = None
    stage_name = "Автоответчик"
    call_delay_seconds = None
    delete_error = None
    create_detail = ""
    comment_error = None

    def __init__(self, _settings):
        self.created = []
        self.deleted = []
        self.comments = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def resolve_destination(self):
        return CrmDestination(1, "Progress Pro 2.0", 42, "Тег", "cats", "Сбор")

    def list_funnel_steps(self, _project_id):
        return [
            {"id": 11, "name": "Новый Лид"},
            {"id": 12, "name": "Автоответчик"},
            {"id": 88, "name": "Повторный лид"},
        ]

    def get_lead(self, lead_id):
        assert str(lead_id) == "111"
        return {"id": 111, "funnel": 12}

    def get_lead_stage_name(self, lead_id, **_kwargs):
        assert str(lead_id) == "111"
        return self.__class__.stage_name

    def first_call_delay_seconds(self, _lead):
        return self.__class__.call_delay_seconds

    def delete_lead(self, lead_id):
        if self.__class__.delete_error is not None:
            raise self.__class__.delete_error
        self.deleted.append(str(lead_id))

    def find_lead_for_listing(self, *_args, **_kwargs):
        return self.__class__.recovered_repeat

    def create_for_phone(self, phone, listing_url, destination, **kwargs):
        self.created.append((phone, listing_url, destination, kwargs))
        return CrmWriteResult(
            ItemStatus.DONE,
            contact_id="99",
            lead_id="222",
            detail=self.__class__.create_detail,
        )

    def add_listing_comment(self, lead_id, listing_url):
        if self.__class__.comment_error is not None:
            raise self.__class__.comment_error
        self.comments.append((str(lead_id), listing_url))


def _run_repeat(
    tmp_path,
    settings,
    monkeypatch,
    source,
    browser,
    *,
    recovered_repeat=None,
    stage_name="Автоответчик",
    call_delay_seconds=None,
    delete_error=None,
    create_detail="",
    comment_error=None,
    phase_messages=None,
):
    FakeRepeatCrm.instances.clear()
    FakeRepeatCrm.recovered_repeat = recovered_repeat
    FakeRepeatCrm.stage_name = stage_name
    FakeRepeatCrm.call_delay_seconds = call_delay_seconds
    FakeRepeatCrm.delete_error = delete_error
    FakeRepeatCrm.create_detail = create_detail
    FakeRepeatCrm.comment_error = comment_error
    monkeypatch.setattr("avito_crm.pipeline.PhoneOcr", FakeOcr)
    monkeypatch.setattr("avito_crm.pipeline.LpTrackerClient", FakeRepeatCrm)
    monkeypatch.setattr(
        "avito_crm.pipeline.AvitoBrowser",
        lambda *_args, **_kwargs: nullcontext(browser),
    )
    with StateStore(tmp_path / "repeat-state.sqlite3") as state:
        summary = Pipeline(
            settings,
            source,
            state,
            source_name="google:test",
            mode="full",
            live=True,
        ).run(
            1,
            phase=(phase_messages.append if phase_messages is not None else None),
        )
    return summary, FakeRepeatCrm.instances[-1]


def test_autoresponder_creates_exactly_one_forced_repeat_lead(tmp_path, settings, monkeypatch):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": ["+79997654321"]})

    summary, crm = _run_repeat(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == ["2"]
    assert len(crm.created) == 1
    phone, listing_url, _destination, options = crm.created[0]
    assert phone == "+79997654321"
    assert listing_url.endswith("item_123456789")
    assert options == {
        "force_create": True,
        "funnel_id": 88,
        "repeat": True,
        "moscow_offset": 0,
    }
    assert source.item.status == ItemStatus.DONE
    assert source.item.values[source.columns.crm_lead_id] == "111"
    assert source.item.values[source.columns.repeat_crm_lead_id] == "222"
    assert source.item.values[source.columns.crm_create_count] == 2
    assert source.item.values[source.columns.funnel_stage] == "Повторный лид"
    assert source.item.values[source.columns.repeat_phone_attempts] == 0
    assert summary.created == 1
    assert summary.repeat_created == 1
    assert summary.stage_synced == 1


def test_local_time_is_rechecked_after_reveal_before_crm_write(tmp_path, settings, monkeypatch):
    source = WindowClosingQueue(settings)
    browser = SequencedBrowser({"2": ["+79997654321"]})

    summary, crm = _run_repeat(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == ["2"]
    assert crm.created == []
    assert summary.created == 0
    assert source.items[0].status == ItemStatus.PENDING
    assert source.items[0].attempts == 0
    assert source.items[0].values[source.columns.phone] == ""
    assert summary.stopped_reason.startswith("Отложено по времени")


def test_expired_row_is_not_navigated_or_mutated_before_later_safe_timezone(
    tmp_path, settings, monkeypatch
):
    source = WindowClosingBeforeNavigationQueue(settings)
    browser = SequencedBrowser({"3": ["+79997654321"]})
    monkeypatch.setattr("avito_crm.pipeline.PhoneOcr", FakeOcr)
    monkeypatch.setattr(
        "avito_crm.pipeline.AvitoBrowser",
        lambda *_args, **_kwargs: nullcontext(browser),
    )

    with StateStore(tmp_path / "pre-reveal-window-state.sqlite3") as state:
        summary = Pipeline(
            settings,
            source,
            state,
            source_name="test",
            mode="capture",
            live=True,
        ).run(10)

    # reveal_phone performs both navigation and the button click. The expired
    # row must never reach it, while a later safe timezone may still proceed.
    assert browser.calls == ["3"]
    assert source.items[0].status in ("", ItemStatus.PENDING)
    assert source.items[0].attempts == 0
    assert source.items[0].values[source.columns.phone] == ""
    assert source.items[1].status == ItemStatus.CAPTURED
    assert summary.inspected == 1
    assert summary.processed == 1
    assert summary.captured == 1
    assert summary.time_deferred == 1
    assert summary.stopped_reason.startswith("Отложено по времени")


def test_historical_autoresponder_row_is_not_reactivated(tmp_path, settings, monkeypatch):
    source = RepeatQueue(settings)
    source.item.status = ItemStatus.DONE
    source.item.values[source.columns.status] = ItemStatus.DONE
    browser = SequencedBrowser({"2": []})

    summary, crm = _run_repeat(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == []
    assert crm.created == []
    assert summary.stage_synced == 0
    assert source.item.values[source.columns.crm_create_count] == "1"


def test_second_crm_lead_can_never_create_a_third(tmp_path, settings, monkeypatch):
    source = RepeatQueue(settings)
    source.item.status = ItemStatus.DONE
    source.item.values[source.columns.status] = ItemStatus.DONE
    source.item.values[source.columns.crm_create_count] = 2
    source.item.values[source.columns.repeat_crm_lead_id] = "222"
    source.item.values[source.columns.funnel_stage] = "Автоответчики"
    browser = SequencedBrowser({"2": []})

    summary, crm = _run_repeat(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == []
    assert crm.created == []
    assert summary.stage_synced == 0
    assert source.list_actionable() == []


def test_repeat_comment_is_repaired_without_creating_a_third_lead(tmp_path, settings, monkeypatch):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": ["+79997654321"]})

    first, _crm = _run_repeat(
        tmp_path,
        settings,
        monkeypatch,
        source,
        browser,
        create_detail="Лид создан; ссылку в комментарий записать не удалось",
    )

    assert first.created == 1
    assert source.item.status == ItemStatus.CRM_COMMENT_PENDING
    assert source.item.values[source.columns.crm_create_count] == 2

    second, crm = _run_repeat(tmp_path, settings, monkeypatch, source, browser)

    assert second.created == 0
    assert crm.created == []
    assert crm.comments == [("222", source.item.url)]
    assert source.item.status == ItemStatus.DONE


def test_live_pipeline_reports_crm_preflight_and_browser_phases(tmp_path, settings, monkeypatch):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": ["+79997654321"]})
    phases = []

    summary, _crm = _run_repeat(
        tmp_path,
        settings,
        monkeypatch,
        source,
        browser,
        phase_messages=phases,
    )

    assert summary.created == 1
    assert any(message.startswith("Подготовка CRM") for message in phases)
    assert any(message.startswith("Синхронизация CRM: 1/1") for message in phases)
    assert "Подключаем Chromium и открываем очередь Avito." in phases
    assert phases[-1] == "Обрабатываем очередь Avito по одной строке."


def test_extension_driver_uses_ordinary_chrome_browser_adapter(tmp_path, settings, monkeypatch):
    configured = replace(
        settings,
        avito_browser_driver="chrome_extension",
        avito_extension_token="a" * 64,
    )
    source = RepeatQueue(configured)
    browser = SequencedBrowser({"2": ["+79997654321"]})
    phases = []
    monkeypatch.setattr(
        "avito_crm.pipeline.ChromeExtensionBrowser",
        lambda *_args, **_kwargs: nullcontext(browser),
    )

    summary, _crm = _run_repeat(
        tmp_path,
        configured,
        monkeypatch,
        source,
        browser,
        phase_messages=phases,
    )

    assert summary.created == 1
    assert browser.calls == ["2"]
    assert (
        "Подключаем обычный Chrome через локальное расширение и открываем очередь Avito." in phases
    )


def test_stop_during_crm_preflight_skips_browser_and_queue(tmp_path, settings, monkeypatch):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": ["+79997654321"]})
    original_list_steps = FakeRepeatCrm.list_funnel_steps

    def list_steps_and_request_stop(self, project_id):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "STOP").touch()
        return original_list_steps(self, project_id)

    monkeypatch.setattr(FakeRepeatCrm, "list_funnel_steps", list_steps_and_request_stop)

    summary, crm = _run_repeat(tmp_path, settings, monkeypatch, source, browser)

    assert summary.stopped_reason == "Остановлено оператором"
    assert browser.calls == []
    assert crm.created == []
    assert crm.deleted == []
    assert source.item.values[source.columns.crm_create_count] == "1"


def test_no_answer_after_late_first_call_is_terminal_without_delete(
    tmp_path, settings, monkeypatch
):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": []})

    summary, crm = _run_repeat(
        tmp_path,
        settings,
        monkeypatch,
        source,
        browser,
        stage_name="Не дозвон",
        call_delay_seconds=601,
    )

    assert browser.calls == []
    assert crm.deleted == []
    assert crm.created == []
    assert source.item.values[source.columns.crm_create_count] == 1
    assert source.item.values[source.columns.crm_lead_id] == "111"
    assert source.item.values[source.columns.repeat_crm_lead_id] == ""
    assert source.item.values[source.columns.funnel_stage] == "Не дозвон"
    assert source.item.status == ItemStatus.DONE
    assert summary.stage_synced == 1
    assert summary.no_answer_synced == 1
    assert summary.created == 0
    assert summary.repeat_created == 0


def test_no_answer_without_first_call_date_never_deletes_or_recreates(
    tmp_path, settings, monkeypatch
):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": []})

    summary, crm = _run_repeat(
        tmp_path,
        settings,
        monkeypatch,
        source,
        browser,
        stage_name="Недозвон",
        call_delay_seconds=None,
    )

    assert browser.calls == []
    assert crm.deleted == []
    assert crm.created == []
    assert source.item.values[source.columns.crm_create_count] == 1
    assert source.item.values[source.columns.crm_lead_id] == "111"
    assert source.item.values[source.columns.funnel_stage] == "Недозвон"
    assert summary.no_answer_synced == 1
    assert summary.errors == 0


def test_no_answer_at_exactly_ten_minutes_is_not_recreated(tmp_path, settings, monkeypatch):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": []})

    summary, crm = _run_repeat(
        tmp_path,
        settings,
        monkeypatch,
        source,
        browser,
        stage_name="Не дозвон",
        call_delay_seconds=600,
    )

    assert browser.calls == []
    assert crm.deleted == []
    assert crm.created == []
    assert source.item.values[source.columns.crm_create_count] == 1
    assert source.item.values[source.columns.crm_lead_id] == "111"
    assert source.item.values[source.columns.funnel_stage] == "Не дозвон"
    assert summary.no_answer_synced == 1
    assert summary.errors == 0


def test_no_answer_delete_error_never_creates_replacement(tmp_path, settings, monkeypatch):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": []})

    summary, crm = _run_repeat(
        tmp_path,
        settings,
        monkeypatch,
        source,
        browser,
        stage_name="Недозвон",
        call_delay_seconds=601,
        delete_error=CrmError("удаление отклонено"),
    )

    assert browser.calls == []
    assert crm.deleted == []
    assert crm.created == []
    assert source.item.values[source.columns.crm_create_count] == 1
    assert source.item.values[source.columns.crm_lead_id] == "111"
    assert source.item.values[source.columns.funnel_stage] == "Недозвон"
    assert summary.no_answer_synced == 1
    assert summary.crm_sync_errors == 0
    assert summary.errors == 0


def test_repeat_phone_reveal_stops_forever_after_three_failed_attempts(
    tmp_path, settings, monkeypatch
):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": [PhoneNotFoundError("номер не открылся")] * 2})

    first, crm = _run_repeat(tmp_path, settings, monkeypatch, source, browser)
    _make_retry_due(source.item, source.columns)
    summary, crm = _run_repeat(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == ["2", "2"]
    assert browser.click_budgets == [2, 1]
    assert crm.created == []
    assert source.item.status == ItemStatus.REPEAT_EXHAUSTED
    assert source.item.values[source.columns.crm_lead_id] == "111"
    assert source.item.values[source.columns.crm_create_count] == 1
    assert int(source.item.values[source.columns.repeat_phone_attempts]) == 3
    assert source.list_actionable() == []
    assert summary.repeat_exhausted == 1
    assert summary.phone_failed == 1
    assert summary.errors == 0


def test_already_exhausted_repeat_stays_skipped_without_inflating_summary(
    tmp_path, settings, monkeypatch
):
    source = RepeatQueue(settings, repeat_failures=settings.repeat_phone_max_attempts)
    source.item.status = ItemStatus.REPEAT_EXHAUSTED
    source.item.values[source.columns.status] = ItemStatus.REPEAT_EXHAUSTED
    browser = SequencedBrowser({"2": []})

    summary, crm = _run_repeat(tmp_path, settings, monkeypatch, source, browser)

    assert browser.calls == []
    assert crm.created == []
    assert source.item.status == ItemStatus.REPEAT_EXHAUSTED
    assert int(source.item.values[source.columns.repeat_phone_attempts]) == 3
    assert source.list_actionable() == []
    assert summary.repeat_exhausted == 0
    assert summary.stage_synced == 0


def test_repeat_lead_is_recovered_before_another_phone_attempt(tmp_path, settings, monkeypatch):
    source = RepeatQueue(settings)
    browser = SequencedBrowser({"2": []})

    summary, crm = _run_repeat(
        tmp_path,
        settings,
        monkeypatch,
        source,
        browser,
        recovered_repeat={"id": 222, "name": "Авито — 123456789 — повторный лид"},
    )

    assert browser.calls == []
    assert crm.created == []
    assert source.item.status == ItemStatus.DONE
    assert source.item.values[source.columns.crm_create_count] == 2
    assert source.item.values[source.columns.repeat_crm_lead_id] == "222"
    assert summary.created == 0
