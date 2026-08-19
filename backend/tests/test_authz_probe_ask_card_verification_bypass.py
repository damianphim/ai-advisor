"""
Throwaway probe: does POST /api/cards/ask/{user_id} enforce the same
email-verification + LLM-budget gates as its sibling AI endpoints
(generate_cards, stream_cards, retranslate_cards, chat.send,
electives.recommend)?
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.conftest import auth


def _fake_message(text: str):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def test_ask_card_enforces_email_verification_gate(client, fake_supabase, monkeypatch):
    fake_supabase.set_table("users", [{"id": "user-1", "email": "attacker@gmail.com"}])

    import api.utils.verified_user as vu
    monkeypatch.setattr(vu, "is_email_verified", lambda _uid: False)

    resp_generate = client.post(
        "/api/cards/generate/user-1",
        json={"force": False, "language": "en"},
        headers=auth("user-1"),
    )
    assert resp_generate.status_code == 403
    assert resp_generate.json()["detail"]["code"] == "email_not_verified"

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_message(
        '{"type": "insight", "icon": "test", "label": "TEST", '
        '"title": "Test title", "body": "Test body", "actions": []}'
    )
    import api.routes.cards as cards_mod
    monkeypatch.setattr(cards_mod, "get_anthropic_client", lambda: mock_client)

    resp_ask = client.post(
        "/api/cards/ask/user-1",
        json={"user_id": "user-1", "question": "What courses should I take?", "language": "en"},
        headers=auth("user-1"),
    )

    # After the fix, ask_card must reject the unverified user just like
    # generate_cards does, and must never reach the paid Anthropic call.
    assert resp_ask.status_code == 403
    assert resp_ask.json()["detail"]["code"] == "email_not_verified"
    assert not mock_client.messages.create.called