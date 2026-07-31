from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
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
        phase: Callable[[str], None] | None = None,
    ) -> RunSummary:
        if limit < 0:
            raise ValueError("Лимит не может быть отрицательным; 0 означает «все строки»")
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
            with ExitStack() as stack:
                crm: LpTrackerClient | None = None
                destination = None
                browser: AvitoBrowser | None = None

                if self.mode in {"crm", "full"} and self.live:
                    self._report_phase(phase, "Подготовка CRM: подключаемся к LPTracker.")
                    crm = stack.enter_context(LpTrackerClient(self.settings))
                    destination = crm.resolve_destination()
                    LOGGER.info(
                        "CRM готова: проект=%s, поле=%s, значение=%s",
                        destination.project_name,
                        destination.field_name,
                        self.settings.lptracker_field_value,
                    )
                    try:
                        if self._sync_crm_rows(
                            crm,
                            destination.project_id,
                            summary,
                            phase=phase,
                        ):
                            return summary
                    except Exception as exc:
                        # Stage monitoring is useful but must not block fresh leads.
                        # The CRM destination itself was already validated above.
                        summary.crm_sync_errors += 1
                        LOGGER.error("Фоновый контроль CRM пропущен: %s", _safe_error(exc))
                        self._report_phase(
                            phase,
                            "Предупреждение CRM-контроля; продолжаем новые объявления.",
                        )
                self._report_phase(phase, "Подготовка очереди Avito: читаем строки.")
                initial_items = self.source.list_actionable(include_manual=self.include_manual)
                initial_row_ids = {item.row_id for item in initial_items}
                needs_browser = self.mode == "capture" or any(
                    self._is_repeat_flow(item)
                    or (
                        self.mode == "full"
                        and not normalize_phone(
                            str(item.values.get(self.source.columns.phone, "") or "")
                        )
                    )
                    for item in initial_items
                    if self._eligible_for_mode(item)
                    and (not self.live or self.source.is_local_window_open(item))
                )
                if needs_browser:
                    self._report_phase(
                        phase,
                        "Запускаем Chromium и открываем очередь Avito.",
                    )
                    ocr = PhoneOcr(self.settings.tesseract_cmd, self.settings.ocr_min_agreement)
                    ocr.check_available()
                    browser = stack.enter_context(AvitoBrowser(self.settings, ocr, notifier))
                self._report_phase(phase, "Обрабатываем очередь Avito по одной строке.")

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
                    mode_eligible = [item for item in items if self._eligible_for_mode(item)]
                    eligible_items = [
                        item
                        for item in mode_eligible
                        if not self.live or self.source.is_local_window_open(item)
                    ]
                    if not eligible_items:
                        if mode_eligible and self.live:
                            LOGGER.info(
                                "Все доступные строки отложены: сейчас нет безопасного "
                                "местного окна 10:00–19:45"
                            )
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
                        repeat_flow = self._is_repeat_flow(item)
                        recreate_flow = self._is_recreate_flow(item)
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

                        if not repeat_flow and self._reconcile_from_state(
                            canonical_url, item, summary.run_id
                        ):
                            LOGGER.info(
                                "Строка %s восстановлена из локального журнала", item.row_id
                            )
                            consecutive_failures = 0
                            unresolved_technical_rows.discard(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            continue

                        attempts = item.attempts + 1
                        stored_phone = normalize_phone(
                            str(item.values.get(self.source.columns.phone, "") or "")
                        )
                        phone = None if repeat_flow else stored_phone
                        max_clicks = 0
                        repeat_phone_attempts = self._int_value(
                            item.values.get(self.source.columns.repeat_phone_attempts)
                        )
                        existing_crm_lead_id = str(
                            item.values.get(self.source.columns.crm_lead_id, "") or ""
                        )
                        self._finalize(
                            canonical_url,
                            item,
                            QueuePatch(
                                status=ItemStatus.PROCESSING,
                                attempts=attempts,
                                phone=stored_phone or "",
                                crm_lead_id=existing_crm_lead_id,
                                run_id=summary.run_id,
                                repeat_phone_attempts=(
                                    repeat_phone_attempts if repeat_flow else None
                                ),
                            ),
                        )

                        try:
                            if not phone:
                                if browser is None:
                                    raise PhoneNotFoundError(
                                        "В строке нет корректного распознанного номера"
                                    )
                                if repeat_flow:
                                    remaining_clicks = max(
                                        1,
                                        self.settings.repeat_phone_max_attempts
                                        - repeat_phone_attempts,
                                    )
                                    max_clicks = min(2 if attempts == 1 else 1, remaining_clicks)
                                else:
                                    max_clicks = 2 if attempts == 1 else 1
                                result = browser.reveal_phone(
                                    canonical_url,
                                    item.row_id,
                                    max_clicks=max_clicks,
                                )
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

                            # Persist the exact revealed number before the CRM request.
                            # If the process stops after LPTracker creates a lead, the
                            # next run can recover it even when Avito later changes its
                            # temporary forwarding number.
                            if self.live and self.mode in {"crm", "full"}:
                                self._finalize(
                                    canonical_url,
                                    item,
                                    QueuePatch(
                                        status=ItemStatus.PROCESSING,
                                        attempts=attempts,
                                        phone=phone,
                                        crm_lead_id=existing_crm_lead_id,
                                        run_id=summary.run_id,
                                        repeat_phone_attempts=(
                                            repeat_phone_attempts if repeat_flow else None
                                        ),
                                    ),
                                )
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
                                        next_retry_at="",
                                    ),
                                )
                                consecutive_failures = 0
                                unresolved_technical_rows.discard(item.row_id)
                                summary.errors = len(unresolved_technical_rows)
                                self._report_progress(progress, summary, item.row_id)
                                continue

                            if crm is None or destination is None:
                                raise RuntimeError("CRM client was not initialized")
                            # Recheck immediately before the irreversible CRM write.
                            # If the reveal crossed 19:45 locally, discard the temporary
                            # number and reopen it in the next safe window.
                            if not self.source.is_local_window_open(item):
                                waiting_status = (
                                    ItemStatus.REPEAT_PENDING if repeat_flow else ItemStatus.PENDING
                                )
                                self._finalize(
                                    canonical_url,
                                    item,
                                    QueuePatch(
                                        status=waiting_status,
                                        attempts=0,
                                        phone="",
                                        crm_lead_id=existing_crm_lead_id,
                                        error=(
                                            "Открытие номера пересекло границу 19:45; "
                                            "номер будет получен заново в безопасное время"
                                        ),
                                        processed_at=utc_now(),
                                        run_id=summary.run_id,
                                        crm_create_count=1 if repeat_flow else 0,
                                        repeat_phone_attempts=(
                                            repeat_phone_attempts if repeat_flow else None
                                        ),
                                        next_retry_at="",
                                    ),
                                )
                                continue
                            repeat_funnel_id = (
                                getattr(self, "_repeat_funnel_id", None) if repeat_flow else None
                            )
                            if repeat_flow and repeat_funnel_id is None:
                                repeat_funnel_id = crm.resolve_funnel_step_id(
                                    destination.project_id,
                                    self.settings.lptracker_repeat_funnel_name,
                                )
                            recreate_funnel_id = (
                                getattr(self, "_new_lead_funnel_id", None)
                                if recreate_flow
                                else None
                            )
                            if recreate_flow and recreate_funnel_id is None:
                                recreate_funnel_id = crm.resolve_funnel_step_id(
                                    destination.project_id,
                                    self.settings.lptracker_new_lead_funnel_name,
                                )
                            write = crm.create_for_phone(
                                phone,
                                canonical_url,
                                destination,
                                force_create=repeat_flow or recreate_flow,
                                funnel_id=recreate_funnel_id or repeat_funnel_id,
                                repeat=repeat_flow,
                            )
                            comment_pending = (
                                "комментар" in write.detail.casefold()
                                and "не удалось" in write.detail.casefold()
                            )
                            self._finalize(
                                canonical_url,
                                item,
                                QueuePatch(
                                    status=(
                                        ItemStatus.CRM_COMMENT_PENDING
                                        if repeat_flow
                                        and write.status == ItemStatus.DONE
                                        and comment_pending
                                        else ItemStatus.CRM_MONITORING
                                        if (not repeat_flow) and write.status == ItemStatus.DONE
                                        else write.status
                                    ),
                                    attempts=attempts,
                                    phone=phone,
                                    crm_lead_id=(
                                        str(
                                            item.values.get(self.source.columns.crm_lead_id, "")
                                            or ""
                                        )
                                        if repeat_flow
                                        else write.lead_id or ""
                                    ),
                                    error=(
                                        write.detail
                                        if "не удалось" in write.detail.casefold()
                                        else ""
                                        if write.status == ItemStatus.DONE
                                        else write.detail
                                    ),
                                    processed_at=utc_now(),
                                    run_id=summary.run_id,
                                    funnel_stage=(
                                        self.settings.lptracker_repeat_funnel_name
                                        if repeat_flow and write.status == ItemStatus.DONE
                                        else self.settings.lptracker_new_lead_funnel_name
                                        if recreate_flow and write.status == ItemStatus.DONE
                                        else None
                                    ),
                                    crm_create_count=(
                                        2
                                        if repeat_flow and write.status == ItemStatus.DONE
                                        else 1
                                        if (not repeat_flow) and write.status == ItemStatus.DONE
                                        else None
                                    ),
                                    repeat_crm_lead_id=(
                                        write.lead_id or "" if repeat_flow else None
                                    ),
                                    repeat_phone_attempts=(
                                        repeat_phone_attempts if repeat_flow else None
                                    ),
                                    next_retry_at="",
                                ),
                            )
                            if write.status == ItemStatus.DONE:
                                if write.created:
                                    summary.created += 1
                                    if repeat_flow:
                                        summary.repeat_created += 1
                                    LOGGER.info(
                                        "Строка %s: лид %s создан", item.row_id, write.lead_id
                                    )
                                else:
                                    LOGGER.info(
                                        "Строка %s: восстановлен ранее созданный лид %s",
                                        item.row_id,
                                        write.lead_id,
                                    )
                            else:
                                summary.duplicates += 1
                                LOGGER.info("Строка %s: дубликат, создание пропущено", item.row_id)
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
                                phone=stored_phone or "",
                                repeat_phone_attempts=(
                                    repeat_phone_attempts if repeat_flow else None
                                ),
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
                                phone=stored_phone or "",
                                repeat_phone_attempts=(
                                    repeat_phone_attempts if repeat_flow else None
                                ),
                            )
                            summary.unavailable += 1
                            consecutive_failures = 0
                            unresolved_technical_rows.discard(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            LOGGER.info("Строка %s: %s", item.row_id, exc)
                            self._report_progress(progress, summary, item.row_id)
                        except PhoneNotFoundError as exc:
                            failed_clicks = max_clicks or (2 if attempts == 1 else 1)
                            if repeat_flow:
                                repeat_phone_attempts += failed_clicks
                            final_attempt = (
                                repeat_phone_attempts >= self.settings.repeat_phone_max_attempts
                                if repeat_flow
                                else attempts >= self.source.max_attempts
                            )
                            if repeat_flow:
                                status = (
                                    ItemStatus.REPEAT_EXHAUSTED
                                    if final_attempt
                                    else ItemStatus.REPEAT_RETRY_PHONE
                                )
                            else:
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
                                phone=stored_phone or "" if repeat_flow else "",
                                repeat_phone_attempts=(
                                    repeat_phone_attempts if repeat_flow else None
                                ),
                                next_retry_at=(
                                    "" if final_attempt else self._next_phone_retry_at()
                                ),
                            )
                            if final_attempt:
                                summary.phone_failed += 1
                                if repeat_flow:
                                    summary.repeat_exhausted += 1
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
                                phone=phone or stored_phone or "",
                                repeat_phone_attempts=(
                                    repeat_phone_attempts if repeat_flow else None
                                ),
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
                            repeat_reveal_failed = repeat_flow and not phone
                            if repeat_reveal_failed:
                                repeat_phone_attempts += 1
                            repeat_reveal_exhausted = (
                                repeat_reveal_failed
                                and repeat_phone_attempts >= self.settings.repeat_phone_max_attempts
                            )
                            final_attempt = (
                                repeat_reveal_exhausted or attempts >= self.source.max_attempts
                            )
                            status = (
                                ItemStatus.REPEAT_EXHAUSTED
                                if repeat_reveal_exhausted
                                else ItemStatus.ERROR
                                if final_attempt
                                else ItemStatus.RECREATE_PENDING
                                if recreate_flow
                                else ItemStatus.REPEAT_RETRY_TECHNICAL
                                if repeat_flow
                                else ItemStatus.RETRY_TECHNICAL
                            )
                            self._finalize_expected(
                                canonical_url,
                                item,
                                attempts,
                                status,
                                error,
                                summary.run_id,
                                phone=phone or stored_phone or "",
                                repeat_phone_attempts=(
                                    repeat_phone_attempts if repeat_flow else None
                                ),
                                next_retry_at=(
                                    "" if final_attempt else self._next_phone_retry_at()
                                ),
                            )
                            if not final_attempt:
                                summary.retries += 1
                            if repeat_reveal_exhausted:
                                summary.phone_failed += 1
                                summary.repeat_exhausted += 1
                            unresolved_technical_rows.add(item.row_id)
                            summary.errors = len(unresolved_technical_rows)
                            consecutive_failures += 1
                            level = logging.ERROR if final_attempt else logging.WARNING
                            LOGGER.log(level, "Строка %s: %s", item.row_id, error)
                            self._report_progress(progress, summary, item.row_id)
                            if consecutive_failures >= self.settings.max_consecutive_failures:
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
                    summary.stopped_reason = "Очередь обработана: все доступные попытки завершены"
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

    def _sync_crm_rows(
        self,
        crm: LpTrackerClient,
        project_id: int,
        summary: RunSummary,
        *,
        phase: Callable[[str], None] | None = None,
    ) -> bool:
        """Refresh funnel stages and return ``True`` when an operator requested stop."""
        self._report_phase(phase, "Подготовка CRM: ищем только активные новые лиды.")
        all_items = self.source.list_all()
        sync_items: list[QueueItem] = []
        comment_items: list[QueueItem] = []
        self._repeat_funnel_id = None
        self._new_lead_funnel_id = None

        for item in all_items:
            if self._operator_stop_requested(summary):
                return True
            count = self._int_value(item.values.get(self.source.columns.crm_create_count))
            first_lead_id = str(item.values.get(self.source.columns.crm_lead_id, "") or "").strip()
            repeat_lead_id = str(
                item.values.get(self.source.columns.repeat_crm_lead_id, "") or ""
            ).strip()
            active_monitor = self._normalized_text(item.status) in {
                ItemStatus.CRM_MONITORING.value,
                ItemStatus.PROCESSING.value,
            }

            # Safe migration for rows created before the two new columns existed.
            if count == 0 and repeat_lead_id:
                count = 2
                self._update_metadata(item, crm_create_count=2)
            elif (
                count == 0
                and first_lead_id
                and self._normalized_text(item.status) == ItemStatus.DONE
            ):
                count = 1
                self._update_metadata(item, crm_create_count=1)

            if count == 1 and active_monitor:
                sync_items.append(item)
            elif count == 2 and self._normalized_text(item.status) == (
                ItemStatus.CRM_COMMENT_PENDING.value
            ):
                comment_items.append(item)

        for item in comment_items[: self.settings.crm_monitor_batch_size]:
            repeat_lead_id = str(
                item.values.get(self.source.columns.repeat_crm_lead_id, "") or ""
            ).strip()
            if not repeat_lead_id or self._crm_monitor_expired(item):
                self._update_metadata(
                    item,
                    status=ItemStatus.CRM_MONITOR_EXPIRED,
                    crm_create_count=2,
                    error=(
                        "Не удалось восстановить комментарий второго лида: CRM ID отсутствует"
                        if not repeat_lead_id
                        else "Не удалось восстановить комментарий второго лида за 24 часа"
                    ),
                )
                continue
            try:
                crm.add_listing_comment(repeat_lead_id, item.url)
                self._update_metadata(
                    item,
                    status=ItemStatus.DONE,
                    crm_create_count=2,
                    repeat_crm_lead_id=repeat_lead_id,
                    error="",
                )
            except Exception as exc:
                summary.crm_sync_errors += 1
                LOGGER.warning(
                    "Строка %s: не удалось дописать ссылку в комментарий CRM: %s",
                    item.row_id,
                    exc,
                )

        if not sync_items:
            self._report_phase(phase, "Подготовка CRM завершена: лидов для сверки нет.")
            return False

        sync_items = sync_items[: self.settings.crm_monitor_batch_size]

        self._report_phase(
            phase,
            f"Подготовка CRM: загружаем шаги воронки; лидов для сверки {len(sync_items)}.",
        )
        steps = crm.list_funnel_steps(project_id)
        if self._operator_stop_requested(summary):
            return True
        repeat_matches = [
            step
            for step in steps
            if self._normalized_text(str(step.get("name", "")))
            == self._normalized_text(self.settings.lptracker_repeat_funnel_name)
        ]
        if len(repeat_matches) == 1:
            try:
                self._repeat_funnel_id = int(repeat_matches[0]["id"])
            except (KeyError, TypeError, ValueError):
                self._repeat_funnel_id = None
        new_lead_matches = [
            step
            for step in steps
            if self._normalized_text(str(step.get("name", "")))
            == self._normalized_text(self.settings.lptracker_new_lead_funnel_name)
        ]
        if len(new_lead_matches) == 1:
            try:
                self._new_lead_funnel_id = int(new_lead_matches[0]["id"])
            except (KeyError, TypeError, ValueError):
                self._new_lead_funnel_id = None

        autoresponder = self._normalized_text(self.settings.lptracker_autoresponder_funnel_name)
        no_answer_stages = {
            self._normalized_text(self.settings.lptracker_no_answer_funnel_name),
            self._normalized_text("Недозвон"),
            self._normalized_text("Не дозвон"),
        }
        repeat_eligible_stages = {
            autoresponder,
            self._normalized_text("Автоответчик"),
            self._normalized_text("Автоответчики"),
        }
        pending_stages = {
            self._normalized_text(name) for name in self.settings.lptracker_pending_funnel_names
        }
        autoresponder_matches = [
            step
            for step in steps
            if self._normalized_text(step.get("name", "")) in repeat_eligible_stages
        ]
        if len(autoresponder_matches) != 1:
            available = ", ".join(str(step.get("name", "")) for step in steps[:30])
            raise SourceError("Шаг автоответчика не найден однозначно. Доступно: " + available)
        autoresponder_step_id = str(autoresponder_matches[0].get("id", "")).strip()
        pending_step_ids = {
            str(step.get("id", "")).strip()
            for step in steps
            if self._normalized_text(step.get("name", "")) in pending_stages
        }
        total_sync_items = len(sync_items)
        for index, item in enumerate(sync_items, start=1):
            if self._operator_stop_requested(summary):
                return True
            self._report_phase(
                phase,
                (
                    f"Синхронизация CRM: {index}/{total_sync_items}; "
                    f"обновлено {summary.stage_synced}, предупреждений "
                    f"{summary.crm_sync_errors}."
                ),
            )
            count = self._int_value(item.values.get(self.source.columns.crm_create_count))
            first_lead_id = str(item.values.get(self.source.columns.crm_lead_id, "") or "").strip()
            repeat_lead_id = str(
                item.values.get(self.source.columns.repeat_crm_lead_id, "") or ""
            ).strip()

            if count != 1:
                continue

            try:
                lead_id = first_lead_id
                if not lead_id:
                    phone = str(item.values.get(self.source.columns.phone, "") or "").strip()
                    lead = crm.find_lead_for_listing(
                        project_id,
                        phone,
                        item.url,
                        repeat=False,
                    )
                    if not lead or not lead.get("id"):
                        raise SourceError("Не найден первый лид по CRM ID, телефону и ссылке")
                    lead_id = str(lead["id"])
                current_error = str(item.values.get(self.source.columns.error, "") or "")
                if "комментар" in self._normalized_text(current_error):
                    try:
                        crm.add_listing_comment(lead_id, item.url)
                    except Exception as exc:
                        summary.crm_sync_errors += 1
                        self._update_metadata(
                            item,
                            status=ItemStatus.CRM_MONITORING,
                            crm_create_count=1,
                            crm_lead_id=lead_id,
                            error=f"Комментарий CRM: {_safe_error(exc)}",
                        )
                        continue
                lead_data = crm.get_lead(lead_id)
                stage = crm.get_lead_stage_name(
                    lead_id,
                    project_id=project_id,
                    funnel_steps=steps,
                    lead=lead_data,
                )
                if not stage:
                    raise SourceError("LPTracker не вернул текущий шаг воронки лида")
                summary.stage_synced += 1
                normalized_stage = self._normalized_text(stage)
                stage_id = self._lead_stage_id(lead_data)
                is_autoresponder = (
                    bool(autoresponder_step_id and stage_id == autoresponder_step_id)
                    or normalized_stage in repeat_eligible_stages
                )
                is_pending = bool(stage_id and stage_id in pending_step_ids) or (
                    normalized_stage in pending_stages
                )
                if normalized_stage in no_answer_stages:
                    summary.no_answer_synced += 1
                repeat_attempts = self._int_value(
                    item.values.get(self.source.columns.repeat_phone_attempts)
                )
                stored_phone = str(item.values.get(self.source.columns.phone, "") or "").strip()

                if normalized_stage in no_answer_stages:
                    # The obsolete 10-minute delete/recreate rule is deliberately
                    # disabled. Time-zone eligibility is enforced before every CRM
                    # creation, so a no-answer result is final for the loader.
                    self._update_metadata(
                        item,
                        status=ItemStatus.DONE,
                        funnel_stage=stage,
                        crm_create_count=1,
                        crm_lead_id=lead_id,
                        error="",
                    )
                    continue

                if is_pending:
                    if self._crm_monitor_expired(item):
                        self._update_metadata(
                            item,
                            status=ItemStatus.CRM_MONITOR_EXPIRED,
                            funnel_stage=stage,
                            crm_create_count=1,
                            crm_lead_id=lead_id,
                            error=(
                                "CRM не завершила первый звонок за "
                                f"{self.settings.crm_monitor_max_hours:g} ч.; "
                                "автоматический контроль остановлен"
                            ),
                        )
                    else:
                        self._update_metadata(
                            item,
                            status=ItemStatus.CRM_MONITORING,
                            funnel_stage=stage,
                            crm_create_count=1,
                            crm_lead_id=lead_id,
                            error="",
                        )
                    continue

                # If the previous process stopped after LPTracker accepted the
                # second lead, recover it by the phone persisted immediately before
                # the API request. This wins over both the current stage and the
                # three-attempt cap because the repeat already exists.
                if not repeat_lead_id and stored_phone:
                    recovered_repeat = crm.find_lead_for_listing(
                        project_id,
                        stored_phone,
                        item.url,
                        repeat=True,
                    )
                    if recovered_repeat and recovered_repeat.get("id"):
                        self._update_metadata(
                            item,
                            status=ItemStatus.DONE,
                            funnel_stage=self.settings.lptracker_repeat_funnel_name,
                            crm_create_count=2,
                            repeat_crm_lead_id=str(recovered_repeat["id"]),
                            error="",
                            crm_lead_id=lead_id,
                        )
                        LOGGER.info(
                            "Строка %s: восстановлен повторный лид %s после прерывания",
                            item.row_id,
                            recovered_repeat["id"],
                        )
                        continue
                if self._normalized_text(item.status) in {
                    ItemStatus.INACTIVE.value,
                    ItemStatus.UNAVAILABLE.value,
                    ItemStatus.INVALID.value,
                }:
                    self._update_metadata(
                        item,
                        funnel_stage=stage,
                        crm_create_count=1,
                        crm_lead_id=lead_id,
                    )
                    continue
                if not is_autoresponder:
                    self._update_metadata(
                        item,
                        status=ItemStatus.DONE,
                        funnel_stage=stage,
                        crm_create_count=1,
                        error="",
                        crm_lead_id=lead_id,
                    )
                    continue
                if repeat_attempts >= self.settings.repeat_phone_max_attempts:
                    already_exhausted = (
                        self._normalized_text(item.status) == ItemStatus.REPEAT_EXHAUSTED.value
                    )
                    self._update_metadata(
                        item,
                        status=ItemStatus.REPEAT_EXHAUSTED,
                        funnel_stage=stage,
                        crm_create_count=1,
                        error=(
                            "Повторное открытие номера прекращено: использованы "
                            f"все {self.settings.repeat_phone_max_attempts} попытки"
                        ),
                        crm_lead_id=lead_id,
                    )
                    # Count only the transition in this run. Rows remain terminal
                    # forever, but must not inflate completion analytics on every
                    # later launch while their CRM stage is still autoresponder.
                    if not already_exhausted:
                        summary.repeat_exhausted += 1
                    continue
                if self._repeat_funnel_id is None:
                    available = ", ".join(str(step.get("name", "")) for step in steps[:30])
                    raise SourceError(
                        f"Шаг воронки {self.settings.lptracker_repeat_funnel_name!r} "
                        f"не найден однозначно. Доступно: {available}"
                    )
                current_status = self._normalized_text(item.status)
                attempts = (
                    item.attempts
                    if current_status
                    in {
                        ItemStatus.REPEAT_PENDING.value,
                        ItemStatus.REPEAT_RETRY_PHONE.value,
                        ItemStatus.REPEAT_RETRY_TECHNICAL.value,
                    }
                    else 0
                )
                self._update_metadata(
                    item,
                    status=ItemStatus.REPEAT_PENDING,
                    attempts=attempts,
                    funnel_stage=stage,
                    crm_create_count=1,
                    error="",
                    crm_lead_id=lead_id,
                )
            except Exception as exc:
                summary.crm_sync_errors += 1
                LOGGER.error("Строка %s: не удалось обновить шаг CRM: %s", item.row_id, exc)
                self._update_metadata(
                    item,
                    status=item.status or ItemStatus.DONE,
                    crm_create_count=1,
                    error=f"Синхронизация CRM: {_safe_error(exc)}",
                )
        self._report_phase(
            phase,
            (
                f"Синхронизация CRM завершена: обновлено {summary.stage_synced}, "
                f"предупреждений {summary.crm_sync_errors}."
            ),
        )
        return False

    def _operator_stop_requested(self, summary: RunSummary) -> bool:
        if not self.stop_file.exists():
            return False
        if not summary.stopped_reason:
            summary.stopped_reason = "Остановлено оператором"
            LOGGER.warning("Остановка принята во время подготовки CRM; новые ссылки не открывались")
        return True

    def _update_metadata(
        self,
        item: QueueItem,
        *,
        status: ItemStatus | str | None = None,
        attempts: int | None = None,
        phone: str | None = None,
        crm_lead_id: str | None = None,
        error: str | None = None,
        funnel_stage: str | None = None,
        crm_create_count: int | None = None,
        repeat_crm_lead_id: str | None = None,
        repeat_phone_attempts: int | None = None,
        next_retry_at: str | None = "",
    ) -> None:
        patch = QueuePatch(
            status=str(status if status is not None else item.status),
            attempts=item.attempts if attempts is None else attempts,
            phone=(
                str(item.values.get(self.source.columns.phone, "") or "")
                if phone is None
                else phone
            ),
            crm_lead_id=(
                str(item.values.get(self.source.columns.crm_lead_id, "") or "")
                if crm_lead_id is None
                else crm_lead_id
            ),
            error=(
                str(item.values.get(self.source.columns.error, "") or "")
                if error is None
                else error
            ),
            # Preserve the first-lead creation timestamp while monitoring so
            # the 24-hour terminal boundary is stable across polling cycles.
            processed_at=str(item.values.get(self.source.columns.processed_at, "") or utc_now()),
            run_id="crm-monitor",
            funnel_stage=funnel_stage,
            crm_create_count=crm_create_count,
            repeat_crm_lead_id=repeat_crm_lead_id,
            repeat_phone_attempts=repeat_phone_attempts,
            next_retry_at=next_retry_at,
        )
        canonical_url = canonical_avito_url(item.url)
        self._finalize(canonical_url, item, patch)

    def _is_repeat_flow(self, item: QueueItem) -> bool:
        return self._int_value(
            item.values.get(self.source.columns.crm_create_count)
        ) == 1 and self._normalized_text(item.status) in {
            ItemStatus.REPEAT_PENDING.value,
            ItemStatus.REPEAT_RETRY_PHONE.value,
            ItemStatus.REPEAT_RETRY_TECHNICAL.value,
        }

    def _is_recreate_flow(self, item: QueueItem) -> bool:
        return (
            self._int_value(item.values.get(self.source.columns.crm_create_count)) == 0
            and self._normalized_text(item.status) == ItemStatus.RECREATE_PENDING.value
        )

    @staticmethod
    def _normalized_text(value: object) -> str:
        return " ".join(str(value or "").split()).casefold()

    @staticmethod
    def _lead_stage_id(lead: dict[str, object]) -> str:
        for key in ("funnel", "funnel_id", "stage", "stage_id", "step", "step_id"):
            value = lead.get(key)
            if isinstance(value, dict):
                value = value.get("id")
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _int_value(value: object) -> int:
        try:
            return int(float(str(value or "0").strip()))
        except (TypeError, ValueError):
            return 0

    def _next_phone_retry_at(self) -> str:
        return (
            datetime.now(UTC) + timedelta(minutes=self.settings.phone_retry_delay_minutes)
        ).isoformat(timespec="seconds")

    def _crm_monitor_expired(self, item: QueueItem) -> bool:
        raw = str(item.values.get(self.source.columns.processed_at, "") or "").strip()
        if not raw:
            return False
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except ValueError:
            return False
        age = datetime.now(UTC) - started.astimezone(UTC)
        return age >= timedelta(hours=self.settings.crm_monitor_max_hours)

    def _notify_completion(self, notifier: NotificationRouter, summary: RunSummary) -> None:
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
            LOGGER.warning("Итоговое уведомление не доставлено (%s)", exc.__class__.__name__)

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

    @staticmethod
    def _report_phase(callback: Callable[[str], None] | None, message: str) -> None:
        if callback is None:
            return
        try:
            callback(message)
        except Exception as exc:
            LOGGER.warning("Не удалось обновить этап работы пульта: %s", exc)

    def _eligible_for_mode(self, item: QueueItem) -> bool:
        phone = normalize_phone(str(item.values.get(self.source.columns.phone, "") or ""))
        status = (item.status or "").strip().lower()
        if not self.live and status == ItemStatus.CAPTURED:
            return False
        if self.mode == "capture":
            return not phone and status != ItemStatus.CAPTURED
        if self.mode == "crm":
            return self._is_repeat_flow(item) or bool(phone)
        return True

    def _goal_reached(self, summary: RunSummary, limit: int) -> bool:
        if limit == 0:
            return False
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
                funnel_stage=str(existing.get("funnel_stage", "")),
                crm_create_count=int(existing.get("crm_create_count", 0) or 0),
                repeat_crm_lead_id=str(existing.get("repeat_crm_lead_id", "")),
                repeat_phone_attempts=int(existing.get("repeat_phone_attempts", 0) or 0),
                next_retry_at=str(existing.get("next_retry_at", "")),
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
        item.values[self.source.columns.crm_lead_id] = patch.crm_lead_id
        item.values[self.source.columns.error] = patch.error
        item.values[self.source.columns.processed_at] = patch.processed_at
        item.values[self.source.columns.run_id] = patch.run_id
        if patch.funnel_stage is not None:
            item.values[self.source.columns.funnel_stage] = patch.funnel_stage
        if patch.crm_create_count is not None:
            item.values[self.source.columns.crm_create_count] = patch.crm_create_count
        if patch.repeat_crm_lead_id is not None:
            item.values[self.source.columns.repeat_crm_lead_id] = patch.repeat_crm_lead_id
        if patch.repeat_phone_attempts is not None:
            item.values[self.source.columns.repeat_phone_attempts] = patch.repeat_phone_attempts
        if patch.next_retry_at is not None:
            item.values[self.source.columns.next_retry_at] = patch.next_retry_at

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
        repeat_phone_attempts: int | None = None,
        next_retry_at: str | None = "",
    ) -> None:
        self._finalize(
            canonical_url,
            item,
            QueuePatch(
                status=status,
                attempts=attempts,
                phone=phone,
                crm_lead_id=str(item.values.get(self.source.columns.crm_lead_id, "") or ""),
                error=detail[:500],
                processed_at=utc_now(),
                run_id=run_id,
                repeat_phone_attempts=repeat_phone_attempts,
                next_retry_at=next_retry_at,
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
