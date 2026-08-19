"""
Throwaway validation probe (Admin Privilege Escalation Specialist).

Confirms whether the admin /verify login rate limiter can be bypassed by
spoofing X-Forwarded-For, when the request's "last" comma-separated value
is taken as authoritative with no verification that it actually originated
from a trusted single-hop proxy.
"""
from __future__ import annotations


def _force_supabase_rpc_failure(monkeypatch):
    """Force the admin rate limiter's Supabase RPC call to fail so it
    exercises the two-step table-based fallback path (which increments a
    real per-key counter, unlike the RPC path which the fake always
    reports as count=1)."""
    from api.utils import supabase_client as supa
    sb = supa.get_supabase()

    def _boom(*a, **kw):
        raise RuntimeError("rpc not available in test")

    monkeypatch.setattr(sb, "rpc", _boom)
    return sb


def test_same_ip_gets_rate_limited(client, fake_supabase, monkeypatch):
    """Baseline: hammering /verify from a single, stable IP trips the
    5-attempts/60s limiter on the 6th attempt, as designed."""
    _force_supabase_rpc_failure(monkeypatch)

    statuses = []
    for _ in range(7):
        r = client.post(
            "/api/admin/verify",
            json={"secret": "wrong-secret"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        statuses.append(r.status_code)

    assert 429 in statuses, f"Expected a 429 lockout for a stable IP, got {statuses}"
    print("Same-IP statuses:", statuses)


def test_spoofed_xff_bypasses_rate_limit(client, fake_supabase, monkeypatch):
    """Regression test for the fix: rotating the X-Forwarded-For value on
    every request used to mint a brand new per-IP rate-limit bucket each
    time (unlimited attempts). The global, non-IP-keyed cap now still
    trips even though every request claims a distinct IP."""
    _force_supabase_rpc_failure(monkeypatch)

    statuses = []
    for i in range(25):
        r = client.post(
            "/api/admin/verify",
            json={"secret": "wrong-secret"},
            headers={"X-Forwarded-For": f"198.51.100.{i}"},
        )
        statuses.append(r.status_code)

    print("Spoofed-XFF statuses:", statuses)
    assert 429 in statuses, (
        f"Expected the global cap to eventually lock out an attacker who "
        f"rotates X-Forwarded-For on every request, got {statuses}"
    )