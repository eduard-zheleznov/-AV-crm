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


def test_state_store_persists_outcome_and_round_counters(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as state:
        summary = RunSummary(
            run_id="run-outcomes",
            requested=5,
            inactive=1,
            unavailable=1,
            phone_failed=1,
            retries=2,
            processed=4,
            inspected=6,
            rounds=3,
        )
        state.begin_run(summary)
        state.finish_run(summary)
        run = state.latest_run()

    assert run["inactive"] == 1
    assert run["unavailable"] == 1
    assert run["phone_failed"] == 1
    assert run["retries"] == 2
    assert run["processed"] == 4
    assert run["rounds"] == 3


def test_state_store_accumulates_a_resumed_run(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as state:
        first = RunSummary(run_id="remote-1", requested=3, created=1, captured=1)
        state.begin_run(first)
        state.finish_run(first)

        resumed = RunSummary(run_id="remote-1", requested=2, created=2, captured=2)
        state.begin_run(resumed)
        state.finish_run(resumed)

        run = state.latest_run()

    assert run["requested"] == 3
    assert run["created"] == 3
    assert run["captured"] == 3


def test_single_instance_lock_cleans_up(tmp_path):
    lock_path = tmp_path / "worker.lock"
    with SingleInstanceLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()
