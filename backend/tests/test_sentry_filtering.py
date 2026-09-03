"""
SYMBOLOS-BACKEND-V (Sentry): Anthropic account-level usage-cap errors
("You have reached your specified API usage limits...") were reported as
unhandled, high-priority bugs by Sentry's Anthropic SDK auto-instrumentation
— even though every AI call site already has a broad except Exception
around it. This isn't a code defect; it's an expected, self-resolving
condition. _sentry_before_send downgrades (not drops) these specifically,
so a genuinely different Anthropic error (bad request, actual rate limit)
still reports normally.
"""
from __future__ import annotations

import httpx
import pytest

import api.main as app_main


def _usage_cap_error():
    import anthropic
    resp = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "You have reached your specified API usage limits. You will regain access on 2026-09-01 at 00:00 UTC.",
        },
    }
    return anthropic.BadRequestError("usage limit", response=resp, body=body)


def _other_bad_request_error():
    import anthropic
    resp = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": "model: unknown model"}}
    return anthropic.BadRequestError("bad model", response=resp, body=body)


class TestSentryBeforeSendDowngradesUsageCapErrors:
    def test_usage_cap_error_is_downgraded_and_fingerprinted(self):
        event = {"level": "error"}
        hint = {"exc_info": (type(_usage_cap_error()), _usage_cap_error(), None)}
        result = app_main._sentry_before_send(event, hint)
        assert result["level"] == "warning"
        assert result["fingerprint"] == ["anthropic-account-usage-cap-reached"]

    def test_different_bad_request_error_passes_through_unchanged(self):
        event = {"level": "error"}
        hint = {"exc_info": (type(_other_bad_request_error()), _other_bad_request_error(), None)}
        result = app_main._sentry_before_send(event, hint)
        assert result["level"] == "error"
        assert "fingerprint" not in result

    def test_unrelated_exception_passes_through_unchanged(self):
        exc = ValueError("something else entirely")
        event = {"level": "error"}
        hint = {"exc_info": (ValueError, exc, None)}
        result = app_main._sentry_before_send(event, hint)
        assert result["level"] == "error"
        assert "fingerprint" not in result

    def test_missing_exc_info_does_not_crash(self):
        event = {"level": "error"}
        result = app_main._sentry_before_send(event, {})
        assert result is event
