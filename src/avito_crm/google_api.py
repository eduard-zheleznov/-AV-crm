from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from http.client import RemoteDisconnected
from typing import TypeVar

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")
RETRYABLE_GOOGLE_STATUSES = {429, 500, 502, 503, 504}
RETRYABLE_TRANSPORT_NAMES = {
    "ChunkedEncodingError",
    "ConnectTimeout",
    "ConnectionError",
    "MaxRetryError",
    "NewConnectionError",
    "ProtocolError",
    "ProxyError",
    "ReadTimeout",
}
RETRYABLE_TRANSPORT_MODULES = ("requests.", "urllib3.")


def google_api_call(
    operation: Callable[[], T],
    *,
    label: str,
    attempts: int = 7,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleeper: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> T:
    """Run a Google API operation with bounded transient-failure backoff."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            status = _google_status(exc)
            transport_error = _is_retryable_transport_error(exc)
            retryable = status in RETRYABLE_GOOGLE_STATUSES or transport_error
            if not retryable or attempt + 1 >= attempts:
                raise

            retry_after = _retry_after_seconds(exc)
            exponential = min(max_delay, base_delay * (2**attempt))
            delay = max(retry_after, exponential + jitter(0.0, min(1.0, exponential * 0.1)))
            LOGGER.warning(
                "%s: временный сбой Google API (%s); повтор %s/%s через %.1f сек.",
                label,
                status if status is not None else exc.__class__.__name__,
                attempt + 2,
                attempts,
                delay,
            )
            sleeper(delay)

    raise AssertionError("unreachable")


def _is_retryable_transport_error(exc: Exception) -> bool:
    """Recognize connection failures without retrying arbitrary application errors."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    builtin_types = (
        BrokenPipeError,
        ConnectionAbortedError,
        ConnectionError,
        ConnectionResetError,
        RemoteDisconnected,
        TimeoutError,
    )

    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)

        if isinstance(current, builtin_types):
            return True
        error_type = type(current)
        if error_type.__name__ in RETRYABLE_TRANSPORT_NAMES and error_type.__module__.startswith(
            RETRYABLE_TRANSPORT_MODULES
        ):
            return True

        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)
        pending.extend(argument for argument in current.args if isinstance(argument, BaseException))

    return False


def _google_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    raw_status = getattr(response, "status_code", None)
    if raw_status is None:
        raw_status = getattr(exc, "status_code", None)
    try:
        return int(raw_status)
    except (TypeError, ValueError):
        message = str(exc)
        for status in RETRYABLE_GOOGLE_STATUSES:
            if f"[{status}]" in message or f" {status}:" in message:
                return status
        return None


def _retry_after_seconds(exc: Exception) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return 0.0
    try:
        value = headers.get("Retry-After")
        return max(0.0, float(value)) if value is not None else 0.0
    except (AttributeError, TypeError, ValueError):
        return 0.0
