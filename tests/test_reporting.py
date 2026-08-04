from avito_crm.models import RunSummary
from avito_crm.reporting import format_run_report


def test_compact_report_uses_the_requested_funnel_and_denominators():
    summary = RunSummary(
        run_id="report",
        requested=10,
        processed=10,
        captured=6,
        created=3,
        inactive=1,
        invalid=1,
        unavailable=1,
        phone_failed=1,
        captchas_solved=2,
        stopped_reason="Очередь обработана: все доступные попытки завершены",
    )

    report = format_run_report(summary, reason=summary.stopped_reason)

    assert "1) Обработано ссылок: 10" in report
    assert "2) Номеров открыто: 6 (60% от ссылок)" in report
    assert "3) Лидов создано: 3 (50% от открытых)" in report
    assert "Почему из открытых номеров не создан новый лид (3)" in report
    assert "Дубликаты / номер уже есть в CRM: 0 (0%)" in report
    assert "Другое — требуется проверить журнал: 3 (100%)" in report
    assert "Сколько чего из неоткрытых номеров (4)" in report
    assert "- Другое: 1 (25%)" in report
    assert "За запуск решено капч: 2 (20% от ссылок)" in report
    assert report.endswith(summary.stopped_reason)


def test_compact_report_handles_an_empty_time_deferred_run_without_division_errors():
    summary = RunSummary(
        run_id="deferred",
        requested=1,
        stopped_reason=(
            "Отложено по времени: для всех доступных строк сейчас нет "
            "безопасного местного окна 10:00–19:45"
        ),
    )

    report = format_run_report(summary, reason=summary.stopped_reason)

    assert "Номеров открыто: 0 (0% от ссылок)" in report
    assert "Лидов создано: 0 (0% от открытых)" in report
    assert "Итог: Отложено по времени" in report


def test_compact_report_separates_time_deferred_rows_from_unopened_other():
    summary = RunSummary(
        run_id="deferred-mixed",
        requested=5,
        processed=3,
        captured=2,
        created=2,
        unavailable=1,
        time_deferred=2,
        stopped_reason="Остановлено оператором",
    )

    report = format_run_report(summary, reason=summary.stopped_reason)

    assert "Сколько чего из неоткрытых номеров (1)" in report
    assert "- Другое: 0 (0%)" in report
    assert "Отложено по местному времени: 2" in report
    assert "в CRM не передавались" in report


def test_compact_report_says_yes_only_for_full_opened_to_crm_conversion():
    complete = format_run_report(
        RunSummary("complete", 4, processed=4, captured=4, created=4)
    )
    partial = format_run_report(
        RunSummary(
            "partial",
            40,
            processed=40,
            captured=34,
            created=19,
            duplicates=15,
        )
    )

    assert "Лидов создано: 4 (да — 100% от открытых)" in complete
    assert "Почему из открытых номеров не создан новый лид" not in complete
    assert "Лидов создано: 19 (56% от открытых)" in partial
    assert "да — 56%" not in partial
    assert "Почему из открытых номеров не создан новый лид (15)" in partial
    assert "Дубликаты / номер уже есть в CRM: 15 (100%)" in partial
    assert "Другое — требуется проверить журнал: 0 (0%)" in partial


def test_compact_report_only_expands_attention_lines_when_needed():
    normal = format_run_report(RunSummary("normal", 0))
    attention = format_run_report(
        RunSummary(
            "attention",
            2,
            processed=2,
            manual_required=1,
            errors=1,
            crm_sync_errors=2,
        )
    )

    assert "Ожидают решения капчи" not in normal
    assert "Внимание:" not in normal
    assert "Ожидают решения капчи: 1" in attention
    assert "технических ошибок — 1; предупреждений CRM — 2" in attention
