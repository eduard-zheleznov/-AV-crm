from __future__ import annotations

from typing import Protocol


class RunReportMetrics(Protocol):
    processed: int
    captured: int
    created: int
    inactive: int
    invalid: int
    unavailable: int
    manual_required: int
    captchas_solved: int
    errors: int
    crm_sync_errors: int


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
    inactive = _count(metrics.inactive)
    invalid = _count(metrics.invalid)
    unavailable = _count(metrics.unavailable)
    manual_required = _count(metrics.manual_required)
    captchas_solved = _count(metrics.captchas_solved)
    errors = _count(metrics.errors)
    crm_sync_errors = _count(metrics.crm_sync_errors)

    unopened = max(0, processed - captured)
    classified_unopened = inactive + invalid + unavailable
    other = max(0, unopened - classified_unopened)
    result = (reason or "Очередь обработана: все доступные попытки завершены").strip()

    lines = [
        "Общая воронка запуска:",
        f"1) Обработано ссылок: {processed}",
        f"2) Номеров открыто: {captured} ({_percent(captured, processed)} от ссылок)",
        (
            f"3) Лидов создано: {created} "
            f"({'да' if created else 'нет'} — {_percent(created, captured)} от открытых)"
        ),
        "",
        f"Сколько чего из неоткрытых номеров ({unopened}):",
        f"- Неактивных объявлений: {inactive} ({_percent(inactive, unopened)})",
        f"- Некорректных ссылок: {invalid} ({_percent(invalid, unopened)})",
        f"- Без кнопки телефона: {unavailable} ({_percent(unavailable, unopened)})",
        f"- Другое: {other} ({_percent(other, unopened)})",
        "",
        (
            f"За запуск решено капч: {captchas_solved} "
            f"({_percent(captchas_solved, processed)} от ссылок)"
        ),
    ]
    if manual_required:
        lines.append(f"Ожидают решения капчи: {manual_required}")
    if errors or crm_sync_errors:
        lines.append(
            f"Внимание: технических ошибок — {errors}; "
            f"предупреждений CRM — {crm_sync_errors}."
        )
    lines.extend(("", f"Итог: {result}"))
    return "\n".join(lines)
