from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from gspread.utils import absolute_range_name

import avito_crm.queue as queue_module
from avito_crm.google_api import google_api_call
from avito_crm.models import ItemStatus, QueueItem, QueuePatch
from avito_crm.queue import GoogleSheetsQueueSource, QueueColumns


class FakeSheet:
    def __init__(self):
        self.values = [["Ссылка"], ["https://www.avito.ru/moskva/item_123456789"]]
        self.header_update = None
        self.batch = None
        self.read_count = 0

    def get_all_values(self):
        self.read_count += 1
        return [row[:] for row in self.values]

    def update(self, values, range_name=None, **_kwargs):
        self.header_update = (values, range_name)
        if self.values:
            self.values[0] = list(values[0])
        else:
            self.values.append(list(values[0]))

    def batch_update(self, data, **_kwargs):
        self.batch = data


class FakeEmptySheet(FakeSheet):
    def __init__(self):
        super().__init__()
        self.values = []
        self.frozen_rows = 0
        self.formatted_range = None
        self.filter_enabled = False

    def freeze(self, *, rows):
        self.frozen_rows = rows

    def format(self, range_name, _format):
        self.formatted_range = range_name

    def set_basic_filter(self):
        self.filter_enabled = True


class FakeResponse:
    status_code = 429
    headers = {}


class FakeRateLimitError(RuntimeError):
    response = FakeResponse()


class FakeRetryingQualifiedSheet(FakeSheet):
    def __init__(self, title):
        super().__init__()
        self.title = title
        self.values = (
            [["Ссылка"]]
            + [[""] for _ in range(399)]
            + [["https://www.avito.ru/moskva/item_123456789"]]
        )
        self.qualified_ranges = []
        self.batch_calls = 0

    def batch_update(self, data, **_kwargs):
        self.batch_calls += 1
        for values in data:
            values["range"] = absolute_range_name(self.title, values["range"])
        self.qualified_ranges.append([values["range"] for values in data])
        if self.batch_calls == 1:
            raise FakeRateLimitError("Google error [429]")
        self.batch = data


def test_google_sheet_uses_current_gspread_argument_order(settings):
    source = object.__new__(GoogleSheetsQueueSource)
    source.columns = QueueColumns.from_settings(settings)
    source.max_attempts = 3
    source.sheet = FakeSheet()

    items = source.list_actionable()

    assert source.sheet.header_update[1] == "1:1"
    assert source.sheet.header_update[0][0][0] == "Ссылка"
    assert len(items) == 1

    source.update(
        items[0],
        QueuePatch(
            status=ItemStatus.CAPTURED,
            attempts=1,
            phone="+79991234567",
            run_id="run-1",
        ),
    )
    assert source.sheet.batch
    assert source.sheet.read_count == 1
    assert any(cell["values"] == [[ItemStatus.CAPTURED]] for cell in source.sheet.batch)


def test_google_sheet_initializes_an_empty_tab(settings):
    source = object.__new__(GoogleSheetsQueueSource)
    source.columns = QueueColumns.from_settings(settings)
    source.max_attempts = 3
    source.sheet = FakeEmptySheet()

    items = source.list_actionable()

    assert items == []
    assert source.sheet.values[0] == ["Ссылка", *source.columns.managed]
    assert source.sheet.frozen_rows == 1
    assert source.sheet.formatted_range == "1:1"
    assert source.sheet.filter_enabled is True


@pytest.mark.parametrize(
    ("title", "qualified_status_cell"),
    [
        ("Лист1", "'Лист1'!B401"),
        ("O'Brien queue", "'O''Brien queue'!B401"),
    ],
)
def test_google_sheet_retry_rebuilds_relative_ranges(
    settings, monkeypatch, title, qualified_status_cell
):
    original_google_api_call = google_api_call

    def no_delay_google_api_call(operation, *, label):
        return original_google_api_call(
            operation,
            label=label,
            sleeper=lambda _delay: None,
            jitter=lambda _start, _end: 0.0,
        )

    monkeypatch.setattr(queue_module, "google_api_call", no_delay_google_api_call)
    source = object.__new__(GoogleSheetsQueueSource)
    source.columns = QueueColumns.from_settings(settings)
    source.max_attempts = 3
    source.sheet = FakeRetryingQualifiedSheet(title)

    items = source.list_actionable()

    assert len(items) == 1
    assert items[0].row_id == "401"
    source.update(items[0], QueuePatch(status=ItemStatus.CAPTURED, attempts=1))

    assert source.sheet.batch_calls == 2
    assert source.sheet.qualified_ranges[0][0] == qualified_status_cell
    assert source.sheet.qualified_ranges[1][0] == qualified_status_cell
    assert "!" not in source.sheet.batch[0]["range"].split("!", 1)[1]


@pytest.mark.parametrize("offset", range(5))
def test_google_queue_enforces_local_1000_to_1945_window(offset):
    source = object.__new__(GoogleSheetsQueueSource)
    source.timezone_guard_enabled = True
    source.local_call_start = datetime.strptime("10:00", "%H:%M").time()
    source.local_lead_cutoff = datetime.strptime("19:45", "%H:%M").time()
    item = QueueItem("2", "https://www.avito.ru/item_123", values={"__moscow_offset": offset})
    moscow = ZoneInfo("Europe/Moscow")

    assert source.is_local_window_open(
        item, now=datetime(2026, 7, 31, 10 - offset, 0, tzinfo=moscow)
    )
    assert source.is_local_window_open(
        item, now=datetime(2026, 7, 31, 19 - offset, 45, tzinfo=moscow)
    )
    assert not source.is_local_window_open(
        item, now=datetime(2026, 7, 31, 9 - offset, 59, tzinfo=moscow)
    )
    assert not source.is_local_window_open(
        item, now=datetime(2026, 7, 31, 19 - offset, 46, tzinfo=moscow)
    )


def test_google_queue_blocks_urls_missing_from_plan():
    source = object.__new__(GoogleSheetsQueueSource)
    source.timezone_guard_enabled = True
    source.local_call_start = datetime.strptime("10:00", "%H:%M").time()
    source.local_lead_cutoff = datetime.strptime("19:45", "%H:%M").time()
    assert not source.is_local_window_open(
        QueueItem("3", "https://www.avito.ru/item_456"),
        now=datetime(2026, 7, 31, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")),
    )
