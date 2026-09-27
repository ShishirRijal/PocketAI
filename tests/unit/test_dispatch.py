from pocket.channels.base import OutboundMessage
from pocket.core.dispatch import Dispatcher
from pocket.data.db import Database
from pocket.data.repositories import UserRepo


class Flaky:
    def __init__(self, name, fail):
        self.name, self.fail, self.sent = name, fail, []

    async def send(self, ucid, msg, meta=None):
        if self.fail:
            raise RuntimeError("63016: outside the 24h window")
        self.sent.append((ucid, msg.text))


async def test_proactive_falls_back_to_next_channel(tmp_path, monkeypatch):
    import pocket.core.dispatch as d

    async def no_sleep(_):
        return None

    monkeypatch.setattr(d.asyncio, "sleep", no_sleep)
    db = Database(f"sqlite:///{tmp_path / 'x.db'}")
    db.create_all()
    with db.session() as s:
        repo = UserRepo(s)
        u = repo.create()
        repo.add_identity(u, "whatsapp", "whatsapp:+1")
        repo.add_identity(u, "telegram", "42")
        uid = u.id
    wa, tg = Flaky("whatsapp", True), Flaky("telegram", False)
    disp = Dispatcher(db, orchestrator=None, adapters={"whatsapp": wa, "telegram": tg})  # type: ignore[arg-type]
    assert await disp.send_to_user(uid, OutboundMessage(text="digest"))
    assert tg.sent == [("42", "digest")]
