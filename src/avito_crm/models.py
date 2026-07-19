from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ItemStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_PHONE = "retry_phone"
    RETRY_TECHNICAL = "retry_technical"
    CAPTURED = "captured"
    DONE = "done"
    DUPLICATE = "duplicate"
    INACTIVE = "inactive"
    UNAVAILABLE = "unavailable"
    NO_PHONE = "no_phone"
    ERROR = "error"
    MANUAL_REQUIRED = "manual_required"
    INVALID = "invalid"


TERMINAL_STATUSES = {
    ItemStatus.DONE.value,
    ItemStatus.DUPLICATE.value,
    ItemStatus.INACTIVE.value,
    ItemStatus.UNAVAILABLE.value,
    ItemStatus.NO_PHONE.value,
    ItemStatus.INVALID.value,
}


@dataclass(slots=True)
class QueueItem:
    row_id: str
    url: str
    status: str = ""
    attempts: int = 0
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PhoneResult:
    phone: str
    source: str
    confidence: float = 1.0
    raw_text: str = ""


@dataclass(slots=True)
class CrmDestination:
    project_id: int
    project_name: str
    field_id: int
    field_name: str
    field_type: str
    field_value: Any


@dataclass(slots=True)
class CrmWriteResult:
    status: ItemStatus
    contact_id: str | None = None
    lead_id: str | None = None
    detail: str = ""


@dataclass(slots=True)
class RunSummary:
    run_id: str
    requested: int
    captured: int = 0
    created: int = 0
    duplicates: int = 0
    errors: int = 0
    invalid: int = 0
    inactive: int = 0
    unavailable: int = 0
    phone_failed: int = 0
    retries: int = 0
    manual_required: int = 0
    inspected: int = 0
    processed: int = 0
    rounds: int = 0
    stopped_reason: str = ""


@dataclass(slots=True)
class QueuePatch:
    status: str
    attempts: int
    phone: str = ""
    crm_lead_id: str = ""
    error: str = ""
    processed_at: str = ""
    run_id: str = ""
