"""The specialist roster and the delegation path."""
from __future__ import annotations

import pytest

from app.core.intent import Intent, route_message
from app.core.specialists import (
    ALL,
    CHAT,
    FREE_MODE_RULES,
    IMAGE,
    VIDEO,
    WEB,
    by_key,
    describe_all,
    for_intent,
    pick,
    wants_delegation,
)


def test_every_intent_maps_to_a_specialist():
    for intent in Intent:
        expert = for_intent(intent)
        assert expert is not None
        assert expert.persona.strip()


def test_work_intents_get_their_own_expert():
    assert for_intent(Intent.WEB_STATIC) is WEB
    assert for_intent(Intent.WEB_FULLSTACK) is WEB
    assert for_intent(Intent.WEB_REPAIR) is WEB
    assert for_intent(Intent.IMAGE_CREATE) is IMAGE
    assert for_intent(Intent.IMAGE_EDIT) is IMAGE
    assert for_intent(Intent.VIDEO_CREATE) is VIDEO
    assert for_intent(Intent.CHAT) is CHAT


def test_web_expert_refuses_filler_and_unverified_claims():
    """The point of a specialist is the habits it brings, not just a label."""
    persona = WEB.persona.lower()
    assert "lorem ipsum" in persona
    assert "dark mode" in persona
    assert "browser" in persona


def test_heavy_experts_use_the_big_tier_and_chat_does_not():
    assert WEB.tier == "big"
    assert VIDEO.tier == "big"
    assert CHAT.tier == "small"


def test_hard_budget_is_positive_and_ordered():
    for expert in ALL:
        assert expert.hard_budget > 0
        assert expert.max_tokens > 0
        assert expert.max_tokens <= expert.hard_budget


def test_web_budget_is_larger_than_chat():
    assert WEB.hard_budget > CHAT.hard_budget


def test_describe_all_lists_every_expert():
    text = describe_all()
    for expert in ALL:
        assert expert.name in text


def test_by_key_round_trips():
    for expert in ALL:
        assert by_key(expert.key) is expert
    assert by_key("nope") is None


@pytest.mark.parametrize(
    "text",
    [
        "buatkan video kasih ke ahlinya",
        "serahkan ke ahli, bikin poster",
        "pakai ahli untuk web",
    ],
)
def test_delegation_phrases_are_detected(text):
    assert wants_delegation(text)


def test_plain_request_is_not_a_delegation():
    assert not wants_delegation("buatkan aku gambar kucing")


def test_pick_narrows_to_the_named_domain():
    assert [s.key for s in pick("buatkan video ke ahlinya")] == ["video"]
    assert [s.key for s in pick("bikin poster ke ahlinya")] == ["image"]


def test_pick_without_a_domain_returns_everyone():
    """'kasih ke ahlinya' alone means: let the right one take it."""
    assert len(pick("kasih ke ahlinya")) == len(ALL)


@pytest.mark.asyncio
async def test_delegation_reaches_the_route_whichever_branch_won():
    """The phrase must survive routing, so the expert is actually used."""
    route = await route_message("buatkan video, kasih ke ahlinya")
    assert route.args["delegate"] is True
    assert route.args["delegate_specialist"] == "video"

    route = await route_message("bikin poster, serahkan ke ahli")
    assert route.args["delegate_specialist"] == "image"


@pytest.mark.asyncio
async def test_a_plain_message_carries_no_delegation():
    route = await route_message("halo apa kabar")
    assert not route.args.get("delegate")


def test_free_mode_rules_keep_standards():
    """Freedom is about initiative, never about shipping bad work."""
    lowered = FREE_MODE_RULES.lower()
    assert "login" in lowered
    assert "kualitas" in lowered
