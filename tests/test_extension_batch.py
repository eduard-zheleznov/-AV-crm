from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from avito_crm.errors import ConfigurationError, PhoneNotFoundError
from avito_crm.extension_batch import (
    BatchUrl,
    _select_google_batch_rows,
    load_batch_urls,
    run_extension_batch,
)
from avito_crm.models import PhoneResult

TEST_PHONE = "+7" + "999" + "123" + "45" + "67"


class _FakeOcr:
    def check_available(self) -> str:
        return "fake-tesseract"


class _FakeBrowser:
    def __init__(self, _settings, _ocr, outcomes):
        self.outcomes = iter(outcomes)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def reveal_phone(self, _url, _row_id, *, max_clicks):
        assert max_clicks in {1, 2}
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _browser_factory(outcomes):
    return lambda settings, ocr: _FakeBrowser(settings, ocr, outcomes)


def test_load_batch_urls_from_txt_deduplicates_and_preserves_input_rows(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text(
        "# test\n"
        "https://www.avito.ru/moskva/test_123\n"
        "\n"
        "https://www.avito.ru/moskva/test_123?from=test\n"
        "https://www.avito.ru/tula/test_456\n",
        encoding="utf-8",
    )

    urls = load_batch_urls(path, limit=2)

    assert [item.input_row for item in urls] == [2, 5]
    assert [item.ordinal for item in urls] == [1, 2]
    assert urls[0].url == "https://www.avito.ru/moskva/test_123"


def test_load_batch_urls_from_csv_and_xlsx(tmp_path):
    csv_path = tmp_path / "urls.csv"
    csv_path.write_text(
        "Статус,Ссылка\nretry_phone,https://www.avito.ru/omsk/test_123\n",
        encoding="utf-8",
    )
    assert load_batch_urls(csv_path, limit=1)[0].input_row == 2

    xlsx_path = tmp_path / "urls.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "OCR"
    worksheet.append(["Ссылка", "Статус"])
    worksheet.append(["https://www.avito.ru/perm/test_456", "retry_phone"])
    workbook.save(xlsx_path)
    workbook.close()
    assert load_batch_urls(xlsx_path, limit=1, sheet="OCR")[0].input_row == 2


def test_load_batch_urls_rejects_invalid_rows_and_short_input(tmp_path):
    invalid = tmp_path / "invalid.txt"
    invalid.write_text("https://example.com/not-avito\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="строках: 1"):
        load_batch_urls(invalid, limit=1)

    short = tmp_path / "short.txt"
    short.write_text("https://www.avito.ru/moskva/test_123\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="только 1"):
        load_batch_urls(short, limit=2)


def test_select_google_batch_rows_is_read_only_and_filters_status():
    values = [
        ["Ссылка", "Статус", "Телефон"],
        ["https://www.avito.ru/omsk/test_123", "done", "stored"],
        ["https://www.avito.ru/perm/test_456", "retry_phone", ""],
        ["https://www.avito.ru/tula/test_789", "retry_technical", ""],
    ]

    selected = _select_google_batch_rows(
        values,
        url_column="Ссылка",
        status_column="Статус",
        statuses={"retry_phone", "retry_technical"},
        limit=2,
    )

    assert [item.input_row for item in selected] == [3, 4]
    assert [item.url for item in selected] == [
        "https://www.avito.ru/perm/test_456",
        "https://www.avito.ru/tula/test_789",
    ]


def test_batch_report_contains_no_url_or_phone(settings):
    url = "https://www.avito.ru/moskva/test_123456789"
    phone = TEST_PHONE
    summary = run_extension_batch(
        settings,
        [BatchUrl(1, 7, url)],
        max_clicks=2,
        circuit_breaker=10,
        ocr=_FakeOcr(),
        browser_factory=_browser_factory([PhoneResult(phone, "ocr-tab-control")]),
        sleep=lambda _seconds: None,
        uniform=lambda low, _high: low,
        monotonic=iter([0.0, 0.0, 1.0, 1.0]).__next__,
    )

    report = summary.report_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in report.splitlines()]
    assert summary.succeeded == 1
    assert records[-1]["crm_writes"] is False
    assert records[-1]["queue_writes"] is False
    assert url not in report
    assert phone not in report
    assert "123456789" not in report
    assert records[1]["status"] == "success"
    assert records[1]["source"] == "ocr-tab-control"


def test_batch_circuit_breaker_stops_repeated_ocr_failures(settings):
    urls = [
        BatchUrl(index, index, f"https://www.avito.ru/moskva/test_{index}12345678")
        for index in range(1, 6)
    ]
    summary = run_extension_batch(
        settings,
        urls,
        max_clicks=1,
        circuit_breaker=3,
        ocr=_FakeOcr(),
        browser_factory=_browser_factory(
            [PhoneNotFoundError("OCR failed") for _ in range(5)]
        ),
        sleep=lambda _seconds: None,
        uniform=lambda low, _high: low,
    )

    assert summary.inspected == 3
    assert summary.succeeded == 0
    assert summary.statuses == {"ocr_failed": 3}
    assert summary.stopped_reason == "Circuit breaker: 3 последовательных OCR-ошибок"


def test_batch_report_path_stays_under_runtime_output(settings):
    summary = run_extension_batch(
        settings,
        [BatchUrl(1, 1, "https://www.avito.ru/moskva/test_123456789")],
        max_clicks=1,
        circuit_breaker=3,
        ocr=_FakeOcr(),
        browser_factory=_browser_factory([PhoneResult(TEST_PHONE, "dom")]),
        sleep=lambda _seconds: None,
        uniform=lambda low, _high: low,
    )

    assert summary.report_path.parent == settings.output_dir
    assert summary.report_path.suffix == ".jsonl"
    assert Path(summary.report_path).is_file()
