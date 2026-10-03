from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord
import httpx
import respx
from sqlalchemy import select

from pocket.channels.base import OutboundMessage
from pocket.channels.discord import DiscordAdapter
from pocket.channels.discord_gateway import GatewayBot, should_handle, to_inbound
from pocket.data.models import Transaction
from pocket.data.repositories import UserRepo
from pocket.runtime import build_runtime

BOT = SimpleNamespace(id=999)


class FakeChannel:
    def __init__(self, cid=555):
        self.id = cid

    @asynccontextmanager
    async def typing(self):
        yield


def msg(text, *, author=42, guild=None, mentions=(), mid=1, channel=None, bot=False):
    return SimpleNamespace(
        id=mid, content=text, author=SimpleNamespace(id=author, bot=bot), guild=guild,
        channel=channel or FakeChannel(), mentions=list(mentions), attachments=[],
    )  # fmt: skip


def test_which_messages_are_handled():
    guild = SimpleNamespace(id=1)
    assert should_handle(msg("23 eur lunch"), BOT, None)  # DM (no guild)
    assert not should_handle(msg("hello all", guild=guild), BOT, None)  # server chatter
    assert should_handle(msg("<@999> lunch 12", guild=guild, mentions=[BOT]), BOT, None)
    assert should_handle(msg("lunch 12", guild=guild, channel=FakeChannel(777)), BOT, "777")
    assert not should_handle(msg("hi", bot=True), BOT, None)
    assert not should_handle(msg("hi", author=999), BOT, None)


def test_mention_is_stripped():
    m = to_inbound(msg("<@999> 23 eur groceries"), 999)
    assert m.text == "23 eur groceries" and m.channel_msg_id == "msg:1"
    assert m.meta == {"channel_id": "555", "message_id": "1"}


class Recorder:
    name = "discord"

    def __init__(self):
        self.sent = []

    async def send(self, ucid, message, meta=None):
        self.sent.append((ucid, message.text, meta))


async def test_plain_dm_gets_logged_and_answered(settings, services):
    rec = Recorder()
    rt = build_runtime(settings, services=services, adapters={"discord": rec})
    with services.db.session() as s:
        repo = UserRepo(s)
        repo.add_identity(repo.get(services.user_id), "discord", "42")
    bot = GatewayBot(rt, "token")
    await bot.handle(msg("23 eur groceries at rimi", mid=10), 999)
    [(ucid, text, meta)] = rec.sent
    assert ucid == "42" and "✅ Logged €23.00 · Groceries" in text and meta["channel_id"] == "555"
    await bot.handle(msg("23 eur groceries at rimi", mid=10), 999)  # same message again
    assert len(rec.sent) == 1
    await bot.handle(msg("lunch 5", author=7, mid=11), 999)  # stranger
    assert len(rec.sent) == 1
    with services.db.session() as s:
        assert len(s.scalars(select(Transaction)).all()) == 1


@respx.mock
async def test_adapter_replies_in_channel():
    route = respx.post("https://discord.com/api/v10/channels/555/messages").mock(return_value=httpx.Response(200, json={}))
    await DiscordAdapter("bot", "app").send("42", OutboundMessage(text="ok"), {"channel_id": "555", "message_id": "10"})
    body = route.calls[0].request.content.decode()
    assert '"message_reference"' in body and '"10"' in body


def test_discord_py_is_importable():
    assert discord.Intents.none().message_content is False
