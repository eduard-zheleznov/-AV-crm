from avito_crm.models import ItemStatus, QueueItem, QueuePatch
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
