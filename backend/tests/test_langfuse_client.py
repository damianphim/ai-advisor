"""
GitHub issue #34: langfuse_client.py was written against the v2 API
(lf.trace() -> trace.generation() -> generation.end()), which no longer
exists in the v4 (OpenTelemetry-based) SDK. Rewritten to use
start_as_current_observation(as_type="generation") + propagate_attributes()
+ generation.update(...).

CI has no real Langfuse credentials, so these tests mock the SDK surface
rather than hitting a real instance — the real API surface (method names,
signatures) was verified separately by introspecting the actual installed
langfuse==4.14.5 package, not just documentation.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from api.utils import langfuse_client as lfc


def _fake_message(text: str, input_tokens=100, output_tokens=50,
                   cache_read=0, cache_creation=0):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        ),
    )


class _FakeGeneration:
    def __init__(self):
        self.update_calls = []

    def update(self, **kwargs):
        self.update_calls.append(kwargs)


class _FakeLangfuseClient:
    def __init__(self):
        self.generation = _FakeGeneration()
        self.flush_calls = 0
        self.start_as_current_observation_calls = []

    def start_as_current_observation(self, **kwargs):
        self.start_as_current_observation_calls.append(kwargs)
        gen = self.generation

        @contextmanager
        def _cm():
            yield gen
        return _cm()

    def flush(self):
        self.flush_calls += 1


@pytest.fixture(autouse=True)
def _reset_client_cache(monkeypatch):
    """_get_client() memoizes the client in a module global; reset it so
    tests don't leak state into each other."""
    monkeypatch.setattr(lfc, "_langfuse", None)


class TestDisabledWithoutCredentials:
    def test_yields_noop_generation_when_keys_missing(self, monkeypatch):
        from api import config
        monkeypatch.setattr(config.settings, "LANGFUSE_PUBLIC_KEY", "")
        monkeypatch.setattr(config.settings, "LANGFUSE_SECRET_KEY", "")

        with lfc.trace_claude(name="chat", input_messages=[], model="m") as gen:
            assert isinstance(gen, lfc._NoopGeneration)
            gen.finish(_fake_message("hi"))  # must not raise


class TestEnabledPath:
    def _patch_enabled(self, monkeypatch):
        fake_client = _FakeLangfuseClient()
        monkeypatch.setattr(lfc, "_get_client", lambda: fake_client)

        import langfuse
        propagate_calls = []

        @contextmanager
        def fake_propagate_attributes(**kwargs):
            propagate_calls.append(kwargs)
            yield

        monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate_attributes)
        return fake_client, propagate_calls

    def test_propagates_trace_level_attributes(self, monkeypatch):
        fake_client, propagate_calls = self._patch_enabled(monkeypatch)

        with lfc.trace_claude(
            name="chat", user_id="user-1", session_id="sess-1",
            metadata={"tab": "chat"}, input_messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5-20251001", max_tokens=512,
        ) as gen:
            gen.finish(_fake_message("hello"))

        assert propagate_calls == [{
            "user_id": "user-1", "session_id": "sess-1", "metadata": {"tab": "chat"},
        }]

    def test_starts_generation_as_type_generation(self, monkeypatch):
        fake_client, _ = self._patch_enabled(monkeypatch)

        with lfc.trace_claude(
            name="chat", input_messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5-20251001", max_tokens=512,
        ) as gen:
            gen.finish(_fake_message("hello"))

        call = fake_client.start_as_current_observation_calls[0]
        assert call["as_type"] == "generation"
        assert call["name"] == "chat"
        assert call["model"] == "claude-haiku-4-5-20251001"
        assert call["model_parameters"] == {"max_tokens": 512}

    def test_finish_maps_anthropic_usage_to_usage_details(self, monkeypatch):
        fake_client, _ = self._patch_enabled(monkeypatch)

        with lfc.trace_claude(name="chat", input_messages=[], model="m") as gen:
            gen.finish(_fake_message(
                "the answer", input_tokens=1200, output_tokens=340,
                cache_read=900, cache_creation=15,
            ))

        update_call = fake_client.generation.update_calls[0]
        assert update_call["output"] == "the answer"
        assert update_call["usage_details"] == {
            "prompt_tokens": 1200,
            "completion_tokens": 340,
            "total_tokens": 1540,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 15,
        }

    def test_flushes_after_successful_call(self, monkeypatch):
        fake_client, _ = self._patch_enabled(monkeypatch)

        with lfc.trace_claude(name="chat", input_messages=[], model="m") as gen:
            gen.finish(_fake_message("hi"))

        assert fake_client.flush_calls == 1

    def test_exception_marks_generation_error_and_still_propagates(self, monkeypatch):
        fake_client, _ = self._patch_enabled(monkeypatch)

        with pytest.raises(ValueError, match="boom"):
            with lfc.trace_claude(name="chat", input_messages=[], model="m") as gen:
                raise ValueError("boom")

        update_call = fake_client.generation.update_calls[0]
        assert update_call["level"] == "ERROR"
        assert update_call["status_message"] == "boom"
        # Flush still happens on the error path so nothing is lost.
        assert fake_client.flush_calls == 1

    def test_finish_error_is_swallowed_not_raised(self, monkeypatch):
        """A malformed response (e.g. missing .content) must not crash the
        actual Claude call — Langfuse is observability, not a hard dependency."""
        fake_client, _ = self._patch_enabled(monkeypatch)

        class _Boom:
            @property
            def content(self):
                raise RuntimeError("malformed response")

        with lfc.trace_claude(name="chat", input_messages=[], model="m") as gen:
            gen.finish(_Boom())  # must not raise
            assert gen.finished is True
