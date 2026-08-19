"""
Regression tests for the newsletters SSRF guard (Strix finding, 2026-08-19).

_is_safe_url() used to only reject a hostname when the URL's literal string
was itself a private/loopback IP — a symbolic hostname that merely *resolved*
to one (DNS rebinding, or just pointing a domain at 127.0.0.1 / a cloud
metadata address) sailed through unchecked. And _fetch_page() followed HTTP
redirects automatically (follow_redirects=True) without ever re-validating
where they pointed, so a safe first hop could redirect straight past the
guard on the second request.
"""
import socket

import httpx
import pytest

from api.routes import newsletters


def _addrinfo(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]


class TestIsSafeUrlResolvesHostnames:
    def test_hostname_resolving_to_a_private_ip_is_blocked(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("127.0.0.1"))
        assert newsletters._is_safe_url("http://sketchy-redirector.example.com/feed") is False

    def test_hostname_resolving_to_a_cloud_metadata_style_link_local_ip_is_blocked(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("169.254.169.254"))
        assert newsletters._is_safe_url("http://sketchy-redirector.example.com/feed") is False

    def test_hostname_resolving_to_a_public_ip_is_allowed(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("93.184.216.34"))
        assert newsletters._is_safe_url("https://mcgill.ca/events") is True

    def test_unresolvable_hostname_is_blocked_rather_than_assumed_safe(self, monkeypatch):
        def _raise(*_a, **_k):
            raise socket.gaierror("nodename nor servname provided")
        monkeypatch.setattr(socket, "getaddrinfo", _raise)
        assert newsletters._is_safe_url("http://does-not-resolve.invalid/feed") is False

    def test_literal_private_ip_still_blocked(self):
        assert newsletters._is_safe_url("http://127.0.0.1/feed") is False

    def test_literal_public_ip_still_allowed(self):
        assert newsletters._is_safe_url("http://93.184.216.34/feed") is True


class _FakeResp:
    def __init__(self, status_code=200, text="", location=None):
        self.status_code = status_code
        self.text = text
        self.is_redirect = location is not None
        self.headers = {"location": location} if location else {}


class TestFetchPageRevalidatesEachRedirectHop:
    def test_redirect_to_a_safe_url_is_followed(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("93.184.216.34"))
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if url == "https://mcgill.ca/events":
                return _FakeResp(status_code=302, location="https://mcgill.ca/events/2026")
            return _FakeResp(status_code=200, text="<html>ok</html>")

        monkeypatch.setattr(httpx, "get", fake_get)
        assert newsletters._fetch_page("https://mcgill.ca/events") == "<html>ok</html>"
        assert calls == ["https://mcgill.ca/events", "https://mcgill.ca/events/2026"]

    def test_redirect_to_an_internal_address_is_not_followed(self, monkeypatch):
        """The bug: a redirect target must be re-validated, not trusted just
        because the first hop passed _is_safe_url."""
        addrs = {
            "mcgill.ca": "93.184.216.34",
            "internal.svc.local": "127.0.0.1",
        }
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, *a, **k: _addrinfo(addrs.get(host, "93.184.216.34")),
        )
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _FakeResp(status_code=302, location="http://internal.svc.local/secrets")

        monkeypatch.setattr(httpx, "get", fake_get)
        assert newsletters._fetch_page("https://mcgill.ca/events") is None
        # Only the first, safe hop was ever requested — the malicious
        # redirect target was blocked before a second request was made.
        assert calls == ["https://mcgill.ca/events"]

    def test_redirect_chain_is_bounded(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addrinfo("93.184.216.34"))
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            n = int(url.rsplit("/", 1)[-1])
            return _FakeResp(status_code=302, location=f"https://mcgill.ca/hop/{n + 1}")

        monkeypatch.setattr(httpx, "get", fake_get)
        assert newsletters._fetch_page("https://mcgill.ca/hop/0") is None
        assert len(calls) == 6  # initial request + 5 allowed redirects, then give up
