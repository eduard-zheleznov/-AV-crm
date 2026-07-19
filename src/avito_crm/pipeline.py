from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from avito_crm.avito import AvitoBrowser
from avito_crm.config import Settings
from avito_crm.crm import LpTrackerClient
from avito_crm.errors import (
    InactiveListingError,
    InvalidListingError,
    ManualActionRequired,
    NotificationError,
    PhoneButtonUnavailableError,
    PhoneNotFoundError,
    SourceError,
)
from avito_crm.models import TERMINAL_STATUSES, ItemStatus, QueueItem, QueuePatch, RunSummary
from avito_crm.notifications import NotificationRouter
from avito_crm.ocr import PhoneOcr
from avito_crm.phone import canonical_avito_url, mask_phone, normalize_phone
from avito_crm.queue import QueueSource
from avito_crm.state import StateStore, utc_now

LOGGER = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        source: QueueSource,
        state: StateStore,
        *,
        source_name: str,
        mode: str,
        live: bool,
        include_manual: bool = False,
        interactive_phone_check: bool = False,
    ) -> None:
        if mode not in {"capture", "crm", "full"}:
            raise ValueError(f"Unknown pipeline mode: {mode}")
        self.settings = settings
        self.source = source
        self.state = state
        self.source_name = source_name
        self.mode = mode
        self.live = live
        self.include_manual = include_manual
        self.interactive_phone_check = interactive_phone_check
        self.stop_file = settings.data_dir / "STOP"

    def run(
        self,
        limit: int,
        *,
        run_id: str | None = None,
        progress: Callable[[RunSummary, str], None] | None = None,
    ) -> RunSummary:
        if limit < 1:
            raise ValueError("Лимит должен быть больше нуля")
        self.stop_file.unlink(missing_ok=True)
        summary = RunSummary(run_id=run_id or uuid.uuid4().hex[:12], requested=limit)
        self.state.begin_run(summary)
        self._report_progress(progress, summary, "")
        consecutive_failures = 0
        processed_rows: set[str] = set()
        captured_rows: set[str] = set()
        unresolved_technical_rows: set[str] = set()
        notifier = NotificationRouter(self.settings)

        try:
            initial_items = self.source.list_actionable(include_manual=self.include_manual)
            initial_row_ids = {item.row_id for item in initial_items}
            with ExitStack() as stack:
                crm: LpTrackerClient | None = None
                destination = None
                browser: AvitoBrowser | None = None

                if self.mode in {"crm", "full"} and self.live:
                    crm = stack.enter_context(LpTrackerClient(self.settings))
                    destination = crm.resolve_destination()
                    LOGGER.info(
                        "CRM готова: проект=%s, поле=%s, значение=%s",
                        destination.project_name,
                        destination.field_name,
                        self.settings.lptracker_field_value,
                    )
                needs_browser = self.mode == "capture" or (
                    self.mode == "full"
                    and any(
                        not normalize_phone(
                            str(item.values.get(self.source.columns.phone, "") or "")
                        )
                        for item in initial_items
                        if self._eligible_for_mode(item)
                    )
                )
                if needs_browser:
                    ocr = PhoneOcr(self.settings.tesseract_cmd, self.settings.ocr_min_agreement)
                    ocr.check_available()
                    browser = stack.enter_context(AvitoBrowser(self.settings, ocr, notifier))

                round_number = 0
                while True:
                    items = (
                        initial_items
                        if round_number == 0
                        else [
                            item
                            for item in self.source.list_actionable(
                                include_manual=self.include_manual
                            )
                            if item.row_id in initial_row_ids
                        ]
                    )
                    eligible_items = [item for item in items if self._eligible_for_mode(item)]
                    if not eligible_items:
                        break

                    round_number += 1
                    summary.rounds = round_number
                    LOGGER.info(
                        "Начинаем круг %s: доступно строк %s",
                        round_number,
                        len(eligible_items),
                    )
                    attempted_this_round = False
                    should_stop = False

                    for item in eligible_items:
                        if self.stop_file.exists():
                            summary.stopped_reason = "Остановлено оператором"
                            should_stop = True
                            break
                        if self._goal_reached(summary, limit):
                            summary.stopped_reason = "Достигнут заданный лимит"
                            should_stop = True
                            break
                        needs_phone = not normalize_phone(
                            str(item.values.get(self.source.columns.phone, "") or "")
                        )
                        if browser and needs_phone and browser.session_limit_reached:
                            summary.stopped_reason = (
                                "Достигнут лимит сессии Avito: "
                                f"{self.settings.avito_max_per_session}"
                            )
                            should_stop = True
                            break

                        attempted_this_round = True
                        summary.inspected += 1
                        processed_rows.add(item.row_id)
                        summary.processed = len(processed_rows)
                        self._report_progress(progress, summary, item.row_id)
                        try:
                            canonical_url = canonical_avito_url(item.url)
                        except InvalidListingError as exc:
                            self._finalize(
                                "invalid:" + hashlib.sha256(item.url.encode()).hexdigest(),
                                item,
                                QueuePatch(
                                    status=ItemStatus.INVALID,
                                    attempts=item.attempts + 1,
                                    error=str(exc),
                                    processed_at=utc_now(),
                                    run_id=summary.run_id,
                                ),
                            )
                            summary.invalid += 1
                            unresolved_technical_rows.discard(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            consecutive_failures = 0
                            self._report_progress(progress, summary, item.row_id)
                            continue

                        if self._reconcile_from_state(canonical_url, item, summary.run_id):
                            LOGGER.info(
                                "Строка %s восстановлена из локального журнала", item.row_id
                            )
                            consecutive_failures = 0
                            unresolved_technical_rows.discard(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            continue

                        attempts = item.attempts + 1
                        phone = normalize_phone(
                            str(item.values.get(self.source.columns.phone, "") or "")
                        )
                        self._finalize(
                            canonical_url,
                            item,
                            QueuePatch(
                                status=ItemStatus.PROCESSING,
                                attempts=attempts,
                                phone=phone or "",
                                run_id=summary.run_id,
                            ),
                        )

                        try:
                            if not phone:
                                if browser is None:
                                    raise PhoneNotFoundError(
                                        "В строке нет корректного распознанного номера"
                                    )
                                result = browser.reveal_phone(canonical_url, item.row_id)
                                phone = result.phone
                                LOGGER.info(
                                    "Строка %s: номер получен (%s, %s)",
                                    item.row_id,
                                    result.source,
                                    mask_phone(phone),
                                )
                                if self.interactive_phone_check:
                                    print(f"\nРаспознанный номер: {phone}")
                                    answer = input(
                                        "Сверьте его с номером в открытом браузере. "
                                        "Если совпадает, введите ДА: "
                                    )
                                    if answer.strip().casefold() != "да":
                                        phone = None
                                        raise PhoneNotFoundError(
                                            "Оператор отклонил результат OCR; номер не сохранён"
                                        )

                            if item.row_id not in captured_rows:
                                captured_rows.add(item.row_id)
                                summary.captured += 1
                            if self.mode == "capture" or not self.live:
                                self._finalize(
                                    canonical_url,
                                    item,
                                    QueuePatch(
                                        status=ItemStatus.CAPTURED,
                                        attempts=attempts,
                                        phone=phone,
                                        processed_at=utc_now(),
                                        run_id=summary.run_id,
                                    ),
                                )
                                consecutive_failures = 0
                                unresolved_technical_rows.discard(item.row_id)
                                summary.errors = len(unresolved_technical_rows)
                                self._report_progress(progress, summary, item.row_id)
                                continue

                            if crm is None or destination is None:
                                raise RuntimeError("CRM client was not initialized")
                            write = crm.create_for_phone(phone, canonical_url, destination)
                            self._finalize(
                                canonical_url,
                                item,
                                QueuePatch(
                                    status=write.status,
                                    attempts=attempts,
                                    phone=phone,
                                    crm_lead_id=write.lead_id or "",
                                    error=(
                                        "" if write.status == ItemStatus.DONE else write.detail
                                    ),
                                    processed_at=utc_now(),
                                    run_id=summary.run_id,
                                ),
                            )
                            if write.status == ItemStatus.DONE:
                                summary.created += 1
                                LOGGER.info(
                                    "Строка %s: лид %s создан", item.row_id, write.lead_id
                                )
                            else:
                                summary.duplicates += 1
                                LOGGER.info(
                                    "Строка %s: дубликат, создание пропущено", item.row_id
                                )
                            consecutive_failures = 0
                            unresolved_technical_rows.discard(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            self._report_progress(progress, summary, item.row_id)
                        except InactiveListingError as exc:
                            self._finalize_expected(
                                canonical_url,
                                item,
                                attempts,
                                ItemStatus.INACTIVE,
                                str(exc),
                                summary.run_id,
                            )
                            summary.inactive += 1
                            consecutive_failures = 0
                            unresolved_technical_rows.discard(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            LOGGER.info("Строка %s: %s", item.row_id, exc)
                            self._report_progress(progress, summary, item.row_id)
                        except PhoneButtonUnavailableError as exc:
                            self._finalize_expected(
                                canonical_url,
                                item,
                                attempts,
                                ItemStatus.UNAVAILABLE,
                                str(exc),
                                summary.run_id,
                            )
                            summary.unavailable += 1
                            consecutive_failures = 0
                            unresolved_technical_rows.discard(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            LOGGER.info("Строка %s: %s", item.row_id, exc)
                            self._report_progress(progress, summary, item.row_id)
                        except PhoneNotFoundError as exc:
                            final_attempt = attempts >= self.source.max_attempts
                            status = (
                                ItemStatus.NO_PHONE if final_attempt else ItemStatus.RETRY_PHONE
                            )
                            self._finalize_expected(
                                canonical_url,
                                item,
                                attempts,
                                status,
                                str(exc),
                                summary.run_id,
                            )
                            if final_attempt:
                                summary.phone_failed += 1
                                LOGGER.info(
                                    "Строка %s: номер не открыт после %s попыток",
                                    item.row_id,
                                    attempts,
                                )
                            else:
                                summary.retries += 1
                                LOGGER.info(
                                    "Строка %s: поставлена на следующий круг (%s/%s)",
                                    item.row_id,
                                    attempts,
                                    self.source.max_attempts,
                                )
                            consecutive_failures = 0
                            unresolved_technical_rows.discard(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            self._report_progress(progress, summary, item.row_id)
                        except ManualActionRequired as exc:
                            self._finalize_expected(
                                canonical_url,
                                item,
                                attempts,
                                ItemStatus.MANUAL_REQUIRED,
                                str(exc),
                                summary.run_id,
                                phone=phone or "",
                            )
                            summary.manual_required += 1
                            unresolved_technical_rows.discard(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            summary.stopped_reason = str(exc)
                            should_stop = True
                            self._report_progress(progress, summary, item.row_id)
                            break
                        except Exception as exc:
                            error = _safe_error(exc)
                            final_attempt = attempts >= self.source.max_attempts
                            status = (
                                ItemStatus.ERROR
                                if final_attempt
                                else ItemStatus.RETRY_TECHNICAL
                            )
                            self._finalize_expected(
                                canonical_url,
                                item,
                                attempts,
                                status,
                                error,
                                summary.run_id,
                                phone=phone or "",
                            )
                            if not final_attempt:
                                summary.retries += 1
                            unresolved_technical_rows.add(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            consecutive_failures += 1
                            level = logging.ERROR if final_attempt else logging.WARNING
                            LOGGER.log(level, "Строка %s: %s", item.row_id, error)
                            self._report_progress(progress, summary, item.row_id)
                            if (
                                consecutive_failures
                                >= self.settings.max_consecutive_failures
                            ):
                                summary.stopped_reason = (
                                    "Аварийная остановка после "
                                    f"{consecutive_failures} последовательных "
                                    "технических ошибок"
                                )
                                should_stop = True
                                break

                    if should_stop:
                        break
                    if not attempted_this_round:
                        break

                if not summary.stopped_reason:
                    summary.stopped_reason = (
                        "Очередь обработана: все доступные попытки завершены"
                    )
        except KeyboardInterrupt:
            summary.stopped_reason = "Остановлено с клавиатуры"
            LOGGER.warning(summary.stopped_reason)
        except Exception as exc:
            summary.stopped_reason = f"Ошибка запуска: {exc.__class__.__name__}"
            raise
        finally:
            try:
                self.state.finish_run(summary)
                self._report_progress(progress, summary, "")
                self._notify_completion(notifier, summary)
            finally:
                notifier.close()
        return summary

    def _notify_completion(
        self, notifier: NotificationRouter, summary: RunSummary
    ) -> None:
        if not notifier.enabled:
            return
        try:
            notifier.send_run_completed(
                summary=summary,
                source_name=self.source_name,
                mode=self.mode,
                live=self.live,
            )
        except NotificationError as exc:
            LOGGER.warning("Итоговое уведомление не доставлено: %s", exc)
        except Exception as exc:
            LOGGER.warning(
                "Итоговое уведомление не доставлено (%s)", exc.__class__.__name__
            )

    @staticmethod
    def _report_progress(
        callback: Callable[[RunSummary, str], None] | None,
        summary: RunSummary,
        row_id: str,
    ) -> None:
        if callback is None:
            return
        try:
            callback(summary, row_id)
        except Exception as exc:
            LOGGER.warning("Не удалось обновить прогресс пульта: %s", exc)

    def _eligible_for_mode(self, item: QueueItem) -> bool:
        phone = normalize_phone(str(item.values.get(self.source.columns.phone, "") or ""))
        status = (item.status or "").strip().lower()
        if not self.live and status == ItemStatus.CAPTURED:
            return False
        if self.mode == "capture":
            return not phone and status != ItemStatus.CAPTURED
        if self.mode == "crm":
            return bool(phone)
        return True

    def _goal_reached(self, summary: RunSummary, limit: int) -> bool:
        if self.mode == "capture" or not self.live:
            return summary.captured >= limit
        return summary.created >= limit

    def _reconcile_from_state(self, canonical_url: str, item: QueueItem, run_id: str) -> bool:
        existing = self.state.get_item(canonical_url)
        if not existing or existing.get("status") not in TERMINAL_STATUSES:
            return False
        self.source.update(
            item,
            QueuePatch(
                status=str(existing["status"]),
                attempts=max(item.attempts, int(existing.get("attempts", 0))),
                phone=str(existing.get("phone", "")),
                crm_lead_id=str(existing.get("crm_lead_id", "")),
                error=str(existing.get("error", "")),
                processed_at=str(existing.get("updated_at", utc_now())),
                run_id=run_id,
            ),
        )
        return True

    def _finalize(self, canonical_url: str, item: QueueItem, patch: QueuePatch) -> None:
        self.source.update(item, patch)
        self.state.record_item(canonical_url, self.source_name, item, patch)
        item.status = str(patch.status)
        item.attempts = patch.attempts
        item.values[self.source.columns.status] = str(patch.status)
        item.values[self.source.columns.phone] = patch.phone

    def _finalize_expected(
        self,
        canonical_url: str,
        item: QueueItem,
        attempts: int,
        status: ItemStatus,
        detail: str,
        run_id: str,
        *,
        phone: str = "",
    ) -> None:
        self._finalize(
            canonical_url,
            item,
            QueuePatch(
                status=status,
                attempts=attempts,
                phone=phone,
                error=detail[:500],
                processed_at=utc_now(),
                run_id=run_id,
            ),
        )


def request_stop(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "STOP"
    path.write_text(utc_now(), encoding="utf-8")
    return path


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, SourceError):
        return str(exc)[:500]
    message = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"[:500]
