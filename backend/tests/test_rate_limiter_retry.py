"""
SYMBOLOS-BACKEND-19: the rate limiter degraded to a 3rpm in-memory fallback on
a SINGLE transient Supabase blip.

is_allowed() called the DB exactly once and treated ANY exception — including
httpx.ConnectError, the exact class of error the rest of the codebase already
retries via with_retry()/_is_connection_error() — as "Supabase is down,
degrade". Every other Supabase call site in this app got that resilience when
it was added; this one, written earlier, never did.

Root-caused by probing production directly: the rate_limits table and the
increment_rate_limit RPC both worked on request. The [SECURITY] log firing
repeatedly was consistent with the limiter never retrying a one-off blip
before giving up — these tests pin that a single connection error is now
absorbed by with_retry rather than immediately degrading enforcement.
"""
import httpx
import pytest

from api import main as app_main
from api.utils import supabase_client as sc


class _Result:
    """A no-args-bound-as-self trap: a zero-arg lambda stored via
    type(...) becomes a class attribute, and accessing it through an
    instance makes Python's descriptor protocol bind it as a method —
    instance.execute() then calls the lambda WITH self, which a genuinely
    zero-arg lambda rejects. A tiny real class sidesteps that entirely."""

    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Enough of the fluent Supabase query builder for is_allowed()'s
    read+write fallback path (used when the RPC itself is unavailable)."""

    def __init__(self, store):
        self._store = store
        self._eq = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def execute(self):
        row = self._store.get((self._eq.get('key'), self._eq.get('window_start')))
        return _Result([row] if row else [])

    def update(self, patch):
        row = self._store.get((self._eq.get('key'), self._eq.get('window_start')))
        if row:
            row.update(patch)
        return self

    def insert(self, row):
        self._store[(row['key'], row['window_start'])] = dict(row)
        return self


class _FakeSupabase:
    """Raises `fail` from .rpc() the first N calls, then succeeds — models a
    transient connection blip that clears up on retry."""

    def __init__(self, fail, fail_times=1):
        self._fail = fail
        self._remaining_failures = fail_times
        self._rpc_calls = 0

    def rpc(self, _name, _params):
        self._rpc_calls += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise self._fail
        return _RpcCall()

    def table(self, _name):
        raise AssertionError(
            "should not fall back to table read/write for a connection/timeout "
            "error — that goes straight to the outer retry instead"
        )


class _RpcCall:
    def execute(self):
        return _Result(1)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sc, "RETRY_BACKOFF", 0)


@pytest.fixture
def limiter():
    return app_main.SupabaseRateLimiter(default_rpm=100)


class TestSurvivesATransientBlip:
    def test_one_connection_error_no_longer_degrades_to_fallback(self, limiter, monkeypatch):
        fake = _FakeSupabase(httpx.ConnectError("connection refused"), fail_times=1)
        monkeypatch.setattr(limiter, "_get_supabase", lambda: fake)

        allowed = limiter.is_allowed("diagnostic-probe")

        assert allowed is True
        assert fake._rpc_calls == 2, "must retry once and succeed, not give up on the first failure"

    def test_read_timeout_also_retries_for_this_specific_call(self, limiter, monkeypatch):
        """Rate-limiter-specific: retry_on_timeout=True here is deliberate.
        A duplicated increment after a timeout makes the limiter slightly
        MORE restrictive, never less — the safe direction to err in for a
        rate limiter, unlike a generic non-idempotent write."""
        fake = _FakeSupabase(httpx.ReadTimeout("The read operation timed out"), fail_times=1)
        monkeypatch.setattr(limiter, "_get_supabase", lambda: fake)

        assert limiter.is_allowed("diagnostic-probe") is True
        assert fake._rpc_calls == 2

    def test_still_falls_back_after_exhausting_retries(self, limiter, monkeypatch):
        """A genuine outage — not just one blip — must still degrade safely
        rather than raising through to the caller."""
        fake = _FakeSupabase(httpx.ConnectError("connection refused"), fail_times=99)
        monkeypatch.setattr(limiter, "_get_supabase", lambda: fake)
        monkeypatch.setattr(limiter, "_fallback_is_allowed", lambda key: "FALLBACK")

        assert limiter.is_allowed("diagnostic-probe") == "FALLBACK"
        assert fake._rpc_calls == sc.MAX_RETRIES

    def test_a_real_non_transient_error_still_falls_back_immediately(self, limiter, monkeypatch):
        """Not every failure should retry — an auth/permissions error retried
        3x would just be 3x slower to reach the same, correct fallback."""
        class _Boom:
            def rpc(self, *_a, **_k):
                raise ValueError("misconfigured client")
        monkeypatch.setattr(limiter, "_get_supabase", lambda: _Boom())
        monkeypatch.setattr(limiter, "_fallback_is_allowed", lambda key: "FALLBACK")

        assert limiter.is_allowed("diagnostic-probe") == "FALLBACK"


class TestRpcMissingStillFallsBackToManualReadWrite:
    """A connection/timeout error on the RPC skips the manual fallback and
    goes straight to the outer retry (tested above). A genuinely different
    failure — the RPC function not existing yet on this DB — is the one case
    the manual read+write path exists for, and must still work."""

    def test_falls_back_to_insert_when_rpc_is_missing_and_no_row_exists(self, limiter, monkeypatch):
        store = {}

        class _NoRpcSupabase:
            def rpc(self, *_a, **_k):
                raise Exception('Could not find the function public.increment_rate_limit')

            def table(self, _name):
                return _FakeQuery(store)

        monkeypatch.setattr(limiter, "_get_supabase", lambda: _NoRpcSupabase())
        monkeypatch.setattr(limiter, "_prune", lambda supabase: None)

        assert limiter.is_allowed("diagnostic-probe", rpm=100) is True
        assert store[("diagnostic-probe", limiter._window_start())]["count"] == 1
