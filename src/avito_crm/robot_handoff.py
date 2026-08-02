from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import mimetypes
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from avito_crm.config import Settings
from avito_crm.crm import LpTrackerClient
from avito_crm.errors import AppError, ConfigurationError, CrmError, ManualReviewRequired
from avito_crm.models import CrmDestination
from avito_crm.phone import extract_phones, normalize_phone
from avito_crm.state import StateStore

LOGGER = logging.getLogger(__name__)
_OUTBOUND_TYPES = {
    "out",
    "outgoing",
    "outbound",
    "исходящий",
    "исходящий звонок",
}
_AUDIO_MIME_BY_SUFFIX = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    status: str
    phone: str
    confidence: float
    phone_count: int
    transcript: str = ""


@dataclass(slots=True)
class HandoffSummary:
    inspected: int = 0
    eligible: int = 0
    completed: int = 0
    ready: int = 0
    manual_required: int = 0
    errors: int = 0
    skipped: int = 0
    details: list[str] = field(default_factory=list)


class GeminiPhoneTranscriber:
    """Download one call recording and extract one dictated Russian phone."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=False,
            headers={"User-Agent": "AvitoCRM/robot-handoff"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> GeminiPhoneTranscriber:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def transcribe(self, recording_url: str) -> TranscriptionResult:
        if not self.settings.gemini_api_key:
            raise ConfigurationError("Для распознавания не заполнен GEMINI_API_KEY")
        audio, mime_type = self._download(recording_url)
        endpoint = (
            f"{self.settings.gemini_api_base_url}/v1beta/models/"
            f"{self.settings.gemini_model}:generateContent"
        )
        response = self.client.post(
            endpoint,
            headers={"x-goog-api-key": self.settings.gemini_api_key},
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": _TRANSCRIPTION_PROMPT},
                            {
                                "inline_data": {
                                    "mime_type": mime_type,
                                    "data": base64.b64encode(audio).decode("ascii"),
                                }
                            },
                        ],
                    }
                ],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseSchema": {
                        "type": "object",
                        "required": ["status", "phone", "confidence", "phone_count"],
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["ok", "no_phone", "ambiguous", "not_speech"],
                            },
                            "phone": {"type": "string"},
                            "confidence": {"type": "number"},
                            "phone_count": {"type": "integer"},
                            "transcript": {"type": "string"},
                        },
                    },
                },
            },
        )
        if response.status_code >= 400:
            raise AppError(
                f"Сервис распознавания временно не обработал запись "
                f"(HTTP {response.status_code})"
            )
        try:
            payload = response.json()
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AppError(
                "Сервис распознавания вернул неполный или некорректный результат"
            ) from exc
        return self._validate_result(parsed)

    def _validate_result(self, parsed: object) -> TranscriptionResult:
        if not isinstance(parsed, dict):
            raise AppError("Сервис распознавания вернул результат неожиданного формата")
        status = str(parsed.get("status", "")).strip().lower()
        transcript = str(parsed.get("transcript", "") or "").strip()
        try:
            confidence = float(parsed.get("confidence", 0))
            phone_count = int(parsed.get("phone_count", 0))
        except (TypeError, ValueError) as exc:
            raise AppError(
                "Сервис распознавания не указал уверенность или число телефонов"
            ) from exc
        phone = normalize_phone(str(parsed.get("phone", "") or "")) or ""
        if status != "ok" or phone_count != 1 or not phone:
            raise ManualReviewRequired(
                "В разговоре не найден один однозначно продиктованный номер"
            )
        if confidence < self.settings.robot_handoff_min_confidence:
            raise ManualReviewRequired(
                "Уверенность распознавания номера ниже безопасного порога"
            )
        transcript_phones = extract_phones(transcript)
        if transcript_phones != [phone]:
            raise ManualReviewRequired(
                "Контрольный фрагмент и итоговый номер распознавания не совпали однозначно"
            )
        return TranscriptionResult(status, phone, confidence, phone_count, transcript)

    def _download(self, url: str) -> tuple[bytes, str]:
        current = _validated_https_url(url)
        for _redirect in range(6):
            with self.client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise AppError("Ссылка записи звонка содержит пустое перенаправление")
                    current = _validated_https_url(str(response.url.join(location)))
                    continue
                if response.status_code >= 400:
                    raise AppError(
                        f"Не удалось скачать запись звонка (HTTP {response.status_code})"
                    )
                declared_length = _safe_int(response.headers.get("content-length"))
                if declared_length > self.settings.gemini_max_audio_bytes:
                    raise AppError(
                        "Запись звонка превышает безопасный размер для распознавания"
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.settings.gemini_max_audio_bytes:
                        raise AppError(
                            "Запись звонка превышает безопасный размер для распознавания"
                        )
                    chunks.append(chunk)
                if total == 0:
                    raise AppError("LPTracker вернул пустую запись звонка")
                mime_type = _audio_mime_type(
                    response.headers.get("content-type", ""),
                    str(response.url),
                )
                return b"".join(chunks), mime_type
        raise AppError("Слишком много перенаправлений при скачивании записи звонка")


class RobotLeadHandoff:
    """Move safe robot-call leads to the next funnel in an idempotent order."""

    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        *,
        crm: LpTrackerClient | None = None,
        transcriber: GeminiPhoneTranscriber | None = None,
        manual_notifier: Callable[[str, str], None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.crm = crm or LpTrackerClient(settings)
        self.transcriber = transcriber or GeminiPhoneTranscriber(settings)
        self.manual_notifier = manual_notifier
        self.now_provider = now_provider
        self._owns_crm = crm is None
        self._owns_transcriber = transcriber is None

    def close(self) -> None:
        if self._owns_transcriber:
            self.transcriber.close()
        if self._owns_crm:
            self.crm.__exit__()

    def __enter__(self) -> RobotLeadHandoff:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def run_once(
        self,
        *,
        apply: bool = False,
        lead_id: str | int | None = None,
        limit: int | None = None,
        retry_analysis: bool = False,
    ) -> HandoffSummary:
        summary = HandoffSummary()
        base_destination = self.crm.resolve_destination()
        project_id = base_destination.project_id
        handoff_destination = self.crm.resolve_custom_destination(
            project_id,
            self.settings.robot_handoff_field_name,
            self.settings.robot_handoff_field_value,
            project_name=base_destination.project_name,
        )
        stage_date_template = self.crm.resolve_custom_destination(
            project_id,
            self.settings.robot_handoff_stage_date_field_name,
            "",
            project_name=base_destination.project_name,
        )
        if stage_date_template.field_type not in {"date", "funnel_date"}:
            raise ConfigurationError(
                f"Поле {stage_date_template.field_name!r} должно иметь тип "
                f"date или funnel_date, "
                f"получен {stage_date_template.field_type or 'неизвестный тип'}"
            )
        steps = self.crm.list_funnel_steps(project_id)
        source_step = _resolve_step(steps, self.settings.robot_handoff_source_funnel_name)
        target_step = _resolve_step(steps, self.settings.robot_handoff_target_funnel_name)
        candidates = self._candidates(project_id, lead_id)
        batch_limit = limit or self.settings.robot_handoff_batch_size
        explain_skips = lead_id is not None
        for candidate in candidates:
            if summary.eligible >= batch_limit:
                break
            summary.inspected += 1
            candidate_id = str(candidate.get("id", "")).strip()
            if not candidate_id:
                _record_skip(
                    summary,
                    "без ID",
                    "LPTracker не вернул ID лида",
                    explain=explain_skips,
                )
                continue
            lead = candidate
            record: dict[str, Any] | None = None
            try:
                stage_name = self.crm.get_lead_stage_name(
                    candidate_id,
                    project_id=project_id,
                    funnel_steps=steps,
                    lead=lead,
                )
                if _normalized(stage_name) != _normalized(source_step["name"]):
                    current_stage = stage_name or "не определён"
                    _record_skip(
                        summary,
                        candidate_id,
                        f"текущий шаг «{current_stage}»; нужен "
                        f"«{source_step['name']}»",
                        explain=explain_skips,
                    )
                    continue
                if lead_id is None:
                    lead = self.crm.get_lead(candidate_id)
                    stage_name = self.crm.get_lead_stage_name(
                        candidate_id,
                        project_id=project_id,
                        funnel_steps=steps,
                        lead=lead,
                    )
                    if _normalized(stage_name) != _normalized(source_step["name"]):
                        summary.skipped += 1
                        continue
                has_source_tag = _custom_has_any_value(
                    lead,
                    handoff_destination.field_id,
                    self.settings.robot_handoff_source_field_values,
                )
                has_target_tag = _custom_has_value(lead, handoff_destination)
                if not has_source_tag and not has_target_tag:
                    _record_skip(
                        summary,
                        candidate_id,
                        f"в категории «{handoff_destination.field_name}» не выбран "
                        "разрешённый тег сбора",
                        explain=explain_skips,
                    )
                    continue
                record = _latest_successful_outgoing_record(lead)
                if record is None:
                    _record_skip(
                        summary,
                        candidate_id,
                        "нет успешной исходящей записи со ссылкой на аудио",
                        explain=explain_skips,
                    )
                    continue
                if not has_source_tag:
                    cached = self.state.get_robot_handoff(candidate_id)
                    if not _is_resumable_partial_handoff(cached, record):
                        _record_skip(
                            summary,
                            candidate_id,
                            f"в категории «{handoff_destination.field_name}» не выбран "
                            "разрешённый тег сбора",
                            explain=explain_skips,
                        )
                        continue
                summary.eligible += 1
                self._handle_one(
                    lead,
                    record,
                    handoff_destination,
                    stage_date_template,
                    target_step,
                    steps,
                    apply=apply,
                    retry_analysis=retry_analysis,
                )
                if apply:
                    summary.completed += 1
                    summary.details.append(f"Лид {candidate_id}: обработан")
                else:
                    summary.ready += 1
                    summary.details.append(f"Лид {candidate_id}: готов к безопасному обновлению")
            except ManualReviewRequired as exc:
                message = _safe_reason(exc)
                cached = self.state.get_robot_handoff(candidate_id)
                record_key = _record_key(record)
                first_manual = not cached or cached.get("status") != "manual_required"
                self.state.record_robot_handoff(
                    candidate_id,
                    record_key=record_key,
                    phone=str((cached or {}).get("phone", "") or ""),
                    status="manual_required",
                    error=message,
                )
                summary.manual_required += 1
                summary.details.append(f"Лид {candidate_id}: ручная проверка — {message}")
                if first_manual and self.manual_notifier:
                    try:
                        self.manual_notifier(candidate_id, message)
                    except Exception as notify_exc:
                        LOGGER.warning(
                            "Не удалось отправить уведомление по лиду %s: %s",
                            candidate_id,
                            notify_exc.__class__.__name__,
                        )
            except AppError as exc:
                message = _safe_reason(exc)
                cached = self.state.get_robot_handoff(candidate_id)
                self.state.record_robot_handoff(
                    candidate_id,
                    record_key=_record_key(record)
                    or str((cached or {}).get("record_key", "") or ""),
                    phone=str((cached or {}).get("phone", "") or ""),
                    status="error",
                    error=message,
                )
                summary.errors += 1
                summary.details.append(
                    f"Лид {candidate_id}: временная техническая ошибка; будет повтор"
                )
                LOGGER.warning(
                    "Техническая ошибка обработки лида %s: %s",
                    candidate_id,
                    exc.__class__.__name__,
                )
            except Exception as exc:
                cached = self.state.get_robot_handoff(candidate_id)
                self.state.record_robot_handoff(
                    candidate_id,
                    record_key=_record_key(record)
                    or str((cached or {}).get("record_key", "") or ""),
                    phone=str((cached or {}).get("phone", "") or ""),
                    status="error",
                    error=exc.__class__.__name__,
                )
                summary.errors += 1
                LOGGER.exception(
                    "Техническая ошибка фоновой обработки лида %s: %s",
                    candidate_id,
                    exc.__class__.__name__,
                )
                summary.details.append(f"Лид {candidate_id}: техническая ошибка")
        return summary

    def _candidates(
        self, project_id: int, lead_id: str | int | None
    ) -> Iterator[dict[str, Any]]:
        if lead_id is not None:
            yield self.crm.get_lead(str(lead_id).strip())
            return
        updated_from = int(
            (datetime.now(UTC) - timedelta(hours=self.settings.robot_handoff_lookback_hours))
            .timestamp()
        )
        seen: set[str] = set()
        # LPTracker currently caps this endpoint at 100 rows even when a larger
        # limit is requested. Keeping the observed page size prevents a full
        # first page from being mistaken for the end of the result set.
        page_size = 100
        scan_limit = 500
        for offset in range(0, scan_limit, page_size):
            request_limit = min(page_size, scan_limit - offset)
            page = self.crm.list_recent_leads(
                project_id,
                updated_from=updated_from,
                limit=request_limit,
                offset=offset,
            )
            new_items = 0
            for candidate in page:
                candidate_id = str(candidate.get("id", "")).strip()
                if candidate_id and candidate_id not in seen:
                    seen.add(candidate_id)
                    new_items += 1
                    yield candidate
            if len(page) < request_limit or not new_items:
                break

    def _handle_one(
        self,
        lead: dict[str, Any],
        record: dict[str, Any],
        handoff_destination: CrmDestination,
        stage_date_template: CrmDestination,
        target_step: dict[str, Any],
        steps: list[dict[str, Any]],
        *,
        apply: bool,
        retry_analysis: bool,
    ) -> None:
        lead_id = str(lead["id"]).strip()
        record_key = _record_key(record)
        cached = self.state.get_robot_handoff(lead_id)
        phone = ""
        stage_due_date = ""
        if cached and cached.get("record_key") == record_key:
            phone = normalize_phone(str(cached.get("phone", ""))) or ""
            stage_due_date = str(cached.get("stage_due_date", "") or "").strip()
            if (
                not phone
                and cached.get("status") == "manual_required"
                and not retry_analysis
            ):
                raise ManualReviewRequired(
                    str(cached.get("error") or "Требуется ручная проверка записи")
                )
        if not phone:
            result = self.transcriber.transcribe(_recording_url(record))
            phone = result.phone
        if not stage_due_date:
            stage_due_date = self._new_stage_due_date()
        if not phone or not stage_due_date:
            raise AppError("Не удалось подготовить безопасное состояние передачи лида")
        if (
            not cached
            or cached.get("record_key") != record_key
            or normalize_phone(str(cached.get("phone", ""))) != phone
            or str(cached.get("stage_due_date", "") or "").strip() != stage_due_date
        ):
            self.state.record_robot_handoff(
                lead_id,
                record_key=record_key,
                phone=phone,
                stage_due_date=stage_due_date,
                status="recognized",
            )
        if not apply:
            return

        current = self._ensure_phone(lead, phone)
        if not _custom_has_value(current, handoff_destination):
            self.crm.update_lead_custom(lead_id, handoff_destination)
            current = self.crm.get_lead(lead_id)
            if not _custom_has_value(current, handoff_destination):
                raise CrmError("LPTracker не подтвердил установку тега передачи")

        stage_date_destination = CrmDestination(
            project_id=stage_date_template.project_id,
            project_name=stage_date_template.project_name,
            field_id=stage_date_template.field_id,
            field_name=stage_date_template.field_name,
            field_type=stage_date_template.field_type,
            field_value=stage_due_date,
        )
        if not _custom_date_has_value(current, stage_date_destination):
            self.crm.update_lead_custom(lead_id, stage_date_destination)
            current = self.crm.get_lead(lead_id)
            if not _custom_date_has_value(current, stage_date_destination):
                raise CrmError("LPTracker не подтвердил дату шага через два дня")

        current_stage = self.crm.get_lead_stage_name(
            lead_id,
            project_id=handoff_destination.project_id,
            funnel_steps=steps,
            lead=current,
        )
        if _normalized(current_stage) != _normalized(str(target_step["name"])):
            self.crm.set_lead_funnel(lead_id, int(target_step["id"]))
            current = self.crm.get_lead(lead_id)
            verified_stage = self.crm.get_lead_stage_name(
                lead_id,
                project_id=handoff_destination.project_id,
                funnel_steps=steps,
                lead=current,
            )
            if _normalized(verified_stage) != _normalized(str(target_step["name"])):
                raise CrmError("LPTracker не подтвердил перевод на шаг «Новый лид»")
        self.state.record_robot_handoff(
            lead_id,
            record_key=record_key,
            phone=phone,
            stage_due_date=stage_due_date,
            status="completed",
        )

    def _new_stage_due_date(self) -> str:
        try:
            timezone = ZoneInfo(self.settings.lptracker_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(
                f"Неизвестный часовой пояс LPTRACKER_TIMEZONE: "
                f"{self.settings.lptracker_timezone!r}"
            ) from exc
        current = self.now_provider() if self.now_provider else datetime.now(timezone)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone)
        else:
            current = current.astimezone(timezone)
        due = current + timedelta(days=self.settings.robot_handoff_stage_delay_days)
        return due.strftime("%d.%m.%Y %H:%M")

    def _ensure_phone(self, lead: dict[str, Any], phone: str) -> dict[str, Any]:
        lead_id = str(lead["id"]).strip()
        details = _phone_details(lead)
        if len(details) != 1:
            raise ManualReviewRequired(
                "В карточке найдено не ровно одно телефонное поле; автоматическая замена отменена"
            )
        current = normalize_phone(_detail_value(details[0]))
        if current != phone:
            detail_id = str(details[0].get("id", "")).strip()
            if not detail_id:
                raise ManualReviewRequired("У телефонного поля LPTracker отсутствует ID")
            self.crm.update_contact_detail(detail_id, phone)
            lead = self.crm.get_lead(lead_id)
            verified = _phone_details(lead)
            if len(verified) != 1 or normalize_phone(_detail_value(verified[0])) != phone:
                raise CrmError("LPTracker не подтвердил точную замену временного номера")
        return lead


def _resolve_step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [step for step in steps if _normalized(step.get("name")) == _normalized(name)]
    if len(matches) != 1 or not str(matches[0].get("id", "")).strip():
        raise ConfigurationError(f"Шаг воронки {name!r} отсутствует или неоднозначен")
    return matches[0]


def _record_skip(
    summary: HandoffSummary,
    lead_id: str,
    reason: str,
    *,
    explain: bool,
) -> None:
    summary.skipped += 1
    if explain:
        summary.details.append(f"Лид {lead_id}: пропущен — {reason}")


def _latest_successful_outgoing_record(lead: dict[str, Any]) -> dict[str, Any] | None:
    records: list[dict[str, Any]] = []
    for key in ("calls_records", "call_records", "calls"):
        value = lead.get(key)
        if isinstance(value, list):
            records.extend(record for record in value if isinstance(record, dict))
    eligible: list[dict[str, Any]] = []
    for record in records:
        if not _recording_url(record):
            continue
        call_type = _normalized(record.get("type") or record.get("direction"))
        if call_type not in _OUTBOUND_TYPES:
            continue
        duration = record.get("duration", record.get("billsec", 1))
        if duration not in (None, "") and _safe_float(duration) <= 0:
            continue
        eligible.append(record)
    return max(eligible, key=_record_sort_key) if eligible else None


def _recording_url(record: dict[str, Any]) -> str:
    for key in ("record", "record_url", "recording", "url"):
        value = str(record.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _record_sort_key(record: dict[str, Any]) -> tuple[float, str]:
    for key in ("time", "created_at", "started_at", "date"):
        raw = record.get(key)
        if raw in (None, ""):
            continue
        numeric = _safe_float(raw)
        if numeric > 0:
            return numeric, str(raw)
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp(), text
        except ValueError:
            return 0.0, text
    return 0.0, ""


def _record_key(record: dict[str, Any] | None) -> str:
    if not record:
        return ""
    material = "|".join(
        str(record.get(key, "") or "")
        for key in (
            "linkedid",
            "time",
            "created_at",
            "started_at",
            "date",
            "duration",
            "record",
            "record_url",
            "recording",
            "url",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _phone_details(lead: dict[str, Any]) -> list[dict[str, Any]]:
    containers: list[object] = []
    contact = lead.get("contact")
    if isinstance(contact, dict):
        containers.extend((contact.get("details"), contact.get("contacts")))
    containers.extend((lead.get("details"), lead.get("contact_details")))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for container in containers:
        if not isinstance(container, list):
            continue
        for detail in container:
            if not isinstance(detail, dict):
                continue
            kind = _normalized(detail.get("type") or detail.get("name"))
            if "phone" not in kind and "телефон" not in kind:
                continue
            identity = str(detail.get("id", "")) or json.dumps(detail, sort_keys=True)
            if identity not in seen:
                seen.add(identity)
                result.append(detail)
    return result


def _detail_value(detail: dict[str, Any]) -> str:
    return str(detail.get("data", detail.get("value", "")) or "")


def _custom_has_value(lead: dict[str, Any], destination: CrmDestination) -> bool:
    values = _custom_field_values(lead, destination.field_id)
    expected = {_normalized(value) for value in _flatten_values(destination.field_value)}
    actual = {_normalized(value) for value in _flatten_values(values)}
    return bool(expected) and expected.issubset(actual)


def _custom_has_any_value(
    lead: dict[str, Any], field_id: str | int, expected_values: tuple[str, ...]
) -> bool:
    expected = {_normalized(value) for value in expected_values if _normalized(value)}
    actual = {
        _normalized(value)
        for value in _flatten_values(_custom_field_values(lead, field_id))
        if _normalized(value)
    }
    return bool(expected & actual)


def _custom_field_values(lead: dict[str, Any], field_id: str | int) -> list[object]:
    custom = lead.get("custom") or []
    values: list[object] = []
    if isinstance(custom, dict):
        for key, item in custom.items():
            if str(key) == str(field_id):
                values.append(item.get("value") if isinstance(item, dict) else item)
            elif isinstance(item, dict) and str(item.get("id", "")) == str(field_id):
                values.append(item.get("value"))
    elif isinstance(custom, list):
        values.extend(
            item.get("value")
            for item in custom
            if isinstance(item, dict)
            and str(item.get("id", "")) == str(field_id)
        )
    return values


def _is_resumable_partial_handoff(
    cached: dict[str, Any] | None, record: dict[str, Any]
) -> bool:
    if not cached or cached.get("status") not in {"recognized", "error"}:
        return False
    return bool(
        normalize_phone(str(cached.get("phone", "")))
        and str(cached.get("stage_due_date", "") or "").strip()
        and str(cached.get("record_key", "") or "").strip() == _record_key(record)
    )


def _custom_date_has_value(lead: dict[str, Any], destination: CrmDestination) -> bool:
    expected = _date_time_prefix(destination.field_value)
    if not expected:
        return False
    custom = lead.get("custom") or []
    values: list[object] = []
    if isinstance(custom, dict):
        for key, item in custom.items():
            if str(key) == str(destination.field_id):
                values.append(item.get("value") if isinstance(item, dict) else item)
            elif isinstance(item, dict) and str(item.get("id", "")) == str(
                destination.field_id
            ):
                values.append(item.get("value"))
    elif isinstance(custom, list):
        values.extend(
            item.get("value")
            for item in custom
            if isinstance(item, dict)
            and str(item.get("id", "")) == str(destination.field_id)
        )
    return any(_date_time_prefix(value) == expected for value in _flatten_values(values))


def _date_time_prefix(value: object) -> str:
    match = re.search(
        r"\b(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})(?::\d{2})?\b",
        str(value or ""),
    )
    return f"{match.group(1)} {match.group(2)}" if match else ""


def _flatten_values(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(item) for item in value.values() if item not in (None, "")]
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_values(item))
        return result
    return [] if value in (None, "") else [str(value)]


def _validated_https_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise AppError("Ссылка записи звонка должна быть безопасной HTTPS-ссылкой")
    if host == "localhost" or host.endswith(".local"):
        raise AppError("Локальные адреса запрещены для скачивания записи")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise AppError("Внутренние IP-адреса запрещены для скачивания записи")
    return parsed.geturl()


def _audio_mime_type(content_type: str, url: str) -> str:
    declared = str(content_type or "").split(";", 1)[0].strip().lower()
    if declared.startswith("audio/") or declared in {"video/mp4", "video/webm"}:
        return declared
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    inferred = _AUDIO_MIME_BY_SUFFIX.get(suffix) or mimetypes.types_map.get(suffix, "")
    if inferred.startswith("audio/") or inferred in {"video/mp4", "video/webm"}:
        return inferred
    raise ManualReviewRequired(
        "Формат записи звонка не распознан как поддерживаемое аудио"
    )


def _normalized(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _safe_int(value: object) -> int:
    try:
        return int(str(value or "0").strip())
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        return float(str(value or "0").strip().replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _safe_reason(exc: BaseException) -> str:
    text = re.sub(r"https?://\S+", "[ссылка скрыта]", str(exc)).strip()
    return text[:500] or exc.__class__.__name__


_TRANSCRIPTION_PROMPT = """
Проанализируй запись исходящего звонка. Найди только постоянный российский номер телефона,
который собеседник явно и полностью продиктовал в разговоре. Не используй номер дозвона,
служебные номера, цифры из приветствия, даты или цены. Не угадывай пропущенные цифры.
Верни status=ok только если найден ровно один однозначный номер из 11 цифр, начинающийся
с 7 или 8; phone верни в формате +7XXXXXXXXXX. Если продиктовано несколько номеров,
часть номера неразборчива или есть сомнение, верни ambiguous/no_phone и пустой phone.
phone_count — число разных полностью продиктованных номеров. transcript — короткий фрагмент
с произнесённым номером, без остального разговора; распознанный номер обязательно
продублируй в этом фрагменте цифрами в формате +7XXXXXXXXXX.
""".strip()
