"""Handoff: a task pauses for the user, then continues where it stopped.

The point of these tests is the failure mode they rule out - a task that asks
for a verification code and then hangs, or one that swallows the user's next
command because it was still waiting for an answer.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.handoff import HandoffService


@pytest.mark.asyncio
async def test_answer_resumes_the_waiting_task():
    service = HandoffService(timeout_seconds=5)
    pending = service.ask_nowait(1, "Kodenya?")

    waiter = asyncio.create_task(service.wait(pending))
    await asyncio.sleep(0)  # let the waiter reach the await
    assert service.answer(1, "483920") is True

    assert await asyncio.wait_for(waiter, timeout=2) == "483920"


@pytest.mark.asyncio
async def test_timeout_returns_none_instead_of_hanging():
    service = HandoffService(timeout_seconds=30)
    pending = service.ask_nowait(2, "Kodenya?")
    service.timeout_seconds = 0.05  # keep the test fast
    assert await service.wait(pending) is None
    assert service.pending(2) is None


@pytest.mark.asyncio
async def test_a_message_with_no_waiter_is_not_consumed():
    """Only a user who is actually being asked loses their message."""
    service = HandoffService()
    assert service.answer(3, "buatkan gambar kucing") is False


@pytest.mark.asyncio
async def test_cancel_releases_the_waiter_with_none():
    service = HandoffService()
    pending = service.ask_nowait(4, "Kodenya?")
    waiter = asyncio.create_task(service.wait(pending))
    await asyncio.sleep(0)
    service.cancel(4)
    assert await asyncio.wait_for(waiter, timeout=2) is None


@pytest.mark.asyncio
async def test_second_question_reuses_the_pending_slot():
    """A stuck task must not be able to stack questions on one user."""
    service = HandoffService()
    first = service.ask_nowait(5, "Kode pertama?")
    second = service.ask_nowait(5, "Kode kedua?")
    assert first is second


@pytest.mark.asyncio
async def test_the_waiting_slot_is_cleared_after_an_answer():
    service = HandoffService(timeout_seconds=5)
    pending = service.ask_nowait(6, "Kodenya?")
    waiter = asyncio.create_task(service.wait(pending))
    await asyncio.sleep(0)
    service.answer(6, "111111")
    await asyncio.wait_for(waiter, timeout=2)
    assert service.pending(6) is None


# --------------------------------------------------------- bot-level wiring
class _FakeMessage:
    def __init__(self, text: str, chat_id: int = 999):
        self.text = text
        self.caption = None
        self.chat_id = chat_id
        self.chat = None
        self.photo = None
        self.voice = None
        self.audio = None
        self.document = None
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return self


class _FakeUser:
    id = 111
    username = "tester"
    first_name = "Test"


class _FakeUpdate:
    def __init__(self, text: str):
        self.effective_message = _FakeMessage(text)
        self.effective_user = _FakeUser()
        self.callback_query = None


class _StubOrchestrator:
    """Enough of the orchestrator for on_message to run its preamble.

    ``classify`` returns a fallback route on purpose: on_message then answers
    "command not found" and stops, which is all this test needs to observe.
    """

    def __init__(self):
        self.memory = self

    def estimate_tokens(self, route):
        return 10

    def confirmation_prompt(self, route):
        return "Jalan?"

    async def remember_message(self, *args, **kwargs):
        return None

    async def classify(self, user_id, text, **kwargs):
        from app.core.intent import Intent, Route

        return Route(Intent.CHAT, {}, confidence=0.1, source="fallback")


@pytest.mark.asyncio
async def test_a_pending_question_swallows_the_next_message(settings, store):
    """The answer must reach the waiting task, not become a new chat message."""
    from app.bot.handlers import TelegramBot

    bot = TelegramBot(settings=settings, store=store, orchestrator=_StubOrchestrator(), scheduler=None)
    pending = bot.handoff.ask_nowait(111, "Kodenya?")
    waiter = asyncio.create_task(bot.handoff.wait(pending))
    await asyncio.sleep(0)

    update = _FakeUpdate("483920")
    await bot.on_message(update, context=None)

    assert await asyncio.wait_for(waiter, timeout=2) == "483920"
    assert update.effective_message.replies, "the user should be told it was received"


@pytest.mark.asyncio
async def test_a_command_still_routes_while_a_question_is_pending(settings, store):
    """A user who changes their mind must not be trapped in the question."""
    from app.bot.handlers import TelegramBot

    bot = TelegramBot(settings=settings, store=store, orchestrator=_StubOrchestrator(), scheduler=None)
    pending = bot.handoff.ask_nowait(111, "Kodenya?")

    update = _FakeUpdate("/menu")
    await bot.on_message(update, context=None)

    # The command was handled, so the question is still unanswered.
    assert not pending.future.done()
    assert bot.handoff.pending(111) is pending
