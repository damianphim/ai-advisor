"""
Langfuse LLM observability — wraps every Claude call to record cost,
latency, token usage, and user/session context.

Gracefully disabled when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are
not set (local dev, CI) so nothing breaks without credentials.

v4 SDK (OpenTelemetry-based): a generation is created directly as the
trace root via start_as_current_observation(as_type="generation") — v2's
separate lf.trace() → trace.generation() two-step no longer exists.
Trace-level fields (user_id, session_id, metadata) are set via
propagate_attributes() rather than passed to the generation call itself.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_langfuse = None


def _get_client():
    global _langfuse
    if _langfuse is not None:
        return _langfuse
    try:
        from ..config import settings
        if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
            return None
        from langfuse import Langfuse
        _langfuse = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
        )
        logger.info("Langfuse observability enabled")
    except Exception as e:
        logger.warning("Langfuse init failed, observability disabled: %s", e)
        _langfuse = None
    return _langfuse


@contextmanager
def trace_claude(
    *,
    name: str,
    user_id: str | None = None,
    session_id: str | None = None,
    metadata: dict | None = None,
    input_messages: list | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
):
    """
    Context manager that wraps a Claude API call with Langfuse tracing.

    Usage:
        with trace_claude(name="chat", user_id=uid, session_id=sid,
                          input_messages=msgs, model=model) as gen:
            response = client.messages.create(...)
            gen.finish(response)
    """
    lf = _get_client()
    if lf is None:
        yield _NoopGeneration()
        return

    from langfuse import propagate_attributes

    with propagate_attributes(
        user_id=str(user_id) if user_id else None,
        session_id=str(session_id) if session_id else None,
        metadata=metadata or {},
    ):
        with lf.start_as_current_observation(
            as_type="generation",
            name=name,
            input=input_messages,
            model=model,
            model_parameters={"max_tokens": max_tokens} if max_tokens else {},
        ) as generation:
            gen_wrapper = _Generation(generation)
            try:
                yield gen_wrapper
            except Exception as e:
                generation.update(level="ERROR", status_message=str(e))
                raise
            finally:
                try:
                    lf.flush()
                except Exception:
                    pass


class _Generation:
    def __init__(self, generation):
        self._g = generation
        self.finished = False

    def finish(self, response: Any) -> None:
        try:
            output = response.content[0].text if response.content else ""
            usage = getattr(response, "usage", None)
            usage_details = None
            if usage is not None:
                input_tokens = getattr(usage, "input_tokens", 0) or 0
                output_tokens = getattr(usage, "output_tokens", 0) or 0
                usage_details = {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                    "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                }
            self._g.update(output=output, usage_details=usage_details)
        except Exception as e:
            logger.debug("Langfuse finish error: %s", e)
        self.finished = True


class _NoopGeneration:
    finished = True
    def finish(self, response: Any) -> None:
        pass
