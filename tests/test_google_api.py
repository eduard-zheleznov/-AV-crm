from __future__ import annotations

from http.client import RemoteDisconnected

import pytest

from avito_crm.google_api import google_api_call


class FakeResponse:
    def __init__(self, status_code: int, retry_after: str | None = None):
        self.status_code = status_code
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


class FakeGoogleError(RuntimeError):
    def __init__(self, status_code: int, retry_after: str | None = None):
        super().__init__(f"Google error [{status_code}]")
        self.response = FakeResponse(status_code, retry_after)


def test_google_api_call_retries_rate_limit_and_recovers():
    calls = 0
    sleeps: list[float] = []

    def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise FakeGoogleError(429)
        return "ok"

    result = google_api_call(
        operation,
        label="test",
        attempts=3,
        sleeper=sleeps.append,
        jitter=lambda _start, _end: 0.0,
    )

    assert result == "ok"
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_google_api_call_honors_retry_after():
    sleeps: list[float] = []
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FakeGoogleError(429, "7")
        return "ok"

    assert (
        google_api_call(
            operation,
            label="test",
            attempts=2,
            sleeper=sleeps.append,
            jitter=lambda _start, _end: 0.0,
        )
        == "ok"
    )
    assert sleeps == [7.0]


def test_google_api_call_does_not_retry_non_transient_error():
    sleeps: list[float] = []

    with pytest.raises(FakeGoogleError):
        google_api_call(
            lambda: (_ for _ in ()).throw(FakeGoogleError(403)),
            label="test",
            sleeper=sleeps.append,
        )

    assert sleeps == []


def test_google_api_call_retries_nested_remote_disconnect_and_recovers():
    calls = 0
    sleeps: list[float] = []

    def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(
                "Connection aborted.",
                RemoteDisconnected("Remote end closed connection without response"),
            )
        return "ok"

    assert (
        google_api_call(
            operation,
            label="test",
            attempts=2,
            sleeper=sleeps.append,
            jitter=lambda _start, _end: 0.0,
        )
        == "ok"
    )
    assert calls == 2
    assert sleeps == [1.0]


def test_google_api_call_retries_timeout_and_reraises_after_limit():
    sleeps: list[float] = []
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        raise TimeoutError("temporary read timeout")

    with pytest.raises(TimeoutError, match="temporary read timeout"):
        google_api_call(
            operation,
            label="test",
            attempts=3,
            sleeper=sleeps.append,
            jitter=lambda _start, _end: 0.0,
        )

    assert calls == 3
    assert sleeps == [1.0, 2.0]
