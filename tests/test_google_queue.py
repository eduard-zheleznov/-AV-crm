from avito_crm.models import ItemStatus, QueuePatch
from avito_crm.queue import GoogleSheetsQueueSource, QueueColumns


class FakeSheet:
    def __init__(self):
        self.values = [["Ссылка"], ["https://www.avito.ru/moskva/item_123456789"]]
        self.header_update = None
        self.batch = None

    def get_all_values(self):
        return [row[:] for row in self.values]

    def update(self, values, range_name=None, **_kwargs):
        self.header_update = (values, range_name)
        self.values[0] = list(values[0])

    def batch_update(self, data, **_kwargs):
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
    assert any(cell["values"] == [[ItemStatus.CAPTURED]] for cell in source.sheet.batch)
