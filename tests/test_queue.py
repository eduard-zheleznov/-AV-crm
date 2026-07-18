from openpyxl import Workbook, load_workbook

from avito_crm.models import ItemStatus, QueuePatch
from avito_crm.queue import QueueColumns, XlsxQueueSource


def test_xlsx_queue_adds_columns_updates_atomically_and_backs_up(tmp_path, settings):
    path = tmp_path / "queue.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Лист1"
    sheet.append(["Ссылка", "Комментарий"])
    sheet.append(["https://www.avito.ru/moskva/item_123456789", "keep me"])
    workbook.save(path)
    workbook.close()

    columns = QueueColumns.from_settings(settings)
    source = XlsxQueueSource(path, "Лист1", columns, 3, tmp_path / "backups")
    items = source.list_actionable()

    assert len(items) == 1
    source.update(
        items[0],
        QueuePatch(
            status=ItemStatus.CAPTURED,
            attempts=1,
            phone="+79991234567",
            processed_at="2026-07-18T00:00:00+00:00",
            run_id="run-1",
        ),
    )

    saved = load_workbook(path)
    saved_sheet = saved["Лист1"]
    headers = {cell.value: cell.column for cell in saved_sheet[1]}
    assert saved_sheet.cell(2, headers["Комментарий"]).value == "keep me"
    assert saved_sheet.cell(2, headers[columns.status]).value == ItemStatus.CAPTURED
    assert saved_sheet.cell(2, headers[columns.phone]).value == "+79991234567"
    saved.close()
    assert len(list((tmp_path / "backups").glob("*.xlsx"))) == 1


def test_terminal_xlsx_rows_are_not_actionable(tmp_path, settings):
    path = tmp_path / "queue.xlsx"
    columns = QueueColumns.from_settings(settings)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Лист1"
    sheet.append([columns.url, *columns.managed])
    sheet.append(["https://www.avito.ru/moskva/item_123456789", "done", "+79991234567"])
    workbook.save(path)
    workbook.close()

    source = XlsxQueueSource(path, "Лист1", columns, 3, tmp_path / "backups")

    assert source.list_actionable() == []
