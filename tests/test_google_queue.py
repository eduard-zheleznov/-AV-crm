from avito_crm.models import ItemStatus, QueuePatch
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
