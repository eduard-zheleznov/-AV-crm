from avito_crm.models import ItemStatus, QueueItem, QueuePatch, RunSummary
from avito_crm.state import SingleInstanceLock, StateStore


def test_state_store_records_resumable_item(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as state:
        summary = RunSummary(run_id="run-1", requested=2, captured=1)
        state.begin_run(summary)
        item = QueueItem("2", "https://www.avito.ru/moskva/item_123456789")
        patch = QueuePatch(
            status=ItemStatus.CAPTURED,
            attempts=1,
            phone="+79991234567",
            run_id="run-1",
        )
        state.record_item(item.url, "test", item, patch)
        state.finish_run(summary)

        assert state.get_item(item.url)["status"] == ItemStatus.CAPTURED
        assert state.latest_run()["captured"] == 1


def test_single_instance_lock_cleans_up(tmp_path):
    lock_path = tmp_path / "worker.lock"
    with SingleInstanceLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()
