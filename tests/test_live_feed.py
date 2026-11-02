"""Tests for the in-chat screen feed.

The feed is what makes "watch it work" real rather than a promise, so these
build actual images and check the GIF that comes out, instead of asserting that
a function was called.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.live_feed import LiveFeed, build_gif


def _make_png(path: Path, color: str, size=(1200, 700)) -> str:
    from PIL import Image

    Image.new("RGB", size, color).save(path)
    return str(path)


# ------------------------------------------------------------------ build_gif
def test_build_gif_stitches_real_frames_into_a_moving_image(tmp_path):
    frames = [
        _make_png(tmp_path / "a.png", "red"),
        _make_png(tmp_path / "b.png", "green"),
        _make_png(tmp_path / "c.png", "blue"),
    ]
    out = tmp_path / "feed.gif"

    result = build_gif(frames, out, width=200)

    assert result == str(out)
    assert out.exists() and out.stat().st_size > 0

    from PIL import Image

    with Image.open(out) as gif:
        # A still image would have one frame; the whole point is that it moves.
        assert getattr(gif, "n_frames", 1) == 3
        # And it must be downscaled for a phone, not sent at full size.
        assert gif.width == 200


def test_build_gif_survives_a_broken_frame(tmp_path):
    """One unreadable frame must not lose the rest, or fail the task."""
    good = _make_png(tmp_path / "good.png", "red")
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"this is not a png")

    out = tmp_path / "feed.gif"
    result = build_gif([good, str(broken)], out, width=100)

    assert result == str(out)
    from PIL import Image

    with Image.open(out) as gif:
        assert getattr(gif, "n_frames", 1) == 1


def test_build_gif_returns_none_when_there_is_nothing_to_show(tmp_path):
    assert build_gif([], tmp_path / "x.gif") is None


# ------------------------------------------------------------------- LiveFeed
class _RecordingSender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.edited: list[tuple[int, str]] = []

    async def send(self, path, caption):
        self.sent.append((path, caption))
        return type("Msg", (), {"message_id": 42})()

    async def edit(self, message_id, path, caption):
        self.edited.append((message_id, path))


def _feed(tmp_path, sender, **kwargs):
    feed = LiveFeed(
        send_animation=sender.send,
        edit_animation=sender.edit,
        outputs_dir=tmp_path,
        **kwargs,
    )
    # The constructor clamps to 2s so a misconfigured bot cannot spam the chat;
    # tests need faster ticks than a human would ever want.
    feed.interval = 0.05
    return feed


def test_set_pref_creates_the_row_when_the_user_is_new(store):
    """A preference set before the first message used to vanish silently."""
    import asyncio

    async def scenario():
        await store.set_pref(4242, "live_feed", "off")
        return await store.get_prefs(4242)

    assert asyncio.run(scenario())["live_feed"] == "off"


@pytest.mark.asyncio
async def test_the_feed_posts_a_moving_tile_and_then_updates_it(tmp_path):
    sender = _RecordingSender()
    feed = _feed(tmp_path, sender, interval=0.05)

    feed.push(_make_png(tmp_path / "1.png", "red"))
    feed.push(_make_png(tmp_path / "2.png", "green"))
    await feed.start()
    await asyncio.sleep(0.2)

    assert len(sender.sent) == 1, "the first batch posts one tile"
    assert sender.sent[0][1], "the tile carries a caption"
    first_gif = sender.sent[0][0]
    from PIL import Image

    with Image.open(first_gif) as gif:
        assert getattr(gif, "n_frames", 1) == 2

    # A new frame arrives: the same tile must be edited, not a new message.
    feed.push(_make_png(tmp_path / "3.png", "blue"))
    await asyncio.sleep(0.2)
    await feed.stop()

    assert sender.edited, "the tile should be updated in place"
    assert sender.edited[0][0] == 42


@pytest.mark.asyncio
async def test_the_feed_stays_silent_when_nothing_changed(tmp_path):
    """Repeating the same screen every few seconds would be spam, not a feed."""
    sender = _RecordingSender()
    feed = _feed(tmp_path, sender, interval=0.05)
    feed.push(_make_png(tmp_path / "1.png", "red"))

    await feed.start()
    await asyncio.sleep(0.25)  # several ticks, but only one frame
    await feed.stop()

    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_the_feed_survives_a_failing_send(tmp_path):
    """A feed that cannot post must not take the task down with it."""

    async def explode(path, caption):
        raise RuntimeError("telegram said no")

    feed = LiveFeed(send_animation=explode, outputs_dir=tmp_path, interval=0.05)
    feed.push(_make_png(tmp_path / "1.png", "red"))

    await feed.start()
    await asyncio.sleep(0.2)
    await feed.stop()  # must return cleanly, not raise


# ------------------------------------------------------------- bot integration
@pytest.mark.asyncio
async def test_siaran_off_stops_the_feed_and_on_restarts_it(settings, store):
    from app.bot.handlers import TelegramBot

    class _Msg:
        def __init__(self):
            self.texts = []
            self.chat_id = 999

        async def reply_text(self, text, **kwargs):
            self.texts.append(text)
            return self

    class _User:
        id = 111

    class _Update:
        def __init__(self):
            self.effective_user = _User()
            self.effective_message = _Msg()
            self.message = self.effective_message

    class _Ctx:
        def __init__(self, args):
            self.args = args

    class _Orch:
        def estimate_tokens(self, route):
            return 1

        def confirmation_prompt(self, route):
            return "?"

    bot = TelegramBot(settings=settings, store=store, orchestrator=_Orch(), scheduler=None)

    off = _Update()
    await bot.cmd_siaran(off, _Ctx(["off"]))
    assert await bot.store.get_prefs(111) is not None
    prefs = await store.get_prefs(111)
    assert prefs["live_feed"] == "off"

    # With it off, a task must not build a feed at all.
    msg = _Msg()
    assert await bot._make_live_feed(msg, 111) is None

    on = _Update()
    await bot.cmd_siaran(on, _Ctx(["on"]))
    assert (await store.get_prefs(111))["live_feed"] == "on"
    assert await bot._make_live_feed(msg, 111) is not None
