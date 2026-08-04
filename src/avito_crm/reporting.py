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
    manual_required: int
    captchas_solved: int
    errors: int
    crm_sync_errors: int
    time_deferred: int


def _count(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _percent(part: int, total: int) -> str:
    if total <= 0:
        return "0%" if part <= 0 else "н/д"
    return f"{int(part * 100 / total + 0.5)}%"


def format_run_report(metrics: RunReportMetrics, *, reason: str = "") -> str:
    """Return one compact, operator-facing funnel report.

    Outcome percentages use the denominator named in the text. The residual
    ``other`` bucket is intentionally derived from all unopened links, so the
    visible categories cannot overstate the total even when low-level counters
    overlap.
    """

    processed = _count(metrics.processed)
    captured = _count(metrics.captured)
    created = _count(metrics.created)
    duplicates = _count(getattr(metrics, "duplicates", 0))
    inactive = _count(metrics.inactive)
    invalid = _count(metrics.invalid)
    unavailable = _count(metrics.unavailable)
    manual_required = _count(metrics.manual_required)
    captchas_solved = _count(metrics.captchas_solved)
    errors = _count(metrics.errors)
    crm_sync_errors = _count(metrics.crm_sync_errors)
    time_deferred = _count(getattr(metrics, "time_deferred", 0))

    unopened = max(0, processed - captured)
    classified_unopened = inactive + invalid + unavailable
    other = max(0, unopened - classified_unopened)
    not_created = max(0, captured - created)
    crm_duplicates = min(duplicates, not_created)
    unexplained_not_created = max(0, not_created - crm_duplicates)
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
        f"- Неактивных объявлений: {inactive} ({_percent(inactive, unopened)})",
        f"- Некорректных ссылок: {invalid} ({_percent(invalid, unopened)})",
        f"- Без кнопки телефона: {unavailable} ({_percent(unavailable, unopened)})",
        f"- Другое: {other} ({_percent(other, unopened)})",
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
                    "- Другое — требуется проверить журнал: "
                    f"{unexplained_not_created} "
                    f"({_percent(unexplained_not_created, not_created)})"
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
