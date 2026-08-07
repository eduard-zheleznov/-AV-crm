from __future__ import annotations

from typing import Protocol


class RunReportMetrics(Protocol):
    processed: int
    captured: int
    created: int
    duplicates: int
    inactive: int
    invalid: int
    unavailable: int
    phone_failed: int
    manual_required: int
    captchas_solved: int
    errors: int
    crm_sync_errors: int
    time_deferred: int
    captured_time_deferred: int
    recovered: int
    crm_write_failed: int


def _count(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _percent(part: int, total: int) -> str:
    if total <= 0:
        return "0%" if part <= 0 else "н/д"
    return f"{int(part * 100 / total + 0.5)}%"


def _allocate(remaining: int, requested: int) -> tuple[int, int]:
    """Allocate a counter without allowing overlapping metrics to overstate totals."""

    value = min(max(0, remaining), max(0, requested))
    return value, max(0, remaining - value)


def format_run_report(metrics: RunReportMetrics, *, reason: str = "") -> str:
    """Return one compact, operator-facing funnel report.

    Outcome percentages use the denominator named in the text. Low-level
    counters may overlap, so each visible reason receives no more than the
    still-unclassified remainder. The report never hides failures in a vague
    ``other`` bucket.
    """

    processed = _count(metrics.processed)
    captured = _count(metrics.captured)
    created = _count(metrics.created)
    duplicates = _count(getattr(metrics, "duplicates", 0))
    inactive = _count(metrics.inactive)
    invalid = _count(metrics.invalid)
    unavailable = _count(metrics.unavailable)
    phone_failed = _count(getattr(metrics, "phone_failed", 0))
    manual_required = _count(metrics.manual_required)
    captchas_solved = _count(metrics.captchas_solved)
    errors = _count(metrics.errors)
    crm_sync_errors = _count(metrics.crm_sync_errors)
    time_deferred = _count(getattr(metrics, "time_deferred", 0))
    captured_time_deferred = _count(
        getattr(metrics, "captured_time_deferred", 0)
    )
    recovered = _count(getattr(metrics, "recovered", 0))
    crm_write_failed = _count(getattr(metrics, "crm_write_failed", 0))

    unopened = max(0, processed - captured)
    unopened_remaining = unopened
    inactive_visible, unopened_remaining = _allocate(unopened_remaining, inactive)
    invalid_visible, unopened_remaining = _allocate(unopened_remaining, invalid)
    unavailable_visible, unopened_remaining = _allocate(
        unopened_remaining, unavailable
    )
    phone_failed_visible, unopened_remaining = _allocate(
        unopened_remaining, phone_failed
    )
    manual_visible, unopened_remaining = _allocate(
        unopened_remaining, manual_required
    )
    technical_unopened, unopened_remaining = _allocate(
        unopened_remaining, max(0, errors - crm_write_failed)
    )
    unfinished_unopened = unopened_remaining

    not_created = max(0, captured - created)
    not_created_remaining = not_created
    crm_duplicates, not_created_remaining = _allocate(
        not_created_remaining, duplicates
    )
    recovered_visible, not_created_remaining = _allocate(
        not_created_remaining, recovered
    )
    captured_deferred_visible, not_created_remaining = _allocate(
        not_created_remaining, captured_time_deferred
    )
    crm_failed_visible, not_created_remaining = _allocate(
        not_created_remaining, crm_write_failed
    )
    unfinished_crm = not_created_remaining
    result = (reason or "Очередь обработана: все доступные попытки завершены").strip()

    if captured > 0 and created == captured:
        created_result = f"да — {_percent(created, captured)} от открытых"
    else:
        created_result = f"{_percent(created, captured)} от открытых"

    lines = [
        "Общая воронка запуска:",
        f"1) Обработано ссылок: {processed}",
        f"2) Номеров открыто: {captured} ({_percent(captured, processed)} от ссылок)",
        f"3) Лидов создано: {created} ({created_result})",
        "",
        f"Сколько чего из неоткрытых номеров ({unopened}):",
        (
            f"- Неактивные / снятые объявления: {inactive_visible} "
            f"({_percent(inactive_visible, unopened)})"
        ),
        (
            f"- Некорректные ссылки: {invalid_visible} "
            f"({_percent(invalid_visible, unopened)})"
        ),
        (
            f"- Без кнопки телефона: {unavailable_visible} "
            f"({_percent(unavailable_visible, unopened)})"
        ),
        (
            f"- Номер показан, но OCR не распознал: {phone_failed_visible} "
            f"({_percent(phone_failed_visible, unopened)})"
        ),
        (
            f"- Ожидают ручного действия / капчи: {manual_visible} "
            f"({_percent(manual_visible, unopened)})"
        ),
        (
            f"- Технические ошибки загрузки: {technical_unopened} "
            f"({_percent(technical_unopened, unopened)})"
        ),
        (
            f"- Не завершены до остановки: {unfinished_unopened} "
            f"({_percent(unfinished_unopened, unopened)})"
        ),
    ]
    if not_created:
        lines.extend(
            (
                "",
                f"Почему из открытых номеров не создан новый лид ({not_created}):",
                (
                    "- Дубликаты / номер уже есть в CRM: "
                    f"{crm_duplicates} ({_percent(crm_duplicates, not_created)})"
                ),
                (
                    "- Связаны с ранее созданным лидом: "
                    f"{recovered_visible} "
                    f"({_percent(recovered_visible, not_created)})"
                ),
                (
                    "- Открыты после границы времени и отложены: "
                    f"{captured_deferred_visible} "
                    f"({_percent(captured_deferred_visible, not_created)})"
                ),
                (
                    "- Ошибка записи в CRM: "
                    f"{crm_failed_visible} "
                    f"({_percent(crm_failed_visible, not_created)})"
                ),
                (
                    "- Не завершена запись CRM до остановки: "
                    f"{unfinished_crm} "
                    f"({_percent(unfinished_crm, not_created)})"
                ),
            )
        )
    lines.extend(
        (
            "",
            (
                f"За запуск решено капч: {captchas_solved} "
                f"({_percent(captchas_solved, processed)} от ссылок)"
            ),
        )
    )
    if time_deferred:
        lines.append(
            f"Отложено по местному времени: {time_deferred} "
            "(в CRM не передавались; будут обработаны в безопасное окно)"
        )
    if manual_required:
        lines.append(f"Ожидают решения капчи: {manual_required}")
    if errors or crm_sync_errors:
        lines.append(
            f"Внимание: технических ошибок — {errors}; "
            f"предупреждений CRM — {crm_sync_errors}."
        )
    lines.extend(("", f"Итог: {result}"))
    return "\n".join(lines)
