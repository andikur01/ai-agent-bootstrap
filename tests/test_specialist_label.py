"""The reply must name the expert that did the work.

The user asked for tasks to be handed to the right specialist. If the reply
never says who handled it, the user cannot tell whether that happened, and the
feature is invisible.
"""
from __future__ import annotations

from app.bot.handlers import _looks_like_credentials, _specialist_label


def test_a_known_specialist_gets_a_named_label():
    label = _specialist_label("web")
    assert label, "the web expert must be named"
    assert "Ahli" in label


def test_an_unknown_specialist_key_is_silent_not_wrong():
    """A bad key must not invent a name; empty means the line is skipped."""
    assert _specialist_label("tidak-ada") == ""


def test_a_verification_code_is_not_mistaken_for_a_login():
    """A one-time code must not be deleted as if it were a password."""
    assert not _looks_like_credentials("123456")
    assert not _looks_like_credentials("kode: 8821")


def test_a_login_answer_is_recognised():
    assert _looks_like_credentials("situs.com aku@email.com rahasia123")
    assert _looks_like_credentials("aku@email.com rahasia123")
